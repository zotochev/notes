from zoneinfo import ZoneInfo

import pytest

from notificator.migrate import move_source, moved_path
from notificator.store import Store
from notificator.sync.engine import SyncEngine, SyncSettings
from tests.sync.fakes import FakeCalendar, FakeSource

TZ = ZoneInfo("Europe/Ulyanovsk")
SETTINGS = SyncSettings(default_calendar="primary", default_tz=TZ)


def event(uid: str, summary: str) -> str:
    return f"<event><uid>{uid}</uid><summary>{summary}</summary><start>2030-01-01 10:00</start></event>"


class World:
    def __init__(self, tmp_path) -> None:
        self.store = Store(tmp_path / "state.db")
        self.calendar = FakeCalendar()
        self.old = FakeSource()
        self.new = FakeSource()

    def sync(self, source_id: str, source: FakeSource):
        return SyncEngine(source_id, source, self.calendar, self.store, SETTINGS).run_cycle()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


def test_moved_events_keep_their_calendar_events(world):
    world.old.files["/Notes/a.md"] = event("u1", "Meeting") + event("u2", "Not copied to the new source")
    world.old.files["/Notes/deep/b.md"] = event("u1", "Other")
    world.sync("owncloud", world.old)
    ids_before = {body["summary"]: key for key, body in world.calendar.events.items()}
    world.new.files["/mylib/Notes/a.md"] = event("u1", "Meeting")
    world.new.files["/mylib/Notes/deep/b.md"] = event("u1", "Other")
    world.calendar.calls.clear()

    result = move_source(world.store, "owncloud", "seafile", [("/Notes", "/mylib/Notes")], apply=True)
    report = world.sync("seafile", world.new)

    assert (result.moved, result.skipped) == (3, [])
    assert result.examples == [("/Notes/a.md", "/mylib/Notes/a.md"), ("/Notes/deep/b.md", "/mylib/Notes/deep/b.md")]
    assert sorted(world.calendar.calls) == ["delete", "update", "update"]
    assert (report.pushed, report.deleted) == (2, 1)
    assert {body["summary"]: key for key, body in world.calendar.events.items()} == {
        "Meeting": ids_before["Meeting"], "Other": ids_before["Other"],
    }
    assert world.calendar.events[ids_before["Meeting"]]["description"].startswith("uid: u1\nfile: /mylib/Notes/a.md")
    assert world.store.event_counts() == {"seafile": 2}


def test_without_apply_nothing_changes(world):
    world.old.files["/a.md"] = event("u1", "Meeting")
    world.sync("owncloud", world.old)

    result = move_source(world.store, "owncloud", "seafile", [("/", "/mylib")], apply=False)

    assert (result.moved, result.examples) == (1, [("/a.md", "/mylib/a.md")])
    assert world.store.event_counts() == {"owncloud": 1}


def test_events_that_cannot_be_moved_are_reported_and_left(world):
    world.old.files["/Notes/a.md"] = event("u1", "Meeting")
    world.old.files["/Copy/a.md"] = event("u1", "Lands on the same new path")
    world.old.files["/Elsewhere/b.md"] = event("u1", "Outside the rules")
    world.sync("owncloud", world.old)
    rules = [("/Notes", "/mylib/Notes"), ("/Copy", "/mylib/Notes")]

    result = move_source(world.store, "owncloud", "seafile", rules, apply=True)

    assert result.moved == 1
    assert result.skipped == [
        "/Elsewhere/b.md uid=u1: путь не подходит ни под одно правило --path",
        "/Notes/a.md uid=u1: в seafile уже отслеживается /mylib/Notes/a.md",
    ]
    assert world.store.event_counts() == {"owncloud": 2, "seafile": 1}


@pytest.mark.parametrize("path, expected", [
    ("/Notes/a.md", "/lib/Notes/a.md"),
    ("/Notes/Work/b.md", "/work/b.md"),
    ("/NotesOld/c.md", None),
])
def test_longest_matching_prefix_is_replaced_at_a_folder_boundary(path, expected):
    assert moved_path(path, [("/Notes", "lib/Notes/"), ("/Notes/Work/", "/work")]) == expected


def test_root_prefix_matches_every_path():
    assert moved_path("/a/b.md", [("/", "/mylib")]) == "/mylib/a/b.md"


CONFIG_JSON = (
    '{"active_source": "owncloud", "sources": {'
    '"owncloud": {"type": "local", "roots": ["."]}, "seafile": {"type": "local", "roots": ["."]}}}'
)


def run_command(tmp_path, monkeypatch, capsys, new_source: FakeSource, *args: str) -> tuple[int, str]:
    from notificator.__main__ import main

    (tmp_path / "config.json").write_text(CONFIG_JSON, encoding="utf-8")
    monkeypatch.setattr("notificator.preview.build_source", lambda cfg: new_source)
    code = main(["--data-dir", str(tmp_path), "move-source", "owncloud", "seafile", *args])
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_command_shows_the_first_cycle_on_the_new_source_before_moving(world, tmp_path, monkeypatch, capsys):
    world.old.files["/Notes/a.md"] = event("u1", "Meeting") + event("u2", "Lost on the way")
    world.old.files["/Notes/b.md"] = event("u1", "File not copied")
    world.sync("owncloud", world.old)
    world.new.files["/mylib/Notes/a.md"] = event("u1", "Meeting")
    world.new.files["/mylib/Notes/c.md"] = event("u1", "Only in the new source")

    code, out = run_command(tmp_path, monkeypatch, capsys, world.new, "--path", "/Notes=/mylib/Notes")

    assert code == 0
    assert "события сохранятся: 1 (из них обновить описание: 1)" in out
    assert "будут удалены из календаря: 2" in out
    assert "удалить 'Lost on the way' uid=u2 seafile:/mylib/Notes/a.md" in out
    assert "удалить 'File not copied' uid=u1 seafile:/mylib/Notes/b.md" in out
    assert "создать 'Only in the new source' file: /mylib/Notes/c.md" in out
    assert "Ничего не изменено" in out
    assert world.store.event_counts() == {"owncloud": 3}


def test_command_moves_with_apply_and_keeps_a_copy_of_the_state(world, tmp_path, monkeypatch, capsys):
    world.old.files["/Notes/a.md"] = event("u1", "Meeting")
    world.sync("owncloud", world.old)
    world.new.files["/mylib/Notes/a.md"] = event("u1", "Meeting")

    code, out = run_command(tmp_path, monkeypatch, capsys, world.new, "--path", "/Notes=/mylib/Notes", "--apply")

    assert code == 0
    assert "будут удалены из календаря: 0" in out and "будут созданы заново: 0" in out
    assert world.store.event_counts() == {"seafile": 1}
    with Store(tmp_path / "state.db.before-move") as backup:
        assert backup.event_counts() == {"owncloud": 1}


def test_command_does_not_move_when_the_new_source_cannot_be_checked(world, tmp_path, monkeypatch, capsys):
    world.old.files["/Notes/a.md"] = event("u1", "Meeting")
    world.sync("owncloud", world.old)
    world.new.fail_listing = True

    code, out = run_command(tmp_path, monkeypatch, capsys, world.new, "--path", "/Notes=/mylib/Notes", "--apply")
    unchecked, _ = run_command(
        tmp_path, monkeypatch, capsys, world.new, "--path", "/Notes=/mylib/Notes", "--apply", "--no-check",
    )

    assert (code, unchecked) == (1, 0)
    assert "перенос не выполнен" in out
    assert world.store.event_counts() == {"seafile": 1}
