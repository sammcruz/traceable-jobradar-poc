"""Arbeitnow public job-board API (no key). Mostly Germany and remote roles.

Response shape (as used here; fields read defensively):
  {"data": [{"slug", "company_name", "title", "description" (HTML), "remote" (bool),
             "url", "tags" [..], "job_types" [..], "location", "created_at" (unix)}],
   "links": {"next": ...}, "meta": {...}}
The `visa_sponsorship=true` filter is documented; we use it to mark which ads offer sponsorship.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ..privacy import html_to_text
from . import get_json, http_session

SOURCE = "arbeitnow"
NAMESPACE = "https://www.arbeitnow.com"


def _pages(session, url: str, params: dict, cutoff: datetime, max_pages: int):
    for page in range(1, max_pages + 1):
        payload = get_json(session, url, params={**params, "page": page}) or {}
        items = payload.get("data") or []
        if not items:
            return
        yield items
        oldest = min((i.get("created_at") or 0) for i in items)
        if oldest and datetime.fromtimestamp(oldest, UTC) < cutoff:
            return
        if not (payload.get("links") or {}).get("next"):
            return


def normalise(item: dict, visa_slugs: set[str]) -> dict:
    created = item.get("created_at")
    posted = datetime.fromtimestamp(created, UTC) if created else None
    slug = str(item.get("slug") or item.get("url"))
    return {
        "source": SOURCE,
        "source_id": slug,
        "title": (item.get("title") or "").strip(),
        "company": (item.get("company_name") or "").strip(),
        "location": (item.get("location") or "").strip(),
        "country": "DE",
        "remote_source": item.get("remote"),
        "visa_source": True if slug in visa_slugs else None,
        "job_types": [str(t) for t in item.get("job_types") or []],
        "tags": [str(t) for t in item.get("tags") or []],
        "description_raw": html_to_text(item.get("description")),
        "url": item.get("url") or "",
        "posted_at": posted,
    }


def fetch(config: dict, days: int) -> tuple[list[dict], list[dict]]:
    """Returns (normalised ads, raw payloads) for ads published in the last `days`."""
    session = http_session()
    cutoff = datetime.now(UTC) - timedelta(days=days)
    url, max_pages = config["url"], int(config.get("max_pages", 40))

    visa_slugs: set[str] = set()
    for items in _pages(session, url, {"visa_sponsorship": "true"}, cutoff, max_pages):
        visa_slugs.update(str(i.get("slug")) for i in items)

    ads, raw = [], []
    for items in _pages(session, url, {}, cutoff, max_pages):
        for item in items:
            ad = normalise(item, visa_slugs)
            if ad["posted_at"] and ad["posted_at"] >= cutoff:
                ads.append(ad)
                raw.append({"source": SOURCE, "source_id": ad["source_id"], "payload": item})
    return ads, raw
