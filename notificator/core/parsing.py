"""
Parsing of source text into EventSpec objects.

Two formats are supported: `<event>...</event>` blocks embedded in arbitrary
text, and CSV with a header row.

The important part of the contract is ParseResult.confirms_absent(): a broken
event is not the same thing as a deleted event. Callers must only delete a
calendar event when the parse positively confirms the uid is gone.
"""
from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import dateparser
from dateutil.rrule import rrulestr
from email_validator import EmailNotValidError, validate_email

from notificator.core.model import EventSpec

MAX_FRAGMENT_BYTES = 512_000

_EVENT_RE = re.compile(r"(?s)<event\b[^>]*>(?:(?!<event\b).)*?</event>")
_UID_RE = re.compile(r"^[A-Za-z0-9]+$")
_UID_IN_FRAGMENT_RE = re.compile(r"<uid>\s*([A-Za-z0-9]+)\s*</uid>")
_SCALAR_FIELDS = (
    "uid", "summary", "start", "end", "description",
    "time_zone", "location", "recurrence", "calendar_id",
)
_CSV_REQUIRED = ("uid", "summary", "start")
# Any fixed instant works: it is only used to tell "date without a time of
# day" apart from "date with a time of day" (see _parse_datetime).
_ANCHOR = datetime(2000, 1, 1)

FieldValue = str | list[str]


class EventError(ValueError):
    """The event is written incorrectly; the message is shown to the user."""


@dataclass(frozen=True, slots=True)
class ParseContext:
    default_tz: ZoneInfo
    # Reference instant for relative dates ("сегодня", "in 5 days"). Aware.
    now: datetime
    default_duration: timedelta = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class ParseIssue:
    message: str
    # uid of the event the issue belongs to, when it could be determined.
    uid: str | None = None
    excerpt: str | None = None


@dataclass(frozen=True, slots=True)
class ParseResult:
    events: tuple[EventSpec, ...] = ()
    issues: tuple[ParseIssue, ...] = ()
    # uids that are present in the text but could not be parsed.
    broken_uids: frozenset[str] = frozenset()
    # True when something failed and we could not tell which uid it was. In
    # that case nothing about absent uids can be concluded for this text.
    opaque_failure: bool = False

    def confirms_absent(self, uid: str) -> bool:
        """True only if this text was fully understood and has no such uid."""
        if self.opaque_failure or uid in self.broken_uids:
            return False
        return all(e.uid != uid for e in self.events)


def parse_file(path: str, text: str, ctx: ParseContext) -> ParseResult:
    """Parse a file's text with the parser that fits its type."""
    parser = _PARSERS_BY_SUFFIX.get(PurePosixPath(path).suffix.lower(), parse_text)
    return parser(text, ctx)


def parse_text(
    text: str, ctx: ParseContext, max_fragment_bytes: int = MAX_FRAGMENT_BYTES
) -> ParseResult:
    """Extract `<event>` blocks from arbitrary text. Lines starting with `#` are ignored."""
    collector = _Collector()
    for fragment in _EVENT_RE.findall(_strip_comments(text)):
        uid_hint = _uid_hint(fragment)
        if len(fragment.encode("utf-8", errors="ignore")) > max_fragment_bytes:
            collector.fail(f"блок <event> больше {max_fragment_bytes} байт", uid_hint, fragment[:200])
            continue
        try:
            fields = _fragment_fields(fragment)
            event = build_event(fields, ctx)
        except EventError as e:
            collector.fail(str(e), uid_hint, fragment)
            continue
        collector.add(event, fragment)
    return collector.result()


def parse_csv(text: str, ctx: ParseContext) -> ParseResult:
    """Parse CSV with a header row; `attendees` are separated by `;`. Rows without uid are skipped."""
    collector = _Collector()
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    columns = {name.strip().lower() for name in reader.fieldnames or [] if name}
    if not columns:
        collector.fail("CSV пуст или не содержит строки заголовков", None, None)
        return collector.result()
    missing = [c for c in _CSV_REQUIRED if c not in columns]
    if missing:
        collector.fail(f"в CSV нет обязательных колонок: {', '.join(missing)}", None, None)
        return collector.result()

    for row_num, row in enumerate(reader, start=2):
        values = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        uid = values.get("uid", "")
        if not uid:
            continue
        fields: dict[str, FieldValue] = {k: values[k] for k in _SCALAR_FIELDS if values.get(k)}
        fields["attendees"] = _split_list(values.get("attendees", ""))
        try:
            event = build_event(fields, ctx)
        except EventError as e:
            collector.fail(f"строка {row_num}: {e}", uid if _UID_RE.match(uid) else None, None)
            continue
        collector.add(event, None, where=f"строка {row_num}: ")
    return collector.result()


# File types with their own format; every other watched file is scanned for <event> blocks.
_PARSERS_BY_SUFFIX = {".csv": parse_csv}


