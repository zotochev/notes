from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from notificator.core.parsing import ParseContext, parse_csv, parse_text

TZ = ZoneInfo("Europe/Ulyanovsk")
CTX = ParseContext(default_tz=TZ, now=datetime(2026, 3, 10, 15, 42, 7, tzinfo=TZ))
DATA = Path(__file__).parents[1] / "data"


def event_xml(**fields: str) -> str:
    body = "".join(f"<{k}>{v}</{k}>" for k, v in fields.items())
    return f"<event>{body}</event>"


def parse_one(**fields: str):
    result = parse_text(event_xml(**fields), CTX)
    assert result.issues == ()
    (event,) = result.events
    return event


def test_minimal_event_gets_default_timezone_and_duration():
    event = parse_one(uid="abc1", summary="Meeting", start="2025-11-11 08:00")

    assert event.start == datetime(2025, 11, 11, 8, 0, tzinfo=TZ)
    assert event.end == event.start + timedelta(minutes=30)
    assert event.time_zone == "Europe/Ulyanovsk"
    assert event.attendees == ()
    assert event.description is None


def test_event_is_found_inside_arbitrary_text():
    text = f"# Заметки\nкакой-то текст <event> без пары\n{event_xml(uid='a1', summary='S', start='2025-11-11')}\nхвост"

    result = parse_text(text, CTX)

    assert [e.uid for e in result.events] == ["a1"]
    assert result.issues == ()


def test_commented_out_event_is_ignored():
    text = "\n".join("# " + line for line in event_xml(uid="a1", summary="S", start="2025-11-11").splitlines())

    result = parse_text(text, CTX)

    assert result.events == ()
    assert result.confirms_absent("a1")


def test_explicit_time_zone_field_is_used():
    event = parse_one(uid="a1", summary="S", start="2025-11-11 08:00", time_zone="Europe/Moscow")

    assert event.time_zone == "Europe/Moscow"
    assert event.start == datetime(2025, 11, 11, 8, 0, tzinfo=ZoneInfo("Europe/Moscow"))


def test_explicit_utc_offset_is_converted_not_discarded():
    event = parse_one(uid="a1", summary="S", start="2026-01-13T14:00:00+03:00")

    assert event.start == datetime(2026, 1, 13, 15, 0, tzinfo=TZ)


def test_empty_optional_fields_count_as_absent():
    event = parse_one(uid="a1", summary="S", start="2025-11-11 08:00", end="", location="", attendees="")

    assert event.end == event.start + timedelta(minutes=30)
    assert event.location is None
    assert event.attendees == ()


@pytest.mark.parametrize("tag", ["email", "item"])
def test_attendees_accept_any_child_tag(tag):
    attendees = f"<{tag}>user@example.com</{tag}><{tag}>friend@example.com</{tag}>"

    event = parse_one(uid="a1", summary="S", start="2025-11-11 08:00", attendees=attendees)

    assert event.attendees == ("user@example.com", "friend@example.com")


def test_recurrence_gets_rrule_prefix():
    event = parse_one(uid="a1", summary="S", start="2025-11-11 08:00", recurrence="FREQ=DAILY;COUNT=2")

    assert event.recurrence == "RRULE:FREQ=DAILY;COUNT=2"


def test_relative_date_without_time_is_pinned_to_midnight():
    event = parse_one(uid="a1", summary="S", start="сегодня")

    assert event.start == datetime(2026, 3, 10, 0, 0, tzinfo=TZ)


def test_relative_date_is_stable_within_a_day():
    later = ParseContext(default_tz=TZ, now=CTX.now + timedelta(hours=3))
    xml = event_xml(uid="a1", summary="S", start="через 5 дней")

    assert parse_text(xml, CTX).events == parse_text(xml, later).events


