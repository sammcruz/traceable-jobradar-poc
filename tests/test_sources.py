import warnings

import pytest
import requests

from jobradar import sources
from jobradar.sources import arbeitnow


class FakeResp:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code, self._payload, self.headers = status, payload or {}, headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Client Error", response=self)

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, script):
        self.script, self.calls = list(script), 0

    def get(self, *a, **k):
        self.calls += 1
        item = self.script.pop(0) if self.script else self.last
        self.last = item
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(sources.time, "sleep", lambda s: None)


def test_get_json_retries_429_and_dropped_connections():
    session = FakeSession([FakeResp(429, headers={"Retry-After": "1"}),
                           requests.ConnectionError("RemoteDisconnected"), FakeResp(200, {"ok": 1})])
    assert sources.get_json(session, "http://x") == {"ok": 1}
    assert session.calls == 3


def test_get_json_gives_up_after_retries():
    session = FakeSession([FakeResp(429)])
    with pytest.raises(requests.HTTPError):
        sources.get_json(session, "http://x", retries=3)
    assert session.calls == 3


def test_arbeitnow_keeps_pages_fetched_before_rate_limit():
    from datetime import UTC, datetime, timedelta

    now = int(datetime.now(UTC).timestamp())
    page1 = FakeResp(200, {"data": [{"slug": "a", "created_at": now}], "links": {"next": "x"}})
    session = FakeSession([page1, FakeResp(429)])
    cutoff = datetime.now(UTC) - timedelta(days=7)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        pages = list(arbeitnow._pages(session, "http://x", {}, cutoff, max_pages=5, pause=0))
    assert len(pages) == 1 and pages[0][0]["slug"] == "a"
    assert any("keeping the 1 pages" in str(w.message) for w in caught)
