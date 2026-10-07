"""
Files in Seafile libraries, read through the Seafile REST API.

Paths look like "/<library name>/<path inside the library>", so a watch path
"/mylib/notes" means the folder "notes" of the library "mylib".
"""
from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote

from notificator.core.model import RemoteFile
from notificator.sources.http import DEFAULT_RETRY_DELAYS, send
from notificator.sync.ports import SourceError


class SeafileSource:
    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        watch_paths: list[str],
        verify_ssl: bool = True,
        timeout: float = 60,
        retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS,
    ) -> None:
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._watch = [_split(p) for p in watch_paths]
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._retry_delays = retry_delays
        self._lock = threading.Lock()
        self._token: str | None = None
        self._repo_ids: dict[str, str] = {}

    def list_files(self) -> list[RemoteFile]:
        repo_ids = self._load_repo_ids()
        files: dict[str, RemoteFile] = {}
        for library in dict.fromkeys(lib for lib, _ in self._watch):
            folders = [folder for lib, folder in self._watch if lib == library]
            repo_id = repo_ids.get(library)
            if repo_id is None:
                raise SourceError(f"библиотека {library!r} не найдена в Seafile (есть: {sorted(repo_ids)})")
            for folder in folders:
                if folder != "/":
                    # A missing folder must be an error, not "no files": check that it exists.
                    self._get(f"/api2/repos/{repo_id}/dir/", {"p": folder})
            # One recursive listing of the library, filtered to the watched folders.
            listing = self._get(f"/api/v2.1/repos/{repo_id}/dir/", {"p": "/", "recursive": "1", "t": "f"})
            for entry in listing["dirent_list"]:
                inner = entry["parent_dir"].rstrip("/") + "/" + entry["name"]
                if any(folder == "/" or inner.startswith(folder + "/") for folder in folders):
                    files[f"/{library}{inner}"] = RemoteFile(
                        path=f"/{library}{inner}",
                        version=entry["id"],
                        link=f"{self._url}/lib/{repo_id}/file{quote(inner)}",
                    )
        return list(files.values())

    def read_text(self, file: RemoteFile) -> str:
        library, inner = _split(file.path)
        repo_id = (self._repo_ids or self._load_repo_ids()).get(library)
        if repo_id is None:
            raise SourceError(f"библиотека {library!r} не найдена в Seafile")
        download_url = self._get(f"/api2/repos/{repo_id}/file/", {"p": inner, "reuse": "1"})
        response = send(
            "GET", download_url, self._retry_delays, verify=self._verify_ssl, timeout=self._timeout
        )
        if response.status_code != 200:
            raise SourceError(f"не удалось скачать {file.path}: HTTP {response.status_code}")
        return response.content.decode("utf-8", errors="ignore")

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
            raise SourceError(f"Seafile ответил HTTP {response.status_code} на {path}: {response.text[:200]}")
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


def _split(path: str) -> tuple[str, str]:
    """Split "/library/a/b" into ("library", "/a/b")."""
    library, _, inner = path.strip().strip("/").partition("/")
    if not library:
        raise SourceError("путь Seafile должен начинаться с имени библиотеки, например /mylib")
    return library, "/" + inner
