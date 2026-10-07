"""A calendar that changes nothing: for seeing what a sync cycle would do."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class PlannedCall:
    action: str
    calendar_id: str
    event_id: str
    body: dict[str, Any] | None = None


class DryRunCalendar:
    """Accepts every call and records it. Use with a throwaway Store: the engine
    records these calls as done."""

    def __init__(self) -> None:
        self.calls: list[PlannedCall] = []

    def insert(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        self.calls.append(PlannedCall("insert", calendar_id, event_id, body))

    def update(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        self.calls.append(PlannedCall("update", calendar_id, event_id, body))

    def delete(self, calendar_id: str, event_id: str) -> None:
        self.calls.append(PlannedCall("delete", calendar_id, event_id))
