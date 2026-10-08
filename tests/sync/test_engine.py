from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from notificator.store import Store
from notificator.sync.engine import SyncEngine, SyncSettings
from notificator.sync.ports import CalendarError, CalendarUnavailable
from tests.sync.fakes import FakeCalendar, FakeSource

TZ = ZoneInfo("Europe/Ulyanovsk")
SETTINGS = SyncSettings(default_calendar="primary", default_tz=TZ)


def event(uid: str, summary: str, start: str = "2030-01-01 10:00", **extra: str) -> str:
    fields = {"uid": uid, "summary": summary, "start": start, **extra}
    return "<event>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</event>"


class World:
    def __init__(self, tmp_path) -> None:
        self.source = FakeSource()
        self.calendar = FakeCalendar()
        self.store = Store(tmp_path / "state.db")
        self.engine = self.engine_for("cloud", self.source)

    def engine_for(self, source_id: str, source: FakeSource, settings: SyncSettings = SETTINGS) -> SyncEngine:
        return SyncEngine(
            source_id, source, self.calendar, self.store, settings,
            clock=lambda: datetime(2029, 6, 1, 12, 0, tzinfo=TZ),
        )

    def sync(self, **kwargs):
        return self.engine.run_cycle(**kwargs)


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


def test_new_event_is_created(world):
    world.source.files["/a.md"] = event("u1", "Meeting")

    report = world.sync()

    assert world.calendar.summaries() == ["Meeting"]
    assert (report.listed, report.read, report.pushed, report.error) == (1, 1, 1, None)


