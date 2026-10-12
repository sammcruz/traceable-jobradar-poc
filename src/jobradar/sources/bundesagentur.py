"""Job search API of the German Federal Employment Agency (Bundesagentur für Arbeit).

Public API; the `X-API-Key: jobboerse-jobsuche` header is the documented public client id.
Search:  GET {url}?was=<keyword>&veroeffentlichtseit=<days>&size=..&page=..&angebotsart=1
         v6 -> {"ergebnisliste": [{"referenznummer", "stellenangebotsTitel", "hauptberuf", "firma",
                 "stellenlokationen": [{"adresse": {"ort", "region", "land"}}], "datumErsteVeroeffentlichung",
                 "gehaltsspanneVon", "gehaltsspanneBis", "verguetungsangabe", "arbeitszeitVollzeit", ...}]}
         v4 -> {"stellenangebote": [{"refnr", "titel", "beruf", "arbeitgeber", "arbeitsort", ...}]}
         Both shapes are read.
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


PERIODS = {"JAHRESGEHALT": "pro Jahr", "MONATSGEHALT": "pro Monat", "STUNDENLOHN": "pro Stunde"}


def ref_number(item: dict) -> str | None:
    """v4 calls it refnr, v6 referenznummer."""
    value = item.get("referenznummer") or item.get("refnr")
    return str(value) if value else None


def _salary_line(item: dict) -> str:
    """v6 gives the salary range as structured fields; render it as text the salary rule understands."""
    lo, hi = item.get("gehaltsspanneVon"), item.get("gehaltsspanneBis")
    if not lo and not hi:
        return ""
    fmt = lambda v: f"{int(v):,}".replace(",", ".")  # noqa: E731 - German thousands separator
    amount = f"{fmt(lo)} - {fmt(hi)} €" if lo and hi and lo != hi else f"{fmt(lo or hi)} €"
    return f"Gehalt: {amount} {PERIODS.get(item.get('verguetungsangabe'), '')}".strip()


def normalise(item: dict, details: dict | None) -> dict:
    refnr = ref_number(item) or ""
    details = details or {}
    # Location: v4 "arbeitsort", v6 first entry of "stellenlokationen[].adresse"
    place = item.get("arbeitsort") or ((item.get("stellenlokationen") or [{}])[0].get("adresse") or {})
    title = (item.get("stellenangebotsTitel") or item.get("titel") or item.get("hauptberuf")
             or item.get("beruf") or "").strip()
    occupation = item.get("hauptberuf") or item.get("beruf") or ""

    description = next((details[k] for k in DESCRIPTION_KEYS if details.get(k)), "")
    if not description:
        description = f"{title}\n{occupation}"
    salary = _salary_line(item)
    if salary:
        description = f"{description}\n{salary}"

    job_types = []
    for key in ("arbeitszeitmodelle", "befristung", "vertragsdauer"):
        val = details.get(key) or item.get(key)
        if isinstance(val, list):
            job_types += [str(v) for v in val]
        elif val and val != "KEINE_ANGABE":
            job_types.append(str(val))
    if item.get("arbeitszeitVollzeit") is True:
        job_types.append("VOLLZEIT")
    if item.get("arbeitszeitTeilzeit") is True:
        job_types.append("TEILZEIT")

    land = str(place.get("land") or "Deutschland")
    return {
        "source": SOURCE,
        "source_id": refnr,
        "title": title,
        "company": (item.get("firma") or item.get("arbeitgeber") or "").strip(),
        "location": ", ".join(x.title() if x.isupper() else x for x in (place.get("ort"), place.get("region")) if x),
        "country": "DE" if land.upper() == "DEUTSCHLAND" else land,
        "remote_source": None,
        "visa_source": None,
        "job_types": job_types,
        "tags": [str(b) for b in (item.get("alleBerufe") or ([occupation] if occupation else []))],
        "description_raw": html_to_text(description),
        "url": item.get("externeUrl") or f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{refnr}",
        "posted_at": _parse_date(item.get("datumErsteVeroeffentlichung")
                                 or (item.get("veroeffentlichungszeitraum") or {}).get("von")
                                 or item.get("aktuelleVeroeffentlichungsdatum")),
    }


ITEM_KEYS = ("ergebnisliste", "stellenangebote", "jobs", "items", "results", "content")


def _items(payload: dict) -> list[dict]:
    """The search response has used different top-level keys across API versions."""
    for key in ITEM_KEYS:
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _search_url(session, config: dict, headers: dict, days: int) -> str:
    """Use the first search endpoint that answers. The agency moves versions (v4 -> v6) without notice."""
    urls = config.get("search_urls") or [config["url"]]
    errors = []
    for url in urls:
        try:
            get_json(session, url, headers=headers, retries=1,
                     params={"was": "software", "veroeffentlichtseit": days, "angebotsart": 1, "size": 1, "page": 1})
            return url
        except requests.HTTPError as exc:
            errors.append(f"{url}: {exc.response.status_code}")
    raise RuntimeError("No Bundesagentur search endpoint answered: " + "; ".join(errors))


def fetch(config: dict, days: int) -> tuple[list[dict], list[dict]]:
    from ..config import get_settings

    session = http_session()
    key = config.get("api_key", "jobboerse-jobsuche")
    headers = {"X-API-Key": key}
    url = _search_url(session, config, headers, days)
    seen: dict[str, dict] = {}
    first_payload = None
    for keyword in config.get("keywords", []):
        for page in range(1, int(config.get("max_pages_per_keyword", 2)) + 1):
            payload = get_json(session, url, headers=headers, params={
                "was": keyword, "veroeffentlichtseit": days, "angebotsart": 1,
                "size": config.get("page_size", 50), "page": page,
            }) or {}
            first_payload = first_payload or payload
            items = _items(payload)
            for item in items:
                if ref_number(item):
                    seen.setdefault(ref_number(item), item)
            if len(items) < int(config.get("page_size", 50)):
                break

    if not seen and first_payload:
        # Unknown response shape: keep one sample so the parser can be adjusted
        debug = get_settings().data_dir / "debug" / "bundesagentur_search_sample.json"
        debug.parent.mkdir(parents=True, exist_ok=True)
        import json

        debug.write_text(json.dumps(first_payload, ensure_ascii=False, indent=1)[:200_000])
        raise RuntimeError(f"Bundesagentur answered from {url} but no ads were parsed. Sample saved to {debug}")

    ads, raw = [], []
    detail_failures, detail_successes = 0, 0
    for refnr, item in seen.items():
        details = None
        if config.get("fetch_details", True) and not (detail_failures >= 5 and detail_successes == 0):
            encoded = base64.b64encode(refnr.encode()).decode()
            for details_url in config.get("details_urls") or [config["details_url"]]:
                try:
                    details = get_json(session, f"{details_url}/{encoded}", headers=headers, retries=1)
                    break
                except requests.RequestException:
                    details = None
            if details:
                detail_successes += 1
            else:
                detail_failures += 1
        ads.append(normalise(item, details))
        raw.append({"source": SOURCE, "source_id": refnr, "payload": {"search": item, "details": details}})
    return ads, raw
