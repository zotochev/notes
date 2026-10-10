import json

import pytest

from notificator.calendars.google_auth import SCOPES, TASKS_SCOPE, GoogleAuth
from notificator.sync.ports import CalendarUnavailable

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar"


def auth(tmp_path, scopes: list[str] | None) -> GoogleAuth:
    token = tmp_path / "token.json"
    if scopes is not None:
        token.write_text(json.dumps({
            "token": "access", "refresh_token": "refresh", "client_id": "id", "client_secret": "secret",
            "token_uri": "https://oauth2.googleapis.com/token", "scopes": scopes,
            "expiry": "2999-01-01T00:00:00.000000Z",
        }), encoding="utf-8")
    return GoogleAuth(tmp_path / "credentials.json", token)


def test_new_sign_ins_ask_for_the_calendar_and_for_tasks():
    assert SCOPES == [CALENDAR_SCOPE, TASKS_SCOPE]


def test_token_from_before_tasks_keeps_working_for_the_calendar(tmp_path):
    old = auth(tmp_path, [CALENDAR_SCOPE])

    creds = old.credentials()

    # Not widened: asking Google to refresh it for scopes it was never given would be refused.
    assert (creds.valid, list(creds.scopes)) == (True, [CALENDAR_SCOPE])
    assert not old.tasks_allowed()


def test_tasks_are_allowed_once_the_token_has_their_scope(tmp_path):
    assert auth(tmp_path, [CALENDAR_SCOPE, TASKS_SCOPE]).tasks_allowed()


def test_without_a_token_nothing_is_allowed(tmp_path):
    missing = auth(tmp_path, None)

    assert not missing.tasks_allowed()
    with pytest.raises(CalendarUnavailable):
        missing.credentials()
