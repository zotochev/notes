"""
Google Calendar backend.

One instance holds one API client, which is not thread-safe: create an
instance per sync cycle or per request, do not share it between threads.
"""
from __future__ import annotations

from typing import Any

import httplib2
from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import HttpRequest

from notificator.sync.ports import (
    CalendarError, CalendarUnavailable, EventAlreadyExists, EventNotFound,
)

_RATE_LIMIT_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded"}


class GoogleCalendar:
    def __init__(self, credentials: Any = None, http: Any = None, retries: int = 3) -> None:
        """Pass `credentials` for real use; `http` replaces the transport in tests."""
        self._service = build("calendar", "v3", credentials=credentials, http=http, cache_discovery=False)
        self._retries = retries

    def insert(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        try:
            self._execute(self._service.events().insert(calendarId=calendar_id, body={**body, "id": event_id}))
        except EventNotFound as e:
            # A new event cannot be "not found": it is the calendar that is missing.
            raise CalendarError(f"календарь {calendar_id} не найден или недоступен этому аккаунту") from e

    def update(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        event = self._execute(
            self._service.events().patch(calendarId=calendar_id, eventId=event_id, body=body)
        )
        if event.get("status") == "cancelled":
            # Deleted in the calendar UI: Google keeps the event as a cancelled stub.
            raise EventNotFound(f"событие {event_id} удалено в календаре")

    def delete(self, calendar_id: str, event_id: str) -> None:
        try:
            self._execute(self._service.events().delete(calendarId=calendar_id, eventId=event_id))
        except EventNotFound:
            pass

    def get(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        """Return the raw event resource. Raises EventNotFound."""
        return self._execute(self._service.events().get(calendarId=calendar_id, eventId=event_id))

    def writable_calendars(self) -> list[dict[str, Any]]:
        """Calendars the user can add events to, as {id, summary, primary}."""
        calendars: list[dict[str, Any]] = []
        page_token = None
        while True:
            page = self._execute(self._service.calendarList().list(pageToken=page_token))
            calendars += [
                {"id": c["id"], "summary": c.get("summary", c["id"]), "primary": bool(c.get("primary"))}
                for c in page.get("items", [])
                if c.get("accessRole") in ("owner", "writer")
            ]
            page_token = page.get("nextPageToken")
            if not page_token:
                return calendars

    def _execute(self, request: HttpRequest) -> Any:
        try:
            return request.execute(num_retries=self._retries)
        except HttpError as e:
            raise _translate(e) from e
        except GoogleAuthError as e:
            raise CalendarUnavailable(f"не удалось получить доступ к Google: {e}") from e
        except (OSError, httplib2.HttpLib2Error) as e:
            raise CalendarUnavailable(f"нет связи с Google: {e}") from e


def _translate(error: HttpError) -> CalendarError:
    status = error.status_code
    reasons = {d.get("reason") for d in error.error_details if isinstance(d, dict)}
    message = f"Google отклонил запрос (HTTP {status}): {error.reason}"
    if status in (404, 410):
        return EventNotFound(message)
    if status == 409:
        return EventAlreadyExists(message)
    if status == 401 or status == 429 or reasons & _RATE_LIMIT_REASONS:
        return CalendarUnavailable(message)
    return CalendarError(message)
