import json

import duckdb

from jobradar import contracts, lineage, storage
from jobradar.erase import erase_cv, find_derived
from jobradar.match import Filters, run_match


def test_ingest_and_enrich(pipeline):
    silver = storage.read("silver.jobs").to_pylist()
    assert len(silver) == 15
    assert all("@" not in r["description"] for r in silver), "e-mails must be redacted at ingest"
    gold = {r["title"]: r for r in storage.read("gold.jobs_enriched").to_pylist()}
    devops = gold["DevOps Engineer Kubernetes & Terraform"]
    assert devops["category"] == "it_ops_cloud"
    assert devops["visa_sponsorship"] == "yes"
    assert devops["remote_mode"] == "remote"
    assert json.loads(devops["field_sources"])["remote_mode"] == "source"
    assert gold["Controller (m/w/d)"]["german_required"] == "yes"


def test_bronze_has_no_contact_data(pipeline):
    raw = storage.read("bronze.bundesagentur_raw").column("payload_json").to_pylist()
    assert all("ansprechpartner" not in r and "max.beispiel@" not in r for r in raw)


def test_contracts_pass(pipeline):
    run_match()
    assert all(not problems for problems in contracts.check_contracts().values())


def test_match_filters_and_lineage(pipeline):
    results = run_match(Filters(exclude_german_required=True), shortlist=10, rerank=5)
    assert results and all(r["german_required"] != "yes" for r in results)
    assert results[0]["title"] == "Senior Data Engineer (Lakehouse)"
    jobs = [e["job"]["name"] for e in lineage.read_events() if e["eventType"] == "COMPLETE"]
    assert {"ingest.arbeitnow", "ingest.bundesagentur", "enrich.jobs", "cv.load", "match.cv"} <= set(jobs)
    match_event = [e for e in lineage.read_events() if e["job"]["name"] == "match.cv"][-1]
    assert match_event["run"]["facets"]["jobradar_model_llm"]["runs_locally"] is True
    gold_in = [d for d in match_event["inputs"] if d["name"] == "gold.jobs_enriched"][0]
    assert "version" in gold_in["facets"]


def test_erase_follows_lineage(pipeline):
    run_match()
    cv_hash = pipeline["cv_hash"]
    derived = find_derived(cv_hash)
    assert "jobradar-lake:gold.matches" in derived["datasets"]
    report = erase_cv(cv_hash)
    assert report["rows_deleted"]["gold.matches"] > 0
    assert storage.query(f"SELECT count(*) AS n FROM gold_matches WHERE cv_hash = '{cv_hash}'",
                         ["gold.matches"]).column("n")[0].as_py() == 0
    assert not list(storage.table_path("gold.matches").parent.parent.parent.joinpath("private").glob("*"))


def test_retention_deletes_old_ads(pipeline):
    from jobradar.ingest import apply_retention

    deleted = apply_retention(days=1)  # sample ads are older than one day
    assert deleted["silver.jobs"] == 15
    assert storage.read("silver.jobs").num_rows == 0


def test_tables_are_plain_delta(pipeline):
    """Exit test without the DuckDB extension: any Delta reader works (deltalake -> Arrow -> DuckDB)."""
    lineage.sync_to_lake()
    con = duckdb.connect()
    con.register("jobs", storage.read("gold.jobs_enriched"))
    assert con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 15
    assert storage.read("lineage.raw_events").num_rows > 0
