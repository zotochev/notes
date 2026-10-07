import os

import pytest

from notificator.sources.local import LocalSource
from notificator.sync.ports import SourceError


def test_lists_files_recursively_and_reads_them(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.md").write_text("привет", encoding="utf-8")
    (tmp_path / "sub" / "b.txt").write_text("b", encoding="utf-8")
    source = LocalSource([tmp_path])

    files = {f.path: f for f in source.list_files()}

    assert files.keys() == {(tmp_path / "a.md").as_posix(), (tmp_path / "sub" / "b.txt").as_posix()}
    assert source.read_text(files[(tmp_path / "a.md").as_posix()]) == "привет"


def test_version_changes_when_the_file_is_modified(tmp_path):
    path = tmp_path / "a.md"
    path.write_text("one", encoding="utf-8")
    source = LocalSource([tmp_path])
    (before,) = source.list_files()

    path.write_text("two!", encoding="utf-8")
    os.utime(path, ns=(path.stat().st_atime_ns, path.stat().st_mtime_ns + 1_000_000))
    (after,) = source.list_files()

    assert after.version != before.version


def test_missing_root_is_an_error_not_an_empty_listing(tmp_path):
    with pytest.raises(SourceError):
        LocalSource([tmp_path / "unmounted"]).list_files()


def test_unreadable_file_is_an_error(tmp_path):
    (tmp_path / "a.md").write_text("x", encoding="utf-8")
    source = LocalSource([tmp_path])
    (file,) = source.list_files()
    (tmp_path / "a.md").unlink()

    with pytest.raises(SourceError):
        source.read_text(file)
