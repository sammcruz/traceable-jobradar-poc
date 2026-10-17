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


# ---- JobTech ----

JOBTECH_HIT = {
    "id": "29123456", "headline": "Data Engineer", "removed": False,
    "description": {"text": "Vi söker en data engineer. Kontakt: Anna Svensson anna@example.se"},
    "employer": {"name": "Exempel AB", "email": "hr@example.se", "phone_number": "08-123456"},
    "workplace_address": {"municipality": "Stockholm", "region": "Stockholms län", "country": "Sverige"},
    "employment_type": {"label": "Vanlig anställning"}, "working_hours_type": {"label": "Heltid"},
    "salary_description": "Fast månadslön",
    "occupation": {"label": "Systemutvecklare"},
    "must_have": {"skills": [{"label": "SQL"}], "languages": [{"label": "Svenska"}]},
    "nice_to_have": {"languages": [{"label": "Engelska"}]},
    "publication_date": "2026-10-14T08:11:00", "webpage_url": "https://arbetsformedlingen.se/platsbanken/annonser/29123456",
}


def test_jobtech_normalise_and_structured_language():
    from jobradar.sources import jobtech

    ad = jobtech.normalise(JOBTECH_HIT)
    assert ad["source_id"] == "29123456" and ad["country"] == "SE" and ad["company"] == "Exempel AB"
    assert ad["location"] == "Stockholm, Stockholms län" and ad["posted_at"].year == 2026
    assert "SQL" in ad["tags"] and "Heltid" in ad["job_types"]
    assert ad["local_language_source"] == "yes"  # Swedish listed as a requirement by the source


def test_jobtech_fetch_pages_and_drops_removed():
    from jobradar.sources import jobtech

    removed = {**JOBTECH_HIT, "id": "2", "removed": True}
    session = FakeSession([FakeResp(200, {"hits": [JOBTECH_HIT, removed]})])
    import jobradar.sources.jobtech as mod

    mod.http_session = lambda: session
    ads, raw = jobtech.fetch({"keywords": ["data engineer"], "request_pause": 0}, days=7)
    assert [a["source_id"] for a in ads] == ["29123456"] and raw[0]["payload"]["id"] == "29123456"


# ---- Adzuna ----

ADZUNA_ITEM = {
    "id": "5012345", "title": "Data Engineer", "description": "Join our <b>data</b> team in Amsterdam.",
    "created": "2026-10-14T09:47:21Z", "redirect_url": "https://www.adzuna.nl/land/ad/5012345",
    "company": {"display_name": "Voorbeeld BV"}, "location": {"display_name": "Amsterdam, Noord-Holland"},
    "category": {"label": "IT Jobs"}, "salary_min": 60000, "salary_max": 80000, "salary_is_predicted": "0",
    "contract_type": "permanent", "contract_time": "full_time",
}


def test_adzuna_normalise_uses_stated_salary_but_not_predicted():
    from jobradar import rules
    from jobradar.sources import adzuna

    ad = adzuna.normalise(ADZUNA_ITEM, "nl")
    assert ad["country"] == "NL" and ad["company"] == "Voorbeeld BV" and ad["url"].endswith("5012345")
    s = rules.salary(ad["description_raw"])
    assert (s["salary_min"], s["salary_max"]) == (60000, 80000)
    assert rules.contract_type(ad["description_raw"], ad["job_types"])[0] == "permanent"
    assert rules.work_time(ad["description_raw"], ad["job_types"])[0] == "full_time"
    predicted = adzuna.normalise({**ADZUNA_ITEM, "salary_is_predicted": "1"}, "nl")
    assert rules.salary(predicted["description_raw"]) is None


def test_adzuna_needs_credentials(monkeypatch):
    from jobradar.sources import adzuna

    monkeypatch.delenv("ADZUNA_APP_ID", raising=False)
    monkeypatch.delenv("ADZUNA_APP_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ADZUNA_APP_ID"):
        adzuna.fetch({}, days=7)


def test_adzuna_respects_call_budget_and_never_leaks_the_key(monkeypatch):
    import jobradar.sources.adzuna as mod

    monkeypatch.setenv("ADZUNA_APP_ID", "id123")
    monkeypatch.setenv("ADZUNA_APP_KEY", "SECRETKEY")
    session = FakeSession([FakeResp(200, {"results": [ADZUNA_ITEM]})])
    monkeypatch.setattr(mod, "http_session", lambda: session)
    cfg = {"countries": ["nl"], "keywords": ["a", "b", "c", "d"], "max_calls": 2, "request_pause": 0,
           "results_per_page": 1}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        ads, _ = mod.fetch(cfg, days=7)
    assert session.calls == 2 and len(ads) == 1
    assert any("max_calls" in str(w.message) for w in caught)

    # A rejected key raises an error that does not contain the key or the URL
    bad = FakeResp(403)
    monkeypatch.setattr(mod, "http_session", lambda: FakeSession([bad]))
    with pytest.raises(RuntimeError) as err:
        mod.fetch({"countries": ["nl"], "keywords": ["a"], "request_pause": 0}, days=7)
    assert "SECRETKEY" not in str(err.value) and "http" not in str(err.value).lower().replace("http 403", "")

    # A dropped connection is reported without the URL as well
    err_conn = requests.ConnectionError("Max retries exceeded with url: /nl/search/1?app_key=SECRETKEY")
    monkeypatch.setattr(mod, "http_session", lambda: FakeSession([err_conn]))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        mod.fetch({"countries": ["nl"], "keywords": ["a"], "request_pause": 0}, days=7)
    assert caught and all("SECRETKEY" not in str(w.message) for w in caught)
