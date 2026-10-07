"""
Import of `state.json` written by the old notificator.

Only the link between a file's event and its Google Calendar event is carried
over. Imported events are marked as unconfirmed, so the first cycle rewrites
each one from its file (an update, not a new event) and removes those that
are no longer in any file.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from notificator.core.model import EventKey, EventSpec
from notificator.store import Store


@dataclass(slots=True)
class ImportResult:
    imported: int = 0
    # Human-readable reasons, one per event that was not imported.
    skipped: list[str] = field(default_factory=list)


def import_legacy_state(
    state_file: Path, store: Store, source: str, default_calendar: str, default_tz: ZoneInfo
) -> ImportResult:
    """Read an old state.json and record its events under `source`. Raises ValueError on a bad file."""
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        index = data["events_index"]
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise ValueError(f"не удалось прочитать старый state.json: {e}") from e

    result = ImportResult()
    already_tracked = store.tracked(source)
    for old_path, bucket in index.items():
        path = old_path.replace("\\", "/")
        for uid, record in bucket.items():
            label = f"{path} uid={uid}"
            parsed = record.get("parsed")
            if not record.get("gcal_event_id"):
                result.skipped.append(f"{label}: не было в календаре")
            elif not parsed:
                result.skipped.append(f"{label}: в старом состоянии нет данных события")
            elif uid in already_tracked.get(path, {}):
                result.skipped.append(f"{label}: уже отслеживается")
            else:
                calendar_id = record.get("calendar_id") or parsed.get("calendar_id") or default_calendar
                store.put_event(
                    EventKey(source, path, uid), record["gcal_event_id"], calendar_id,
                    _spec(uid, parsed, default_tz), fingerprint=None,
                )
                result.imported += 1
    return result


def _spec(uid: str, parsed: dict, default_tz: ZoneInfo) -> EventSpec:
    """A displayable spec from old data. It is replaced by the real one on the first cycle."""
    tz_name = parsed.get("time_zone") or default_tz.key
    start = _moment(parsed.get("start"), default_tz) or datetime.now(default_tz)
    end = _moment(parsed.get("end"), default_tz) or start + timedelta(minutes=30)
    return EventSpec(
        uid=uid,
        summary=parsed.get("summary") or "(без названия)",
        start=start,
        end=end,
        time_zone=str(tz_name),
        location=parsed.get("location"),
        recurrence=parsed.get("recurrence"),
        calendar_id=parsed.get("calendar_id"),
    )


def _moment(value: object, default_tz: ZoneInfo) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=default_tz)
