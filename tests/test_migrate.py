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
