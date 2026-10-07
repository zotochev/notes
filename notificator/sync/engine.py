"""
One synchronisation cycle for one source: list, read what changed, plan, apply.

The engine is synchronous on purpose. It is run from a worker thread, owns its
Store, and shares nothing in memory with the web layer.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from zoneinfo import ZoneInfo

from notificator.core.model import RemoteFile, TrackedEvent
from notificator.core.parsing import ParseContext, parse_file
from notificator.core.planning import (
    Action, Delete, Push, files_to_read, plan_file, plan_vanished, too_many_deletes,
)
from notificator.store import Store
from notificator.sync.ports import (
    Calendar, CalendarError, CalendarUnavailable, EventAlreadyExists, EventNotFound, Source, SourceError,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SyncSettings:
    default_calendar: str
    default_tz: ZoneInfo
    extensions: frozenset[str] = frozenset({".md", ".txt", ".csv"})
    # Deletions wait for approval when a cycle wants to delete at least
    # `held_deletes_min` events and more than this share of everything tracked.
    max_delete_ratio: float = 0.2
    held_deletes_min: int = 5
    read_concurrency: int = 5


@dataclass(slots=True)
class CycleReport:
    listed: int = 0
    read: int = 0
    read_failed: int = 0
    pushed: int = 0
    deleted: int = 0
    failed: int = 0
    # Deletions that were planned but are waiting for approval.
    held_deletes: int = 0
    # Set when the cycle stopped early; nothing after that point was changed.
    error: str | None = None


class SyncEngine:
    def __init__(
        self,
        source_id: str,
        source: Source,
        calendar: Calendar,
        store: Store,
        settings: SyncSettings,
        clock: Callable[[], datetime] | None = None,
        new_event_id: Callable[[], str] = lambda: uuid.uuid4().hex,
    ) -> None:
        self._source_id = source_id
        self._source = source
        self._calendar = calendar
        self._store = store
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(settings.default_tz))
        self._new_event_id = new_event_id

    def run_cycle(self, allow_mass_delete: bool = False) -> CycleReport:
        report = CycleReport()
        try:
            self._run(report, allow_mass_delete)
        except SourceError as e:
            report.error = f"источник недоступен: {e}"
        except CalendarUnavailable as e:
            report.error = f"календарь недоступен: {e}"
        if report.error:
            logger.warning("Cycle for %s stopped: %s", self._source_id, report.error)
        return report

    def _run(self, report: CycleReport, allow_mass_delete: bool) -> None:
        listing = {f.path: f for f in self._source.list_files() if self._is_watched(f.path)}
        report.listed = len(listing)
        versions = self._store.file_versions(self._source_id)
        tracked = self._store.tracked(self._source_id)

        plans: dict[str, list[Action]] = {}
        to_read = files_to_read(listing.values(), versions)
        ctx = ParseContext(default_tz=self._settings.default_tz, now=self._clock())
        for file, text in self._read_all(to_read, report):
            parsed = parse_file(file.path, text, ctx)
            self._store.set_issues(
                self._source_id, file.path, "parse", [(i.uid, i.message) for i in parsed.issues]
            )
            plans[file.path] = plan_file(
                self._source_id, file, parsed, tracked.get(file.path, {}), self._settings.default_calendar
            )
        vanished = plan_vanished(listing.keys(), tracked)

        all_actions = [a for actions in (*plans.values(), *vanished.values()) for a in actions]
        delete_count = sum(isinstance(a, Delete) for a in all_actions)
        tracked_count = sum(len(events) for events in tracked.values())
        hold_deletes = not allow_mass_delete and too_many_deletes(
            delete_count, tracked_count, self._settings.max_delete_ratio, self._settings.held_deletes_min
        )
        if hold_deletes:
            report.held_deletes = delete_count
            logger.warning("Holding %d of %d deletions for approval", delete_count, tracked_count)

        for path, actions in plans.items():
            complete = self._apply(path, actions, tracked.get(path, {}), report, hold_deletes)
            if complete:
                # Only now is the file "done": until then it is read again every cycle.
                self._store.set_file_version(self._source_id, path, listing[path].version)
        for path, deletes in vanished.items():
            if self._apply(path, deletes, tracked[path], report, hold_deletes):
                self._store.forget_file(self._source_id, path)
        for path in versions.keys() - listing.keys() - tracked.keys():
            self._store.forget_file(self._source_id, path)

    def _is_watched(self, path: str) -> bool:
        return PurePosixPath(path).suffix.lower() in self._settings.extensions

    def _read_all(self, files: list[RemoteFile], report: CycleReport) -> list[tuple[RemoteFile, str]]:
        """Read files concurrently. A file that fails to read is skipped: its events stay as they are."""
        def read(file: RemoteFile) -> str | SourceError:
            try:
                return self._source.read_text(file)
            except SourceError as e:
                return e

        with ThreadPoolExecutor(max_workers=self._settings.read_concurrency) as pool:
            results = list(pool.map(read, files))
        texts: list[tuple[RemoteFile, str]] = []
        for file, result in zip(files, results):
            if isinstance(result, SourceError):
                report.read_failed += 1
                self._store.set_issues(self._source_id, file.path, "source", [(None, str(result))])
            else:
                report.read += 1
                self._store.set_issues(self._source_id, file.path, "source", [])
                texts.append((file, result))
        return texts

    def _apply(
        self,
        path: str,
        actions: list[Action],
        tracked: dict[str, TrackedEvent],
        report: CycleReport,
        hold_deletes: bool,
    ) -> bool:
        """Apply the actions of one file. Returns True when nothing is left to do for it."""
        errors: list[tuple[str | None, str]] = []
        held = False
        for action in actions:
            current = tracked.get(action.key.uid)
            try:
                if isinstance(action, Push):
                    self._push(action, current)
                    report.pushed += 1
                elif hold_deletes:
                    held = True
                elif current is not None:
                    self._delete(current)
                    report.deleted += 1
            except CalendarUnavailable:
                raise
            except CalendarError as e:
                report.failed += 1
                errors.append((action.key.uid, str(e)))
                self._store.log(action.key, "error", str(e))
        self._store.set_issues(self._source_id, path, "sync", errors)
        return not errors and not held

    def _push(self, push: Push, current: TrackedEvent | None) -> None:
        if current is not None and current.calendar_id != push.calendar_id:
            # An event cannot be moved between calendars in place.
            self._delete(current)
            current = None
        if current is None:
            self._create(push)
            return
        try:
            self._calendar.update(push.calendar_id, current.gcal_event_id, push.body)
        except EventNotFound:
            try:
                # A write that was recorded but never reached the calendar.
                self._calendar.insert(push.calendar_id, current.gcal_event_id, push.body)
            except EventAlreadyExists:
                # Deleted by hand in the calendar: the id cannot be reused.
                self._create(push)
                return
        self._store.put_event(push.key, current.gcal_event_id, push.calendar_id, push.spec, push.fingerprint)
        self._store.log(push.key, "updated", push.spec.summary)

    def _create(self, push: Push) -> None:
        event_id = self._new_event_id()
        # Record the id before the call: if we crash after the calendar accepted
        # the event, the next cycle finds it by this id instead of creating a duplicate.
        self._store.put_event(push.key, event_id, push.calendar_id, push.spec, fingerprint=None)
        self._calendar.insert(push.calendar_id, event_id, push.body)
        self._store.put_event(push.key, event_id, push.calendar_id, push.spec, push.fingerprint)
        self._store.log(push.key, "created", push.spec.summary)

    def _delete(self, event: TrackedEvent) -> None:
        self._calendar.delete(event.calendar_id, event.gcal_event_id)
        self._store.delete_event(event.key)
        self._store.log(event.key, "deleted")
