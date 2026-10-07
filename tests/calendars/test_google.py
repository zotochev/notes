import json

import httplib2
import pytest

from notificator.calendars.google import GoogleCalendar
from notificator.sync.ports import (
    CalendarError, CalendarUnavailable, EventAlreadyExists, EventNotFound,
)

BODY = {"summary": "Meeting"}


class FakeHttp:
    """Transport that replays prepared responses and records requests."""

    def __init__(self, *responses) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, str, dict | None]] = []

    def request(self, uri, method="GET", body=None, headers=None, **kwargs):
        self.requests.append((method, uri, json.loads(body) if body else None))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        status, payload = response
        return httplib2.Response({"status": str(status)}), json.dumps(payload).encode()


def error(status: int, reason: str = "invalid", message: str = "Bad thing"):
    return status, {"error": {"code": status, "message": message, "errors": [{"reason": reason}]}}


def calendar(*responses) -> tuple[GoogleCalendar, FakeHttp]:
    http = FakeHttp(*responses)
    return GoogleCalendar(http=http, retries=0), http


def test_insert_sends_our_event_id():
    cal, http = calendar((200, {"id": "abc123"}))

    cal.insert("me@example.com", "abc123", BODY)

    ((method, uri, body),) = http.requests
    assert method == "POST"
    assert uri.endswith("/calendars/me%40example.com/events?alt=json")
    assert body == {"summary": "Meeting", "id": "abc123"}


def test_insert_of_existing_id():
    cal, _ = calendar(error(409, "duplicate"))

    with pytest.raises(EventAlreadyExists):
        cal.insert("primary", "abc123", BODY)


def test_insert_into_missing_calendar_names_the_calendar():
    cal, _ = calendar(error(404, "notFound", "Not Found"))

    with pytest.raises(CalendarError, match="календарь work@example.com не найден") as info:
        cal.insert("work@example.com", "abc123", BODY)

    assert not isinstance(info.value, EventNotFound)


def test_update_patches_the_event():
    cal, http = calendar((200, {"id": "abc123", "status": "confirmed"}))

    cal.update("primary", "abc123", BODY)

    ((method, uri, body),) = http.requests
    assert (method, body) == ("PATCH", BODY)
    assert "/events/abc123" in uri


@pytest.mark.parametrize("response", [error(404, "notFound"), (200, {"id": "abc123", "status": "cancelled"})])
def test_update_of_missing_or_cancelled_event(response):
    cal, _ = calendar(response)

    with pytest.raises(EventNotFound):
        cal.update("primary", "abc123", BODY)


@pytest.mark.parametrize("response", [(204, {}), error(404, "notFound"), error(410, "deleted")])
def test_delete_is_idempotent(response):
    cal, http = calendar(response)

    cal.delete("primary", "abc123")

    assert http.requests[0][0] == "DELETE"


@pytest.mark.parametrize(
    "response",
    [error(401, "authError"), error(403, "rateLimitExceeded"), error(429, "rateLimitExceeded"), OSError("no route")],
)
def test_problems_that_affect_every_event_stop_the_cycle(response):
    cal, _ = calendar(response)

    with pytest.raises(CalendarUnavailable):
        cal.insert("primary", "abc123", BODY)


def test_rejected_event_is_an_ordinary_error_with_googles_message():
    cal, _ = calendar(error(400, "invalid", "Invalid attendee email"))

    with pytest.raises(CalendarError, match="Invalid attendee email") as info:
        cal.insert("primary", "abc123", BODY)

    assert not isinstance(info.value, CalendarUnavailable)


def test_list_events_follows_pages():
    cal, http = calendar(
        (200, {"items": [{"id": "a"}], "nextPageToken": "p2"}),
        (200, {"items": [{"id": "b"}]}),
    )

    assert cal.list_events("primary") == [{"id": "a"}, {"id": "b"}]
    assert "showDeleted=false" in http.requests[0][1]
    assert "pageToken=p2" in http.requests[1][1]


def test_writable_calendars_follow_pages_and_skip_read_only():
    cal, http = calendar(
        (200, {"items": [{"id": "a", "summary": "Mine", "accessRole": "owner", "primary": True}], "nextPageToken": "p2"}),
        (200, {"items": [{"id": "b", "accessRole": "reader"}, {"id": "c", "accessRole": "writer"}]}),
    )

    assert cal.writable_calendars() == [
        {"id": "a", "summary": "Mine", "primary": True},
        {"id": "c", "summary": "c", "primary": False},
    ]
    assert "pageToken=p2" in http.requests[1][1]
