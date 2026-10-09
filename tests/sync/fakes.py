"""In-memory stand-ins for a file source and a calendar, with switchable failures."""
from __future__ import annotations

from typing import Any

from notificator.core.model import RemoteFile
from notificator.sync.ports import CalendarError, EventAlreadyExists, EventNotFound, SourceError


class FakeSource:
    def __init__(self) -> None:
        self.files: dict[str, str | bytes] = {}
        self.links: dict[str, str] = {}
        self.fail_listing = False
        self.fail_reading: set[str] = set()
        self.reads: list[str] = []

    def list_files(self) -> list[RemoteFile]:
        if self.fail_listing:
            raise SourceError("listing failed")
        return [RemoteFile(p, version=str(hash(t)), link=self.links.get(p)) for p, t in self.files.items()]

    def stat(self, path: str) -> RemoteFile | None:
        if self.fail_listing:
            raise SourceError("stat failed")
        if path not in self.files:
            return None
        return RemoteFile(path, version=str(hash(self.files[path])), link=self.links.get(path))

    def read_text(self, file: RemoteFile) -> str:
        return self.read_bytes(file).decode("utf-8")

    def read_bytes(self, file: RemoteFile) -> bytes:
        self.reads.append(file.path)
        if file.path in self.fail_reading:
            raise SourceError(f"cannot read {file.path}")
        content = self.files[file.path]
        return content if isinstance(content, bytes) else content.encode("utf-8")


class FakeCalendar:
    def __init__(self) -> None:
        # (calendar_id, event_id) -> body
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        # Ids of deleted events: like Google, they can be neither updated nor reused.
        self.deleted: set[tuple[str, str]] = set()
        # Raised by the next write, once.
        self.fail_next: Exception | None = None
        # When set, the next insert is stored and then reports a failure, like a lost response.
        self.lose_next_insert_response = False
        self.calls: list[str] = []

    def summaries(self, calendar_id: str | None = None) -> list[str]:
        return sorted(
            body["summary"] for (cal, _), body in self.events.items() if calendar_id in (None, cal)
        )

    def insert(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        self._before("insert")
        key = (calendar_id, event_id)
        if key in self.events or key in self.deleted:
            raise EventAlreadyExists(event_id)
        self.events[key] = body
        if self.lose_next_insert_response:
            self.lose_next_insert_response = False
            raise CalendarError("connection reset")

    def update(self, calendar_id: str, event_id: str, body: dict[str, Any]) -> None:
        self._before("update")
        key = (calendar_id, event_id)
        if key not in self.events:
            raise EventNotFound(event_id)
        self.events[key] = body

    def delete(self, calendar_id: str, event_id: str) -> None:
        self._before("delete")
        key = (calendar_id, event_id)
        if self.events.pop(key, None) is not None:
            self.deleted.add(key)

    def get(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        key = (calendar_id, event_id)
        if key in self.events:
            return {"id": event_id, "status": "confirmed", **self.events[key]}
        if key in self.deleted:
            return {"id": event_id, "status": "cancelled"}
        raise EventNotFound(event_id)

    def delete_by_hand(self, summary: str) -> None:
        """Simulate the user deleting an event in the calendar UI."""
        (key,) = [k for k, body in self.events.items() if body["summary"] == summary]
        del self.events[key]
        self.deleted.add(key)

    def _before(self, call: str) -> None:
        self.calls.append(call)
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error
