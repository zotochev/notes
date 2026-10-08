"""A small in-process Seafile REST API, shaped like the responses of Seafile 11."""
from __future__ import annotations

import json
import threading
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlparse

USER, PASSWORD = "me@example.com", "secret"


class FakeOtherSite(ThreadingHTTPServer):
    """Some other web application: answers every request with its own page and HTTP 200."""

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _OtherSiteHandler)
        self.requests = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _OtherSiteHandler(BaseHTTPRequestHandler):
    server: FakeOtherSite

    def log_message(self, *args) -> None:
        pass

    def do_GET(self) -> None:
        self.server.requests += 1
        data = b"<!DOCTYPE html><html><body>another application</body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class FakeSeafile(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        # library name -> {path inside library: text}
        self.libraries: dict[str, dict[str, str]] = {}
        self.extra_repos: list[dict] = []
        self.token = "token-1"
        self.broken: set[str] = set()
        # (folder, recursive) of every v2.1 listing request, in order.
        self.listings: list[tuple[str, bool]] = []
        self.parent_dir_trailing_slash = False
        self.logins = 0
        # The address Seafile puts into download links; None means its real one.
        self.file_server_root: str | None = None
        # False: this address has no file server behind it (the API is reached past the proxy that has it).
        self.serves_files = True
        self.downloads = 0
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def revoke_token(self) -> None:
        self.token = self.token + "x"

    def folders(self, library: str) -> set[str]:
        result = {"/"}
        for path in self.libraries[library]:
            parts = path.strip("/").split("/")[:-1]
            result.update("/" + "/".join(parts[: i + 1]) for i in range(len(parts)))
        return result


class _Handler(BaseHTTPRequestHandler):
    server: FakeSeafile

    def log_message(self, *args) -> None:
        pass

    def do_POST(self) -> None:
        form = parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        if self.path != "/api2/auth-token/":
            self._json(404, {})
        elif form.get("username") == [USER] and form.get("password") == [PASSWORD]:
            self.server.logins += 1
            self._json(200, {"token": self.server.token})
        else:
            self._json(400, {"non_field_errors": ["Unable to login with provided credentials."]})

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        parts = url.path.strip("/").split("/")
        if url.path in self.server.broken:
            self._json(500, {})
        elif parts[0] == "seafhttp":
            self.server.downloads += 1
            if not self.server.serves_files:
                self._json(404, {})
                return
            library, inner = parts[2], "/" + unquote("/".join(parts[3:]))
            self._reply(
                200, self.server.libraries[library][inner].encode("utf-8"),
                {"Content-Disposition": "attachment;filename*=\"utf-8' 'file\""},
            )
        elif self.headers.get("Authorization") != f"Token {self.server.token}":
            self._json(401, {"detail": "Invalid token"})
        elif url.path == "/api2/repos/":
            repos = [{"id": "id-" + name, "name": name, "type": "repo"} for name in self.server.libraries]
            self._json(200, repos + self.server.extra_repos)
        else:
            self._repo_call(parts, query)

    def _repo_call(self, parts: list[str], query: dict[str, str]) -> None:
        repo = parts.index("repos") + 1
        library = parts[repo].removeprefix("id-")
        files = self.server.libraries[library]
        kind = "/".join(parts[repo + 1:])
        path = query.get("p", "/")
        if kind == "file/detail":
            if path in files:
                self._json(200, {"type": "file", "id": sha1(files[path].encode("utf-8")).hexdigest()})
            else:
                self._json(404, {"error_msg": "File not found"})
        elif kind == "file":
            if path in files:
                root = self.server.file_server_root or self.server.url
                self._json(200, f"{root}/seafhttp/files/{library}{quote(path)}")
            else:
                self._json(404, {"error_msg": "File not found"})
        elif path.rstrip("/") not in self.server.folders(library) and path != "/":
            self._json(404, {"error_msg": f"Folder {path}/ not found."})
        elif parts[0] == "api2":
            self._json(200, [])
        else:
            folder = path.rstrip("/") or "/"
            recursive = query.get("recursive") == "1"
            self.server.listings.append((folder, recursive))
            slash = "/" if self.server.parent_dir_trailing_slash else ""
            base = folder.rstrip("/") + "/"
            entries = [
                {
                    "type": "file",
                    "name": p.rsplit("/", 1)[1],
                    "parent_dir": (p.rsplit("/", 1)[0] + slash) or "/",
                    "id": sha1(text.encode("utf-8")).hexdigest(),
                }
                for p, text in files.items()
                if p.startswith(base) and (recursive or "/" not in p[len(base):])
            ]
            if not recursive:
                entries += [
                    {"type": "dir", "name": d.rsplit("/", 1)[1], "parent_dir": base, "id": self._dir_id(files, d)}
                    for d in sorted(self.server.folders(library))
                    if d != "/" and d.rsplit("/", 1)[0] + "/" == base
                ]
            self._json(200, {"dirent_list": entries})

    @staticmethod
    def _dir_id(files: dict[str, str], folder: str) -> str:
        """Like Seafile: a hash of everything under the folder."""
        inside = sorted((p, text) for p, text in files.items() if p.startswith(folder + "/"))
        return sha1(repr(inside).encode("utf-8")).hexdigest()

    def _json(self, status: int, payload) -> None:
        self._reply(status, json.dumps(payload).encode("utf-8"))

    def _reply(self, status: int, data: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
