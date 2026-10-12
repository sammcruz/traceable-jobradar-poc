"""OpenLineage events for every pipeline step.

Each event is written to a local JSON-lines log you own (data/lineage/events.jsonl) and,
if OPENLINEAGE_URL is set, also sent to Marquez. Events are plain dicts that follow the
OpenLineage spec, so the log stays readable without any client library.

Custom facets are prefixed `jobradar_` and described by JSON Schemas in docs/facets/.
"""

from __future__ import annotations

import json
import uuid
import warnings
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

import requests

from . import PRODUCER, storage
from .config import get_settings

SPEC = "https://openlineage.io/spec/2-0-2/OpenLineage.json#/$defs/RunEvent"
FACET_BASE = "https://openlineage.io/spec/facets"
CUSTOM_FACET_BASE = f"{PRODUCER}/blob/main/docs/facets"
LAKE_NAMESPACE = "jobradar-lake"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def facet(kind: str, version: str, **fields: Any) -> dict:
    """Standard OpenLineage facet, e.g. facet('SchemaDatasetFacet', '1-1-1', fields=[...])."""
    return {"_producer": PRODUCER, "_schemaURL": f"{FACET_BASE}/{version}/{kind}.json", **fields}


def custom_facet(kind: str, **fields: Any) -> dict:
    return {"_producer": PRODUCER, "_schemaURL": f"{CUSTOM_FACET_BASE}/{kind}.json", **fields}


def lake_dataset(name: str, with_facets: bool = True) -> dict:
    """A Delta table as an OpenLineage dataset, with schema and version facets."""
    ds: dict = {"namespace": LAKE_NAMESPACE, "name": name, "facets": {}}
    if with_facets and storage.exists(name):
        ds["facets"]["schema"] = facet("SchemaDatasetFacet", "1-1-1", fields=storage.schema_fields(name))
        ds["facets"]["version"] = facet("DatasetVersionDatasetFacet", "1-0-1",
                                        datasetVersion=str(storage.version(name)))
        ds["facets"]["dataSource"] = facet("DatasourceDatasetFacet", "1-0-1",
                                           name="delta", uri=str(storage.table_path(name)))
    return ds


def external_dataset(namespace: str, name: str) -> dict:
    return {"namespace": namespace, "name": name, "facets": {}}


def private_dataset(name: str) -> dict:
    """Datasets derived from your CV. Only hashes are ever put in lineage."""
    return {"namespace": "jobradar-private", "name": name, "facets": {}}


def emit(event: dict) -> None:
    settings = get_settings()
    settings.lineage_log.parent.mkdir(parents=True, exist_ok=True)
    with settings.lineage_log.open("a") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    if settings.openlineage_url:
        try:
            requests.post(f"{settings.openlineage_url.rstrip('/')}/api/v1/lineage", json=event, timeout=5)
        except requests.RequestException as exc:  # lineage must never break the pipeline
            warnings.warn(f"Could not send lineage to {settings.openlineage_url}: {exc}", stacklevel=2)


class Run:
    def __init__(self, job: str, description: str):
        self.job = job
        self.description = description
        self.run_id = str(uuid.uuid4())
        self.inputs: list[dict] = []
        self.outputs: list[str | dict] = []
        self.run_facets: dict = {}
        self.output_stats: dict[str, int] = {}

    def _event(self, event_type: str) -> dict:
        settings = get_settings()
        outputs = []
        for out in self.outputs:
            ds = lake_dataset(out) if isinstance(out, str) else out
            name = out if isinstance(out, str) else out["name"]
            if name in self.output_stats:
                ds.setdefault("facets", {})
                ds["outputFacets"] = {
                    "outputStatistics": facet("OutputStatisticsOutputDatasetFacet", "1-0-2",
                                              rowCount=self.output_stats[name])
                }
            outputs.append(ds)
        return {
            "eventType": event_type,
            "eventTime": _now(),
            "producer": PRODUCER,
            "schemaURL": SPEC,
            "run": {"runId": self.run_id, "facets": self.run_facets},
            "job": {
                "namespace": settings.namespace,
                "name": self.job,
                "facets": {
                    "documentation": facet("DocumentationJobFacet", "1-0-1", description=self.description),
                    "jobType": facet("JobTypeJobFacet", "2-0-3", processingType="BATCH",
                                     integration="JOBRADAR", jobType="TASK"),
                },
            },
            "inputs": self.inputs,
            "outputs": outputs if event_type != "START" else [],
        }


@contextmanager
def run(job: str, description: str):
    """Wrap a pipeline step: emits START, then COMPLETE or FAIL with inputs/outputs."""
    r = Run(job, description)
    emit(r._event("START"))
    try:
        yield r
    except Exception as exc:
        r.run_facets["errorMessage"] = facet("ErrorMessageRunFacet", "1-0-1",
                                             message=str(exc)[:500], programmingLanguage="python")
        emit(r._event("FAIL"))
        raise
    emit(r._event("COMPLETE"))


def read_events() -> list[dict]:
    path = get_settings().lineage_log
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def sync_to_lake() -> int:
    """Copy the event log into a Delta table you own (lineage.raw_events). Returns row count."""
    import pyarrow as pa

    events = read_events()
    rows = [
        {
            "run_id": e["run"]["runId"],
            "event_type": e["eventType"],
            "event_time": e["eventTime"],
            "job": e["job"]["name"],
            "inputs": [f'{d["namespace"]}:{d["name"]}' for d in e.get("inputs", [])],
            "outputs": [f'{d["namespace"]}:{d["name"]}' for d in e.get("outputs", [])],
            "event_json": json.dumps(e, ensure_ascii=False),
        }
        for e in events
    ]
    schema = pa.schema([
        ("run_id", pa.string()), ("event_type", pa.string()), ("event_time", pa.string()),
        ("job", pa.string()), ("inputs", pa.list_(pa.string())), ("outputs", pa.list_(pa.string())),
        ("event_json", pa.string()),
    ])
    storage.overwrite("lineage.raw_events", pa.Table.from_pylist(rows, schema=schema))
    return len(rows)
