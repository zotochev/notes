"""
Interfaces the sync engine depends on, and the errors they are allowed to raise.

Implementations must report failure by raising. Returning an empty list or an
empty string for "something went wrong" is exactly what this design forbids:
the engine treats every returned value as the truth.
"""
from __future__ import annotations

from typing import Any, Protocol

from notificator.core.model import RemoteFile


class SourceError(Exception):
    """Listing or reading failed; nothing can be concluded about the files involved."""


class CalendarError(Exception):
    """A calendar call failed for one event; other events may still succeed."""


class EventNotFound(CalendarError):
    """The event does not exist in the calendar, or was deleted there."""


class EventAlreadyExists(CalendarError):
    """An event with this id already exists, possibly as a deleted one."""


class CalendarUnavailable(CalendarError):
    """The calendar cannot be used at all right now (not authorised, no network)."""


class Source(Protocol):
    def list_files(self) -> list[RemoteFile]:
        """Return every file under the watched paths. Must be complete or raise SourceError."""
        ...

    def stat(self, path: str) -> RemoteFile | None:
        """Return one file exactly as list_files would report it, or None when it no longer exists.

        None is as strong a claim as a listing without the file: when the watched
        path around it cannot be confirmed to exist, raise SourceError instead.
        """
        ...

    def read_text(self, file: RemoteFile) -> str:
        """Return the file's text or raise SourceError."""
        ...

    def read_bytes(self, file: RemoteFile) -> bytes:
        """Return the file's content as it is stored, or raise SourceError."""
        ...


class Tasks(Protocol):
    """Google Tasks. Unlike a calendar event, a task gets its id from the service."""

    def insert(self, tasklist: str, body: dict[str, Any]) -> str:
        """Create a task and return its id."""
        ...

    def update(self, tasklist: str, task_id: str, body: dict[str, Any]) -> None:
        """Update an existing task. Raises EventNotFound, also when it was deleted in Google."""
        ...

    def delete(self, tasklist: str, task_id: str) -> None:
        """Delete a task. Deleting a missing task is not an error."""
        ...

    def find(self, tasklist: str, marker: str) -> str | None:
        """Return the id of the task whose notes begin with `marker`, or None."""
        ...


class Calendar(Protocol):
    def insert(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        """Create an event with the given id. Raises EventAlreadyExists."""
        ...

    def update(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        """Update an existing event. Raises EventNotFound, also when it was deleted in the calendar."""
        ...

    def delete(self, calendar_id: str, event_id: str) -> None:
        """Delete an event. Deleting a missing event is not an error."""
        ...
