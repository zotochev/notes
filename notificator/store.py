"""
SQLite storage of what has been synchronised.

Every method commits before returning, so a crash loses at most the call in
flight. One Store wraps one connection and must be used from one thread; open
another Store on the same file to read from elsewhere.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notificator.core.model import TASK, EventKey, EventSpec, TrackedEvent

_SETUP_LOCK = threading.Lock()
_SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    source  TEXT NOT NULL,
    path    TEXT NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (source, path)
);
CREATE TABLE IF NOT EXISTS events (
    source        TEXT NOT NULL,
    path          TEXT NOT NULL,
    uid           TEXT NOT NULL,
    gcal_event_id TEXT NOT NULL,
    calendar_id   TEXT NOT NULL,
    fingerprint   TEXT,
    spec          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'event',
    PRIMARY KEY (source, path, uid)
);
CREATE TABLE IF NOT EXISTS issues (
    id      INTEGER PRIMARY KEY,
    source  TEXT NOT NULL,
    path    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    uid     TEXT,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS issues_by_file ON issues (source, path, kind);
CREATE TABLE IF NOT EXISTS cycles (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    source TEXT NOT NULL,
    report TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS journal (
    id     INTEGER PRIMARY KEY,
    at     TEXT NOT NULL,
    source TEXT NOT NULL,
    path   TEXT NOT NULL,
    uid    TEXT,
    action TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Issue:
    source: str
    path: str
    # "parse": the file is written incorrectly; "sync": the calendar refused;
    # "source": the file could not be read; "held": a deletion waits for approval.
    kind: str
    uid: str | None
    message: str


@dataclass(frozen=True, slots=True)
class JournalEntry:
    at: str
    source: str
    path: str
    uid: str | None
    action: str
    detail: str


class Store:
    def __init__(self, path: str | Path) -> None:
        # The timeout is how long a write waits for another connection's write to finish.
        self._db = sqlite3.connect(str(path), timeout=30)
        # With WAL this keeps the file consistent after a crash without forcing
        # the disk to flush on every commit; a cycle makes thousands of commits.
        self._db.execute("PRAGMA synchronous=NORMAL")
        # Set up the file only once: opening an existing database must not
        # write, or every reader would contend with the sync thread.
        # Switching a file to WAL cannot wait for other connections, so threads
        # that open a new database at the same moment must take turns.
        with _SETUP_LOCK:
            version = self._db.execute("PRAGMA user_version").fetchone()[0]
            if version < _SCHEMA_VERSION:
                self._db.execute("PRAGMA journal_mode=WAL")
                # Version 2 added events.kind: a database made by version 1 has the table without it.
                upgrade = "ALTER TABLE events ADD COLUMN kind TEXT NOT NULL DEFAULT 'event';" if version == 1 else ""
                self._db.executescript(_SCHEMA + upgrade + f"PRAGMA user_version = {_SCHEMA_VERSION};")

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def backup_to(self, path: str | Path) -> None:
        """Write a consistent copy of the database to another file."""
        target = sqlite3.connect(str(path))
        try:
            self._db.backup(target)
        finally:
            target.close()

    # --- files ---

    def file_versions(self, source: str) -> dict[str, str]:
        rows = self._db.execute("SELECT path, version FROM files WHERE source = ?", (source,))
        return dict(rows.fetchall())

    def set_file_version(self, source: str, path: str, version: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO files (source, path, version) VALUES (?, ?, ?) "
                "ON CONFLICT (source, path) DO UPDATE SET version = excluded.version",
                (source, path, version),
            )

    def clear_file_version(self, source: str, path: str) -> None:
        """Make the file count as changed, so the next cycle reads it again."""
        with self._db:
            self._db.execute("DELETE FROM files WHERE source = ? AND path = ?", (source, path))

    def forget_file(self, source: str, path: str) -> None:
        """Drop the version and issues of a file. Its events are removed separately, one by one."""
        with self._db:
            self._db.execute("DELETE FROM files WHERE source = ? AND path = ?", (source, path))
            self._db.execute("DELETE FROM issues WHERE source = ? AND path = ?", (source, path))

    # --- events ---

    def tracked(self, source: str) -> dict[str, dict[str, TrackedEvent]]:
        """Tracked events of one source, keyed by path, then uid."""
        rows = self._db.execute(
            "SELECT path, uid, gcal_event_id, calendar_id, fingerprint, kind, spec FROM events WHERE source = ?",
            (source,),
        )
        result: dict[str, dict[str, TrackedEvent]] = {}
        for path, uid, gcal_event_id, calendar_id, fp, kind, spec in rows:
            start = json.loads(spec).get("start") if kind == TASK else None
            result.setdefault(path, {})[uid] = TrackedEvent(
                EventKey(source, path, uid), gcal_event_id, calendar_id, fp, kind,
                datetime.fromisoformat(start).date() if start else None,
            )
        return result

    def tracked_event_ids(self) -> set[tuple[str, str]]:
        """(calendar_id, gcal_event_id) of every tracked calendar event, whatever its source."""
        return set(
            self._db.execute("SELECT calendar_id, gcal_event_id FROM events WHERE kind = 'event'").fetchall()
        )

    def tracked_in_other_sources(self, source: str) -> list[TrackedEvent]:
        """Tracked events that belong to any source except the given one."""
        rows = self._db.execute(
            "SELECT source, path, uid, gcal_event_id, calendar_id, fingerprint, kind FROM events "
            "WHERE source != ? ORDER BY source, path, uid",
            (source,),
        )
        return [
            TrackedEvent(EventKey(s, path, uid), event_id, cal, fp, kind)
            for s, path, uid, event_id, cal, fp, kind in rows
        ]

    def forget_other_sources_without_events(self, source: str) -> None:
        """Drop file versions and issues of other sources that have no events left."""
        with self._db:
            for table in ("files", "issues"):
                self._db.execute(
                    f"DELETE FROM {table} WHERE source != ? AND source NOT IN (SELECT source FROM events)",
                    (source,),
                )

    def put_event(
        self, key: EventKey, gcal_event_id: str, calendar_id: str, spec: EventSpec, fingerprint: str | None
    ) -> None:
        """Insert or replace an event. Pass fingerprint=None to record a write that is about to start."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO events "
                "(source, path, uid, gcal_event_id, calendar_id, fingerprint, spec, kind) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (key.source, key.path, key.uid, gcal_event_id, calendar_id, fingerprint, _spec_json(spec),
                 spec.kind),
            )

    def move_event(self, key: EventKey, new_key: EventKey) -> None:
        """Give an event another identity, keeping its calendar event. The write counts as unconfirmed."""
        with self._db:
            self._db.execute(
                "UPDATE events SET source = ?, path = ?, uid = ?, fingerprint = NULL "
                "WHERE source = ? AND path = ? AND uid = ?",
                (new_key.source, new_key.path, new_key.uid, key.source, key.path, key.uid),
            )

    def delete_event(self, key: EventKey) -> None:
        with self._db:
            self._db.execute(
                "DELETE FROM events WHERE source = ? AND path = ? AND uid = ?",
                (key.source, key.path, key.uid),
            )

    def events(self, source: str) -> list[dict[str, Any]]:
        """Tracked events with what they say, for display. `synced` is False while a write is unconfirmed."""
        rows = self._db.execute(
            "SELECT path, uid, gcal_event_id, calendar_id, fingerprint IS NOT NULL, spec, kind FROM events "
            "WHERE source = ? ORDER BY path, uid",
            (source,),
        )
        return [
            # The spec's own calendar_id (None = default) must not hide where the event really is.
            {**json.loads(spec), "path": path, "uid": uid, "gcal_event_id": gcal_event_id,
             "calendar_id": calendar_id, "synced": bool(synced), "kind": kind}
            for path, uid, gcal_event_id, calendar_id, synced, spec, kind in rows
        ]

    def event_counts(self) -> dict[str, int]:
        """Number of tracked events per source."""
        return dict(self._db.execute("SELECT source, COUNT(*) FROM events GROUP BY source").fetchall())

    # --- issues ---

    def set_issues(self, source: str, path: str, kind: str, issues: list[tuple[str | None, str]]) -> None:
        """Replace all issues of one kind for a file with (uid, message) pairs."""
        with self._db:
            self._db.execute(
                "DELETE FROM issues WHERE source = ? AND path = ? AND kind = ?", (source, path, kind)
            )
            self._db.executemany(
                "INSERT INTO issues (source, path, kind, uid, message) VALUES (?, ?, ?, ?, ?)",
                [(source, path, kind, uid, message) for uid, message in issues],
            )

    def issues(self, source: str | None = None) -> list[Issue]:
        """Issues of one source, or of all sources when none is given."""
        rows = self._db.execute(
            "SELECT source, path, kind, uid, message FROM issues WHERE ?1 IS NULL OR source = ?1 "
            "ORDER BY source, path, id",
            (source,),
        )
        return [Issue(*row) for row in rows]

    # --- cycles ---

    def add_cycle(self, source: str, report: dict[str, Any], keep: int = 200) -> None:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._db:
            self._db.execute(
                "INSERT INTO cycles (at, source, report) VALUES (?, ?, ?)",
                (at, source, json.dumps(report, ensure_ascii=False)),
            )
            self._db.execute(
                "DELETE FROM cycles WHERE id <= (SELECT MAX(id) FROM cycles) - ?", (keep,)
            )

    def cycles(self, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent cycles first, as {id, at, source, ...report fields}."""
        rows = self._db.execute("SELECT id, at, source, report FROM cycles ORDER BY id DESC LIMIT ?", (limit,))
        return [{"id": id_, "at": at, "source": source, **json.loads(report)} for id_, at, source, report in rows]

    # --- journal ---

    def log(self, key: EventKey, action: str, detail: str = "") -> None:
        at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._db:
            self._db.execute(
                "INSERT INTO journal (at, source, path, uid, action, detail) VALUES (?, ?, ?, ?, ?, ?)",
                (at, key.source, key.path, key.uid, action, detail),
            )

    def journal(self, limit: int = 200) -> list[JournalEntry]:
        """Most recent entries first."""
        rows = self._db.execute(
            "SELECT at, source, path, uid, action, detail FROM journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [JournalEntry(*row) for row in rows]


def _spec_json(spec: EventSpec) -> str:
    data = asdict(spec)
    data["start"] = spec.start.isoformat() if spec.start else None
    data["end"] = spec.end.isoformat() if spec.end else None
    return json.dumps(data, ensure_ascii=False)
