"""Vector store (LanceDB, a versioned open format). One embedding per job ad and per CV.

Embeddings are cached by content hash and model: an ad is only re-embedded when its text or
the embedding model changes. The Lance table version goes into lineage like a Delta version.
"""

from __future__ import annotations

import lancedb
import pyarrow as pa

from . import lineage, models
from .config import get_settings

JOBS = "jobs"


def _db():
    path = get_settings().vectors
    path.mkdir(parents=True, exist_ok=True)
    return lancedb.connect(str(path))


def _open(name: str):
    try:
        return _db().open_table(name)
    except ValueError:  # table does not exist yet
        return None


def job_text(row: dict) -> str:
    return f"{row['title']}\n{row.get('company') or ''}\n{(row.get('description') or '')[:2500]}"


def ensure_job_vectors(rows: list[dict], progress=None, chunk: int = 64) -> tuple[dict[str, list[float]], int]:
    """Return {job_id: vector} for rows, embedding only new or changed ads. Second value: newly embedded.

    Embeds and saves in chunks: progress is reported after each chunk, and an interrupted run
    keeps everything embedded so far (the next run continues where this one stopped)."""
    model = get_settings().embed_model if get_settings().provider != "fake" else "fake-hash-256"
    table = _open(JOBS)
    cached: dict[str, tuple[str, list[float]]] = {}
    if table is not None:
        for r in table.to_arrow().select(["job_id", "content_hash", "model", "vector"]).to_pylist():
            if r["model"] == model:
                cached[r["job_id"]] = (r["content_hash"], r["vector"])

    todo = [r for r in rows if cached.get(r["job_id"], ("", None))[0] != r["content_hash"]]
    if progress:
        progress(0, len(todo))
    for start in range(0, len(todo), chunk):
        part = todo[start: start + chunk]
        vectors = models.embed([job_text(r) for r in part])
        new = pa.Table.from_pylist([
            {"job_id": r["job_id"], "content_hash": r["content_hash"], "model": model, "vector": v}
            for r, v in zip(part, vectors, strict=True)
        ])
        if table is None:
            table = _db().create_table(JOBS, data=new)
        else:
            ids = ", ".join("'" + r["job_id"].replace("'", "''") + "'" for r in part)
            table.delete(f"job_id IN ({ids})")
            table.add(new)
        for r, v in zip(part, vectors, strict=True):
            cached[r["job_id"]] = (r["content_hash"], v)
        if progress:
            progress(min(start + chunk, len(todo)), len(todo))
    return {r["job_id"]: cached[r["job_id"]][1] for r in rows if r["job_id"] in cached}, len(todo)


def delete_jobs(job_ids: list[str]) -> int:
    table = _open(JOBS)
    if table is None or not job_ids:
        return 0
    before = table.count_rows()
    ids = ", ".join("'" + i.replace("'", "''") + "'" for i in job_ids)
    table.delete(f"job_id IN ({ids})")
    return before - table.count_rows()


def vectors_dataset() -> dict:
    table = _open(JOBS)
    ds = lineage.external_dataset("jobradar-vectors", "lance.jobs")
    if table is not None:
        ds["facets"]["version"] = lineage.facet("DatasetVersionDatasetFacet", "1-0-1",
                                                datasetVersion=str(table.version))
        ds["facets"]["dataSource"] = lineage.facet("DatasourceDatasetFacet", "1-0-1",
                                                   name="lancedb", uri=str(get_settings().vectors / JOBS))
    return ds


def job_vectors(job_ids: list[str]) -> dict[str, list[float]]:
    table = _open(JOBS)
    if table is None:
        return {}
    wanted = set(job_ids)
    return {r["job_id"]: r["vector"] for r in table.to_arrow().select(["job_id", "vector"]).to_pylist()
            if r["job_id"] in wanted}
