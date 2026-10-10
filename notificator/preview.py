"""Preview of a sync cycle: what would be written to the calendar, without writing anything."""
from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from notificator.calendars.dry_run import DryRunCalendar, DryRunTasks, PlannedCall
from notificator.config import STATE_FILE, Config
from notificator.core.planning import too_many_deletes
from notificator.store import Store
from notificator.sync.engine import CycleReport, SyncEngine
from notificator.wiring import build_source, sync_settings

__all__ = ["Preview", "preview_cycle"]


@dataclass(frozen=True, slots=True)
class Preview:
    report: CycleReport
    calls: list[PlannedCall]
    # True when the real cycle would hold the deletions for approval.
    deletes_need_approval: bool


def preview_cycle(
    config: Config,
    data_dir: Path,
    source_name: str | None = None,
    before: Callable[[Store], None] = lambda store: None,
) -> Preview:
    """Run a cycle for one source against a throwaway copy of the state.

    `before` may change the copy first, to preview a cycle that follows some other change.
    """
    name = source_name or config.active_source
    source = build_source(config.sources[name])

    settings = sync_settings(config)
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / STATE_FILE
        if (data_dir / STATE_FILE).is_file():
            with Store(data_dir / STATE_FILE) as real:
                real.backup_to(copy)
        calendar = DryRunCalendar()
        with Store(copy) as store:
            before(store)
            tracked_before = sum(store.event_counts().values())
            # Deletions are allowed so that they are listed; whether the real
            # cycle would hold them is worked out below, without a second pass.
            report = SyncEngine(
                name, source, calendar, store, settings, tasks=DryRunTasks(calendar.calls),
            ).run_cycle(allow_mass_delete=True)
    needs_approval = too_many_deletes(
        report.deleted, tracked_before, settings.max_delete_ratio, settings.held_deletes_min
    )
    return Preview(report, calendar.calls, deletes_need_approval=needs_approval)
