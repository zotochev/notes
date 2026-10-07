"""HTTP requests with retries for file sources."""
from __future__ import annotations

import time

import requests

from notificator.sync.ports import SourceError

_RETRY_STATUSES = (502, 503, 504)
DEFAULT_RETRY_DELAYS = (0.5, 2.0)


def send(method: str, url: str, retry_delays: tuple[float, ...] = DEFAULT_RETRY_DELAYS, **kwargs) -> requests.Response:
    """Send a request, retrying network failures and 502/503/504.

    Returns the last response whatever its status; raises SourceError only
    when no response could be obtained at all.
    """
    for delay in (*retry_delays, None):
        try:
            response = requests.request(method, url, **kwargs)
        except requests.RequestException as e:
            if delay is None:
                raise SourceError(f"{method} {url.split('?')[0]}: {e}") from e
        else:
            if response.status_code not in _RETRY_STATUSES or delay is None:
                return response
        time.sleep(delay)
    raise AssertionError("unreachable")
