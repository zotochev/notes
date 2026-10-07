"""Preview of a sync cycle: what would be written to the calendar, without writing anything."""
from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from notificator.calendars.dry_run import DryRunCalendar, PlannedCall
from notificator.config import STATE_FILE, Config
from notificator.store import Store
from notificator.sync.engine import CycleReport, SyncEngine
from notificator.wiring import build_source, sync_settings


@dataclass(frozen=True, slots=True)
class Preview:
    report: CycleReport
    calls: list[PlannedCall]
    # True when the real cycle would hold the deletions for approval.
    deletes_need_approval: bool


def preview_cycle(config: Config, data_dir: Path, source_name: str | None = None) -> Preview:
    """Run a cycle for one source against a throwaway copy of the state."""
    name = source_name or config.active_source
    source = build_source(config.sources[name])

    def run(allow_mass_delete: bool) -> tuple[CycleReport, list[PlannedCall]]:
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / STATE_FILE
            if (data_dir / STATE_FILE).is_file():
                with Store(data_dir / STATE_FILE) as real:
                    real.backup_to(copy)
            calendar = DryRunCalendar()
            with Store(copy) as store:
                report = SyncEngine(name, source, calendar, store, sync_settings(config)).run_cycle(
                    allow_mass_delete
                )
            return report, calendar.calls

    report, calls = run(allow_mass_delete=False)
    if report.held_deletes:
        report, calls = run(allow_mass_delete=True)
        return Preview(report, calls, deletes_need_approval=True)
    return Preview(report, calls, deletes_need_approval=False)
