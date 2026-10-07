from zoneinfo import ZoneInfo

import pytest

from notificator.cleanup import delete_untracked, find_untracked
from notificator.store import Store
from notificator.sync.engine import SyncEngine, SyncSettings
from tests.sync.fakes import FakeCalendar, FakeSource

SETTINGS = SyncSettings(default_calendar="primary", default_tz=ZoneInfo("Europe/Ulyanovsk"))
OLD_HEADER = "uid: old1\nfile: /notes/old.md\n---\nтекст"


class ListableFakeCalendar(FakeCalendar):
    def writable_calendars(self):
        return [{"id": c, "summary": c.title(), "primary": c == "primary"} for c in ("primary", "work")]

    def list_events(self, calendar_id):
        return [{"id": i, **body} for (cal, i), body in self.events.items() if cal == calendar_id]


@pytest.fixture
def world(tmp_path):
    calendar = ListableFakeCalendar()
    source = FakeSource()
    with Store(tmp_path / "state.db") as store:
        yield calendar, source, store, SyncEngine("cloud", source, calendar, store, SETTINGS)


def test_finds_leftovers_but_not_tracked_or_foreign_events(world):
    calendar, source, store, engine = world
    source.files["/a.md"] = "<event><uid>u1</uid><summary>Tracked</summary><start>2030-01-01 10:00</start></event>"
    engine.run_cycle()
    calendar.events[("primary", "old-1")] = {
        "summary": "Leftover", "description": OLD_HEADER, "start": {"dateTime": "2029-05-05T10:00:00+04:00"},
    }
    calendar.events[("work", "old-2")] = {
        "summary": "Leftover with link", "start": {"date": "2029-05-06"},
        "description": 'uid: old2\nfile: /b.md\nlink: <a href="https://x/y">b.md</a>\n---\n',
    }
    calendar.events[("primary", "mine-1")] = {"summary": "Dentist", "description": "uid: looks similar but is not ours"}
    calendar.events[("primary", "mine-2")] = {"summary": "No description"}

    found = find_untracked(calendar, store)

    assert [(e.calendar_name, e.event_id, e.summary, e.uid, e.file, e.start) for e in found] == [
        ("Primary", "old-1", "Leftover", "old1", "/notes/old.md", "2029-05-05T10:00:00+04:00"),
        ("Work", "old-2", "Leftover with link", "old2", "/b.md", "2029-05-06"),
    ]


def test_deleting_leftovers_keeps_everything_else(world):
    calendar, source, store, engine = world
    source.files["/a.md"] = "<event><uid>u1</uid><summary>Tracked</summary><start>2030-01-01 10:00</start></event>"
    engine.run_cycle()
    calendar.events[("primary", "old-1")] = {"summary": "Leftover", "description": OLD_HEADER}
    calendar.events[("primary", "mine-1")] = {"summary": "Dentist"}

    delete_untracked(calendar, find_untracked(calendar, store))

    assert calendar.summaries() == ["Dentist", "Tracked"]
    assert find_untracked(calendar, store) == []
