"""Job-ad sources. Each returns normalised ads (dicts in the silver schema) plus raw payloads."""

from __future__ import annotations

import time

import requests

from .. import PRODUCER, __version__

USER_AGENT = f"jobradar/{__version__} (+{PRODUCER}; research POC)"
RETRY_STATUS = (429, 500, 502, 503, 504)


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json"
    return s


def _wait_seconds(resp: requests.Response | None, attempt: int) -> float:
    """Honour Retry-After when the server sends it, otherwise back off 2, 4, 8, 16... seconds (max 60)."""
    retry_after = resp.headers.get("Retry-After") if resp is not None else None
    if retry_after and retry_after.isdigit():
        return min(float(retry_after), 120.0)
    return min(2.0 ** (attempt + 1), 60.0)


def get_json(session: requests.Session, url: str, *, params=None, headers=None, retries: int = 5,
             pause: float = 0.5):
    """GET with polite pacing. Retries 429/5xx and dropped connections with backoff; `pause` spaces requests."""
    for attempt in range(retries):
        last = attempt == retries - 1
        try:
            resp = session.get(url, params=params, headers=headers, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if last:
                raise
            time.sleep(_wait_seconds(None, attempt))  # e.g. RemoteDisconnected: the server hung up on us
            continue
        if resp.status_code in RETRY_STATUS and not last:
            time.sleep(_wait_seconds(resp, attempt))
            continue
        resp.raise_for_status()
        time.sleep(pause)
        return resp.json()
    return None
