"""
Files on a WebDAV server (ownCloud Infinite Scale, or any other).

Paths are relative to the base URL, e.g. "/notes/meeting.md".

Listing asks for a whole subtree at once (PROPFIND Depth: infinity). When the
server refuses that, or a subtree is too big to come back in one answer, the
subtree is walked directory by directory instead. A directory that cannot be
listed even on its own makes the whole listing fail: a partial listing would
look like deleted files.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import requests

from notificator.core.model import RemoteFile
from notificator.sources.http import DEFAULT_RETRY_DELAYS, send
from notificator.sync.ports import SourceError

logger = logging.getLogger(__name__)

_DAV = "{DAV:}"
_OC = "{http://owncloud.org/ns}"
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:" xmlns:oc="http://owncloud.org/ns"><D:prop>'
    "<D:resourcetype/><D:getetag/><D:getlastmodified/><D:getcontentlength/><oc:privatelink/>"
    "</D:prop></D:propfind>"
).encode()


class _InfinityNotSupported(Exception):
    pass


class _NotFound(SourceError):
    pass


class _TooBigForOneRequest(Exception):
    """A whole-tree request failed in a way that asking for less may fix: timeout, 5xx, cut-off answer."""


@dataclass(frozen=True, slots=True)
class _Entry:
    path: str
    is_dir: bool
    version: str = ""
    link: str | None = None


class WebDavSource:
    def __init__(
        self,
        url: str,
        username: str,
        password: Callable[[], str],
        watch_paths: list[str] | None = None,
        verify_ssl: bool = True,
        depth_infinity: bool = True,
        concurrency: int = 8,
        timeout: float = 60,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        """`password` is called for every request batch, so a rotating token can be supplied."""
        self._base_url = url.rstrip("/")
        self._prefix = unquote(urlparse(self._base_url).path)
        self._username = username
        self._password = password
        self._watch_paths = [_normalize(p) for p in (watch_paths or ["/"])]
        self._verify_ssl = verify_ssl
        # Set to False for servers that reject Depth: infinity without saying so clearly.
        self._depth_infinity = depth_infinity
        self._concurrency = concurrency
        self._timeout = timeout
        self._retry_delays = retry_delays
        # Directories the server could not return whole; they are listed piece by piece.
        self._split: set[str] = set()

    def list_files(self) -> list[RemoteFile]:
        auth = (self._username, self._password())
        files: dict[str, RemoteFile] = {}
        with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
            for root in self._watch_paths:
                top = self._children(root, auth)
                subtrees = pool.map(lambda d: self._subtree(d, auth), [e.path for e in top if e.is_dir])
                for entry in (*top, *(e for subtree in subtrees for e in subtree)):
                    if not entry.is_dir:
                        files[entry.path] = RemoteFile(entry.path, entry.version, entry.link)
        return list(files.values())

    def stat(self, path: str) -> RemoteFile | None:
        auth = (self._username, self._password())
        try:
            entries = self._propfind(path, "0", auth)
        except _NotFound:
            # Gone for sure only while its watch path is still there; this raises when it is not.
            root = next((w for w in self._watch_paths if w == "/" or path.startswith(w + "/")), "/")
            self._propfind(root, "0", auth)
            return None
        if len(entries) != 1 or entries[0].is_dir:
            raise SourceError(f"это не файл: {path}")
        return RemoteFile(path, entries[0].version, entries[0].link)

    def read_text(self, file: RemoteFile) -> str:
        response = self._send("GET", file.path, (self._username, self._password()))
        if response.status_code != 200:
            raise SourceError(f"не удалось скачать {file.path}: HTTP {response.status_code}")
        return response.content.decode("utf-8", errors="ignore")

    def _subtree(self, path: str, auth: tuple[str, str]) -> list[_Entry]:
        """Every entry under a directory: in one request when the server manages it, in pieces otherwise."""
        if self._depth_infinity and path not in self._split:
            try:
                return self._propfind(path, "infinity", auth)
            except _InfinityNotSupported:
                self._depth_infinity = False
            except _TooBigForOneRequest as e:
                # Remembered, so later listings do not wait for the same timeout again.
                self._split.add(path)
                logger.info("Каталог %s не отдаётся одним запросом (%s), читаю его по частям", path, e)
        entries: list[_Entry] = []
        for child in self._children(path, auth):
            if child.is_dir:
                entries.extend(self._subtree(child.path, auth))
            else:
                entries.append(child)
        return entries

    def _children(self, path: str, auth: tuple[str, str]) -> list[_Entry]:
        return [e for e in self._propfind(path, "1", auth) if e.path != path]

    def _propfind(self, path: str, depth: str, auth: tuple[str, str]) -> list[_Entry]:
        whole_tree = depth == "infinity"
        try:
            # A whole-tree request that timed out will time out again: split instead of retrying.
            response = self._send(
                "PROPFIND", path, auth, retry=not whole_tree,
                data=_PROPFIND_BODY, headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
            )
        except SourceError as e:
            if whole_tree:
                raise _TooBigForOneRequest(str(e)) from e
            raise
        status = response.status_code
        if whole_tree:
            # RFC 4918: a server that refuses infinite depth answers 403 with <propfind-finite-depth/>.
            if status in (400, 501) or (status == 403 and "propfind-finite-depth" in response.text.lower()):
                raise _InfinityNotSupported()
            if status >= 500:
                raise _TooBigForOneRequest(f"HTTP {status}")
        if status in (401, 403):
            raise SourceError(f"ошибка авторизации WebDAV (HTTP {status}) для {path}")
        if status == 404:
            raise _NotFound(f"не найдено на сервере: {path}")
        if status != 207:
            raise SourceError(f"не удалось получить список {path}: HTTP {status}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
            if whole_tree:
                raise _TooBigForOneRequest("ответ оборван") from e
            raise SourceError(f"некорректный ответ сервера для {path}: {e}") from e
        return [self._entry(r) for r in root.iter(f"{_DAV}response")]

    def _entry(self, response: ET.Element) -> _Entry:
        href = unquote(urlparse(response.findtext(f"{_DAV}href") or "").path)
        path = _normalize(href.removeprefix(self._prefix))
        prop = _ok_prop(response)
        if prop is None:
            raise SourceError(f"сервер не вернул свойства для {path}")
        resource_type = prop.find(f"{_DAV}resourcetype")
        if resource_type is not None and resource_type.find(f"{_DAV}collection") is not None:
            return _Entry(path, is_dir=True)
        version = (prop.findtext(f"{_DAV}getetag") or "").strip('"') or ":".join(
            filter(None, (prop.findtext(f"{_DAV}getlastmodified"), prop.findtext(f"{_DAV}getcontentlength")))
        )
        if not version:
            raise SourceError(f"сервер не сообщает версию файла {path} (нет ETag и даты изменения)")
        return _Entry(path, is_dir=False, version=version, link=prop.findtext(f"{_OC}privatelink") or None)

    def _send(
        self, method: str, path: str, auth: tuple[str, str], retry: bool = True, **kwargs
    ) -> requests.Response:
        return send(
            method, self._base_url + quote(path, safe="/"), self._retry_delays if retry else (),
            auth=auth, verify=self._verify_ssl, timeout=self._timeout, **kwargs,
        )


def _normalize(path: str) -> str:
    return "/" + path.strip().strip("/")


def _ok_prop(response: ET.Element) -> ET.Element | None:
    """The <prop> of the successful <propstat>.

    A response may carry several <propstat> blocks, one per status: properties
    the server knows (200) and ones it does not (404, e.g. oc:privatelink on a
    non-ownCloud server). Taking the first <prop> could pick the 404 block.
    """
    for propstat in response.findall(f"{_DAV}propstat"):
        if " 200 " in (propstat.findtext(f"{_DAV}status") or "") + " ":
            return propstat.find(f"{_DAV}prop")
    return response.find(f".//{_DAV}prop")