@pytest.mark.parametrize(
    "fields, expected",
    [
        ({"uid": "a1", "start": "2025-11-11"}, "summary"),
        ({"uid": "a1", "summary": "S"}, "start"),
        ({"uid": "a1", "summary": "S", "start": "не дата"}, "формат даты"),
        ({"uid": "a1", "summary": "S", "start": "2025-11-11 09:00", "end": "2025-11-11 08:00"}, "позже"),
        ({"uid": "a1", "summary": "S", "start": "2025-11-11", "time_zone": "Mars/Olympus"}, "часовой пояс"),
        ({"uid": "a1", "summary": "S", "start": "2025-11-11", "recurrence": "FREQ=SOMETIMES"}, "повторения"),
        ({"uid": "a1", "summary": "S", "start": "2025-11-11", "attendees": "<email>nope</email>"}, "email"),
        ({"uid": "a1", "summary": "Tom & Jerry", "start": "2025-11-11"}, "XML"),
    ],
)
def test_broken_event_is_reported_but_not_confirmed_absent(fields, expected):
    result = parse_text(event_xml(**fields), CTX)

    assert result.events == ()
    (issue,) = result.issues
    assert expected in issue.message
    assert issue.uid == "a1"
    assert result.broken_uids == {"a1"}
    assert not result.confirms_absent("a1")
    assert result.confirms_absent("other")


@pytest.mark.parametrize("fields", [{"summary": "S", "start": "2025-11-11"}, {"uid": "not-valid!", "summary": "S"}])
def test_broken_event_without_readable_uid_blocks_all_absence_conclusions(fields):
    result = parse_text(event_xml(**fields), CTX)

    assert result.opaque_failure
    assert not result.confirms_absent("anything")


def test_one_broken_event_does_not_affect_valid_neighbours():
    text = event_xml(uid="good", summary="S", start="2025-11-11") + event_xml(uid="bad", summary="S", start="???")

    result = parse_text(text, CTX)

    assert [e.uid for e in result.events] == ["good"]
    assert result.broken_uids == {"bad"}
    assert result.confirms_absent("removed")


def test_duplicate_uid_keeps_first_and_reports_second():
    text = event_xml(uid="a1", summary="first", start="2025-11-11") + event_xml(uid="a1", summary="second", start="2025-11-12")

    result = parse_text(text, CTX)

    assert [e.summary for e in result.events] == ["first"]
    assert len(result.issues) == 1
    assert result.broken_uids == frozenset()


def test_oversized_fragment_is_reported():
    xml = event_xml(uid="a1", summary="S" * 100, start="2025-11-11")

    result = parse_text(xml, CTX, max_fragment_bytes=50)

    assert result.events == ()
    assert not result.confirms_absent("a1")


def test_empty_text_confirms_everything_absent():
    result = parse_text("", CTX)

    assert result == parse_text("просто текст без событий", CTX)
    assert result.confirms_absent("a1")


def test_sample_file_with_embedded_and_commented_events():
    result = parse_text((DATA / "embedded_event.txt").read_text(encoding="utf-8"), CTX)

    (event,) = result.events
    assert event.uid == "1"
    assert event.start == datetime(2026, 1, 13, 14, 0, tzinfo=TZ)
    assert event.end == datetime(2026, 1, 13, 15, 14, tzinfo=TZ)
    assert event.location == "Онлайн"
    assert event.recurrence == "RRULE:FREQ=DAILY;COUNT=2"


def test_csv_basic():
    text = "﻿UID, Summary ,start,attendees\na1,Meeting,2025-11-11 08:00,user@example.com; friend@example.com\n"

    result = parse_csv(text, CTX)

    assert result.issues == ()
    (event,) = result.events
    assert event.summary == "Meeting"
    assert event.start == datetime(2025, 11, 11, 8, 0, tzinfo=TZ)
    assert event.attendees == ("user@example.com", "friend@example.com")


def test_csv_bad_row_is_reported_with_row_number_and_keeps_other_rows():
    text = "uid,summary,start\na1,Good,2025-11-11\na2,Bad,не дата\n,NoUid,2025-11-11\n"

    result = parse_csv(text, CTX)

    assert [e.uid for e in result.events] == ["a1"]
    (issue,) = result.issues
    assert issue.message.startswith("строка 3:")
    assert result.broken_uids == {"a2"}
    assert not result.opaque_failure


def test_csv_duplicate_uid():
    result = parse_csv("uid,summary,start\na1,First,2025-11-11\na1,Second,2025-11-12\n", CTX)

    assert [e.summary for e in result.events] == ["First"]
    assert len(result.issues) == 1
    assert result.broken_uids == frozenset()


@pytest.mark.parametrize("text", ["", "uid,summary\na1,S\n"])
def test_csv_without_usable_header_confirms_nothing(text):
    result = parse_csv(text, CTX)

    assert result.events == ()
    assert result.opaque_failure
    assert not result.confirms_absent("a1")
