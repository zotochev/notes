"""
Files on a WebDAV server (ownCloud Infinite Scale, or any other).

Paths are relative to the base URL, e.g. "/notes/meeting.md".

Listing asks for one directory at a time (PROPFIND Depth: 1), never for a
whole tree; see notificator.sources.batching. A directory that cannot be
listed makes the whole listing fail: a partial listing would look like
deleted files.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlparse

import requests

from notificator.core.model import RemoteFile
from notificator.sources.batching import Folder, Level, TreeWalker
from notificator.sources.http import DEFAULT_RETRY_DELAYS, send
from notificator.sync.ports import SourceError

_DAV = "{DAV:}"
_OC = "{http://owncloud.org/ns}"
_PROPFIND_BODY = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<D:propfind xmlns:D="DAV:" xmlns:oc="http://owncloud.org/ns"><D:prop>'
    "<D:resourcetype/><D:getetag/><D:getlastmodified/><D:getcontentlength/><oc:privatelink/>"
    "</D:prop></D:propfind>"
).encode()


class _NotFound(SourceError):
    pass


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
        trust_folder_etags: bool = False,
        concurrency: int = 4,
        timeout: float = 60,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        """`password` is called for every listing, so a rotating token can be supplied.

        `trust_folder_etags` is for servers where a directory's ETag changes
        whenever anything under it changes (ownCloud, Nextcloud, Seafile): an
        unchanged directory is then not listed again. On other servers it
        would hide changes made deeper in the tree.
        """
        self._base_url = url.rstrip("/")
        self._prefix = unquote(urlparse(self._base_url).path)
        self._username = username
        self._password = password
        self._watch_paths = [_normalize(p) for p in (watch_paths or ["/"])]
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._trust_folder_etags = trust_folder_etags
        self._auth = (username, "")
        self.walker = TreeWalker(self._level, max_concurrency=concurrency)

    def list_files(self) -> list[RemoteFile]:
        self._auth = (self._username, self._password())
        return self.walker.run(self._watch_paths)

    def _level(self, path: str) -> Level:
        children = self._children(path, self._auth)
        return Level(
            folders=[
                Folder(e.path, (e.version or None) if self._trust_folder_etags else None)
                for e in children if e.is_dir
            ],
            files=[RemoteFile(e.path, e.version, e.link) for e in children if not e.is_dir],
        )

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

    def _children(self, path: str, auth: tuple[str, str]) -> list[_Entry]:
        return [e for e in self._propfind(path, "1", auth) if e.path != path]

    def _propfind(self, path: str, depth: str, auth: tuple[str, str]) -> list[_Entry]:
        response = self._send(
            "PROPFIND", path, auth,
            data=_PROPFIND_BODY, headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
        )
        status = response.status_code
        if status in (401, 403):
            raise SourceError(f"ошибка авторизации WebDAV (HTTP {status}) для {path}")
        if status == 404:
            raise _NotFound(f"не найдено на сервере: {path}")
        if status != 207:
            raise SourceError(f"не удалось получить список {path}: HTTP {status}")
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as e:
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
            return _Entry(path, is_dir=True, version=(prop.findtext(f"{_DAV}getetag") or "").strip('"'))
        version = (prop.findtext(f"{_DAV}getetag") or "").strip('"') or ":".join(
            filter(None, (prop.findtext(f"{_DAV}getlastmodified"), prop.findtext(f"{_DAV}getcontentlength")))
        )
        if not version:
            raise SourceError(f"сервер не сообщает версию файла {path} (нет ETag и даты изменения)")
        return _Entry(path, is_dir=False, version=version, link=prop.findtext(f"{_OC}privatelink") or None)

    def _send(self, method: str, path: str, auth: tuple[str, str], **kwargs) -> requests.Response:
        return send(
            method, self._base_url + quote(path, safe="/"), self._retry_delays,
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