def build_event(fields: Mapping[str, FieldValue], ctx: ParseContext) -> EventSpec:
    """Validate raw field values and build an EventSpec. Empty optional fields count as absent."""
    uid = _scalar(fields, "uid")
    if not uid:
        raise EventError("не задан uid")
    if not _UID_RE.match(uid):
        raise EventError(f"uid {uid!r} должен состоять только из латинских букв и цифр")
    summary = _scalar(fields, "summary")
    if not summary:
        raise EventError("не задан summary")

    tz_name = _scalar(fields, "time_zone")
    tz = _zone(tz_name) if tz_name else ctx.default_tz

    start_text = _scalar(fields, "start")
    if not start_text:
        raise EventError("не задан start")
    start = _parse_datetime(start_text, "start", tz, ctx)
    end_text = _scalar(fields, "end")
    end = _parse_datetime(end_text, "end", tz, ctx) if end_text else start + ctx.default_duration
    if end <= start:
        raise EventError("end должен быть позже start")

    recurrence = _scalar(fields, "recurrence")
    return EventSpec(
        uid=uid,
        summary=summary,
        start=start,
        end=end,
        time_zone=tz.key,
        description=_scalar(fields, "description"),
        location=_scalar(fields, "location"),
        attendees=tuple(_email(a) for a in _list(fields, "attendees")),
        recurrence=_rrule(recurrence) if recurrence else None,
        calendar_id=_scalar(fields, "calendar_id"),
    )


class _Collector:
    def __init__(self) -> None:
        self._events: dict[str, EventSpec] = {}
        self._issues: list[ParseIssue] = []
        self._failed_uids: set[str] = set()
        self._opaque = False

    def add(self, event: EventSpec, excerpt: str | None, where: str = "") -> None:
        if event.uid in self._events:
            # The first occurrence stays valid, so the uid is not "broken".
            self._issues.append(ParseIssue(
                f"{where}uid {event.uid!r} используется более одного раза", event.uid, excerpt,
            ))
            return
        self._events[event.uid] = event

    def fail(self, message: str, uid: str | None, excerpt: str | None) -> None:
        self._issues.append(ParseIssue(message, uid, excerpt))
        if uid is None:
            self._opaque = True
        else:
            self._failed_uids.add(uid)

    def result(self) -> ParseResult:
        return ParseResult(
            events=tuple(self._events.values()),
            issues=tuple(self._issues),
            broken_uids=frozenset(self._failed_uids - self._events.keys()),
            opaque_failure=self._opaque,
        )


def _strip_comments(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))


def _uid_hint(fragment: str) -> str | None:
    m = _UID_IN_FRAGMENT_RE.search(fragment)
    return m.group(1) if m else None


def _fragment_fields(fragment: str) -> dict[str, FieldValue]:
    try:
        root = ET.fromstring(fragment)
    except ET.ParseError as e:
        raise EventError(f"некорректный XML: {e}") from e
    fields: dict[str, FieldValue] = {}
    for child in root:
        if child.tag in fields:
            raise EventError(f"тег <{child.tag}> указан более одного раза")
        text = (child.text or "").strip()
        if child.tag == "attendees":
            # <attendees><email>a@b.c</email>...</attendees>; the child tag name is not significant.
            fields[child.tag] = (
                [(item.text or "").strip() for item in child] if len(child) else _split_list(text)
            )
        elif child.tag in _SCALAR_FIELDS:
            if len(child):
                raise EventError(f"тег <{child.tag}> не должен содержать вложенных тегов")
            fields[child.tag] = text
    return fields


def _split_list(text: str) -> list[str]:
    return [part.strip() for part in text.split(";") if part.strip()]


def _scalar(fields: Mapping[str, FieldValue], name: str) -> str | None:
    value = fields.get(name)
    if isinstance(value, str):
        return value.strip() or None
    return None


def _list(fields: Mapping[str, FieldValue], name: str) -> list[str]:
    value = fields.get(name)
    if isinstance(value, list):
        return [v for v in value if v]
    return []


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (KeyError, ValueError, OSError) as e:
        raise EventError(f"неизвестный часовой пояс {name!r}") from e


def _email(value: str) -> str:
    try:
        return validate_email(value, check_deliverability=False).normalized
    except EmailNotValidError as e:
        raise EventError(f"некорректный email участника {value!r}: {e}") from e


def _rrule(value: str) -> str:
    rule = value if value.upper().startswith("RRULE:") else "RRULE:" + value
    try:
        rrulestr(rule)
    except Exception as e:
        raise EventError(f"некорректное правило повторения: {e}") from e
    return rule


def _parse_datetime(text: str, field: str, tz: ZoneInfo, ctx: ParseContext) -> datetime:
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = _parse_free_form(text, field, tz, ctx)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _parse_free_form(text: str, field: str, tz: ZoneInfo, ctx: ParseContext) -> datetime:
    base = ctx.now.astimezone(tz).replace(tzinfo=None)
    try:
        parsed = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": base})
        anchored = dateparser.parse(text, settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": _ANCHOR})
    except Exception:
        parsed = anchored = None
    if parsed is None:
        raise EventError(
            f"неверный формат даты в {field}: {text!r}, попробуйте что-то вроде '2025-11-11 08:00'"
        )
    # "сегодня" resolves to the current time of day and would differ on every
    # scan. If the text resolves to midnight against a midnight anchor, it has
    # no time of day of its own, so pin it to midnight.
    if anchored is not None and anchored.time() == time(0, 0):
        parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed
