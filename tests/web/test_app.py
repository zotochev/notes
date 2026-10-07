import pytest
from fastapi.testclient import TestClient

from notificator.config import Config
from notificator.service import SyncService
from notificator.sync.ports import CalendarUnavailable
from notificator.web.app import create_app
from tests.sync.fakes import FakeCalendar, FakeSource

CONFIG = Config.model_validate({
    "active_source": "cloud",
    "sources": {"cloud": {"type": "webdav", "url": "https://c.example", "username": "u", "password": "hunter2"}},
})


def event(uid: str, summary: str, start: str = "2030-01-01 10:00") -> str:
    return f"<event><uid>{uid}</uid><summary>{summary}</summary><start>{start}</start></event>"


class World:
    def __init__(self, tmp_path) -> None:
        self.source = FakeSource()
        self.calendar = FakeCalendar()
        self.calendar_error: Exception | None = None
        self.service = SyncService(CONFIG, tmp_path, source=self.source, calendar_factory=self._calendar)
        self.client = TestClient(create_app(
            self.service, CONFIG, tmp_path, start_service=False, calendar_factory=self._calendar,
        ))

    def _calendar(self) -> FakeCalendar:
        if self.calendar_error:
            raise self.calendar_error
        return self.calendar

    def status(self) -> dict:
        return self.client.get("/api/status").json()


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_page_is_served(world):
    response = world.client.get("/")

    assert response.status_code == 200
    assert "Notificator" in response.text


def test_status_before_the_first_cycle(world):
    status = world.status()

    assert status["state"] == "starting"
    assert status["lastCycle"] is None


def test_status_and_events_after_a_clean_cycle(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()

    status = world.status()
    (shown,) = world.client.get("/api/events").json()

    assert status["state"] == "ok"
    assert status["counts"] == {"events": 1, "files": 1, "unsynced": 0, "issues": 0, "heldDeletes": 0}
    assert (shown["summary"], shown["path"], shown["uid"], shown["synced"]) == ("Meeting", "/a.md", "u1", True)
    assert shown["calendar_id"] == "primary"
    assert shown["start"] == "2030-01-01T10:00:00+04:00"


def test_cycle_failure_is_the_headline_state(world):
    world.calendar_error = CalendarUnavailable("Google не авторизован")
    world.service.run_cycle()

    status = world.status()

    assert status["state"] == "error"
    assert "Google не авторизован" in status["lastCycle"]["error"]


def test_broken_event_puts_the_page_into_attention_and_fixing_it_clears_it(world):
    world.source.files["/a.md"] = event("u1", "Meeting", start="не дата")
    world.service.run_cycle()

    (issue,) = world.client.get("/api/issues").json()
    assert world.status()["state"] == "attention"
    assert (issue["kind"], issue["path"], issue["uid"]) == ("parse", "/a.md", "u1")

    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()

    assert world.status()["state"] == "ok"
    assert world.client.get("/api/issues").json() == []


def test_held_deletions_are_listed_and_can_be_approved(world):
    for i in range(10):
        world.source.files[f"/{i}.md"] = event("u1", f"Event {i}")
    world.service.run_cycle()
    world.source.files.clear()
    world.service.run_cycle()

    assert world.status()["counts"]["heldDeletes"] == 10
    assert {i["kind"] for i in world.client.get("/api/issues").json()} == {"held"}
    assert len(world.calendar.summaries()) == 10

    assert world.client.post("/api/deletions/approve").json() == {"ok": True}
    world.service.run_cycle()

    assert world.status()["state"] == "ok"
    assert world.calendar.summaries() == []


def test_event_can_be_looked_up_in_google(world):
    world.source.files["/notes/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()

    response = world.client.get("/api/events/google", params={"path": "/notes/a.md", "uid": "u1"})

    remote = response.json()
    assert response.status_code == 200
    assert (remote["status"], remote["summary"], remote["calendarId"], remote["synced"]) == (
        "confirmed", "Meeting", "primary", True,
    )
    assert remote["description"].startswith("uid: u1\nfile: /notes/a.md")


def test_event_deleted_by_hand_is_shown_as_cancelled(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()
    world.calendar.delete_by_hand("Meeting")

    remote = world.client.get("/api/events/google", params={"path": "/a.md", "uid": "u1"}).json()

    assert remote["status"] == "cancelled"


def test_looking_up_an_untracked_or_unreachable_event_explains_why(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()

    unknown = world.client.get("/api/events/google", params={"path": "/a.md", "uid": "nope"})
    world.calendar.events.clear()
    missing = world.client.get("/api/events/google", params={"path": "/a.md", "uid": "u1"})
    world.calendar_error = CalendarUnavailable("Google не авторизован")
    unavailable = world.client.get("/api/events/google", params={"path": "/a.md", "uid": "u1"})

    assert (unknown.status_code, unknown.json()["detail"]) == (404, "Это событие не отслеживается")
    assert (missing.status_code, missing.json()["detail"]) == (404, "События нет в Google Calendar")
    assert (unavailable.status_code, unavailable.json()["detail"]) == (503, "Google не авторизован")


def test_journal_and_cycle_history(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()

    (entry,) = world.client.get("/api/journal").json()
    (cycle,) = world.client.get("/api/cycles").json()

    assert (entry["action"], entry["path"], entry["detail"]) == ("created", "/a.md", "Meeting")
    assert cycle["pushed"] == 1


def test_config_is_shown_without_passwords(world):
    response = world.client.get("/api/config")

    assert "hunter2" not in response.text
    assert response.json()["sources"]["cloud"]["password"] == "***"


def test_validate_reports_events_and_issues(world):
    text = event("u1", "Good") + event("u2", "Bad", start="не дата")

    result = world.client.post("/api/validate", json={"text": text}).json()

    assert [e["summary"] for e in result["events"]] == ["Good"]
    assert [i["uid"] for i in result["issues"]] == ["u2"]


def test_google_login_without_redirect_uri_explains_what_is_missing(world):
    response = world.client.get("/google/login", follow_redirects=False)

    assert response.status_code == 400
    assert "redirect_uri" in response.json()["detail"]


def test_unknown_oauth_state_is_rejected(world):
    response = world.client.get("/google/oauth/callback?state=forged&code=x", follow_redirects=False)

    assert response.status_code == 400
