import pytest

from notificator.core.model import RemoteFile
from notificator.sources.seafile import SeafileSource
from notificator.sync.ports import SourceError
from tests.sources.seafile_server import PASSWORD, USER, FakeSeafile


@pytest.fixture
def server():
    srv = FakeSeafile()
    srv.libraries["mylib"] = {"/bar.md": "bar", "/notes/план.md": "план", "/notes/deep/c.txt": "c", "/other/d.md": "d"}
    srv.libraries["work"] = {"/w.md": "w"}
    yield srv
    srv.shutdown()
    srv.server_close()


def source(server: FakeSeafile, watch_paths=("/mylib",), password: str = PASSWORD) -> SeafileSource:
    return SeafileSource(server.url, USER, password, list(watch_paths), retry_delays=(0.0,), timeout=5)


def paths(files: list[RemoteFile]) -> set[str]:
    return {f.path for f in files}


@pytest.mark.parametrize("trailing_slash", [False, True])
def test_lists_a_library_with_its_name_as_path_prefix(server, trailing_slash):
    server.parent_dir_trailing_slash = trailing_slash

    files = source(server).list_files()

    assert paths(files) == {"/mylib/bar.md", "/mylib/notes/план.md", "/mylib/notes/deep/c.txt", "/mylib/other/d.md"}


def test_file_has_a_web_link_and_a_content_version(server):
    before = {f.path: f for f in source(server).list_files()}
    server.libraries["mylib"]["/bar.md"] = "changed"
    after = {f.path: f for f in source(server).list_files()}

    assert before["/mylib/notes/план.md"].link == f"{server.url}/lib/id-mylib/file/notes/%D0%BF%D0%BB%D0%B0%D0%BD.md"
    assert after["/mylib/bar.md"].version != before["/mylib/bar.md"].version
    assert after["/mylib/other/d.md"].version == before["/mylib/other/d.md"].version


def test_watch_paths_select_folders_and_libraries(server):
    files = source(server, ["/mylib/notes", "work/"]).list_files()

    assert paths(files) == {"/mylib/notes/план.md", "/mylib/notes/deep/c.txt", "/work/w.md"}


def test_folder_name_is_not_matched_as_a_prefix_of_another_folder(server):
    server.libraries["mylib"]["/notes-old/x.md"] = "x"

    assert "/mylib/notes-old/x.md" not in paths(source(server, ["/mylib/notes"]).list_files())


def test_reads_file_text(server):
    src = source(server)
    files = {f.path: f for f in src.list_files()}

    assert src.read_text(files["/mylib/notes/план.md"]) == "план"


def test_reads_without_a_prior_listing(server):
    assert source(server).read_text(RemoteFile("/mylib/bar.md", "v")) == "bar"


def test_signs_in_once_and_again_after_the_token_is_revoked(server):
    src = source(server)
    src.list_files()
    src.list_files()
    assert server.logins == 1

    server.revoke_token()

    assert len(src.list_files()) == 4
    assert server.logins == 2


def test_wrong_password_is_an_error(server):
    with pytest.raises(SourceError, match="входа"):
        source(server, password="wrong").list_files()


def test_missing_library_is_an_error(server):
    with pytest.raises(SourceError, match="не найдена"):
        source(server, ["/no-such-library"]).list_files()


def test_missing_folder_is_an_error_not_an_empty_listing(server):
    with pytest.raises(SourceError, match="404"):
        source(server, ["/mylib/no-such-folder"]).list_files()


def test_two_libraries_with_one_name_are_an_error(server):
    server.extra_repos = [{"id": "another-id", "name": "mylib", "type": "srepo"}]

    with pytest.raises(SourceError, match="несколько библиотек"):
        source(server).list_files()


def test_failed_listing_request_is_an_error(server):
    server.broken = {"/api/v2.1/repos/id-mylib/dir/"}

    with pytest.raises(SourceError):
        source(server).list_files()


def test_reading_a_missing_file_is_an_error(server):
    with pytest.raises(SourceError):
        source(server).read_text(RemoteFile("/mylib/gone.md", "v"))


def test_watch_path_without_a_library_is_rejected():
    with pytest.raises(SourceError, match="имени библиотеки"):
        SeafileSource("http://x", USER, PASSWORD, ["/"])


def test_stat_reports_a_file_as_the_listing_does(server):
    listed = {f.path: f for f in source(server).list_files()}

    assert source(server).stat("/mylib/notes/план.md") == listed["/mylib/notes/план.md"]


def test_stat_of_a_missing_file_is_an_error(server):
    with pytest.raises(SourceError):
        source(server).stat("/mylib/gone.md")
