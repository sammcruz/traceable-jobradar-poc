"""Job-ad sources. Each returns normalised ads (dicts in the silver schema) plus raw payloads."""

from __future__ import annotations

import time

import requests

from .. import PRODUCER, __version__

USER_AGENT = f"jobradar/{__version__} (+{PRODUCER}; research POC)"


def http_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json"
    return s


def get_json(session: requests.Session, url: str, *, params=None, headers=None, retries: int = 3):
    """GET with polite pacing and simple retries on 429/5xx."""
    for attempt in range(retries):
        resp = session.get(url, params=params, headers=headers, timeout=30)
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(2 ** attempt)
            continue
        resp.raise_for_status()
        time.sleep(0.3)
        return resp.json()
    return None
