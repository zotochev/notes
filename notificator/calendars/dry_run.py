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
    kind: str = "event"


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


class DryRunTasks:
    """The same for Google Tasks. It shares the list of calls with a DryRunCalendar, to keep their order."""

    def __init__(self, calls: list[PlannedCall]) -> None:
        self.calls = calls

    def insert(self, tasklist: str, body: dict[str, Any]) -> str:
        task_id = f"dry-run-{len(self.calls)}"
        self.calls.append(PlannedCall("insert", tasklist, task_id, body, kind="task"))
        return task_id

    def update(self, tasklist: str, task_id: str, body: dict[str, Any]) -> None:
        self.calls.append(PlannedCall("update", tasklist, task_id, body, kind="task"))

    def delete(self, tasklist: str, task_id: str) -> None:
        self.calls.append(PlannedCall("delete", tasklist, task_id, kind="task"))

    def find(self, tasklist: str, marker: str) -> str | None:
        return None
