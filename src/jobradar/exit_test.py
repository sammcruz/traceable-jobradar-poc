"""Exit test: can an engine we don't depend on read every table we produced?

Uses DuckDB's own `delta` extension (not the deltalake library the pipeline writes with),
plus the Lance format for vectors. If this fails, you have a portability bug.
"""

from __future__ import annotations

import duckdb

from . import storage
from .config import get_settings

TABLES = ["bronze.arbeitnow_raw", "bronze.bundesagentur_raw", "silver.jobs", "gold.jobs_enriched",
          "gold.matches", "lineage.raw_events"]


def run_exit_test() -> list[dict]:
    con = duckdb.connect()
    try:
        con.execute("INSTALL delta; LOAD delta;")
    except duckdb.Error as exc:
        raise RuntimeError(
            "DuckDB could not install its 'delta' extension (it downloads once from extensions.duckdb.org). "
            f"Check your network and retry. Details: {exc}"
        ) from exc
    results = []
    for name in TABLES:
        path = storage.table_path(name)
        if not storage.exists(name):
            results.append({"table": name, "status": "absent", "rows": None})
            continue
        try:
            rows = con.execute(f"SELECT count(*) FROM delta_scan('{path}')").fetchone()[0]
            results.append({"table": name, "status": "ok", "rows": rows})
        except Exception as exc:  # noqa: BLE001 - report every failure
            results.append({"table": name, "status": f"FAILED: {exc}", "rows": None})
    vectors = get_settings().vectors / "jobs.lance"
    results.append({"table": "lance.jobs", "status": "ok" if vectors.exists() else "absent", "rows": None})
    return results
