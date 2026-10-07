"""HTTP requests with retries for file sources."""
from __future__ import annotations

import threading
import time

import requests

from notificator.sync.ports import SourceError

_RETRY_STATUSES = (502, 503, 504)
DEFAULT_RETRY_DELAYS = (0.5, 2.0)
_local = threading.local()


def _session() -> requests.Session:
    """One session per thread: connections are kept open between requests, and
    a session is not shared between threads."""
    session = getattr(_local, "session", None)
    if session is None:
        session = _local.session = requests.Session()
    return session


def send(method: str, url: str, retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS, **kwargs) -> requests.Response:
    """Send a request, retrying network failures and 502/503/504.

    Returns the last response whatever its status; raises SourceError only
    when no response could be obtained at all.
    """
    for delay in (*retry_delays, None):
        try:
            response = _session().request(method, url, **kwargs)
        except requests.RequestException as e:
            if delay is None:
                raise SourceError(f"{method} {url.split('?')[0]}: {e}") from e
        else:
            if response.status_code not in _RETRY_STATUSES or delay is None:
                return response
        time.sleep(delay)
    raise AssertionError("unreachable")
