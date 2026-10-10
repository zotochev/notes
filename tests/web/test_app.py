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


def test_status_shows_what_a_running_cycle_is_doing(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    seen: list[str] = []
    original = world.source.read_text

    def read_and_look(file):
        seen.append(world.status()["progress"])
        return original(file)

    world.source.read_text = read_and_look
    world.service.run_cycle()

    assert seen == ["файлов в облаке 1, подходящих 1, нужно прочитать 1"]
    assert world.status()["progress"] == ""


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


def test_events_of_the_previous_source_are_shown_until_their_removal_is_approved(world, tmp_path):
    old_source = FakeSource()
    for i in range(10):
        old_source.files[f"/{i}.md"] = event("u1", f"Old {i}")
    previous = Config.model_validate({**CONFIG.model_dump(mode="json"), "active_source": "old",
                                      "sources": {"old": {"type": "local", "roots": ["."]}}})
    SyncService(previous, tmp_path, source=old_source, calendar_factory=lambda: world.calendar).run_cycle()
    world.source.files["/a.md"] = event("u1", "Active")

    world.service.run_cycle()

    status = world.status()
    assert status["inactiveSources"] == {"old": 10}
    assert (status["state"], status["counts"]["events"], status["counts"]["heldDeletes"]) == ("attention", 1, 10)
    assert {i["source"] for i in world.client.get("/api/issues").json()} == {"old"}

    world.client.post("/api/deletions/approve")
    world.service.run_cycle()

    assert world.status()["inactiveSources"] == {}
    assert world.status()["state"] == "ok"
    assert world.calendar.summaries() == ["Active"]


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


def test_one_file_can_be_synced_on_request(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()
    world.source.files["/a.md"] = event("u1", "Renamed")

    response = world.client.post("/api/files/sync", json={"path": "/a.md"})

    assert (response.status_code, response.json()["pushed"], response.json()["error"]) == (200, 1, None)
    assert world.client.get("/api/events").json()[0]["summary"] == "Renamed"
    assert world.status()["fileSyncs"] == 1


def test_syncing_an_untracked_file_explains_why_not(world):
    response = world.client.post("/api/files/sync", json={"path": "/nope.md"})

    assert response.json()["error"] == "файл не отслеживается"


def test_listing_progress_is_empty_for_a_source_that_lists_in_one_go(world):
    assert world.client.get("/api/listing").json() is None


def test_listing_progress_shows_the_batches_of_a_source_that_walks_its_tree(world):
    from notificator.sources.batching import Folder, Level, TreeWalker

    levels = {"/": Level([Folder("/notes", "m1")], []), "/notes": Level([], [])}
    world.source.walker = TreeWalker(levels.__getitem__)
    world.source.walker.run(["/"])

    listing = world.client.get("/api/listing").json()

    assert (listing["active"], listing["read"], listing["concurrency"]) == (False, 2, 1)
    assert [g["folder"] for g in listing["groups"]] == ["/", "/notes"]


def test_task_can_be_looked_up_in_google_and_task_lists_are_listed(tmp_path):
    from tests.sync.fakes import FakeTasks

    source, calendar, tasks = FakeSource(), FakeCalendar(), FakeTasks()
    service = SyncService(CONFIG, tmp_path, source=source, calendar_factory=lambda: calendar, tasks_factory=lambda: tasks)
    client = TestClient(create_app(
        service, CONFIG, tmp_path, start_service=False, calendar_factory=lambda: calendar, tasks_factory=lambda: tasks,
    ))
    source.files["/a.md"] = "<task><uid>t1</uid><summary>Call the bank</summary><due>2030-01-15</due></task>"
    service.run_cycle()

    (shown,) = client.get("/api/events").json()
    remote = client.get("/api/events/google", params={"path": "/a.md", "uid": "t1"}).json()
    lists = client.get("/api/tasklists").json()
    status = client.get("/api/status").json()

    assert (shown["kind"], shown["calendar_id"], shown["end"]) == ("task", "@default", None)
    assert (remote["kind"], remote["title"], remote["status"], remote["calendarId"]) == (
        "task", "Call the bank", "needsAction", "@default",
    )
    assert lists == [{"id": "@default", "title": "My Tasks"}, {"id": "work", "title": "Work"}]
    assert (status["tasksAllowed"], status["defaultTasklist"]) == (False, "@default")


def test_validate_reports_tasks(world):
    text = "<task><uid>t1</uid><summary>Some day</summary></task>"

    (shown,) = world.client.post("/api/validate", json={"text": text}).json()["events"]

    assert (shown["kind"], shown["start"], shown["end"]) == ("task", None, None)


def test_refused_or_incomplete_google_answer_is_explained(world):
    refused = world.client.get("/google/oauth/callback?error=access_denied&state=x", follow_redirects=False)
    bare = world.client.get("/google/oauth/callback", follow_redirects=False)

    assert (refused.status_code, bare.status_code) == (400, 400)
    assert "Google не выдал доступ: access_denied" in refused.json()["detail"]
    assert "нет кода входа" in bare.json()["detail"]


def test_status_names_the_code_version_the_process_runs(world):
    version = world.status()["version"]

    # A short commit hash and its date; None only when the code is not in a git checkout.
    assert version is None or len(version.split()) == 2


def test_status_shows_the_stage_of_a_running_cycle_and_nothing_when_idle(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.source.files["/b.md"] = event("u1", "Other")
    seen: list[dict] = []
    read, insert = world.source.read_bytes, world.calendar.insert

    def read_and_look(file):
        seen.append(world.status()["stage"])
        return read(file)

    def insert_and_look(*args):
        seen.append(world.status()["stage"])
        return insert(*args)

    world.source.read_bytes, world.calendar.insert = read_and_look, insert_and_look
    world.service.run_cycle()

    reading, writing = seen[0], seen[-1]
    assert (reading["phase"], reading["total"], reading["file"]) == ("reading", 2, None)
    assert (writing["phase"], writing["done"], writing["total"]) == ("writing", 1, 2)
    assert writing["seconds"] >= 0
    assert world.status()["stage"] is None


def test_stage_of_a_single_file_sync_names_the_file(world):
    world.source.files["/a.md"] = event("u1", "Meeting")
    world.service.run_cycle()
    world.source.files["/a.md"] = event("u1", "Renamed")
    seen: list[dict] = []
    update = world.calendar.update
    world.calendar.update = lambda *args: (seen.append(world.status()["stage"]), update(*args))[1]

    world.client.post("/api/files/sync", json={"path": "/a.md"})

    assert (seen[0]["phase"], seen[0]["file"], seen[0]["total"]) == ("writing", "/a.md", 1)
