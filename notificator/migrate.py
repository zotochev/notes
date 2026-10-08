"""
Moving tracked events from one source to another that holds the same files.

Only the identity of each event changes: its source and the path of its file.
The link to the Google Calendar event stays, so after the move the new source
keeps the same calendar events instead of deleting them and creating new ones.
Moved events are marked as unconfirmed: the first cycle rewrites each one from
its file (an update, not a new event) and removes those no file has any more.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from notificator.core.model import EventKey
from notificator.store import Store


@dataclass(slots=True)
class MoveResult:
    moved: int = 0
    # Human-readable reasons, one per event that was not moved.
    skipped: list[str] = field(default_factory=list)
    # A few (old path, new path) pairs, to check the path rules by eye.
    examples: list[tuple[str, str]] = field(default_factory=list)


def move_source(
    store: Store, old: str, new: str, prefixes: list[tuple[str, str]], apply: bool, max_examples: int = 5
) -> MoveResult:
    """Re-key the events of source `old` to source `new`. With apply=False only reports what would happen.

    `prefixes` are (old path prefix, new path prefix) pairs; an event whose path matches none stays where it is.
    """
    result = MoveResult()
    taken = store.tracked(new)
    for path, events in sorted(store.tracked(old).items()):
        new_path = moved_path(path, prefixes)
        for uid, event in sorted(events.items()):
            label = f"{path} uid={uid}"
            if new_path is None:
                result.skipped.append(f"{label}: путь не подходит ни под одно правило --path")
            elif uid in taken.get(new_path, {}):
                result.skipped.append(f"{label}: в {new} уже отслеживается {new_path}")
            else:
                if apply:
                    store.move_event(event.key, EventKey(new, new_path, uid))
                # Two old paths may land on one new path.
                taken.setdefault(new_path, {})[uid] = event
                result.moved += 1
        if new_path is not None and len(result.examples) < max_examples:
            result.examples.append((path, new_path))
    return result


def moved_path(path: str, prefixes: list[tuple[str, str]]) -> str | None:
    """The path after replacing its longest matching prefix, or None when no prefix matches."""
    for old, new in sorted(prefixes, key=lambda pair: -len(_normalize(pair[0]))):
        old, new = _normalize(old), _normalize(new)
        if path == old or path.startswith(old.rstrip("/") + "/"):
            return new.rstrip("/") + path[len(old.rstrip("/")):]
    return None


def _normalize(prefix: str) -> str:
    return "/" + prefix.strip().strip("/")
