import pytest

from notificator.calendars.google_tasks import GoogleTasks
from notificator.sync.ports import CalendarError, CalendarUnavailable, EventNotFound, TaskListNotFound
from tests.calendars.test_google import FakeHttp, error

BODY = {"title": "Call the bank", "notes": "uid: t1\nfile: /a.md\n---\n", "due": None}


def tasks(*responses) -> tuple[GoogleTasks, FakeHttp]:
    http = FakeHttp(*responses)
    return GoogleTasks(http=http, retries=0), http


def test_insert_returns_the_id_google_chose():
    service, http = tasks((200, {"id": "abc"}))

    assert service.insert("@default", BODY) == "abc"
    ((method, uri, body),) = http.requests
    assert (method, body) == ("POST", BODY)
    assert "/lists/%40default/tasks" in uri or "/lists/@default/tasks" in uri


def test_insert_into_a_missing_list_names_the_list():
    service, _ = tasks(error(404))

    with pytest.raises(TaskListNotFound, match="список задач 'work' не найден.*идентификатор"):
        service.insert("work", BODY)


def test_looking_for_a_task_in_a_missing_list_names_the_list_too():
    service, _ = tasks(error(404, message="Task list not found"))

    with pytest.raises(TaskListNotFound, match="список задач 'no-such-list' не найден"):
        service.find("no-such-list", "uid: t1\nfile: /a.md\n")


def test_update_patches_only_what_we_set():
    service, http = tasks((200, {"id": "abc", "status": "completed"}))

    service.update("@default", "abc", BODY)

    ((method, _, body),) = http.requests
    assert (method, body) == ("PATCH", BODY)


@pytest.mark.parametrize("response", [(200, {"id": "abc", "deleted": True}), error(404)])
def test_update_of_a_task_deleted_in_google_is_not_found(response):
    service, _ = tasks(response)

    with pytest.raises(EventNotFound):
        service.update("@default", "abc", BODY)


def test_delete_of_a_missing_task_is_not_an_error():
    service, _ = tasks(error(404))

    service.delete("@default", "abc")


def test_find_looks_through_every_page_and_skips_deleted_tasks():
    service, http = tasks(
        (200, {"items": [{"id": "x", "notes": "uid: t1\nfile: /a.md\n---\n", "deleted": True},
                         {"id": "y", "notes": "uid: t9\nfile: /a.md\n---\n"}], "nextPageToken": "p2"}),
        (200, {"items": [{"id": "z"}, {"id": "ours", "notes": "uid: t1\nfile: /a.md\nlink: l\n---\ntext"}]}),
    )

    assert service.find("@default", "uid: t1\nfile: /a.md\n") == "ours"
    assert "showHidden=true" in http.requests[0][1] and "pageToken=p2" in http.requests[1][1]


def test_find_returns_nothing_when_there_is_no_such_task():
    service, _ = tasks((200, {}))

    assert service.find("@default", "uid: t1\nfile: /a.md\n") is None


def test_task_lists_are_listed_with_their_titles():
    service, _ = tasks((200, {"items": [{"id": "a1", "title": "My Tasks"}, {"id": "b2"}]}))

    assert service.tasklists() == [{"id": "a1", "title": "My Tasks"}, {"id": "b2", "title": "b2"}]


def test_missing_permission_is_explained_and_does_not_stop_the_whole_cycle():
    service, _ = tasks(error(403, "insufficientPermissions"))

    with pytest.raises(CalendarError, match="нет доступа к Google Tasks") as raised:
        service.insert("@default", BODY)
    assert not isinstance(raised.value, CalendarUnavailable)


def test_rate_limit_stops_the_cycle_like_for_the_calendar():
    service, _ = tasks(error(403, "rateLimitExceeded"))

    with pytest.raises(CalendarUnavailable):
        service.insert("@default", BODY)
