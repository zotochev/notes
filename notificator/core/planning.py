"""
Planning: compare what files say with what the calendar is known to hold and
decide what to do. Pure functions, no I/O.

The rule that makes this safe: a Delete is only ever planned from positive
evidence — a file that was read and fully understood and no longer has the
uid, or a file that a complete listing no longer contains. Failures to list
or read never reach these functions.
"""
from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from notificator.core.model import EventKey, EventSpec, RemoteFile, TrackedEvent
from notificator.core.parsing import ParseResult
from notificator.core.rendering import fingerprint, render_body


@dataclass(frozen=True, slots=True)
class Push:
    """Make the calendar event match `body` (create it if needed)."""
    key: EventKey
    calendar_id: str
    body: dict[str, Any]
    fingerprint: str
    spec: EventSpec


@dataclass(frozen=True, slots=True)
class Delete:
    key: EventKey


Action = Push | Delete


def files_to_read(listing: Iterable[RemoteFile], known_versions: Mapping[str, str]) -> list[RemoteFile]:
    """Files that are new or whose version differs from the one last fully applied."""
    return [f for f in listing if known_versions.get(f.path) != f.version]


def plan_file(
    source: str,
    file: RemoteFile,
    parsed: ParseResult,
    tracked: Mapping[str, TrackedEvent],
    default_calendar: str,
) -> list[Action]:
    """Actions for one file that was read successfully. `tracked` is keyed by uid."""
    actions: list[Action] = []
    for spec in parsed.events:
        body = render_body(spec, file.path, file.link)
        fp = fingerprint(body)
        calendar_id = spec.calendar_id or default_calendar
        known = tracked.get(spec.uid)
        if known is None or known.fingerprint != fp or known.calendar_id != calendar_id:
            actions.append(Push(EventKey(source, file.path, spec.uid), calendar_id, body, fp, spec))
    actions.extend(Delete(t.key) for uid, t in tracked.items() if parsed.confirms_absent(uid))
    return actions


def plan_vanished(
    listed_paths: Collection[str], tracked: Mapping[str, Mapping[str, TrackedEvent]]
) -> dict[str, list[Delete]]:
    """Deletes for files that a complete listing no longer contains. `tracked` is keyed by path, then uid."""
    return {
        path: [Delete(t.key) for t in events.values()]
        for path, events in tracked.items()
        if path not in listed_paths and events
    }


def too_many_deletes(delete_count: int, tracked_count: int, max_ratio: float, min_count: int) -> bool:
    """True when a plan deletes a suspiciously large share of everything we track.

    A complete listing can still be wrong (an unmounted disk, a mistyped watch
    path), so large deletions wait for a human instead of being trusted.
    """
    if delete_count < min_count or tracked_count == 0:
        return False
    return delete_count > tracked_count * max_ratio
