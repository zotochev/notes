"""
Admin API. Handlers only read the database and send signals to the service;
they never touch the sync state directly.

Authentication is expected to be done by a reverse proxy in front of this app.
"""
from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel

from notificator.calendars.google import GoogleCalendar
from notificator.config import STATE_FILE, Config
from notificator.core.parsing import ParseContext, parse_text
from notificator.service import SyncService
from notificator.store import Store
from notificator.sync.ports import CalendarError, EventNotFound
from notificator.wiring import google_auth

_INDEX = Path(__file__).parent / "index.html"
_SECRET_KEYS = ("password",)


class _Text(BaseModel):
    text: str


def create_app(
    service: SyncService,
    config: Config,
    data_dir: Path,
    start_service: bool = True,
    calendar_factory: Callable[[], Any] | None = None,
) -> FastAPI:
    """`calendar_factory` replaces Google Calendar in tests."""
    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_service:
            service.start()
        yield
        if start_service:
            service.stop()

    app = FastAPI(title="Notificator", lifespan=lifespan)
    auth = google_auth(config, data_dir)
    google = calendar_factory or (lambda: GoogleCalendar(auth.credentials()))
    source = service.source_name

    def store() -> Store:
        return Store(data_dir / STATE_FILE)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_INDEX, media_type="text/html; charset=utf-8")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        with store() as db:
            last = next(iter(db.cycles(1)), None)
            issues = db.issues()
            events = db.events(source)
            counts_by_source = db.event_counts()
        held = sum(i.kind == "held" for i in issues)
        if last is None:
            state = "starting"
        elif last["error"]:
            state = "error"
        elif issues:
            state = "attention"
        else:
            state = "ok"
        return {
            "source": source,
            "sourceType": config.sources[source].type,
            "state": state,
            "running": service.running,
            "progress": service.progress,
            "secondsUntilNextCycle": service.seconds_until_next_cycle,
            "lastCycle": last,
            "counts": {
                "events": len(events),
                "files": len({e["path"] for e in events}),
                "unsynced": sum(not e["synced"] for e in events),
                "issues": len(issues) - held,
                "heldDeletes": held,
            },
            # Events of sources that are no longer active: they are being removed from the calendar.
            "inactiveSources": {name: n for name, n in counts_by_source.items() if name != source},
            "googleSignedIn": auth.is_signed_in(),
            "defaultCalendar": config.default_calendar,
        }

    @app.get("/api/events")
    def events() -> list[dict[str, Any]]:
        with store() as db:
            return db.events(source)

    @app.get("/api/events/google")
    def event_in_google(path: str, uid: str) -> dict[str, Any]:
        """The event as Google Calendar has it right now, for comparing with the file."""
        with store() as db:
            tracked = next((e for e in db.events(source) if e["path"] == path and e["uid"] == uid), None)
        if tracked is None:
            raise HTTPException(status_code=404, detail="Это событие не отслеживается")
        try:
            remote = google().get(tracked["calendar_id"], tracked["gcal_event_id"])
        except EventNotFound:
            raise HTTPException(status_code=404, detail="События нет в Google Calendar") from None
        except CalendarError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e
        return {**remote, "calendarId": tracked["calendar_id"], "synced": tracked["synced"]}

    @app.get("/api/issues")
    def issues() -> list[dict[str, Any]]:
        with store() as db:
            return [asdict(i) for i in db.issues()]

    @app.get("/api/journal")
    def journal(limit: int = 200) -> list[dict[str, Any]]:
        with store() as db:
            return [asdict(e) for e in db.journal(min(limit, 1000))]

    @app.get("/api/cycles")
    def cycles() -> list[dict[str, Any]]:
        with store() as db:
            return db.cycles(30)

    @app.post("/api/sync")
    def sync_now() -> dict[str, bool]:
        service.trigger()
        return {"ok": True}

    @app.post("/api/deletions/approve")
    def approve_deletions() -> dict[str, bool]:
        service.approve_deletions()
        return {"ok": True}

    @app.get("/api/config")
    def shown_config() -> dict[str, Any]:
        return _masked(config.model_dump(mode="json"))

    @app.post("/api/validate")
    def validate(body: _Text) -> dict[str, Any]:
        """Check text with <event> blocks the way a sync cycle would."""
        tz = ZoneInfo(config.time_zone)
        parsed = parse_text(body.text, ParseContext(default_tz=tz, now=datetime.now(tz)))
        return {
            "events": [
                {**asdict(e), "start": e.start.isoformat(), "end": e.end.isoformat()} for e in parsed.events
            ],
            "issues": [{"uid": i.uid, "message": i.message} for i in parsed.issues],
        }

    @app.get("/api/calendars")
    def calendars() -> list[dict[str, Any]]:
        try:
            return google().writable_calendars()
        except CalendarError as e:
            raise HTTPException(status_code=503, detail=str(e)) from e

    @app.get("/google/login", include_in_schema=False)
    def google_login() -> RedirectResponse:
        try:
            return RedirectResponse(auth.authorization_url())
        except CalendarError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @app.get("/google/oauth/callback", include_in_schema=False)
    def google_callback(state: str, code: str) -> RedirectResponse:
        try:
            auth.finish_sign_in(state, code)
        except CalendarError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        service.trigger()
        return RedirectResponse("/")

    @app.post("/api/google/logout")
    def google_logout() -> dict[str, bool]:
        auth.sign_out()
        return {"ok": True}

    return app


def _masked(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: ("***" if k in _SECRET_KEYS and v else _masked(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [_masked(v) for v in value]
    return value
