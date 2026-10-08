"""
The running service: sync cycles for the active source in a background thread.

Whoever synchronises owns the engine and its Store: the background thread for
cycles, the calling thread for a single-file sync, never both at once.
Everything the web layer needs to show is written to the database; the only
things shared in memory are the "running" flag and the wake-up signals.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from notificator.calendars.google import GoogleCalendar
from notificator.config import STATE_FILE, Config
from notificator.store import Store
from notificator.sync.engine import CycleReport, SyncEngine
from notificator.sync.ports import Calendar, CalendarUnavailable, Source
from notificator.wiring import build_source, google_auth, sync_settings

logger = logging.getLogger(__name__)


class SyncService:
    def __init__(
        self,
        config: Config,
        data_dir: Path,
        source: Source | None = None,
        calendar_factory: Callable[[], Calendar] | None = None,
    ) -> None:
        """`source` and `calendar_factory` replace the configured ones in tests."""
        self.source_name = config.active_source
        self._db_path = data_dir / STATE_FILE
        self._source = source or build_source(config.sources[config.active_source])
        self._calendar_factory = calendar_factory or (
            lambda: GoogleCalendar(google_auth(config, data_dir).credentials())
        )
        self._settings = sync_settings(config)
        self._interval = config.scan_interval_sec
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._deletions_approved = threading.Event()
        self._running = threading.Event()
        # Held by whatever is synchronising: a cycle and a single-file sync never run together.
        self._busy = threading.Lock()
        # How many single-file syncs have finished; the admin page reloads its data when this changes.
        self.file_syncs = 0
        self._next_run_at: float | None = None
        self._thread: threading.Thread | None = None
        # What the running cycle is doing right now, for the admin page.
        self.progress = ""

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=120)

    def trigger(self) -> None:
        """Run the next cycle now instead of waiting for the interval."""
        self._wake.set()

    def approve_deletions(self) -> None:
        """Let the next cycle carry out deletions that were held for approval."""
        self._deletions_approved.set()
        self._wake.set()

    @property
    def running(self) -> bool:
        return self._running.is_set()

    @property
    def listing(self) -> dict[str, Any] | None:
        """How the source's file listing is going, batch by batch; None for a source that lists in one go."""
        walker = getattr(self._source, "walker", None)
        return walker.snapshot() if walker is not None else None

    @property
    def seconds_until_next_cycle(self) -> int | None:
        if self.running or self._next_run_at is None:
            return None
        return max(0, round(self._next_run_at - time.time()))

    def run_cycle(self) -> CycleReport:
        """Run one cycle and record its report. Never raises: any failure becomes report.error."""
        allow_mass_delete = self._deletions_approved.is_set()
        with self._busy:
            self._running.set()
            try:
                with Store(self._db_path) as store:
                    report = self._guarded(store, lambda engine: engine.run_cycle(allow_mass_delete))
                    if allow_mass_delete and report.error is None:
                        self._deletions_approved.clear()
                    store.add_cycle(self.source_name, asdict(report))
            finally:
                self._running.clear()
                self.progress = ""
        return report

    def sync_file(self, path: str) -> CycleReport | None:
        """Read and apply one file now, in the calling thread. Returns None when a sync is already running.

        The report is not recorded as a cycle. Never raises: any failure becomes report.error.
        """
        if not self._busy.acquire(blocking=False):
            return None
        try:
            self._running.set()
            with Store(self._db_path) as store:
                report = self._guarded(store, lambda engine: engine.sync_file(path))
            self.file_syncs += 1
        finally:
            self._running.clear()
            self.progress = ""
            self._busy.release()
        return report

    def _guarded(self, store: Store, run: Callable[[SyncEngine], CycleReport]) -> CycleReport:
        try:
            calendar = self._calendar_factory()
            return run(SyncEngine(
                self.source_name, self._source, calendar, store, self._settings, on_progress=self._set_progress,
            ))
        except CalendarUnavailable as e:
            return CycleReport(error=f"календарь недоступен: {e}")
        except Exception as e:
            # A bug must be visible in the admin page, not only in a log nobody reads.
            logger.exception("Sync crashed")
            return CycleReport(error=f"внутренняя ошибка: {type(e).__name__}: {e}")

    def _set_progress(self, message: str) -> None:
        self.progress = message

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_cycle()
            self._next_run_at = time.time() + self._interval
            self._wake.wait(self._interval)
            self._wake.clear()
