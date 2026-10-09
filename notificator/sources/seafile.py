"""
Files in Seafile libraries, read through the Seafile REST API.

Paths look like "/<library name>/<path inside the library>", so a watch path
"/mylib/notes" means the folder "notes" of the library "mylib".

Listing asks for one folder at a time, never for a whole tree: a recursive
listing of a big library can occupy the server for minutes. A folder's id is
a hash of everything under it, so an unchanged folder is not listed again;
see notificator.sources.batching.
"""
from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from notificator.core.model import RemoteFile
from notificator.sources.batching import Folder, Level, TreeWalker
from notificator.sources.http import DEFAULT_RETRY_DELAYS, send
from notificator.sync.ports import SourceError


class _NotFound(SourceError):
    pass


class SeafileSource:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        watch_paths: list[str],
        verify_ssl: bool = True,
        public_url: str | None = None,
        concurrency: int = 4,
        timeout: float = 60,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self._url = url.rstrip("/")
        # Where people open the files: differs from `url` when the service reaches Seafile by a local address.
        self._public_url = (public_url or url).rstrip("/")
        self._username = username
        self._password = password
        self._watch = [_split(p) for p in watch_paths]
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._lock = threading.Lock()
        self._token: str | None = None
        self._repo_ids: dict[str, str] = {}
        self.walker = TreeWalker(self._level, max_concurrency=concurrency)
        # Which address file downloads work at: the one in Seafile's links, or the one this source was given.
        self._download_as_given = False

    def list_files(self) -> list[RemoteFile]:
        repo_ids = self._load_repo_ids()
        for library in dict.fromkeys(lib for lib, _ in self._watch):
            if library not in repo_ids:
                raise SourceError(f"библиотека {library!r} не найдена в Seafile (есть: {sorted(repo_ids)})")
        return self.walker.run([f"/{library}{folder}".rstrip("/") for library, folder in self._watch])

    def _level(self, path: str) -> Level:
        library, folder = _split(path)
        repo_id = self._repo_id(library)
        try:
            entries = self._get(f"/api/v2.1/repos/{repo_id}/dir/", {"p": folder})["dirent_list"]
            return Level(
                folders=[Folder(_join(path, e["name"]), e["id"]) for e in entries if e["type"] == "dir"],
                files=[
                    self._remote_file(library, repo_id, _join(folder, e["name"]), e["id"])
                    for e in entries if e["type"] != "dir"
                ],
            )
        except (KeyError, TypeError) as e:
            raise SourceError(f"некорректный ответ Seafile на список папки {path}") from e

    def stat(self, path: str) -> RemoteFile | None:
        library, inner = _split(path)
        repo_id = self._repo_id(library)
        try:
            detail = self._get(f"/api2/repos/{repo_id}/file/detail/", {"p": inner})
        except _NotFound:
            # Gone for sure only while its library and watched folder are still there; this raises when not.
            repo_id = self._load_repo_ids().get(library)
            if repo_id is None:
                raise SourceError(f"библиотека {library!r} не найдена в Seafile") from None
            for lib, folder in self._watch:
                if lib == library and folder != "/" and inner.startswith(folder + "/"):
                    self._get(f"/api2/repos/{repo_id}/dir/", {"p": folder})
            return None
        try:
            return self._remote_file(library, repo_id, inner, detail["id"])
        except (KeyError, TypeError) as e:
            raise SourceError(f"Seafile не сообщил версию файла {path}") from e

    def read_text(self, file: RemoteFile) -> str:
        return self.read_bytes(file).decode("utf-8", errors="ignore")

    def read_bytes(self, file: RemoteFile) -> bytes:
        library, inner = _split(file.path)
        repo_id = self._repo_id(library)
        download_url = self._get(f"/api2/repos/{repo_id}/file/", {"p": inner, "reuse": "1"})
        if not isinstance(download_url, str):
            raise SourceError(f"не удалось скачать {file.path}: Seafile не дал ссылку на файл")
        return self._download(download_url, file.path).content

    def _download(self, given: str, path: str) -> requests.Response:
        """Fetch a file by the link Seafile gave, making sure the answer is the file.

        The link is built from Seafile's own idea of its address, which may be
        stale or unreachable from here, and another server at that address may
        answer 200 with a page of its own. Text that is not the file would
        read as "no events", so an answer is taken only when it carries the
        Content-Disposition header the Seafile file server always sends. The
        same link is also tried at the address this source was given.
        """
        ours = urlunsplit((*urlsplit(self._url)[:2], *urlsplit(given)[2:]))
        candidates = list(dict.fromkeys([ours, given]))
        if self._download_as_given:
            candidates.reverse()
        problems: list[str] = []
        for url in candidates:
            where = "{0}://{1}".format(*urlsplit(url)[:2])
            try:
                response = send("GET", url, self._retry_delays, verify=self._verify_ssl, timeout=self._timeout)
            except SourceError as e:
                problems.append(str(e))
                continue
            if response.status_code == 200 and "Content-Disposition" in response.headers:
                # Remembered, so that the address that does not work is not tried first for every file.
                self._download_as_given = url == given
                return response
            kind = response.headers.get("Content-Type", "без типа")
            problems.append(f"{where} ответил HTTP {response.status_code} ({kind}), но это не файл")
        raise SourceError(
            f"не удалось скачать {path}: {'; '.join(problems)}. "
            "Проверьте адрес файлового сервера (FILE_SERVER_ROOT) в настройках Seafile"
        )

    def _remote_file(self, library: str, repo_id: str, inner: str, object_id: str) -> RemoteFile:
        return RemoteFile(
            path=f"/{library}{inner}",
            version=object_id,
            link=f"{self._public_url}/lib/{repo_id}/file{quote(inner)}",
        )

    def _repo_id(self, library: str) -> str:
        repo_id = (self._repo_ids or self._load_repo_ids()).get(library)
        if repo_id is None:
            raise SourceError(f"библиотека {library!r} не найдена в Seafile")
        return repo_id

    def _load_repo_ids(self) -> dict[str, str]:
        repo_ids: dict[str, str] = {}
        for repo in self._get("/api2/repos/"):
            if repo["name"] in repo_ids and repo_ids[repo["name"]] != repo["id"]:
                raise SourceError(f"в Seafile несколько библиотек с именем {repo['name']!r}")
            repo_ids[repo["name"]] = repo["id"]
        self._repo_ids = repo_ids
        return repo_ids

    def _get(self, path: str, params: dict[str, str] | None = None) -> Any:
        response = self._authorized_get(path, params)
        if response.status_code == 401:
            # The token was revoked or expired: sign in again, once.
            with self._lock:
                self._token = None
            response = self._authorized_get(path, params)
        if response.status_code != 200:
            error = _NotFound if response.status_code == 404 else SourceError
            raise error(f"Seafile ответил HTTP {response.status_code} на {path}: {response.text[:200]}")
        try:
            return response.json()
        except ValueError as e:
            raise SourceError(f"некорректный ответ Seafile на {path}: {e}") from e

    def _authorized_get(self, path: str, params: dict[str, str] | None):
        return send(
            "GET", self._url + path, self._retry_delays,
            params=params, headers={"Authorization": f"Token {self._sign_in()}"},
            verify=self._verify_ssl, timeout=self._timeout,
        )

    def _sign_in(self) -> str:
        with self._lock:
            if self._token is None:
                response = send(
                    "POST", self._url + "/api2/auth-token/", self._retry_delays,
                    data={"username": self._username, "password": self._password},
                    verify=self._verify_ssl, timeout=self._timeout,
                )
                if response.status_code != 200:
                    raise SourceError(f"ошибка входа в Seafile (HTTP {response.status_code})")
                try:
                    self._token = response.json()["token"]
                except (ValueError, KeyError) as e:
                    raise SourceError("Seafile не вернул токен при входе") from e
            return self._token


def _join(folder: str, name: str) -> str:
    return folder.rstrip("/") + "/" + name


def _split(path: str) -> tuple[str, str]:
    """Split "/library/a/b" into ("library", "/a/b")."""
    library, _, inner = path.strip().strip("/").partition("/")
    if not library:
        raise SourceError("путь Seafile должен начинаться с имени библиотеки, например /mylib")
    return library, "/" + inner
