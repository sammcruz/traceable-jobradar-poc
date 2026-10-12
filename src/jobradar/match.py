"""Match your CV against this week's job ads.

1. Filter  gold.jobs_enriched with your hard requirements (DuckDB SQL).
2. Shortlist by embedding similarity between your profile and each ad.
3. Rerank   the shortlist with the local LLM: score 0-100, reasons and gaps.
Results are appended to gold.matches with the CV hash, job version and model, so every
recommendation can be explained later, and erased with `jobradar erase`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta

import pyarrow as pa

from . import index, lineage, models, storage
from .config import get_settings
from .cv import get_profile, profile_text

TS = pa.timestamp("us", tz="UTC")
MATCH_SCHEMA = pa.schema([
    ("match_id", pa.string()), ("run_id", pa.string()), ("cv_hash", pa.string()),
    ("job_id", pa.string()), ("job_content_hash", pa.string()), ("rank", pa.int32()),
    ("similarity", pa.float64()), ("score", pa.int32()),
    ("reasons", pa.list_(pa.string())), ("gaps", pa.list_(pa.string())),
    ("model", pa.string()), ("prompt_version", pa.string()), ("filters_json", pa.string()),
    ("created_at", TS),
])

RERANK_SYSTEM = """You assess how well a candidate fits a job advertisement.
Answer with one JSON object:
  score: integer 0-100 (70+ strong fit, 40-69 partial fit, <40 weak fit)
  reasons: up to 3 short strings, why it fits
  gaps: up to 3 short strings, what is missing or risky (e.g. language, seniority, a key skill)
Judge only skills, experience, seniority and language requirements. Ignore age, gender, nationality."""


@dataclass
class Filters:
    categories: list[str] = field(default_factory=list)
    exclude_german_required: bool = False
    remote_modes: list[str] = field(default_factory=list)
    salary_only: bool = False
    visa_only: bool = False
    contract_types: list[str] = field(default_factory=list)
    seniority: list[str] = field(default_factory=list)
    posted_within_days: int | None = None
    sources: list[str] = field(default_factory=list)


def _in(column: str, values: list[str]) -> str:
    quoted = ", ".join("'" + v.replace("'", "''") + "'" for v in values)
    return f"{column} IN ({quoted})"


def candidate_sql(f: Filters) -> str:
    where = ["1=1"]
    if f.categories:
        where.append(_in("category", f.categories))
    if f.exclude_german_required:
        where.append("german_required <> 'yes'")
    if f.remote_modes:
        where.append(_in("remote_mode", f.remote_modes))
    if f.salary_only:
        where.append("salary_min IS NOT NULL")
    if f.visa_only:
        where.append("visa_sponsorship = 'yes'")
    if f.contract_types:
        where.append(_in("contract_type", f.contract_types))
    if f.seniority:
        where.append(_in("seniority", f.seniority))
    if f.sources:
        where.append(_in("source", f.sources))
    if f.posted_within_days:
        since = (datetime.now(UTC) - timedelta(days=f.posted_within_days)).strftime("%Y-%m-%d %H:%M:%S")
        where.append(f"posted_at >= TIMESTAMPTZ '{since}+00'")
    return f"SELECT * FROM gold_jobs_enriched WHERE {' AND '.join(where)}"


def _fake_rerank(user: str) -> dict:
    """Deterministic stand-in: score = share of the ad's skills the candidate has."""
    import re

    cand = re.search(r"^Skills: (.*)$", user, re.MULTILINE)
    ad = re.search(r"^Skills in ad: (.*)$", user, re.MULTILINE)
    have = {s.strip().lower() for s in (cand.group(1) if cand else "").split(",") if s.strip()}
    want = {s.strip().lower() for s in (ad.group(1) if ad else "").split(",") if s.strip()}
    overlap = sorted(have & want)
    missing = sorted(want - have)
    score = round(100 * len(overlap) / len(want)) if want else 30
    gaps = [f"Missing: {', '.join(missing[:4])}"] if missing else []
    if "German required: yes" in user:
        gaps.append("German required")
        score = max(0, score - 20)
    return {"score": score, "reasons": [f"Shared skills: {', '.join(overlap[:5])}"] if overlap else [],
            "gaps": gaps}


