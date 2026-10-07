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
                        stat = path.stat()
                        files[path.as_posix()] = RemoteFile(path.as_posix(), f"{stat.st_mtime_ns}:{stat.st_size}")
            except OSError as e:
                raise SourceError(f"не удалось прочитать каталог {root}: {e}") from e
        return list(files.values())

    def read_text(self, file: RemoteFile) -> str:
        try:
            return Path(file.path).read_text(encoding="utf-8", errors="ignore")
        except OSError as e:
            raise SourceError(f"не удалось прочитать файл: {e}") from e
