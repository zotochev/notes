"""Rendering of an EventSpec into a Google Calendar event body."""
from __future__ import annotations

import html
import json
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any

from notificator.core.model import EventSpec


def render_body(spec: EventSpec, path: str, link: str | None) -> dict[str, Any]:
    """Build the event resource. Absent optional fields are sent explicitly so that a patch clears them."""
    return {
        "summary": spec.summary,
        "description": _description(spec, path, link),
        "start": {"dateTime": spec.start.isoformat(), "timeZone": spec.time_zone},
        "end": {"dateTime": spec.end.isoformat(), "timeZone": spec.time_zone},
        "location": spec.location,
        "attendees": [{"email": a} for a in spec.attendees],
        "recurrence": [spec.recurrence] if spec.recurrence else [],
    }


def fingerprint(body: dict[str, Any]) -> str:
    """Stable hash of a body: equal fingerprints mean the calendar needs no update."""
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return sha256(canonical.encode("utf-8")).hexdigest()


def _description(spec: EventSpec, path: str, link: str | None) -> str:
    lines = [f"uid: {spec.uid}", f"file: {path}"]
    if link:
        # Google Calendar renders a small subset of HTML in descriptions, including <a>.
        name = html.escape(PurePosixPath(path).name)
        lines.append(f'link: <a href="{html.escape(link, quote=True)}">{name}</a>')
    return "\n".join(lines) + "\n---\n" + (spec.description or "")
