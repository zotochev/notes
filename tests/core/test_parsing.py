from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from notificator.core.parsing import ParseContext, parse_csv, parse_file, parse_text

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
def test_csv_without_required_columns_is_ignored(text):
    result = parse_csv(text, CTX)

    assert result.events == ()
    assert result.issues == ()
    assert not result.opaque_failure


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|", ":", "~", "\x1f", "§"])
def test_csv_delimiter_is_found_from_the_header(delimiter):
    rows = [
        ["Location", "UID", " Summary ", "start", "attendees"],
        ["Room, 2nd floor; left" if delimiter == "\t" else "Room", "a1", "Meeting", "2025-11-11", ""],
    ]
    text = "﻿" + "\r\n".join(delimiter.join(row) for row in rows) + "\r\n"

    result = parse_csv(text, CTX)

    assert result.issues == ()
    (event,) = result.events
    assert (event.uid, event.summary, event.start.date()) == ("a1", "Meeting", datetime(2025, 11, 11).date())
    assert event.location.startswith("Room")


def test_csv_with_semicolons_takes_quoted_attendees_and_commas_inside_fields():
    text = (
        'uid;summary;start;attendees\n'
        'a1;Meeting, weekly;2025-11-11 08:00;"user@example.com; friend@example.com"\n'
        'a2;Other;2025-11-12;user@example.com, friend@example.com\n'
    )

    result = parse_csv(text, CTX)

    assert result.issues == ()
    assert [e.summary for e in result.events] == ["Meeting, weekly", "Other"]
    assert [e.attendees for e in result.events] == [("user@example.com", "friend@example.com")] * 2


def test_csv_whose_columns_are_named_only_inside_another_column_is_ignored():
    assert parse_csv("uid summary start\na1 Meeting 2025-11-11\n", CTX).events == ()
    assert parse_csv("title;text\nx;uid,summary,start\n", CTX).events == ()


def test_csv_header_names_may_be_quoted():
    text = '﻿"uid";"summary";"start"\r\n"a1";"Meeting; weekly";"2025-11-11 08:00"\r\n'

    result = parse_csv(text, CTX)

    assert result.issues == ()
    assert [(e.uid, e.summary) for e in result.events] == [("a1", "Meeting; weekly")]


def test_csv_value_split_by_an_unquoted_delimiter_is_reported_not_guessed():
    text = 'uid:summary:start\na1:Broken:2025-11-11 08:00\na2:Fine:"2025-11-11 08:00"\n'

    result = parse_csv(text, CTX)

    assert [e.uid for e in result.events] == ["a2"]
    (issue,) = result.issues
    assert issue.message.startswith("строка 2: значений больше, чем колонок")
    # The row's uid cannot be trusted either, so nothing about absent uids is concluded from this file.
    assert not result.confirms_absent("a1") and not result.confirms_absent("gone")


def test_csv_trailing_empty_values_are_not_an_error():
    result = parse_csv("uid,summary,start\na1,Meeting,2025-11-11,,\n", CTX)

    assert (len(result.events), result.issues) == (1, ())


def test_csv_with_a_header_and_no_rows_has_no_events_and_no_issues():
    result = parse_csv("uid|summary|start\n", CTX)

    assert (result.events, result.issues) == ((), ())
    assert result.confirms_absent("a1")


def test_csv_that_does_not_start_with_the_header_is_ignored():
    assert parse_csv("\nuid,summary,start\na1,Meeting,2025-11-11\n", CTX).events == ()
    assert parse_csv("sep=;\nuid;summary;start\na1;Meeting;2025-11-11\n", CTX).events == ()


def workbook(**sheets: list[list]) -> bytes:
    """An .xlsx file with the given sheets, each a list of rows."""
    import io

    from openpyxl import Workbook

    book = Workbook()
    book.remove(book.active)
    for title, rows in sheets.items():
        sheet = book.create_sheet(title)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_xlsx_sheet_with_the_required_columns_is_an_events_table():
    data = workbook(Plan=[
        ["Location", "UID", " Summary ", "Start", "end", "attendees", None],
        ["Room 5", 3333344447, "Meeting", datetime(2025, 11, 11, 8, 30), None, "user@example.com; friend@example.com"],
        [None, "a2", "Whole day", datetime(2025, 11, 12), None, None],
        [None, "a3", "Typed as text", "2025-11-13 09:00", "2025-11-13 10:00", "user@example.com, friend@example.com"],
        [None, None, "No uid", datetime(2025, 11, 14), None, None],
    ])

    result = parse_file("/plan.xlsx", data, CTX)

    assert result.issues == ()
    first, second, third = result.events
    assert (first.uid, first.summary, first.location) == ("3333344447", "Meeting", "Room 5")
    assert first.start == datetime(2025, 11, 11, 8, 30, tzinfo=TZ)
    assert first.attendees == third.attendees == ("user@example.com", "friend@example.com")
    assert second.start == parse_text(
        "<event><uid>x</uid><summary>s</summary><start>2025-11-12</start></event>", CTX
    ).events[0].start
    assert (third.start, third.end) == (
        datetime(2025, 11, 13, 9, 0, tzinfo=TZ), datetime(2025, 11, 13, 10, 0, tzinfo=TZ),
    )


def test_xlsx_sheets_without_the_columns_are_ignored_and_the_rest_are_all_read():
    data = workbook(
        Notes=[["Just", "some", "numbers"], [1, 2, 3]],
        Empty=[],
        Q1=[["uid", "summary", "start"], ["a1", "First", "2025-11-11"]],
        Q2=[["uid", "summary", "start"], ["a2", "Second", "2025-11-12"]],
    )

    result = parse_file("/plan.xlsx", data, CTX)

    assert ([e.summary for e in result.events], result.issues) == (["First", "Second"], ())


