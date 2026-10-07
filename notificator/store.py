"""
SQLite storage of what has been synchronised.

Every method commits before returning, so a crash loses at most the call in
flight. One Store wraps one connection and must be used from one thread; open
another Store on the same file to read from elsewhere.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from notificator.core.model import EventKey, EventSpec, TrackedEvent

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
        self._db = sqlite3.connect(str(path))
        self._db.execute("PRAGMA journal_mode=WAL")
        with self._db:
            self._db.executescript(_SCHEMA)

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
            "SELECT path, uid, gcal_event_id, calendar_id, fingerprint FROM events WHERE source = ?",
            (source,),
        )
        result: dict[str, dict[str, TrackedEvent]] = {}
        for path, uid, gcal_event_id, calendar_id, fp in rows:
            result.setdefault(path, {})[uid] = TrackedEvent(
                EventKey(source, path, uid), gcal_event_id, calendar_id, fp
            )
        return result

    def put_event(
        self, key: EventKey, gcal_event_id: str, calendar_id: str, spec: EventSpec, fingerprint: str | None
    ) -> None:
        """Insert or replace an event. Pass fingerprint=None to record a write that is about to start."""
        with self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO events "
                "(source, path, uid, gcal_event_id, calendar_id, fingerprint, spec) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key.source, key.path, key.uid, gcal_event_id, calendar_id, fingerprint, _spec_json(spec)),
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
            "SELECT path, uid, gcal_event_id, calendar_id, fingerprint IS NOT NULL, spec FROM events "
            "WHERE source = ? ORDER BY path, uid",
            (source,),
        )
        return [
            {"path": path, "gcal_event_id": gcal_event_id, "calendar_id": calendar_id,
             "synced": bool(synced), **json.loads(spec), "uid": uid}
            for path, uid, gcal_event_id, calendar_id, synced, spec in rows
        ]

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

    def issues(self, source: str) -> list[Issue]:
        rows = self._db.execute(
            "SELECT source, path, kind, uid, message FROM issues WHERE source = ? ORDER BY path, id",
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
    data["start"] = spec.start.isoformat()
    data["end"] = spec.end.isoformat()
    return json.dumps(data, ensure_ascii=False)
