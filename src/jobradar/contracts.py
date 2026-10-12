"""Data contracts (ODCS v3) for the tables other people would consume.

`check_contracts()` verifies that each Delta table has the columns and types its contract promises,
and the quality rules we can express simply (not null, allowed values). It runs in CI.
The same YAML files also work with the Data Contract CLI if you want richer checks.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from . import storage
from .config import REPO_ROOT

CONTRACTS_DIR = REPO_ROOT / "contracts"

# ODCS logical types -> substrings of the Arrow type names we accept
TYPE_MAP = {
    "string": ("string",),
    "integer": ("int",),
    "number": ("double", "float", "decimal"),
    "boolean": ("bool",),
    "timestamp": ("timestamp",),
    "array": ("list",),
}


def load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def check(contract: dict) -> list[str]:
    problems: list[str] = []
    for obj in contract.get("schema", []):
        table = obj.get("physicalName") or obj["name"]
        if not storage.exists(table):
            problems.append(f"{table}: table does not exist")
            continue
        actual = {f["name"]: f["type"] for f in storage.schema_fields(table)}
        data = storage.read(table)
        for prop in obj.get("properties", []):
            col = prop["name"]
            if col not in actual:
                problems.append(f"{table}.{col}: missing column")
                continue
            expected = TYPE_MAP.get(prop.get("logicalType", "string"), ())
            if expected and not any(e in actual[col] for e in expected):
                problems.append(f"{table}.{col}: type {actual[col]} does not match {prop.get('logicalType')}")
            values = data.column(col).to_pylist()
            if prop.get("required") and any(v is None for v in values):
                problems.append(f"{table}.{col}: null values in a required column")
            allowed = next((q.get("arguments", {}).get("validValues") for q in prop.get("quality", [])
                            if q.get("metric") == "invalidValues"), None)
            if allowed:
                bad = sorted({v for v in values if v is not None and v not in allowed})
                if bad:
                    problems.append(f"{table}.{col}: values outside contract: {bad[:5]}")
    return problems


def check_contracts() -> dict[str, list[str]]:
    return {p.name: check(load(p)) for p in sorted(CONTRACTS_DIR.glob("*.odcs.yaml"))}
