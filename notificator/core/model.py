"""Immutable description of a calendar event as written in a source file."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
