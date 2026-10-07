"""
The running service: sync cycles for the active source in a background thread.

The thread owns the engine and its Store. Everything the web layer needs to
show is written to the database; the only things shared in memory are the
"running" flag and the wake-up signals.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

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
        self._next_run_at: float | None = None
        self._thread: threading.Thread | None = None

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
    def seconds_until_next_cycle(self) -> int | None:
        if self.running or self._next_run_at is None:
            return None
        return max(0, round(self._next_run_at - time.time()))

    def run_cycle(self) -> CycleReport:
        """Run one cycle and record its report. Never raises: any failure becomes report.error."""
        allow_mass_delete = self._deletions_approved.is_set()
        self._running.set()
        try:
            with Store(self._db_path) as store:
                try:
                    calendar = self._calendar_factory()
                    report = SyncEngine(
                        self.source_name, self._source, calendar, store, self._settings
                    ).run_cycle(allow_mass_delete)
                except CalendarUnavailable as e:
                    report = CycleReport(error=f"календарь недоступен: {e}")
                except Exception as e:
                    # A bug must be visible in the admin page, not only in a log nobody reads.
                    logger.exception("Sync cycle crashed")
                    report = CycleReport(error=f"внутренняя ошибка: {type(e).__name__}: {e}")
                if allow_mass_delete and report.error is None:
                    self._deletions_approved.clear()
                store.add_cycle(self.source_name, asdict(report))
        finally:
            self._running.clear()
        return report

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_cycle()
            self._next_run_at = time.time() + self._interval
            self._wake.wait(self._interval)
            self._wake.clear()
