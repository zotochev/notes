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


@pytest.mark.parametrize("allow_infinity", [True, False])
def test_lists_every_file_with_or_without_depth_infinity(server, allow_infinity):
    server.allow_infinity = allow_infinity

    files = source(server).list_files()

    assert paths(files) == set(FILES)
    assert all(f.version for f in files)


def test_server_refusing_depth_infinity_is_asked_only_once(server):
    server.allow_infinity = False
    src = source(server, concurrency=1)

    src.list_files()
    src.list_files()

    assert sum(depth == "infinity" for _, _, depth in server.requests) == 1


def test_directory_too_big_for_one_request_is_listed_in_pieces(server):
    server.too_big = {"/notes"}

    files = source(server).list_files()

    assert paths(files) == set(FILES)
    # The parent is split, but its subdirectory is still fetched whole.
    assert ("PROPFIND", "/notes/deep", "infinity") in server.requests


def test_splitting_goes_as_deep_as_needed(server):
    server.too_big = {"/notes", "/notes/deep", "/notes/deep/er"}

    assert paths(source(server).list_files()) == set(FILES)


def test_too_big_directory_is_not_asked_whole_again(server):
    server.too_big = {"/notes"}
    src = source(server)

    src.list_files()
    src.list_files()

    assert server.requests.count(("PROPFIND", "/notes", "infinity")) == 1


def test_whole_tree_timeout_is_not_retried_before_splitting(server):
    server.too_big = {"/notes"}

    WebDavSource(server.url, USER, lambda: PASSWORD, retry_delays=(0.0, 0.0, 0.0), timeout=5).list_files()

    assert server.requests.count(("PROPFIND", "/notes", "infinity")) == 1


def test_directory_that_fails_even_on_its_own_still_fails_the_listing(server):
    server.too_big = {"/notes"}
    server.broken = {"/notes/deep"}

    with pytest.raises(SourceError, match="/notes/deep"):
        source(server).list_files()


def test_depth_infinity_can_be_disabled_up_front(server):
    source(server, depth_infinity=False).list_files()

    assert all(depth == "1" for _, _, depth in server.requests)


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


@pytest.mark.parametrize("allow_infinity", [True, False])
def test_one_failing_directory_fails_the_whole_listing(server, allow_infinity):
    server.allow_infinity = allow_infinity
    server.broken = {"/notes/deep" if not allow_infinity else "/notes"}

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


@pytest.mark.parametrize("path", ["/gone.md", "/notes"])
def test_stat_of_a_missing_file_or_a_directory_is_an_error(server, path):
    with pytest.raises(SourceError):
        source(server).stat(path)
