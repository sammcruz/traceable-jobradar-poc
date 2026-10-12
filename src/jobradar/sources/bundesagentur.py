"""Job search API of the German Federal Employment Agency (Bundesagentur für Arbeit).

Public API; the `X-API-Key: jobboerse-jobsuche` header is the documented public client id.
Search:  GET {url}?was=<keyword>&veroeffentlichtseit=<days>&size=..&page=..&angebotsart=1
         -> {"stellenangebote": [{"refnr", "titel", "beruf", "arbeitgeber",
              "arbeitsort": {"ort", "region", "land", ...}, "aktuelleVeroeffentlichungsdatum", ...}]}
Details: GET {details_url}/<base64(refnr)> -> a description field (name varies by API version,
         read defensively). If details fail, title + occupation are used as the description.

We never keep employer contact data (addresses, contact persons): only the fields mapped below.
"""

from __future__ import annotations

import base64
from datetime import UTC, date, datetime

import requests

from ..privacy import html_to_text
from . import get_json, http_session

SOURCE = "bundesagentur"
NAMESPACE = "https://rest.arbeitsagentur.de"
DESCRIPTION_KEYS = ("stellenangebotsBeschreibung", "stellenbeschreibung", "stellenBeschreibung",
                    "beschreibung")


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.combine(date.fromisoformat(value[:10]), datetime.min.time(), UTC)
    except ValueError:
        return None


def normalise(item: dict, details: dict | None) -> dict:
    refnr = str(item.get("refnr"))
    place = item.get("arbeitsort") or {}
    details = details or {}
    description = next((details[k] for k in DESCRIPTION_KEYS if details.get(k)), "")
    if not description:
        description = f"{item.get('titel', '')}\n{item.get('beruf', '')}"
    job_types = []
    for key in ("arbeitszeitmodelle", "befristung", "vertragsdauer"):
        val = details.get(key)
        if isinstance(val, list):
            job_types += [str(v) for v in val]
        elif val:
            job_types.append(str(val))
    return {
        "source": SOURCE,
        "source_id": refnr,
        "title": (item.get("titel") or item.get("beruf") or "").strip(),
        "company": (item.get("arbeitgeber") or "").strip(),
        "location": ", ".join(x for x in (place.get("ort"), place.get("region")) if x),
        "country": "DE" if (place.get("land") or "Deutschland") == "Deutschland" else str(place.get("land")),
        "remote_source": None,
        "visa_source": None,
        "job_types": job_types,
        "tags": [str(item["beruf"])] if item.get("beruf") else [],
        "description_raw": html_to_text(description),
        "url": item.get("externeUrl") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
        "posted_at": _parse_date(item.get("aktuelleVeroeffentlichungsdatum")),
    }


def fetch(config: dict, days: int) -> tuple[list[dict], list[dict]]:
    session = http_session()
    headers = {"X-API-Key": config.get("api_key", "jobboerse-jobsuche")}
    seen: dict[str, dict] = {}
    for keyword in config.get("keywords", []):
        for page in range(1, int(config.get("max_pages_per_keyword", 2)) + 1):
            payload = get_json(session, config["url"], headers=headers, params={
                "was": keyword, "veroeffentlichtseit": days, "angebotsart": 1,
                "size": config.get("page_size", 50), "page": page,
            }) or {}
            items = payload.get("stellenangebote") or []
            for item in items:
                if item.get("refnr"):
                    seen.setdefault(str(item["refnr"]), item)
            if len(items) < int(config.get("page_size", 50)):
                break

    ads, raw = [], []
    for refnr, item in seen.items():
        details = None
        if config.get("fetch_details", True):
            encoded = base64.b64encode(refnr.encode()).decode()
            try:
                details = get_json(session, f"{config['details_url']}/{encoded}", headers=headers, retries=2)
            except requests.RequestException:
                details = None
        ads.append(normalise(item, details))
        raw.append({"source": SOURCE, "source_id": refnr, "payload": {"search": item, "details": details}})
    return ads, raw
