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
from datetime import date, datetime, time, timedelta
from pathlib import PurePosixPath
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import dateparser
from dateutil.rrule import rrulestr
from email_validator import EmailNotValidError, validate_email

from notificator.core.model import TASK, EventSpec

MAX_FRAGMENT_BYTES = 512_000

# An <event> or <task> block that has no other such block inside it.
_BLOCK_RE = re.compile(r"(?s)<(event|task)\b[^>]*>(?:(?!<(?:event|task)\b).)*?</\1>")
_UID_RE = re.compile(r"^[A-Za-z0-9]+$")
_UID_IN_FRAGMENT_RE = re.compile(r"<uid>\s*([A-Za-z0-9]+)\s*</uid>")
_SCALAR_FIELDS = (
    "uid", "summary", "start", "end", "description",
    "time_zone", "location", "recurrence", "calendar_id", "due", "tasklist",
)
# What a task cannot have, and what an event cannot.
_EVENT_ONLY_FIELDS = ("start", "end", "location", "recurrence", "attendees", "calendar_id")
_TASK_ONLY_FIELDS = ("due", "tasklist")
# A table is an events table when its header has these columns and one of the date columns.
_CSV_REQUIRED = ("uid", "summary")
_CSV_DATE_COLUMNS = ("start", "due")
_CSV_DELIMITERS = (",", ";", "\t", "|")
# A space is left out on purpose: titles and dates contain spaces.
_CSV_NEVER_DELIMITERS = '_" '
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
    # True when the file could not be opened as what its type says it is, so
    # there is no telling whether it was ever meant to hold events.
    unreadable: bool = False

    def confirms_absent(self, uid: str) -> bool:
        """True only if this text was fully understood and has no such uid."""
        if self.opaque_failure or uid in self.broken_uids:
            return False
        return all(e.uid != uid for e in self.events)


def is_binary(path: str) -> bool:
    """True for a file type that must be read as bytes, not as text."""
    return PurePosixPath(path).suffix.lower() in _BINARY_PARSERS_BY_SUFFIX


def parse_file(path: str, content: str | bytes, ctx: ParseContext) -> ParseResult:
    """Parse a file with the parser that fits its type: bytes when is_binary(path), text otherwise."""
    suffix = PurePosixPath(path).suffix.lower()
    if isinstance(content, bytes):
        return _BINARY_PARSERS_BY_SUFFIX[suffix](content, ctx)
    return _PARSERS_BY_SUFFIX.get(suffix, parse_text)(content, ctx)


def parse_text(
    text: str, ctx: ParseContext, max_fragment_bytes: int = MAX_FRAGMENT_BYTES
) -> ParseResult:
    """Extract `<event>` and `<task>` blocks from arbitrary text. Lines starting with `#` are ignored."""
    collector = _Collector()
    for match in _BLOCK_RE.finditer(_strip_comments(text)):
        fragment, kind = match.group(0), match.group(1)
        uid_hint = _uid_hint(fragment)
        if len(fragment.encode("utf-8", errors="ignore")) > max_fragment_bytes:
            collector.fail(f"блок <{kind}> больше {max_fragment_bytes} байт", uid_hint, fragment[:200])
            continue
        try:
            fields = _fragment_fields(fragment)
            event = build_task(fields, ctx) if kind == TASK else build_event(fields, ctx)
        except EventError as e:
            collector.fail(str(e), uid_hint, fragment)
            continue
        collector.add(event, fragment)
    return collector.result()


def parse_csv(text: str, ctx: ParseContext) -> ParseResult:
    """Parse CSV with a header row; `attendees` are separated by `;` or `,`. Rows without uid are skipped.

    The delimiter is the character that makes the first line contain the
    required columns: usually a comma, semicolon or tab, but any other will
    do. A CSV where none does is not an events table: it is ignored, like a
    file with no events.
    """
    collector = _Collector()
    text = text.lstrip("﻿")
    delimiter = _csv_delimiter(text)
    if delimiter is None:
        return collector.result()
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    for row_num, row in enumerate(reader, start=2):
        if any(extra.strip() for extra in row.get(None) or []):
            # A delimiter inside an unquoted value: the values no longer line up
            # with the columns, so not even the uid of this row can be trusted.
            collector.fail(
                f"строка {row_num}: значений больше, чем колонок; значение с разделителем нужно взять в кавычки",
                None, None,
            )
            continue
        values = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        _add_table_row(collector, values, f"строка {row_num}: ", ctx)
    return collector.result()


def _is_events_header(names: list[str]) -> bool:
    """True when a table's column names (already lower-case) make it an events table."""
    return set(_CSV_REQUIRED) <= set(names) and any(column in names for column in _CSV_DATE_COLUMNS)


def parse_xlsx(data: bytes, ctx: ParseContext) -> ParseResult:
    """Parse an Excel workbook: every sheet whose first row names the required columns is an events table.

    Other sheets are ignored, and so is a workbook without such a sheet. Rows without uid are skipped.
    """
    collector = _Collector()
    try:
        from openpyxl import load_workbook
    except ImportError:
        collector.fail("для файлов Excel нужен пакет openpyxl: pip install -r requirements.txt", None, None)
        return collector.result()
    try:
        # read_only: sheets are streamed. data_only: a formula gives its last computed value, not its text.
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = [(sheet.title, list(sheet.iter_rows(values_only=True))) for sheet in workbook.worksheets]
        workbook.close()
    except Exception as e:
        # openpyxl raises many unrelated types for a damaged, encrypted or non-Excel file.
        collector.fail_whole_file(f"не удалось открыть книгу Excel: {type(e).__name__}: {e}")
        return collector.result()

    for title, rows in sheets:
        names = [_cell_text(cell).lower() for cell in (rows[0] if rows else ())]
        if not _is_events_header(names):
            continue
        for row_num, row in enumerate(rows[1:], start=2):
            values = {name: _cell_text(cell) for name, cell in zip(names, row) if name}
            _add_table_row(collector, values, f"лист «{title}», строка {row_num}: ", ctx)
    return collector.result()


