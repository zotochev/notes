"""Google OAuth: stored user token, sign-in flow, sign-out."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from google.auth.exceptions import GoogleAuthError, RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from notificator.sync.ports import CalendarUnavailable

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]


class GoogleAuth:
    def __init__(self, client_secrets_file: Path, token_file: Path, redirect_uri: str | None = None) -> None:
        self._client_secrets_file = client_secrets_file
        self._token_file = token_file
        self._redirect_uri = redirect_uri
        # Sign-ins in progress, by OAuth state. The same Flow must finish the
        # sign-in: it holds the PKCE verifier that belongs to the URL it issued.
        self._pending: dict[str, Flow] = {}

    def credentials(self) -> Credentials:
        """Return valid credentials, refreshing them if needed. Raises CalendarUnavailable."""
        if not self._token_file.is_file():
            raise CalendarUnavailable("Google не авторизован: войдите через админку")
        try:
            creds = Credentials.from_authorized_user_file(str(self._token_file), scopes=SCOPES)
            if not creds.valid:
                creds.refresh(Request())
                self._save(creds)
        except RefreshError as e:
            raise CalendarUnavailable(f"доступ к Google отозван или истёк, войдите заново: {e}") from e
        except (GoogleAuthError, ValueError, OSError) as e:
            raise CalendarUnavailable(f"не удалось получить доступ к Google: {e}") from e
        return creds

    def is_signed_in(self) -> bool:
        return self._token_file.is_file()

    def authorization_url(self) -> str:
        """Start a sign-in and return the Google page to send the user to."""
        if not self._redirect_uri:
            raise CalendarUnavailable("в настройках не задан google.redirect_uri")
        try:
            config = json.loads(self._client_secrets_file.read_text(encoding="utf-8"))
            flow = Flow.from_client_config(config, scopes=SCOPES)
        except (OSError, ValueError) as e:
            raise CalendarUnavailable(f"не удалось прочитать {self._client_secrets_file.name}: {e}") from e
        flow.redirect_uri = self._redirect_uri
        url, state = flow.authorization_url(
            access_type="offline", include_granted_scopes="true", prompt="consent"
        )
        self._pending = {state: flow}
        return url

    def finish_sign_in(self, state: str, code: str) -> None:
        """Finish the sign-in with the state and code Google passed to the redirect URI."""
        flow = self._pending.pop(state, None)
        if flow is None:
            raise CalendarUnavailable("вход в Google не был начат из этой админки или уже завершён")
        try:
            flow.fetch_token(code=code)
        except Exception as e:
            raise CalendarUnavailable(f"Google не выдал токен: {e}") from e
        self._save(flow.credentials)

    def sign_out(self) -> None:
        self._token_file.unlink(missing_ok=True)

    def _save(self, creds: Credentials) -> None:
        tmp = self._token_file.with_suffix(".tmp")
        tmp.write_text(creds.to_json(), encoding="utf-8")
        tmp.replace(self._token_file)
        logger.info("Saved Google token to %s", self._token_file)