def test_unchanged_file_is_not_read_or_pushed_again(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.source.reads.clear()
    world.calendar.calls.clear()

    report = world.sync()

    assert world.source.reads == []
    assert world.calendar.calls == []
    assert report.pushed == 0


def test_changed_event_is_updated_in_place(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    ids_before = set(world.calendar.events)

    world.source.files["/a.md"] = event("u1", "Renamed")
    world.sync()

    assert world.calendar.summaries() == ["Renamed"]
    assert set(world.calendar.events) == ids_before


def test_only_the_changed_event_of_a_file_is_pushed(world):
    world.source.files["/a.md"] = event("u1", "One") + event("u2", "Two")
    world.sync()
    world.calendar.calls.clear()

    world.source.files["/a.md"] = event("u1", "One") + event("u2", "Two changed")
    world.sync()

    assert world.calendar.calls == ["update"]


def test_event_removed_from_file_is_deleted(world):
    world.source.files["/a.md"] = event("u1", "Keep") + event("u2", "Drop")
    world.sync()

    world.source.files["/a.md"] = event("u1", "Keep")
    report = world.sync()

    assert world.calendar.summaries() == ["Keep"]
    assert report.deleted == 1


def test_events_of_a_vanished_file_are_deleted(world):
    world.source.files["/a.md"] = event("u1", "Stays")
    world.source.files["/b.md"] = event("u1", "Goes")
    world.sync()

    del world.source.files["/b.md"]
    world.sync()

    assert world.calendar.summaries() == ["Stays"]
    assert world.store.file_versions("cloud").keys() == {"/a.md"}


def test_long_read_phase_reports_progress(world, monkeypatch, caplog):
    monkeypatch.setattr("notificator.sync.engine._PROGRESS_INTERVAL_SEC", 0)
    many_files(world, 3)
    world.source.files["/skip.py"] = "not watched"

    with caplog.at_level("INFO"):
        world.sync()

    messages = [r.getMessage() for r in caplog.records]
    assert "Источник cloud: файлов в облаке 4, подходящих 3, нужно прочитать 3" in messages
    assert "Источник cloud: прочитано файлов 3 из 3" in messages
    assert "Источник cloud: изменений для календаря: 3, записываю…" in messages
    assert "Источник cloud: записано в календарь 3 из 3" in messages


def test_progress_is_passed_to_the_callback(world, monkeypatch):
    monkeypatch.setattr("notificator.sync.engine._PROGRESS_INTERVAL_SEC", 0)
    many_files(world, 2)
    said: list[str] = []
    engine = SyncEngine(
        "cloud", world.source, world.calendar, world.store, SETTINGS, on_progress=said.append,
    )

    engine.run_cycle()

    assert said[0] == "получаю список файлов…"
    assert said[-1] == "записано в календарь 2 из 2"


def test_held_deletions_are_not_counted_as_work_to_do(world, monkeypatch, caplog):
    many_files(world, 10)
    world.sync()
    world.source.files = {"/new.md": event("u1", "New")}

    with caplog.at_level("INFO"):
        world.sync()

    assert "Источник cloud: изменений для календаря: 1, записываю…" in [r.getMessage() for r in caplog.records]


def test_unwatched_extensions_are_ignored(world):
    world.source.files["/a.py"] = event("u1", "Code")

    world.sync()

    assert world.calendar.summaries() == []
    assert world.source.reads == []


def test_csv_file_is_parsed_by_its_type(world):
    world.source.files["/plan.csv"] = "uid,summary,start\nu1,From CSV,2030-01-01 10:00\n"

    world.sync()

    assert world.calendar.summaries() == ["From CSV"]


def test_file_link_is_put_into_the_description(world):
    world.source.files["/notes/a.md"] = event("u1", "Meeting", description="Agenda")
    world.source.links["/notes/a.md"] = "https://cloud.example/f/1?x=1&y=2"

    world.sync()

    (body,) = world.calendar.events.values()
    assert body["description"] == (
        'uid: u1\nfile: /notes/a.md\nlink: <a href="https://cloud.example/f/1?x=1&amp;y=2">a.md</a>\n---\nAgenda'
    )


# --- failures must never look like "the events are gone" ---


def test_failed_listing_changes_nothing(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.calendar.calls.clear()

    world.source.fail_listing = True
    report = world.sync()

    assert report.error is not None
    assert world.calendar.summaries() == ["Meeting"]
    assert world.calendar.calls == []


def test_failed_read_keeps_events_and_is_retried(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()

    world.source.files["/a.md"] = event("u1", "Renamed")
    world.source.fail_reading = {"/a.md"}
    report = world.sync()

    assert report.read_failed == 1
    assert world.calendar.summaries() == ["Meeting"]
    assert [i.kind for i in world.store.issues("cloud")] == ["source"]

    world.source.fail_reading = set()
    world.sync()

    assert world.calendar.summaries() == ["Renamed"]
    assert world.store.issues("cloud") == []


def test_broken_event_stays_in_calendar_and_is_reported(world):
    world.source.files["/a.md"] = event("u1", "Meeting") + event("u2", "Other")
    world.sync()

    world.source.files["/a.md"] = event("u1", "Meeting", start="не дата") + event("u2", "Other")
    report = world.sync()

    assert world.calendar.summaries() == ["Meeting", "Other"]
    assert report.deleted == 0
    (issue,) = world.store.issues("cloud")
    assert (issue.kind, issue.uid) == ("parse", "u1")


def test_fixing_a_broken_event_clears_the_issue(world):
    world.source.files["/a.md"] = event("u1", "Meeting", start="не дата")
    world.sync()

    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()

    assert world.calendar.summaries() == ["Meeting"]
    assert world.store.issues("cloud") == []


def test_calendar_failure_for_one_event_is_reported_and_retried(world):
    world.source.files["/a.md"] = event("u1", "One") + event("u2", "Two")
    world.calendar.fail_next = CalendarError("quota exceeded")

    report = world.sync()

    assert report.failed == 1
    assert world.calendar.summaries() == ["Two"]
    (issue,) = world.store.issues("cloud")
    assert (issue.kind, issue.uid, issue.message) == ("sync", "u1", "quota exceeded")

    report = world.sync()

    assert world.calendar.summaries() == ["One", "Two"]
    assert world.store.issues("cloud") == []
    assert world.calendar.calls.count("insert") == 3


def test_sync_error_is_cleared_when_the_file_is_put_back_as_it_was(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.source.files["/a.md"] = event("u1", "Meeting", location="Rejected place")
    world.calendar.fail_next = CalendarError("rejected")
    world.sync()
    assert len(world.store.issues("cloud")) == 1

    world.source.files["/a.md"] = event("u1", "Meeting")
    report = world.sync()

    assert world.store.issues("cloud") == []
    assert report.pushed == 0


def test_read_error_is_cleared_when_the_file_is_put_back_as_it_was(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.source.files["/a.md"] = event("u1", "Changed")
    world.source.fail_reading = {"/a.md"}
    world.sync()
    assert len(world.store.issues("cloud")) == 1

    world.source.files["/a.md"] = event("u1", "Meeting")
    world.source.fail_reading = set()
    world.sync()

    assert world.store.issues("cloud") == []


def test_sync_error_is_cleared_when_the_failing_event_is_removed(world):
    world.source.files["/a.md"] = event("u1", "Good") + event("u2", "Bad")
    world.calendar.fail_next = CalendarError("rejected")
    world.sync()
    world.sync()

    world.source.files["/a.md"] = event("u2", "Bad")
    world.calendar.fail_next = None
    world.sync()
    world.source.files["/a.md"] = ""
    world.sync()

    assert world.store.issues("cloud") == []
    assert world.calendar.summaries() == []


def test_event_that_failed_in_a_missing_calendar_is_created_once_the_calendar_is_fixed(world):
    world.source.files["/a.md"] = event("u1", "Meeting", calendar_id="no-such-calendar")
    world.calendar.fail_next = CalendarError("calendar not found")
    world.sync()
    assert world.calendar.summaries() == []

    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()

    assert world.calendar.summaries("primary") == ["Meeting"]
    assert world.store.issues("cloud") == []


def test_issues_of_a_vanished_file_are_removed(world):
    world.source.files["/unreadable.md"] = event("u1", "Never read")
    world.source.files["/broken.md"] = event("u1", "Broken", start="не дата")
    world.source.fail_reading = {"/unreadable.md"}
    world.sync()
    assert {i.kind for i in world.store.issues("cloud")} == {"source", "parse"}

    world.source.files.clear()
    world.sync()

    assert world.store.issues("cloud") == []


def test_issues_survive_restart(world, tmp_path):
    world.source.files["/a.md"] = event("u1", "Broken", start="не дата")
    world.sync()
    world.store.close()

    world.store = Store(tmp_path / "state.db")

    assert [i.kind for i in world.store.issues("cloud")] == ["parse"]


def test_repeated_failure_is_journalled_once(world):
    world.source.files["/a.md"] = event("u1", "One")

    for _ in range(3):
        world.calendar.fail_next = CalendarError("calendar not found")
        world.sync()

    assert [e.action for e in world.store.journal()] == ["error"]
    assert len(world.store.issues("cloud")) == 1


def test_unavailable_calendar_stops_the_cycle(world):
    world.source.files["/a.md"] = event("u1", "One")
    world.source.files["/b.md"] = event("u1", "Two")
    world.calendar.fail_next = CalendarUnavailable("not authorised")

    report = world.sync()

    assert "not authorised" in report.error
    assert world.calendar.calls == ["insert"]

    world.sync()

    assert world.calendar.summaries() == ["One", "Two"]


def test_lost_insert_response_does_not_create_a_duplicate(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.calendar.lose_next_insert_response = True

    world.sync()
    world.sync()

    assert world.calendar.summaries() == ["Meeting"]


def test_event_deleted_by_hand_in_calendar_is_recreated_on_next_change(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.calendar.delete_by_hand("Meeting")

    world.source.files["/a.md"] = event("u1", "Meeting again")
    world.sync()

    assert world.calendar.summaries() == ["Meeting again"]


def test_changing_calendar_moves_the_event(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()

    world.source.files["/a.md"] = event("u1", "Meeting", calendar_id="work")
    world.sync()

    assert world.calendar.summaries("primary") == []
    assert world.calendar.summaries("work") == ["Meeting"]


# --- large deletions wait for a human ---


def many_files(world, count: int) -> None:
    for i in range(count):
        world.source.files[f"/{i}.md"] = event("u1", f"Event {i}")


def test_mass_deletion_is_held_until_approved(world):
    many_files(world, 10)
    world.sync()

    world.source.files = {"/0.md": world.source.files["/0.md"]}
    report = world.sync()

    assert report.held_deletes == 9
    assert len(world.calendar.summaries()) == 10

    report = world.sync(allow_mass_delete=True)

    assert report.deleted == 9
    assert world.calendar.summaries() == ["Event 0"]


def test_held_deletion_does_not_block_other_changes(world):
    many_files(world, 10)
    world.sync()

    world.source.files = {"/0.md": event("u1", "Renamed")}
    world.source.files["/new.md"] = event("u1", "Brand new")
    world.sync()

    summaries = world.calendar.summaries()
    assert "Renamed" in summaries and "Brand new" in summaries
    assert len(summaries) == 11


def test_small_deletions_are_not_held(world):
    many_files(world, 10)
    world.sync()

    del world.source.files["/3.md"]
    report = world.sync()

    assert (report.deleted, report.held_deletes) == (1, 0)


# --- sources are independent ---


def test_switching_source_removes_the_events_of_the_previous_one(world):
    world.source.files["/a.md"] = event("u1", "From cloud")
    world.source.files["/unreadable.md"] = event("u1", "Never read")
    world.source.fail_reading = {"/unreadable.md"}
    world.sync()
    seafile = FakeSource()
    seafile.files["/a.md"] = event("u1", "From seafile")

    report = world.engine_for("seafile", seafile).run_cycle()

    assert world.calendar.summaries() == ["From seafile"]
    assert (report.pushed, report.deleted) == (1, 1)
    assert world.store.tracked("cloud") == {}
    assert world.store.file_versions("cloud") == {}
    assert world.store.issues() == []


def test_previous_source_is_kept_while_the_new_one_cannot_be_listed(world):
    world.source.files["/a.md"] = event("u1", "From cloud")
    world.sync()
    seafile = FakeSource()
    seafile.fail_listing = True

    report = world.engine_for("seafile", seafile).run_cycle()

    assert report.error is not None
    assert world.calendar.summaries() == ["From cloud"]


def test_removing_many_events_of_the_previous_source_waits_for_approval(world):
    many_files(world, 10)
    world.sync()
    seafile = FakeSource()
    seafile.files["/a.md"] = event("u1", "From seafile")
    engine = world.engine_for("seafile", seafile)

    report = engine.run_cycle()

    assert report.held_deletes == 10
    assert len(world.calendar.summaries()) == 11
    assert {(i.source, i.kind) for i in world.store.issues()} == {("cloud", "held")}

    report = engine.run_cycle(allow_mass_delete=True)

    assert report.deleted == 10
    assert world.calendar.summaries() == ["From seafile"]
    assert world.store.issues() == []


def test_failed_removal_of_a_previous_source_event_is_reported_and_retried(world):
    world.source.files["/a.md"] = event("u1", "From cloud")
    world.sync()
    seafile = FakeSource()
    engine = world.engine_for("seafile", seafile)
    world.calendar.fail_next = CalendarError("quota exceeded")

    engine.run_cycle()

    assert world.calendar.summaries() == ["From cloud"]
    assert [(i.source, i.kind) for i in world.store.issues()] == [("cloud", "sync")]

    engine.run_cycle()

    assert world.calendar.summaries() == []
    assert world.store.issues() == []


def test_state_survives_restart(world, tmp_path):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.calendar.calls.clear()
    world.store.close()

    world.store = Store(tmp_path / "state.db")
    world.engine = world.engine_for("cloud", world.source)
    world.source.files["/a.md"] = event("u1", "Renamed")
    world.sync()

    assert world.calendar.calls == ["update"]
    assert world.calendar.summaries() == ["Renamed"]


def test_one_file_is_synced_without_listing_the_source(world):
    world.source.files["/a.md"] = event("u1", "Meeting") + event("u2", "Gone")
    world.source.files["/b.md"] = event("u1", "Other")
    world.sync()
    world.source.files["/a.md"] = event("u1", "Renamed")
    world.source.files["/b.md"] = event("u1", "Not asked for")
    world.source.fail_listing = True
    world.source.reads.clear()

    report = world.engine.sync_file("/a.md")

    assert (report.read, report.pushed, report.deleted, report.error) == (1, 1, 1, None)
    assert world.calendar.summaries() == ["Other", "Renamed"]
    assert world.source.reads == ["/a.md"]


def test_file_synced_on_its_own_is_not_read_again_by_the_next_cycle(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    world.source.files["/a.md"] = event("u1", "Renamed")
    world.engine.sync_file("/a.md")
    world.source.reads.clear()

    world.sync()

    assert world.source.reads == []


def test_syncing_a_file_clears_its_fixed_issue(world):
    world.source.files["/a.md"] = event("u1", "Meeting", start="не дата")
    world.sync()
    world.source.files["/a.md"] = event("u1", "Meeting")

    report = world.engine.sync_file("/a.md")

    assert report.pushed == 1
    assert world.store.issues() == []


def test_syncing_a_missing_file_keeps_its_events(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.sync()
    del world.source.files["/a.md"]

    report = world.engine.sync_file("/a.md")

    assert "файл не прочитан" in report.error
    assert world.calendar.summaries() == ["Meeting"]
    assert [i.kind for i in world.store.issues()] == ["source"]


def test_unknown_file_is_not_synced_on_request(world):
    world.source.files["/a.md"] = event("u1", "Meeting")

    report = world.engine.sync_file("/a.md")

    assert report.error == "файл не отслеживается"
    assert world.calendar.summaries() == []


def test_mass_deletion_in_one_file_is_held_too(world):
    world.source.files["/a.md"] = "".join(event(f"u{i}", f"Event {i}") for i in range(10))
    world.sync()
    world.source.files["/a.md"] = "nothing here"

    report = world.engine.sync_file("/a.md")

    assert (report.held_deletes, report.deleted) == (10, 0)
    assert len(world.calendar.summaries()) == 10
