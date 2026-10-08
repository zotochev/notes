import pytest

from notificator.core.model import RemoteFile
from notificator.sources.webdav import WebDavSource
from notificator.sync.ports import SourceError
from tests.sources.webdav_server import PASSWORD, USER, FakeWebDav

FILES = {
    "/root.md": "root",
    "/notes/a.md": "a",
    "/notes/deep/er/b.txt": "b",
    "/Мои заметки/план на год.md": "план",
    "/empty-sibling/c.md": "c",
}


@pytest.fixture
def server():
    srv = FakeWebDav()
    srv.files.update(FILES)
    yield srv
    srv.shutdown()
    srv.server_close()


def source(server: FakeWebDav, **kwargs) -> WebDavSource:
    kwargs.setdefault("password", lambda: PASSWORD)
    return WebDavSource(server.url, USER, retry_delays=(0.0,), timeout=5, **kwargs)


def paths(files: list[RemoteFile]) -> set[str]:
    return {f.path for f in files}


def test_lists_every_file(server):
    files = source(server).list_files()

    assert paths(files) == set(FILES)
    assert all(f.version for f in files)


def test_listing_asks_one_directory_at_a_time_and_never_a_whole_tree(server):
    source(server).list_files()

    assert sorted(path for _, path, _ in server.requests) == [
        "/", "/empty-sibling", "/notes", "/notes/deep", "/notes/deep/er", "/Мои заметки",
    ]
    assert all(depth == "1" for _, _, depth in server.requests)


def test_every_directory_is_listed_again_unless_folder_etags_are_trusted(server):
    src = source(server)
    src.list_files()
    server.requests.clear()

    src.list_files()

    assert len(server.requests) == 6


def test_unchanged_directories_are_not_listed_again_when_folder_etags_are_trusted(server):
    src = source(server, trust_folder_etags=True)
    before = src.list_files()
    server.requests.clear()

    after = src.list_files()

    assert server.requests == [("PROPFIND", "/", "1")]
    assert {f.path: f for f in after} == {f.path: f for f in before}


def test_only_the_changed_branch_is_listed_again_when_folder_etags_are_trusted(server):
    src = source(server, trust_folder_etags=True)
    src.list_files()
    server.files["/notes/deep/er/b.txt"] = "changed"
    server.files["/notes/deep/new.md"] = "new"
    del server.files["/empty-sibling/c.md"]
    server.requests.clear()

    files = {f.path: f for f in src.list_files()}

    assert sorted(path for _, path, _ in server.requests) == ["/", "/notes", "/notes/deep", "/notes/deep/er"]
    assert files == {f.path: f for f in source(server).list_files()}
    assert "/notes/deep/new.md" in files and "/empty-sibling/c.md" not in files


def test_watch_paths_limit_the_listing(server):
    files = source(server, watch_paths=["notes/", "/Мои заметки"]).list_files()

    assert paths(files) == {"/notes/a.md", "/notes/deep/er/b.txt", "/Мои заметки/план на год.md"}


def test_version_changes_with_content(server):
    before = {f.path: f.version for f in source(server).list_files()}
    server.files["/notes/a.md"] = "changed"

    after = {f.path: f.version for f in source(server).list_files()}

    assert after["/notes/a.md"] != before["/notes/a.md"]
    assert after["/root.md"] == before["/root.md"]


def test_private_link_is_reported_when_the_server_has_it(server):
    assert all(f.link is None for f in source(server).list_files())

    server.private_links = True

    links = {f.path: f.link for f in source(server).list_files()}
    assert links["/notes/a.md"] == "https://cloud.example/f//notes/a.md"


def test_reads_file_text(server):
    text = source(server).read_text(RemoteFile("/Мои заметки/план на год.md", "v"))

    assert text == "план"


def test_one_failing_directory_fails_the_whole_listing(server):
    server.broken = {"/notes/deep"}

    with pytest.raises(SourceError):
        source(server).list_files()


def test_temporary_server_error_is_retried(server):
    server.flaky = {"/notes": 1}

    assert paths(source(server).list_files()) == set(FILES)


def test_wrong_password_is_an_error(server):
    with pytest.raises(SourceError, match="авторизации"):
        source(server, password=lambda: "wrong").list_files()


def test_missing_watch_path_is_an_error(server):
    with pytest.raises(SourceError):
        source(server, watch_paths=["/no-such-dir"]).list_files()


def test_truncated_response_is_an_error(server):
    server.truncate_xml = True

    with pytest.raises(SourceError, match="некорректный ответ"):
        source(server).list_files()


def test_reading_a_missing_file_is_an_error(server):
    with pytest.raises(SourceError):
        source(server).read_text(RemoteFile("/gone.md", "v"))


def test_unreachable_server_is_an_error(server):
    src = source(server)
    server.shutdown()
    server.server_close()

    with pytest.raises(SourceError):
        src.list_files()


def test_stat_reports_a_file_as_the_listing_does(server):
    server.private_links = True
    src = source(server)
    listed = {f.path: f for f in src.list_files()}

    assert src.stat("/Мои заметки/план на год.md") == listed["/Мои заметки/план на год.md"]


def test_stat_of_a_missing_file_says_it_is_gone(server):
    assert source(server).stat("/notes/gone.md") is None
    assert source(server, watch_paths=["/notes"]).stat("/notes/deep/gone.md") is None


def test_stat_does_not_call_a_file_gone_when_its_watch_path_is_missing_or_it_is_a_directory(server):
    with pytest.raises(SourceError):
        source(server, watch_paths=["/unmounted"]).stat("/unmounted/a.md")
    with pytest.raises(SourceError):
        source(server).stat("/notes")
