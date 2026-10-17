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
    assert gold["Controller (m/w/d)"]["local_language_required"] == "yes"
    assert gold["Controller (m/w/d)"]["local_language"] == "de"


def test_bronze_has_no_contact_data(pipeline):
    raw = storage.read("bronze.bundesagentur_raw").column("payload_json").to_pylist()
    assert all("ansprechpartner" not in r and "max.beispiel@" not in r for r in raw)


def test_contracts_pass(pipeline):
    run_match()
    assert all(not problems for problems in contracts.check_contracts().values())


def test_match_filters_and_lineage(pipeline):
    results = run_match(Filters(exclude_local_language_required=True), shortlist=10, rerank=5)
    assert results and all(r["local_language_required"] != "yes" for r in results)
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


def test_upsert_tolerates_duplicate_keys():
    import pyarrow as pa

    storage.upsert("bronze.dups", pa.table({"raw_id": ["a", "b"], "v": [1, 2]}), "raw_id")
    result = storage.upsert("bronze.dups", pa.table({"raw_id": ["a", "a", "c"], "v": [3, 4, 5]}), "raw_id")
    rows = {r["raw_id"]: r["v"] for r in storage.read("bronze.dups").to_pylist()}
    assert rows == {"a": 4, "b": 2, "c": 5} and result["inserted"] == 1


def test_embedding_reports_progress_and_resumes():
    from jobradar import index

    rows = [{"job_id": f"j{i}", "content_hash": f"h{i}", "title": f"Data engineer {i}", "description": "SQL"}
            for i in range(10)]
    seen = []
    _, n = index.ensure_job_vectors(rows[:4], progress=lambda d, t: seen.append((d, t)), chunk=3)
    assert n == 4 and seen[-1] == (4, 4) and (3, 4) in seen
    _, n = index.ensure_job_vectors(rows)  # an interrupted run continues: only the 6 missing are embedded
    assert n == 6


def test_ingest_records_source_terms_in_lineage_and_rows(pipeline):
    events = [e for e in lineage.read_events() if e["job"]["name"] == "ingest.arbeitnow" and e["eventType"] == "COMPLETE"]
    facet = events[-1]["inputs"][0]["facets"]["jobradar_source_terms"]
    assert facet["source"] == "arbeitnow" and facet["attribution"] and facet["terms_status"] in ("verified", "to_verify")
    row = storage.read("silver.jobs").to_pylist()[0]
    assert row["attribution"] and row["license"]


def test_retention_covers_every_source_table(pipeline):
    from jobradar.ingest import SOURCES

    assert {"arbeitnow", "bundesagentur", "jobtech", "adzuna"} <= set(SOURCES)


def test_rows_enriched_before_the_rename_are_carried_over(pipeline):
    """An LLM-enriched row written with german_required (older schema) keeps its value after the upgrade."""
    import pyarrow as pa

    from jobradar.enrich import run_enrich

    silver = {r["job_id"]: r for r in storage.read("silver.jobs").to_pylist()}
    job_id = next(j for j, r in silver.items() if r["country"] == "DE")
    old = storage.read("gold.jobs_enriched").to_pylist()
    for r in old:
        if r["job_id"] == job_id:
            r["german_required"] = "nice_to_have"
        for k in ("local_language_required", "local_language", "country"):
            r.pop(k)
    # keep the old rows as 'deep enriched by the same model' so enrich reuses them
    for r in old:
        r["deep_enriched"], r["enrich_model"] = True, "fake"
    cols = {k: [r.get(k) for r in old] for k in old[0]}
    storage.overwrite("gold.jobs_enriched", pa.table(cols))
    run_enrich(deep=False)
    gold = {r["job_id"]: r for r in storage.read("gold.jobs_enriched").to_pylist()}
    assert gold[job_id]["local_language_required"] == "nice_to_have" and gold[job_id]["local_language"] == "de"
