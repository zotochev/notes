import time

import pytest

from notificator.config import STATE_FILE, Config
from notificator.service import SyncService
from notificator.store import Store
from notificator.sync.ports import CalendarUnavailable
from tests.sync.fakes import FakeCalendar, FakeSource

CONFIG = Config.model_validate({
    "active_source": "cloud",
    "sources": {"cloud": {"type": "local", "roots": ["."]}},
    "scan_interval_sec": 3600,
})


def event(uid: str, summary: str) -> str:
    return f"<event><uid>{uid}</uid><summary>{summary}</summary><start>2030-01-01 10:00</start></event>"


class World:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.source = FakeSource()
        self.calendar = FakeCalendar()
        self.calendar_error: Exception | None = None
        self.service = SyncService(CONFIG, tmp_path, source=self.source, calendar_factory=self._calendar)

    def _calendar(self) -> FakeCalendar:
        if self.calendar_error:
            raise self.calendar_error
        return self.calendar

    def cycles(self) -> list[dict]:
        with Store(self.tmp_path / STATE_FILE) as store:
            return store.cycles()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_cycle_is_recorded_with_its_report(world):
    world.source.files["/a.md"] = event("u1", "Meeting")

    report = world.service.run_cycle()

    assert report.pushed == 1
    (cycle,) = world.cycles()
    assert (cycle["source"], cycle["pushed"], cycle["error"]) == ("cloud", 1, None)


def test_unauthorised_calendar_is_recorded_as_the_cycle_error(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.calendar_error = CalendarUnavailable("Google не авторизован")

    world.service.run_cycle()

    assert "Google не авторизован" in world.cycles()[0]["error"]
    assert world.source.reads == []


def test_unexpected_crash_is_recorded_instead_of_killing_the_service(world):
    world.source.list_files = lambda: 1 / 0

    report = world.service.run_cycle()

    assert "внутренняя ошибка: ZeroDivisionError" in report.error
    assert world.cycles()[0]["error"] == report.error
    assert not world.service.running


def test_approval_lets_exactly_one_cycle_delete(world):
    for i in range(10):
        world.source.files[f"/{i}.md"] = event("u1", f"Event {i}")
    world.service.run_cycle()
    world.source.files.clear()

    assert world.service.run_cycle().held_deletes == 10

    world.service.approve_deletions()
    assert world.service.run_cycle().deleted == 10

    for i in range(10):
        world.source.files[f"/{i}.md"] = event("u1", f"Event {i}")
    world.service.run_cycle()
    world.source.files.clear()
    assert world.service.run_cycle().held_deletes == 10


def test_approval_is_kept_while_cycles_fail(world):
    for i in range(10):
        world.source.files[f"/{i}.md"] = event("u1", f"Event {i}")
    world.service.run_cycle()
    world.source.files.clear()
    world.service.approve_deletions()

    world.source.fail_listing = True
    world.service.run_cycle()
    world.source.fail_listing = False

    assert world.service.run_cycle().deleted == 10


def test_background_loop_runs_and_reacts_to_trigger(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.start()
    try:
        deadline = time.time() + 10
        while len(world.cycles()) < 1 and time.time() < deadline:
            time.sleep(0.05)
        assert world.calendar.summaries() == ["Meeting"]
        assert world.service.seconds_until_next_cycle > 3000

        world.source.files["/a.md"] = event("u1", "Renamed")
        world.service.trigger()
        while len(world.cycles()) < 2 and time.time() < deadline:
            time.sleep(0.05)
        assert world.calendar.summaries() == ["Renamed"]
    finally:
        world.service.stop()


def test_one_file_is_synced_without_recording_a_cycle(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()
    world.source.files["/a.md"] = event("u1", "Renamed")

    report = world.service.sync_file("/a.md")

    assert report.pushed == 1
    assert world.calendar.summaries() == ["Renamed"]
    assert len(world.cycles()) == 1
    assert (world.service.file_syncs, world.service.running) == (1, False)


def test_file_is_not_synced_while_a_cycle_runs(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()
    world.source.files["/a.md"] = event("u1", "Renamed")
    during_cycle = []
    original = world.source.read_text

    def read_and_try(file):
        during_cycle.append(world.service.sync_file("/a.md"))
        return original(file)

    world.source.read_text = read_and_try
    world.service.run_cycle()

    assert during_cycle == [None]
