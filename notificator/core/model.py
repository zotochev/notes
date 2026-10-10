"""Immutable values shared by the core and the layers around it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


# The two kinds of things a file can ask for. An event is the default.
EVENT = "event"
TASK = "task"


@dataclass(frozen=True, slots=True)
class EventKey:
    """Identity of an event: where it is written, not what it says."""
    source: str
    path: str
    uid: str


@dataclass(frozen=True, slots=True)
class RemoteFile:
    """A file as reported by a source listing. `path` is POSIX-style, relative to the source root."""
    path: str
    # Opaque change marker (etag, object id, mtime): a different value means "read it again".
    version: str
    link: str | None = None


@dataclass(frozen=True, slots=True)
class TrackedEvent:
    """What we remember about an event or a task we put into Google."""
    key: EventKey
    # Empty for a task whose first write was started and never confirmed: Google names tasks itself.
    gcal_event_id: str
    # The calendar of an event, the task list of a task.
    calendar_id: str
    # Fingerprint of the body last confirmed in the calendar. None means the
    # calendar state is unknown (a write was started but never confirmed).
    fingerprint: str | None
    kind: str = EVENT
    # The due date of a task as it was last written, None when it had none.
    due: date | None = None


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What the user asked for: a calendar event or, when `kind` is TASK, a task.

    `start`/`end` are timezone-aware. An event always has both. A task has no
    `end`, and its `start` is the midnight that begins its due date, or None
    when it has no due date.
    """
    uid: str
    summary: str
    start: datetime | None
    end: datetime | None
    time_zone: str
    description: str | None = None
    location: str | None = None
    attendees: tuple[str, ...] = ()
    recurrence: str | None = None
    # For a task this is its task list. None means "use the default one from configuration".
    calendar_id: str | None = None
    kind: str = EVENT
