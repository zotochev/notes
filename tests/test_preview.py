import pytest

from notificator.config import STATE_FILE, Config
from notificator.preview import preview_cycle
from notificator.store import Store
from notificator.sync.engine import SyncEngine
from notificator.wiring import sync_settings
from tests.sync.fakes import FakeCalendar, FakeSource

CONFIG = Config.model_validate({"active_source": "cloud", "sources": {"cloud": {"type": "local", "roots": ["."]}}})


def event(uid: str, summary: str) -> str:
    return f"<event><uid>{uid}</uid><summary>{summary}</summary><start>2030-01-01 10:00</start></event>"


@pytest.fixture
def source(monkeypatch):
    fake = FakeSource()
    monkeypatch.setattr("notificator.preview.build_source", lambda cfg: fake)
    return fake


def sync_for_real(tmp_path, source: FakeSource) -> FakeCalendar:
    calendar = FakeCalendar()
    with Store(tmp_path / STATE_FILE) as store:
        SyncEngine("cloud", source, calendar, store, sync_settings(CONFIG)).run_cycle()
    return calendar


def test_preview_lists_calls_and_changes_nothing(tmp_path, source):
    source.files["/a.md"] = event("u1", "Old")
    sync_for_real(tmp_path, source)
    source.files["/a.md"] = event("u1", "New")
    source.files["/b.md"] = event("u1", "Added")

    result = preview_cycle(CONFIG, tmp_path)

    assert sorted((c.action, c.body["summary"]) for c in result.calls) == [("insert", "Added"), ("update", "New")]
    assert not result.deletes_need_approval
    with Store(tmp_path / STATE_FILE) as store:
        assert [e["summary"] for e in store.events("cloud")] == ["Old"]


def test_preview_of_a_mass_deletion_lists_it_and_reads_the_source_once(tmp_path, source):
    for i in range(10):
        source.files[f"/{i}.md"] = event("u1", f"Event {i}")
    sync_for_real(tmp_path, source)
    for i in range(1, 10):
        del source.files[f"/{i}.md"]
    source.files["/0.md"] = event("u1", "Changed")
    source.reads.clear()

    result = preview_cycle(CONFIG, tmp_path)

    assert result.deletes_need_approval
    assert [c.action for c in result.calls].count("delete") == 9
    assert source.reads == ["/0.md"]


def test_preview_without_any_state(tmp_path, source):
    source.files["/a.md"] = event("u1", "First")

    result = preview_cycle(CONFIG, tmp_path)

    assert [c.action for c in result.calls] == ["insert"]
    assert not (tmp_path / STATE_FILE).exists()
