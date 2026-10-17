"""JobTech JobSearch: the Swedish Public Employment Service (Arbetsförmedlingen, Platsbanken) open job-ad API.

No account or key. Search:  GET {url}/search?q=<keyword>&limit=100&offset=<n>&published-after=<ISO datetime>
Response: {"total": {"value": N}, "hits": [{"id", "headline", "description": {"text", "text_formatted"},
           "employer": {"name"}, "workplace_address": {"municipality", "region", "country"},
           "employment_type": {"label"}, "working_hours_type": {"label"}, "duration": {"label"},
           "salary_description", "occupation": {"label"}, "must_have": {"skills", "languages"},
           "nice_to_have": {"skills", "languages"}, "publication_date", "webpage_url", "removed"}]}

Ads are full text, but mostly in Swedish. `must_have.languages` is structured, so when the ad lists Swedish
as a requirement we record that as coming from the source rather than guessing it.
Fields are read defensively. We keep no employer contact data: only the fields mapped below.
"""

from __future__ import annotations

import warnings
from datetime import UTC, datetime, timedelta

import requests

from ..privacy import html_to_text
from . import get_json, http_session

SOURCE = "jobtech"
NAMESPACE = "https://jobsearch.api.jobtechdev.se"
PAGE_LIMIT = 100       # the API's maximum page size
MAX_OFFSET = 2000      # the API does not page beyond this offset
SWEDISH = {"svenska", "swedish"}


def _labels(items) -> list[str]:
    return [str(i.get("label")) for i in (items or []) if isinstance(i, dict) and i.get("label")]


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _language_from_source(hit: dict) -> str | None:
    """Structured language requirements: Swedish listed as must-have -> yes, as nice-to-have -> nice_to_have."""
    must = {x.lower() for x in _labels((hit.get("must_have") or {}).get("languages"))}
    nice = {x.lower() for x in _labels((hit.get("nice_to_have") or {}).get("languages"))}
    if must & SWEDISH:
        return "yes"
    if nice & SWEDISH:
        return "nice_to_have"
    return None


def normalise(hit: dict) -> dict:
    desc = hit.get("description") or {}
    text = desc.get("text") or html_to_text(desc.get("text_formatted"))
    parts = [text or ""]
    if hit.get("salary_description"):
        parts.append(f"Lön: {hit['salary_description']}")
    place = hit.get("workplace_address") or {}
    employer = hit.get("employer") or {}
    skills = _labels((hit.get("must_have") or {}).get("skills"))
    occupation = (hit.get("occupation") or {}).get("label")
    job_types = [lbl for lbl in (
        (hit.get("employment_type") or {}).get("label"),
        (hit.get("working_hours_type") or {}).get("label"),
        (hit.get("duration") or {}).get("label"),
    ) if lbl]
    return {
        "source": SOURCE,
        "source_id": str(hit.get("id")),
        "title": (hit.get("headline") or "").strip(),
        "company": (employer.get("name") or "").strip(),
        "location": ", ".join(x for x in (place.get("municipality") or place.get("city"), place.get("region")) if x),
        "country": "SE",
        "remote_source": None,
        "visa_source": None,
        "job_types": job_types,
        "tags": ([occupation] if occupation else []) + skills,
        "description_raw": "\n".join(p for p in parts if p),
        "url": hit.get("webpage_url") or f"https://arbetsformedlingen.se/platsbanken/annonser/{hit.get('id')}",
        "posted_at": _parse_ts(hit.get("publication_date")),
        "local_language_source": _language_from_source(hit),
    }


def fetch(config: dict, days: int) -> tuple[list[dict], list[dict]]:
    session = http_session()
    url = config.get("url", NAMESPACE).rstrip("/")
    pause = float(config.get("request_pause", 0.3))
    published_after = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
    max_pages = int(config.get("max_pages_per_keyword", 3))
    seen: dict[str, dict] = {}
    failures = 0
    for keyword in config.get("keywords", []):
        for page in range(max_pages):
            offset = page * PAGE_LIMIT
            if offset >= MAX_OFFSET:
                break
            try:
                payload = get_json(session, f"{url}/search", pause=pause, params={
                    "q": keyword, "limit": PAGE_LIMIT, "offset": offset, "published-after": published_after,
                }) or {}
            except requests.RequestException as exc:
                failures += 1
                warnings.warn(f"jobtech: '{keyword}' offset {offset} failed ({type(exc).__name__}); skipping",
                              RuntimeWarning, stacklevel=2)
                break
            hits = payload.get("hits") or []
            for hit in hits:
                if hit.get("id") and not hit.get("removed"):
                    seen.setdefault(str(hit["id"]), hit)
            if len(hits) < PAGE_LIMIT:
                break
    if not seen and failures:
        raise RuntimeError(f"JobTech answered with errors for {failures} requests and returned no ads")
    ads = [normalise(h) for h in seen.values()]
    raw = [{"source": SOURCE, "source_id": str(h["id"]), "payload": h} for h in seen.values()]
    return ads, raw
