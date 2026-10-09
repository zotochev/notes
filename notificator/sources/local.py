"""Files in local directories."""
from __future__ import annotations

from pathlib import Path

from notificator.core.model import RemoteFile
from notificator.sync.ports import SourceError


class LocalSource:
    def __init__(self, roots: list[Path]) -> None:
        self._roots = [r.resolve() for r in roots]

    def list_files(self) -> list[RemoteFile]:
        files: dict[str, RemoteFile] = {}
        for root in self._roots:
            if not root.is_dir():
                # A missing root is a failure, not an empty directory: it may be an unmounted disk.
                raise SourceError(f"каталог недоступен: {root}")
            try:
                for path in root.rglob("*"):
                    if path.is_file():
                        files[path.as_posix()] = _remote_file(path)
            except OSError as e:
                raise SourceError(f"не удалось прочитать каталог {root}: {e}") from e
        return list(files.values())

    def stat(self, path: str) -> RemoteFile | None:
        file = Path(path)
        try:
            if file.is_file():
                return _remote_file(file)
        except OSError as e:
            raise SourceError(f"не удалось прочитать файл: {e}") from e
        if not any(root in file.parents and root.is_dir() for root in self._roots):
            # As in a listing: a missing root may be an unmounted disk.
            raise SourceError(f"каталог недоступен: {file.parent}")
        return None

    def read_text(self, file: RemoteFile) -> str:
        return self.read_bytes(file).decode("utf-8", errors="ignore")

    def read_bytes(self, file: RemoteFile) -> bytes:
        try:
            return Path(file.path).read_bytes()
        except OSError as e:
            raise SourceError(f"не удалось прочитать файл: {e}") from e


def _remote_file(path: Path) -> RemoteFile:
    stat = path.stat()
    return RemoteFile(path.as_posix(), f"{stat.st_mtime_ns}:{stat.st_size}")