def _rerank(profile: dict, job: dict) -> dict:
    prompt = (
        f"CANDIDATE\n{profile_text(profile)}\nYears of experience: {profile.get('years_experience')}\n"
        f"Seniority: {profile.get('seniority')}\n\n"
        f"JOB\nTitle: {job['title']}\nCompany: {job['company']}\nLocation: {job['location']}\n"
        f"German required: {job['german_required']}; remote: {job['remote_mode']}; "
        f"seniority: {job['seniority']}; contract: {job['contract_type']}\n"
        f"Skills in ad: {', '.join(job.get('skills') or [])}\n"
        f"Summary: {job.get('summary') or ''}\n\nAd text:\n{(job.get('description') or '')[:3000]}"
    )
    answer = models.chat_json(RERANK_SYSTEM, prompt, fake=_fake_rerank)
    try:
        score = max(0, min(100, int(answer.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {"score": score,
            "reasons": [str(x)[:200] for x in (answer.get("reasons") or [])][:3],
            "gaps": [str(x)[:200] for x in (answer.get("gaps") or [])][:3]}


def run_match(filters: Filters | None = None, cv_hash: str | None = None, shortlist: int = 30,
              rerank: int = 10, progress=None) -> list[dict]:
    filters = filters or Filters()
    profile_record = get_profile(cv_hash)
    if not profile_record:
        raise RuntimeError("No CV loaded yet. Run `jobradar cv load path/to/cv.pdf` first.")
    if not storage.exists("gold.jobs_enriched"):
        raise RuntimeError("No enriched jobs yet. Run `jobradar enrich` first.")
    cv_hash = profile_record["cv_hash"]
    profile, cv_vec = profile_record["profile"], profile_record["vector"]
    settings = get_settings()
    model_name = settings.llm_model if settings.provider != "fake" else "fake"

    with lineage.run("match.cv", "Filter, shortlist by similarity, rerank with the local LLM") as run:
        run.inputs = [lineage.lake_dataset("gold.jobs_enriched"), index.vectors_dataset(),
                      lineage.private_dataset(f"cv_profile/{cv_hash[:12]}")]
        candidates = storage.query(candidate_sql(filters), ["gold.jobs_enriched"]).to_pylist()
        vectors = index.job_vectors([c["job_id"] for c in candidates])
        scored = sorted(
            ((models.cosine(cv_vec, vectors[c["job_id"]]), c) for c in candidates if c["job_id"] in vectors),
            key=lambda x: x[0], reverse=True,
        )[:shortlist]

        descriptions = {}
        if scored:
            ids = [c["job_id"] for _, c in scored]
            for r in storage.query("SELECT job_id, description FROM silver_jobs WHERE "
                                   + _in("job_id", ids), ["silver.jobs"]).to_pylist():
                descriptions[r["job_id"]] = r["description"]

        results = []
        for i, (sim, job) in enumerate(scored):
            job = {**job, "description": descriptions.get(job["job_id"], "")}
            judged = _rerank(profile, job) if i < rerank else {"score": None, "reasons": [], "gaps": []}
            results.append({**job, "similarity": round(sim, 4), **judged})
            if progress:
                progress(i + 1, len(scored))
        results.sort(key=lambda r: (r["score"] is not None, r["score"] or 0, r["similarity"]), reverse=True)

        now = datetime.now(UTC)
        rows = [{
            "match_id": str(uuid.uuid4()), "run_id": run.run_id, "cv_hash": cv_hash,
            "job_id": r["job_id"], "job_content_hash": r["content_hash"], "rank": n + 1,
            "similarity": r["similarity"], "score": r["score"], "reasons": r["reasons"], "gaps": r["gaps"],
            "model": model_name, "prompt_version": models.PROMPT_VERSION,
            "filters_json": json.dumps(asdict(filters)), "created_at": now,
        } for n, r in enumerate(results)]
        if rows:
            storage.append("gold.matches", pa.Table.from_pylist(rows, schema=MATCH_SCHEMA))
        run.outputs = ["gold.matches"]
        run.output_stats = {"gold.matches": len(rows)}
        run.run_facets["jobradar_cv"] = lineage.custom_facet("CvRunFacet", cv_hash=cv_hash, converter=None)
        run.run_facets["jobradar_match"] = lineage.custom_facet(
            "MatchRunFacet", candidates=len(candidates), shortlisted=len(scored),
            reranked=min(rerank, len(scored)), filters=asdict(filters))
        run.run_facets["jobradar_model_llm"] = models.model_facet("rerank")
        run.run_facets["jobradar_model_embedding"] = models.model_facet("embedding")
    for r in results:
        r.pop("description", None)
    return results
