"""
Finding calendar events that notificator created but no longer tracks:
leftovers of the old version, duplicates, events of a lost state database.

They are recognised by the header every notificator event has at the top of
its description ("uid: ...", "file: ...", "---"), so the old state is not needed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from notificator.store import Store

# Written by core.rendering and, in the same form, by the old version.
_HEADER = re.compile(r"\Auid: (?P<uid>\S+)\nfile: (?P<file>[^\n]+)\n(?:link: [^\n]*\n)?---\n")


class ListableCalendar(Protocol):
    def writable_calendars(self) -> list[dict[str, Any]]: ...
    def list_events(self, calendar_id: str) -> list[dict[str, Any]]: ...
    def delete(self, calendar_id: str, event_id: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Untracked:
    calendar_id: str
    calendar_name: str
    event_id: str
    summary: str
    start: str
    uid: str
    file: str


def find_untracked(calendar: ListableCalendar, store: Store) -> list[Untracked]:
    """Events with a notificator header, in any writable calendar, that the store does not track."""
    tracked = store.tracked_event_ids()
    found: list[Untracked] = []
    for cal in calendar.writable_calendars():
        for event in calendar.list_events(cal["id"]):
            header = _HEADER.match(event.get("description") or "")
            if header is None or (cal["id"], event["id"]) in tracked:
                continue
            start = event.get("start", {})
            found.append(Untracked(
                calendar_id=cal["id"],
                calendar_name=cal["summary"],
                event_id=event["id"],
                summary=event.get("summary", ""),
                start=start.get("dateTime") or start.get("date") or "",
                uid=header["uid"],
                file=header["file"],
            ))
    return found


def delete_untracked(calendar: ListableCalendar, events: list[Untracked]) -> None:
    for event in events:
        calendar.delete(event.calendar_id, event.event_id)
