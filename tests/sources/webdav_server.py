"""A small in-process WebDAV server with switchable misbehaviour."""
from __future__ import annotations

import base64
import threading
from hashlib import md5
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote

PREFIX = "/dav/spaces/abc$def"
USER, PASSWORD = "test", "secret"


class FakeWebDav(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.files: dict[str, str] = {}
        self.allow_infinity = True
        self.private_links = False
        # Paths answering 500 forever, and paths answering 503 a given number of times.
        self.broken: set[str] = set()
        self.flaky: dict[str, int] = {}
        # Directories whose whole tree is "too big": Depth: infinity on them answers 504.
        self.too_big: set[str] = set()
        self.truncate_xml = False
        self.requests: list[tuple[str, str, str]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}{PREFIX}"

    def dirs(self) -> set[str]:
        result = {"/"}
        for path in self.files:
            parts = path.strip("/").split("/")[:-1]
            result.update("/" + "/".join(parts[: i + 1]) for i in range(len(parts)))
        return result


class _Handler(BaseHTTPRequestHandler):
    server: FakeWebDav

    def log_message(self, *args) -> None:
        pass

    def do_PROPFIND(self) -> None:
        path = self._enter()
        if path is None:
            return
        depth = self.headers.get("Depth", "infinity")
        if depth == "infinity" and path in self.server.too_big:
            self._reply(504, "")
            return
        if depth == "infinity" and not self.server.allow_infinity:
            self._reply(403, '<d:error xmlns:d="DAV:"><d:propfind-finite-depth/></d:error>')
            return
        if path not in self.server.dirs() and path not in self.server.files:
            self._reply(404, "")
            return
        everything = sorted(self.server.dirs() | self.server.files.keys())
        base = path.rstrip("/") + "/"
        members = [path] + [
            p for p in everything
            if p != path and p.startswith(base) and (depth == "infinity" or "/" not in p[len(base):])
        ]
        xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        xml += "".join(self._response_xml(p) for p in members) + "</d:multistatus>"
        self._reply(207, xml[: len(xml) // 2] if self.server.truncate_xml else xml)

    def do_GET(self) -> None:
        path = self._enter()
        if path is None:
            return
        if path in self.server.files:
            self._reply(200, self.server.files[path])
        else:
            self._reply(404, "")

    def _enter(self) -> str | None:
        """Common checks. Returns the relative path, or None when a reply was already sent."""
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        path = "/" + unquote(self.path).removeprefix(PREFIX).strip("/")
        self.server.requests.append((self.command, path, self.headers.get("Depth", "")))
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self._reply(401, "")
        elif path in self.server.broken:
            self._reply(500, "")
        elif self.server.flaky.get(path, 0) > 0:
            self.server.flaky[path] -= 1
            self._reply(503, "")
        else:
            return path
        return None

    def _response_xml(self, path: str) -> str:
        is_dir = path in self.server.dirs()
        href = quote(PREFIX + path) + ("/" if is_dir and path != "/" else "")
        if is_dir:
            props = "<d:resourcetype><d:collection/></d:resourcetype>"
        else:
            etag = md5(self.server.files[path].encode()).hexdigest()
            props = f'<d:resourcetype/><d:getetag>"{etag}"</d:getetag>'
        link = f"<oc:privatelink>https://cloud.example/f/{quote(path)}</oc:privatelink>"
        unknown = ""
        if self.server.private_links and not is_dir:
            props += link
        else:
            unknown = (
                "<d:propstat><d:prop><oc:privatelink/></d:prop>"
                "<d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
            )
        # The 404 block comes first on purpose: clients must not take the first <prop> they see.
        return (
            f"<d:response><d:href>{href}</d:href>{unknown}"
            f"<d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
            "</d:response>"
        )

    def _reply(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
