"""Delta Lake tables on local disk, queried with DuckDB.

Layout: <data_dir>/lake/<layer>/<table>, e.g. lake/silver/jobs.
Every table is plain Delta: any engine that reads Delta can read it (see `jobradar exit-test`).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from .config import get_settings


def table_path(name: str) -> Path:
    layer, table = name.split(".", 1)
    return get_settings().lake / layer / table


def exists(name: str) -> bool:
    return DeltaTable.is_deltatable(str(table_path(name)))


def version(name: str) -> int | None:
    return DeltaTable(str(table_path(name))).version() if exists(name) else None


def read(name: str) -> pa.Table:
    return DeltaTable(str(table_path(name))).to_pyarrow_table()


def overwrite(name: str, data: pa.Table) -> int:
    path = table_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_deltalake(str(path), data, mode="overwrite", schema_mode="overwrite")
    return version(name)


def append(name: str, data: pa.Table) -> int:
    path = table_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_deltalake(str(path), data, mode="append", schema_mode="merge")
    return version(name)


def upsert(name: str, data: pa.Table, key: str) -> dict:
    """Insert new rows, update changed rows (by key). Creates the table on first write."""
    if data.num_rows == 0:
        return {"inserted": 0, "updated": 0, "version": version(name)}
    if not exists(name):
        overwrite(name, data)
        return {"inserted": data.num_rows, "updated": 0, "version": version(name)}
    dt = DeltaTable(str(table_path(name)))
    metrics = (
        dt.merge(source=data, predicate=f"t.{key} = s.{key}", source_alias="s", target_alias="t",
                 merge_schema=True)
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute()
    )
    return {
        "inserted": metrics.get("num_target_rows_inserted", 0),
        "updated": metrics.get("num_target_rows_updated", 0),
        "version": version(name),
    }


def delete(name: str, predicate: str) -> int:
    """Delete rows matching a SQL predicate. Returns rows deleted."""
    if not exists(name):
        return 0
    dt = DeltaTable(str(table_path(name)))
    metrics = dt.delete(predicate)
    return int(metrics.get("num_deleted_rows", 0))


def vacuum(name: str, hours: int = 0) -> int:
    """Physically remove files no longer referenced. hours=0 makes deletes final (no time travel back)."""
    if not exists(name):
        return 0
    dt = DeltaTable(str(table_path(name)))
    removed = dt.vacuum(retention_hours=hours, enforce_retention_duration=False, dry_run=False)
    return len(removed)


def schema_fields(name: str) -> list[dict]:
    if not exists(name):
        return []
    # deltalake returns an arro3 schema; pyarrow imports it through the Arrow C interface
    schema = pa.schema(DeltaTable(str(table_path(name))).schema().to_arrow())
    return [{"name": f.name, "type": str(f.type)} for f in schema]


def query(sql: str, tables: list[str]) -> pa.Table:
    """Run DuckDB SQL over Delta tables. Refer to them as layer_table (e.g. silver_jobs)."""
    con = duckdb.connect()
    for name in tables:
        if exists(name):
            con.register(name.replace(".", "_"), DeltaTable(str(table_path(name))).to_pyarrow_table())
    result = con.execute(sql)
    return result.to_arrow_table() if hasattr(result, "to_arrow_table") else result.fetch_arrow_table()
