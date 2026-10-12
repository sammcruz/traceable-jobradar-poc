"""Ingest: sources -> bronze (scrubbed raw payloads) -> silver.jobs (one row per ad).

Only ads from the last N days are kept. `apply_retention` deletes anything older,
everywhere it was copied to, and vacuums so the deletes are physical.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pyarrow as pa

from . import lineage, storage
from .config import get_settings
from .privacy import redact, scrub_payload, sha256
from .sources import arbeitnow, bundesagentur

SOURCES: dict[str, tuple[Callable, str, str]] = {
    "arbeitnow": (arbeitnow.fetch, arbeitnow.NAMESPACE, "/api/job-board-api"),
    "bundesagentur": (bundesagentur.fetch, bundesagentur.NAMESPACE, "/jobboerse/jobsuche-service/jobs"),
}

TS = pa.timestamp("us", tz="UTC")
SILVER_SCHEMA = pa.schema([
    ("job_id", pa.string()), ("source", pa.string()), ("source_id", pa.string()),
    ("title", pa.string()), ("company", pa.string()), ("location", pa.string()), ("country", pa.string()),
    ("remote_source", pa.bool_()), ("visa_source", pa.bool_()),
    ("job_types", pa.list_(pa.string())), ("tags", pa.list_(pa.string())),
    ("description", pa.string()), ("language", pa.string()), ("redactions", pa.int32()),
    ("url", pa.string()), ("posted_at", TS), ("ingested_at", TS), ("content_hash", pa.string()),
])
BRONZE_SCHEMA = pa.schema([
    ("raw_id", pa.string()), ("source", pa.string()), ("source_id", pa.string()),
    ("fetched_at", TS), ("payload_json", pa.string()),
])

_DE = re.compile(r"\b(und|der|die|das|wir|sie|mit|für|ist|nicht|ihre|deine|bei|auf)\b", re.IGNORECASE)
_EN = re.compile(r"\b(and|the|with|you|we|our|for|are|your|will|this|of)\b", re.IGNORECASE)


def detect_language(text: str) -> str:
    de, en = len(_DE.findall(text)), len(_EN.findall(text))
    if de == en == 0:
        return "unknown"
    return "de" if de > en else "en"


def to_silver(ad: dict, now: datetime) -> dict:
    description, n = redact(ad.get("description_raw") or "")
    title, n_title = redact(ad.get("title") or "")
    row = {k: ad.get(k) for k in SILVER_SCHEMA.names if k in ad}
    row.update({
        "job_id": f'{ad["source"]}:{ad["source_id"]}',
        "title": title,
        "description": description,
        "language": detect_language(f"{title}\n{description}"),
        "redactions": n + n_title,
        "ingested_at": now,
        "content_hash": sha256(f'{title}\n{ad.get("company", "")}\n{description}'),
    })
    return row


def ingest(source: str, fetched: tuple[list[dict], list[dict]] | None = None, days: int | None = None) -> dict:
    """Fetch one source (or use pre-fetched ads, e.g. test fixtures) and land it in bronze + silver."""
    settings = get_settings()
    days = days or int(settings.sources.get("days", 7))
    fetch, namespace, endpoint = SOURCES[source]
    now = datetime.now(UTC)

    with lineage.run(f"ingest.{source}", f"Fetch {source} job ads from the last {days} days") as run:
        run.inputs = [lineage.external_dataset(namespace, endpoint)]
        ads, raw = fetched if fetched is not None else fetch(settings.sources[source], days)

        bronze_rows = [{
            "raw_id": f'{r["source"]}:{r["source_id"]}', "source": r["source"], "source_id": r["source_id"],
            "fetched_at": now, "payload_json": json.dumps(scrub_payload(r["payload"]), ensure_ascii=False,
                                                          default=str),
        } for r in raw]
        bronze_name = f"bronze.{source}_raw"
        storage.upsert(bronze_name, pa.Table.from_pylist(bronze_rows, schema=BRONZE_SCHEMA), key="raw_id")

        silver_rows = [to_silver(ad, now) for ad in ads]
        # One row per job_id even if a source returned it twice
        silver_rows = list({r["job_id"]: r for r in silver_rows}.values())
        result = storage.upsert("silver.jobs", pa.Table.from_pylist(silver_rows, schema=SILVER_SCHEMA),
                                key="job_id")

        run.outputs = [bronze_name, "silver.jobs"]
        run.output_stats = {bronze_name: len(bronze_rows), "silver.jobs": len(silver_rows)}
        run.run_facets["jobradar_privacy"] = lineage.custom_facet(
            "PrivacyRunFacet", redactions=sum(r["redactions"] for r in silver_rows),
            personal_fields_dropped=True, method="regex: contact lines, e-mails, phone numbers")
    return {"source": source, "ads": len(silver_rows), **result}


def apply_retention(days: int | None = None) -> dict:
    """Delete ads older than the retention window from every table that holds them."""
    settings = get_settings()
    days = days or int(settings.sources.get("days", 7))
    cutoff = datetime.now(UTC) - timedelta(days=days)
    cutoff_sql = cutoff.strftime("%Y-%m-%d %H:%M:%S")
    deleted: dict[str, int] = {}

    with lineage.run("retention.jobs", f"Delete job ads older than {days} days, then vacuum") as run:
        if not storage.exists("silver.jobs"):
            return {}
        old = storage.query(
            f"SELECT job_id FROM silver_jobs WHERE posted_at < TIMESTAMPTZ '{cutoff_sql}+00'", ["silver.jobs"]
        ).column("job_id").to_pylist()
        if old:
            ids = ", ".join("'" + i.replace("'", "''") + "'" for i in old)
            for name, col in (("silver.jobs", "job_id"), ("gold.jobs_enriched", "job_id"),
                              ("bronze.arbeitnow_raw", "raw_id"), ("bronze.bundesagentur_raw", "raw_id")):
                deleted[name] = storage.delete(name, f"{col} IN ({ids})")
                storage.vacuum(name)
            from .index import delete_jobs  # vectors are a copy too
            deleted["vectors.jobs"] = delete_jobs(old)
        run.outputs = [n for n in deleted if "." in n and not n.startswith("vectors")]
        run.run_facets["jobradar_retention"] = lineage.custom_facet(
            "RetentionRunFacet", retention_days=days, cutoff=cutoff.isoformat(), deleted=deleted)
    return deleted