def _add_table_row(collector: _Collector, values: dict[str, str], where: str, ctx: ParseContext) -> None:
    """Turn one table row, keyed by lower-case column name, into an event, a task or an issue.

    A row with `due` and without `start` is a task; any other row is an event.
    A table with no `start` column at all can only hold tasks.
    """
    uid = values.get("uid", "")
    if not uid:
        return
    fields: dict[str, FieldValue] = {k: values[k] for k in _SCALAR_FIELDS if values.get(k)}
    attendees = _split_list(values.get("attendees", "").replace(",", ";"))
    if attendees:
        fields["attendees"] = attendees
    is_task = "start" not in values or (bool(values.get("due")) and not values.get("start"))
    try:
        if values.get("due") and values.get("start"):
            raise EventError("заполнены и start, и due: у события задаётся start, у задачи — due")
        event = build_task(fields, ctx) if is_task else build_event(fields, ctx)
    except EventError as e:
        collector.fail(f"{where}{e}", uid if _UID_RE.match(uid) else None, None)
        return
    collector.add(event, None, where=where)


def _cell_text(value: object) -> str:
    """An Excel cell as the text a person would have typed into a CSV."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        # A date cell comes as midnight: written without a time it reads as a date, like in text files.
        return value.date().isoformat() if value.time() == time() else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        # A number typed as 123 is stored as 123.0.
        return str(int(value))
    return str(value).strip()


def _csv_delimiter(text: str) -> str | None:
    """The delimiter with which the header line has every required column, or None when there is none.

    Any single character of the header may be the delimiter, except those a
    column name or a quoted field is made of. The usual ones are tried first.
    """
    header = text.split("\n", 1)[0].rstrip("\r")
    unusual = sorted({c for c in header if not (c.isalnum() or c in _CSV_NEVER_DELIMITERS)})
    for delimiter in dict.fromkeys((*_CSV_DELIMITERS, *unusual)):
        try:
            names = next(csv.reader([header], delimiter=delimiter), [])
        except csv.Error:
            continue
        if _is_events_header([name.strip().lower() for name in names]):
            return delimiter
    return None


# File types with their own format; every other watched file is scanned for <event> blocks.
_PARSERS_BY_SUFFIX = {".csv": parse_csv, ".tsv": parse_csv}
# File types that are not text: their parsers take the file's bytes.
_BINARY_PARSERS_BY_SUFFIX = {".xlsx": parse_xlsx}


def build_event(fields: Mapping[str, FieldValue], ctx: ParseContext) -> EventSpec:
    """Validate raw field values and build an EventSpec. Empty optional fields count as absent."""
    uid, summary, tz = _common_fields(fields, ctx)
    for name in _TASK_ONLY_FIELDS:
        if fields.get(name):
            raise EventError(f"у события не может быть {name}: это поле задачи (<task>)")

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


def build_task(fields: Mapping[str, FieldValue], ctx: ParseContext) -> EventSpec:
    """Validate raw field values and build the EventSpec of a task. Its due date is optional."""
    uid, summary, tz = _common_fields(fields, ctx)
    for name in _EVENT_ONLY_FIELDS:
        if fields.get(name):
            hint = "срок задаётся в due" if name in ("start", "end") else "это поле события"
            raise EventError(f"у задачи не может быть {name}: {hint}")
    due_text = _scalar(fields, "due")
    # Google Tasks keeps only the date of a due time.
    due = _parse_datetime(due_text, "due", tz, ctx).date() if due_text else None
    return EventSpec(
        uid=uid,
        summary=summary,
        start=datetime.combine(due, time(), tz) if due else None,
        end=None,
        time_zone=tz.key,
        description=_scalar(fields, "description"),
        calendar_id=_scalar(fields, "tasklist"),
        kind=TASK,
    )


def _common_fields(fields: Mapping[str, FieldValue], ctx: ParseContext) -> tuple[str, str, ZoneInfo]:
    """uid, summary and time zone: what an event and a task both have."""
    uid = _scalar(fields, "uid")
    if not uid:
        raise EventError("не задан uid")
    if not _UID_RE.match(uid):
        raise EventError(f"uid {uid!r} должен состоять только из латинских букв и цифр")
    summary = _scalar(fields, "summary")
    if not summary:
        raise EventError("не задан summary")
    tz_name = _scalar(fields, "time_zone")
    return uid, summary, _zone(tz_name) if tz_name else ctx.default_tz


class _Collector:
    def __init__(self) -> None:
        self._events: dict[str, EventSpec] = {}
        self._issues: list[ParseIssue] = []
        self._failed_uids: set[str] = set()
        self._opaque = False
        self._unreadable = False

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

    def fail_whole_file(self, message: str) -> None:
        """The file cannot be opened as its type at all."""
        self.fail(message, None, None)
        self._unreadable = True

    def result(self) -> ParseResult:
        return ParseResult(
            events=tuple(self._events.values()),
            issues=tuple(self._issues),
            broken_uids=frozenset(self._failed_uids - self._events.keys()),
            opaque_failure=self._opaque,
            unreadable=self._unreadable,
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
