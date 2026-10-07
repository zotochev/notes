"""Immutable values shared by the core and the layers around it."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
    """What we remember about an event we put into the calendar."""
    key: EventKey
    gcal_event_id: str
    calendar_id: str
    # Fingerprint of the body last confirmed in the calendar. None means the
    # calendar state is unknown (a write was started but never confirmed).
    fingerprint: str | None


@dataclass(frozen=True, slots=True)
class EventSpec:
    """What the user asked for. `start`/`end` are always timezone-aware."""
    uid: str
    summary: str
    start: datetime
    end: datetime
    time_zone: str
    description: str | None = None
    location: str | None = None
    attendees: tuple[str, ...] = ()
    recurrence: str | None = None
    # None means "use the default calendar from configuration".
    calendar_id: str | None = None