def test_xlsx_without_any_events_sheet_is_ignored():
    result = parse_file("/budget.xlsx", workbook(Budget=[["item", "price"], ["tea", 5]]), CTX)

    assert (result.events, result.issues, result.opaque_failure) == ((), (), False)


def test_xlsx_bad_row_is_reported_with_its_sheet_and_row():
    data = workbook(Plan=[["uid", "summary", "start"], ["a1", "Good", "2025-11-11"], ["a2", "Bad", "не дата"]])

    result = parse_file("/plan.xlsx", data, CTX)

    assert [e.uid for e in result.events] == ["a1"]
    (issue,) = result.issues
    assert issue.message.startswith("лист «Plan», строка 3:")
    assert result.broken_uids == {"a2"}


def test_file_that_is_not_a_workbook_is_reported_and_confirms_nothing():
    result = parse_file("/plan.xlsx", b"<!DOCTYPE html><html>not a workbook</html>", CTX)

    (issue,) = result.issues
    assert issue.message.startswith("не удалось открыть книгу Excel")
    assert not result.confirms_absent("a1")


def task_xml(**fields: str) -> str:
    return "<task>" + "".join(f"<{k}>{v}</{k}>" for k, v in fields.items()) + "</task>"


def test_task_block_gives_a_task_with_a_due_date():
    text = task_xml(uid="t1", summary="Call the bank", due="2026-10-15 15:00", description="about the card",
                    tasklist="work")

    (task,) = parse_text(text, CTX).events

    assert (task.kind, task.uid, task.summary, task.description) == ("task", "t1", "Call the bank", "about the card")
    # Google Tasks keeps the date only.
    assert (task.start, task.end) == (datetime(2026, 10, 15, tzinfo=TZ), None)
    assert task.calendar_id == "work"


def test_task_may_have_no_due_date():
    (task,) = parse_text(task_xml(uid="t1", summary="Some day"), CTX).events

    assert (task.kind, task.start, task.end, task.calendar_id) == ("task", None, None, None)


def test_event_stays_the_default_and_blocks_of_both_kinds_are_read_from_one_text():
    text = event_xml(uid="u1", summary="Meeting", start="2026-10-15 15:00") + "\n" + task_xml(uid="t1", summary="Prepare")

    result = parse_text(text, CTX)

    assert [(e.uid, e.kind) for e in result.events] == [("u1", "event"), ("t1", "task")]
    assert result.issues == ()


def test_start_in_a_task_and_due_in_an_event_are_errors():
    task = parse_text(task_xml(uid="t1", summary="Call", start="2026-10-15"), CTX)
    event = parse_text(event_xml(uid="u1", summary="Meeting", start="2026-10-15", due="2026-10-16"), CTX)

    assert (task.events, event.events) == ((), ())
    assert "у задачи не может быть start" in task.issues[0].message
    assert "у события не может быть due" in event.issues[0].message
    assert (task.broken_uids, event.broken_uids) == ({"t1"}, {"u1"})


def test_fields_of_the_other_kind_are_ignored():
    task_text = task_xml(
        uid="t1", summary="Call", due="2026-10-15", end="2026-10-16", location="Room",
        recurrence="not even a rule", attendees="not an email", calendar_id="primary",
    )
    event_text = event_xml(uid="u1", summary="Meeting", start="2026-10-15 15:00", tasklist="work")

    (task,), (event,) = parse_text(task_text, CTX).events, parse_text(event_text, CTX).events

    assert (task.kind, task.calendar_id, task.location, task.attendees, task.recurrence, task.end) == (
        "task", None, None, (), None, None,
    )
    assert (event.kind, event.calendar_id) == ("event", None)


def test_task_and_event_cannot_share_a_uid():
    text = event_xml(uid="x1", summary="Meeting", start="2026-10-15") + task_xml(uid="x1", summary="Call")

    result = parse_text(text, CTX)

    assert [e.kind for e in result.events] == ["event"]
    assert "более одного раза" in result.issues[0].message


def test_table_row_with_due_instead_of_start_is_a_task():
    text = (
        "uid,summary,start,due,tasklist,location,calendar_id\n"
        "u1,Meeting,2026-10-15 15:00,,work,Room,cal1\n"
        "t1,Call the bank,,2026-10-16,work,Room,cal1\n"
        "x1,Both,2026-10-15,2026-10-16,,,\n"
        "x2,Neither,,,,,\n"
    )

    result = parse_csv(text, CTX)

    # Each row takes the columns of its own kind and ignores the rest.
    assert [(e.uid, e.kind, e.calendar_id, e.location) for e in result.events] == [
        ("u1", "event", "cal1", "Room"), ("t1", "task", "work", None),
    ]
    assert [i.message for i in result.issues] == [
        "строка 4: заполнены и start, и due: у события задаётся start, у задачи — due",
        "строка 5: не задан start",
    ]


def test_table_without_a_start_column_holds_tasks_and_some_may_have_no_due_date():
    result = parse_csv("uid;summary;due\nt1;Call the bank;2026-10-16\nt2;Some day;\n", CTX)

    assert [(e.uid, e.kind, e.start) for e in result.events] == [
        ("t1", "task", datetime(2026, 10, 16, tzinfo=TZ)), ("t2", "task", None),
    ]
    assert result.issues == ()


def test_xlsx_sheet_of_tasks_is_read_too():
    data = workbook(Tasks=[["uid", "summary", "due"], ["t1", "Call the bank", datetime(2026, 10, 16)]])

    (task,) = parse_file("/tasks.xlsx", data, CTX).events

    assert (task.kind, task.start) == ("task", datetime(2026, 10, 16, tzinfo=TZ))
