import json
from zoneinfo import ZoneInfo

import pytest

from notificator.legacy import import_legacy_state
from notificator.store import Store
from notificator.sync.engine import SyncEngine, SyncSettings
from tests.sync.fakes import FakeCalendar, FakeSource

TZ = ZoneInfo("Europe/Ulyanovsk")
SETTINGS = SyncSettings(default_calendar="primary", default_tz=TZ)


def old_record(uid: str, summary: str, gcal_event_id: str | None, calendar_id: str | None = None) -> dict:
    return {
        "event_key": "/notes/a.md", "file_path": "/notes/a.md", "content_hash": "x", "status": "valid",
        "gcal_event_id": gcal_event_id, "calendar_id": calendar_id,
        "parsed": {
            "uid": uid, "summary": summary, "description": f"uid: {uid}\nfile: /notes/a.md\n---\n",
            "start": "2030-01-01T10:00:00+04:00", "end": "2030-01-01T10:30:00+04:00",
            "time_zone": "Europe/Ulyanovsk", "location": None, "attendees": [], "recurrence": None, "calendar_id": None,
        },
    }


def event(uid: str, summary: str) -> str:
    return f"<event><uid>{uid}</uid><summary>{summary}</summary><start>2030-01-01 10:00</start></event>"


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "state.db") as s:
        yield s


def write_state(tmp_path, events_index: dict):
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"files_index": {}, "events_index": events_index, "errors": {}}), encoding="utf-8")
    return path


def test_imported_events_are_updated_in_place_and_stale_ones_removed(tmp_path, store):
    state = write_state(tmp_path, {"/notes/a.md": {
        "u1": old_record("u1", "Old title", "gcal-1"),
        "u2": old_record("u2", "Removed from file since", "gcal-2", calendar_id="work"),
    }})
    calendar = FakeCalendar()
    calendar.events[("primary", "gcal-1")] = {"summary": "Old title"}
    calendar.events[("work", "gcal-2")] = {"summary": "Removed from file since"}
    calendar.events[("primary", "by-hand")] = {"summary": "Dentist"}
    source = FakeSource()
    source.files["/notes/a.md"] = event("u1", "New title")

    result = import_legacy_state(state, store, "cloud", "primary", TZ)
    SyncEngine("cloud", source, calendar, store, SETTINGS).run_cycle()

    assert (result.imported, result.skipped) == (2, [])
    assert set(calendar.events) == {("primary", "gcal-1"), ("primary", "by-hand")}
    assert calendar.summaries() == ["Dentist", "New title"]
    assert calendar.calls == ["update", "delete"]


def test_events_of_a_file_that_no_longer_exists_are_removed(tmp_path, store):
    state = write_state(tmp_path, {"/gone.md": {"u1": old_record("u1", "Orphan", "gcal-1")}})
    calendar = FakeCalendar()
    calendar.events[("primary", "gcal-1")] = {"summary": "Orphan"}
    source = FakeSource()
    source.files["/other.md"] = "просто текст"

    import_legacy_state(state, store, "cloud", "primary", TZ)
    SyncEngine("cloud", source, calendar, store, SETTINGS).run_cycle()

    assert calendar.events == {}


def test_imported_events_are_listed_before_the_first_cycle(tmp_path, store):
    state = write_state(tmp_path, {"C:\\notes\\a.md": {"u1": old_record("u1", "Old title", "gcal-1")}})

    import_legacy_state(state, store, "disk", "primary", TZ)

    (shown,) = store.events("disk")
    assert (shown["path"], shown["summary"], shown["calendar_id"], shown["synced"]) == (
        "C:/notes/a.md", "Old title", "primary", False,
    )


def test_events_that_cannot_be_carried_over_are_reported(tmp_path, store):
    never_synced = old_record("u1", "Never synced", None)
    no_data = {**old_record("u2", "x", "gcal-2"), "parsed": None}
    state = write_state(tmp_path, {"/a.md": {"u1": never_synced, "u2": no_data, "u3": old_record("u3", "Fine", "gcal-3")}})

    first = import_legacy_state(state, store, "cloud", "primary", TZ)
    second = import_legacy_state(state, store, "cloud", "primary", TZ)

    assert first.imported == 1
    assert [s.split(": ")[1] for s in first.skipped] == ["не было в календаре", "в старом состоянии нет данных события"]
    assert second.imported == 0
    assert "/a.md uid=u3: уже отслеживается" in second.skipped


def test_unreadable_state_file_is_an_error(tmp_path, store):
    (tmp_path / "state.json").write_text("[]", encoding="utf-8")

    with pytest.raises(ValueError, match="не удалось прочитать"):
        import_legacy_state(tmp_path / "state.json", store, "cloud", "primary", TZ)
