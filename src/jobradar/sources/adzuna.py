"""Adzuna job-search API (free key, register at https://developer.adzuna.com/signup).

Credentials come from the environment (.env, which is git-ignored): ADZUNA_APP_ID and ADZUNA_APP_KEY.
Search: GET {url}/{country}/search/{page}?app_id=..&app_key=..&what=<keyword>&max_days_old=<n>&sort_by=date
Response: {"count": N, "results": [{"id", "title", "description" (an excerpt), "created", "redirect_url",
           "company": {"display_name"}, "location": {"display_name", "area"}, "category": {"label"},
           "salary_min", "salary_max", "salary_is_predicted", "contract_type", "contract_time"}]}

Things to know:
- Descriptions are excerpts, so rules that read the full ad (language, visa, remote) will see less than for other sources.
- Salaries flagged as predicted are Adzuna's estimate, not the employer's: we do not use them.
- The query string carries the key, so error messages are rebuilt without the URL. The key never reaches
  the terminal, the lineage log or Marquez.
- Calls are budgeted per run (`max_calls`): a new key starts on a small test plan.
"""

from __future__ import annotations

import os
import warnings
from datetime import UTC, datetime

import requests

from ..privacy import html_to_text
from . import get_json, http_session


class _BudgetOrQuotaReached(Exception):
    """Internal: stop fetching, keep what we have."""


SOURCE = "adzuna"
NAMESPACE = "https://api.adzuna.com"
EURO_COUNTRIES = {"nl", "de", "fr", "it", "at", "be", "es", "ie"}  # salary text is only written for EUR


def credentials() -> tuple[str, str]:
    app_id, app_key = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
    if not app_id or not app_key:
        raise RuntimeError("Adzuna needs ADZUNA_APP_ID and ADZUNA_APP_KEY in your .env "
                           "(free key: https://developer.adzuna.com/signup)")
    return app_id, app_key


def _safe(exc: requests.RequestException) -> str:
    """Describe an error without the URL (which contains the key)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return f"{type(exc).__name__}" + (f", HTTP {status}" if status else "")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _salary_line(item: dict, country: str) -> str:
    lo, hi = item.get("salary_min"), item.get("salary_max")
    if not (lo or hi) or str(item.get("salary_is_predicted")) == "1" or country.lower() not in EURO_COUNTRIES:
        return ""
    fmt = lambda v: f"{int(v):,}".replace(",", ".")  # noqa: E731
    amount = f"{fmt(lo)} - {fmt(hi)} €" if lo and hi and lo != hi else f"{fmt(lo or hi)} €"
    return f"Salary: {amount} per year"


def normalise(item: dict, country: str) -> dict:
    description = html_to_text(item.get("description") or "")
    salary = _salary_line(item, country)
    if salary:
        description = f"{description}\n{salary}"
    job_types = [str(item[k]).replace("_", "-") for k in ("contract_type", "contract_time") if item.get(k)]
    category = (item.get("category") or {}).get("label")
    return {
        "source": SOURCE,
        "source_id": str(item.get("id")),
        "title": (item.get("title") or "").strip(),
        "company": ((item.get("company") or {}).get("display_name") or "").strip(),
        "location": ((item.get("location") or {}).get("display_name") or "").strip(),
        "country": country.upper(),
        "remote_source": None,
        "visa_source": None,
        "job_types": job_types,
        "tags": [category] if category else [],
        "description_raw": description,
        "url": item.get("redirect_url") or "",
        "posted_at": _parse_ts(item.get("created")),
    }


def fetch(config: dict, days: int) -> tuple[list[dict], list[dict]]:
    app_id, app_key = credentials()
    session = http_session()
    url = config.get("url", f"{NAMESPACE}/v1/api/jobs").rstrip("/")
    pause = float(config.get("request_pause", 2.5))  # the free plan allows about 25 calls a minute
    budget = int(config.get("max_calls", 20))
    per_page = int(config.get("results_per_page", 50))
    pages = int(config.get("max_pages_per_keyword", 1))
    seen: dict[str, tuple[dict, str]] = {}
    calls = 0
    try:
        for country in config.get("countries", ["nl"]):
            for keyword in config.get("keywords", []):
                for page in range(1, pages + 1):
                    if calls >= budget:
                        warnings.warn(f"adzuna: stopped after {budget} calls (max_calls in config/sources.yml)",
                                      RuntimeWarning, stacklevel=2)
                        raise _BudgetOrQuotaReached
                    calls += 1
                    try:
                        payload = get_json(session, f"{url}/{country}/search/{page}", retries=2, pause=pause, params={
                            "app_id": app_id, "app_key": app_key, "what": keyword, "max_days_old": days,
                            "results_per_page": per_page, "sort_by": "date", "content-type": "application/json",
                        }) or {}
                    except requests.RequestException as exc:
                        status = getattr(getattr(exc, "response", None), "status_code", None)
                        if status in (401, 403):
                            raise RuntimeError(
                                f"Adzuna rejected the credentials (HTTP {status}). Check ADZUNA_APP_ID and "
                                "ADZUNA_APP_KEY in .env; a new key can take a while to activate.") from None
                        warnings.warn(f"adzuna: '{keyword}' ({country}) failed ({_safe(exc)}); "
                                      "keeping what was fetched", RuntimeWarning, stacklevel=2)
                        if status == 429:
                            raise _BudgetOrQuotaReached from None  # over quota: stop, keep the partial result
                        break
                    results = payload.get("results") or []
                    for item in results:
                        if item.get("id"):
                            seen.setdefault(str(item["id"]), (item, country))
                    if len(results) < per_page:
                        break
    except _BudgetOrQuotaReached:
        pass
    ads = [normalise(item, country) for item, country in seen.values()]
    raw = [{"source": SOURCE, "source_id": str(item["id"]), "payload": item} for item, _ in seen.values()]
    return ads, raw
