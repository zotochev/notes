import pytest

from notificator.core.model import EventKey
from notificator.sync.engine import SyncEngine
from notificator.sync.ports import CalendarError
from tests.sync.test_engine import SETTINGS, World, event


def task(uid: str, summary: str, due: str | None = "2030-01-15", **extra: str) -> str:
    fields = {"uid": uid, "summary": summary, **({"due": due} if due else {}), **extra}
    return "<task>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</task>"


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.store.close()


def test_task_is_created_in_the_default_list(world):
    world.source.files["/a.md"] = task("t1", "Call the bank", description="ask about the card")
    world.source.links["/a.md"] = "https://cloud.example/a"

    report = world.sync()

    ((tasklist, _), body), = world.tasks.tasks.items()
    assert (report.pushed, tasklist) == (1, "@default")
    assert (body["title"], body["due"]) == ("Call the bank", "2030-01-15T00:00:00.000Z")
    assert body["notes"] == "uid: t1\nfile: /a.md\nlink: https://cloud.example/a\n---\nask about the card"
    assert world.calendar.events == {}
    (shown,) = world.store.events("cloud")
    assert (shown["kind"], shown["summary"], shown["synced"]) == ("task", "Call the bank", True)


def test_unchanged_task_is_not_pushed_again(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.sync()
    world.tasks.calls.clear()
    world.source.files["/b.md"] = "unrelated"

    world.sync()

    assert world.tasks.calls == []


def test_changed_task_is_updated_in_place_and_stays_completed(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.sync()
    (key,) = world.tasks.tasks
    world.tasks.complete("Call the bank")
    world.source.files["/a.md"] = task("t1", "Call the bank today", due="2030-02-01")

    world.sync()

    assert list(world.tasks.tasks) == [key]
    body = world.tasks.tasks[key]
    assert (body["title"], body["due"], body["status"]) == ("Call the bank today", "2030-02-01T00:00:00.000Z", "completed")


def test_task_may_have_no_due_date_and_losing_it_clears_it_in_google(world):
    world.source.files["/a.md"] = task("t1", "Some day", due="2030-01-15")
    world.sync()
    world.source.files["/a.md"] = task("t1", "Some day", due=None)

    world.sync()

    (body,) = world.tasks.tasks.values()
    assert body["due"] is None


def test_task_removed_from_the_file_is_deleted(world):
    world.source.files["/a.md"] = task("t1", "Stays") + task("t2", "Goes")
    world.sync()
    world.source.files["/a.md"] = task("t1", "Stays")

    report = world.sync()

    assert (world.tasks.titles(), report.deleted) == (["Stays"], 1)


def test_event_and_task_live_side_by_side_in_one_file(world):
    world.source.files["/a.md"] = event("u1", "Meeting") + "\n" + task("t1", "Prepare the slides")

    world.sync()

    assert (world.calendar.summaries(), world.tasks.titles()) == (["Meeting"], ["Prepare the slides"])
    assert {uid: t.kind for uid, t in world.store.tracked("cloud")["/a.md"].items()} == {"u1": "event", "t1": "task"}


def test_changing_the_task_list_moves_the_task(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.sync()
    world.source.files["/a.md"] = task("t1", "Call the bank", tasklist="work")

    world.sync()

    assert (world.tasks.titles("@default"), world.tasks.titles("work")) == ([], ["Call the bank"])


def test_event_turned_into_a_task_and_back(world):
    world.source.files["/a.md"] = event("x1", "Thing")
    world.sync()

    world.source.files["/a.md"] = task("x1", "Thing")
    world.sync()
    assert (world.calendar.summaries(), world.tasks.titles()) == ([], ["Thing"])

    world.source.files["/a.md"] = event("x1", "Thing")
    world.sync()
    assert (world.calendar.summaries(), world.tasks.titles()) == (["Thing"], [])


def test_lost_insert_response_does_not_create_a_second_task(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.tasks.lose_next_insert_response = True

    first = world.sync()
    second = world.sync()

    assert (first.failed, second.failed) == (1, 0)
    assert world.tasks.titles() == ["Call the bank"]
    assert world.tasks.calls.count("insert") == 1
    assert world.store.events("cloud")[0]["synced"]


def test_unconfirmed_task_removed_from_the_file_is_found_and_deleted(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.tasks.lose_next_insert_response = True
    world.sync()
    world.source.files["/a.md"] = "nothing here"

    world.sync()

    assert world.tasks.titles() == []
    assert world.store.events("cloud") == []


def test_task_deleted_by_hand_in_google_is_made_again_on_the_next_change(world):
    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.sync()
    world.tasks.delete_by_hand("Call the bank")
    world.source.files["/a.md"] = task("t1", "Call the bank again")

    world.sync()

    assert world.tasks.titles() == ["Call the bank again"]


def test_failed_task_is_reported_and_retried_without_touching_events(world):
    world.source.files["/a.md"] = event("u1", "Meeting") + task("t1", "Call the bank")
    world.tasks.fail_next = CalendarError("нет доступа к Google Tasks")

    report = world.sync()

    assert (world.calendar.summaries(), report.failed) == (["Meeting"], 1)
    (issue,) = world.store.issues()
    assert (issue.kind, issue.uid) == ("sync", "t1")

    assert world.sync().failed == 0
    assert (world.tasks.titles(), world.store.issues()) == (["Call the bank"], [])


def test_without_a_tasks_service_tasks_are_errors_and_events_still_work(world):
    engine = SyncEngine("cloud", world.source, world.calendar, world.store, SETTINGS)
    world.source.files["/a.md"] = event("u1", "Meeting") + task("t1", "Call the bank")

    report = engine.run_cycle()

    assert (world.calendar.summaries(), report.failed) == (["Meeting"], 1)
    assert "задачи Google не подключены" in world.store.issues()[0].message


def test_tasks_in_a_table_are_the_rows_with_due_instead_of_start(world):
    world.source.files["/plan.csv"] = (
        "uid;summary;start;due\n"
        "u1;Meeting;2030-01-01 10:00;\n"
        "t1;Call the bank;;2030-01-15\n"
    )

    world.sync()

    assert (world.calendar.summaries(), world.tasks.titles()) == (["Meeting"], ["Call the bank"])


def test_moved_task_keeps_its_google_task(world):
    from notificator.migrate import move_source

    world.source.files["/a.md"] = task("t1", "Call the bank")
    world.sync()
    (key,) = world.tasks.tasks
    world.source.files = {"/lib/a.md": task("t1", "Call the bank")}

    move_source(world.store, "cloud", "new", [("/", "/lib")], apply=True)
    world.engine_for("new", world.source).run_cycle()

    assert list(world.tasks.tasks) == [key]
    assert world.tasks.tasks[key]["notes"].startswith("uid: t1\nfile: /lib/a.md\n")
    assert world.store.tracked("new")["/lib/a.md"]["t1"].key == EventKey("new", "/lib/a.md", "t1")


def test_task_sent_to_a_list_that_does_not_exist_is_reported_and_works_once_the_list_is_fixed(world):
    world.source.files["/a.md"] = task("t1", "Call the bank", tasklist="typo")

    first, second = world.sync(), world.sync()

    assert (first.failed, second.failed) == (1, 1)
    assert "список задач 'typo' не найден" in world.store.issues()[0].message

    world.source.files["/a.md"] = task("t1", "Call the bank")
    report = world.sync()

    assert (report.failed, world.tasks.titles("@default"), world.store.issues()) == (0, ["Call the bank"], [])


def test_task_that_never_reached_a_missing_list_can_be_removed_from_the_file(world):
    world.source.files["/a.md"] = task("t1", "Call the bank", tasklist="typo")
    world.sync()
    world.source.files["/a.md"] = "# the task is commented out"

    report = world.sync()

    assert (report.failed, world.store.events("cloud"), world.store.issues()) == (0, [], [])
