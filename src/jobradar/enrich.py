"""Enrich: silver.jobs -> gold.jobs_enriched.

Three tiers, cheapest first, and every field records where its value came from (`field_sources`):
  source  - the API already said so (e.g. Arbeitnow's remote flag or visa filter)
  rule    - a deterministic pattern (see rules.py)
  embed   - zero-shot category from embedding similarity to category descriptions
  llm     - the local model filled a gap (`enrich --deep`), recorded with model + prompt version

Runs on everything with tiers 1-3 in minutes. The LLM tier is slower (seconds per ad on a laptop),
so it is opt-in and can be limited to categories, and results are cached by content hash.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pyarrow as pa

from . import index, lineage, models, rules, storage
from .config import get_settings

ENUMS = {
    "german_required": {"yes", "no", "nice_to_have", "unknown"},
    "remote_mode": {"remote", "hybrid", "onsite", "unknown"},
    "visa_sponsorship": {"yes", "no", "unknown"},
    "contract_type": {"permanent", "fixed_term", "freelance", "internship", "working_student",
                      "apprenticeship", "unknown"},
    "work_time": {"full_time", "part_time", "full_or_part_time", "unknown"},
    "seniority": {"junior", "mid", "senior", "lead", "unknown"},
}
TS = pa.timestamp("us", tz="UTC")
GOLD_SCHEMA = pa.schema([
    ("job_id", pa.string()), ("content_hash", pa.string()), ("source", pa.string()),
    ("title", pa.string()), ("company", pa.string()), ("location", pa.string()), ("url", pa.string()),
    ("language", pa.string()), ("posted_at", TS),
    ("category", pa.string()), ("category_label", pa.string()), ("category_confidence", pa.float64()),
    ("german_required", pa.string()), ("remote_mode", pa.string()),
    ("salary_min", pa.float64()), ("salary_max", pa.float64()), ("salary_currency", pa.string()),
    ("salary_period", pa.string()), ("salary_text", pa.string()),
    ("visa_sponsorship", pa.string()), ("contract_type", pa.string()), ("work_time", pa.string()),
    ("seniority", pa.string()), ("skills", pa.list_(pa.string())), ("summary", pa.string()),
    ("field_sources", pa.string()), ("deep_enriched", pa.bool_()),
    ("enrich_model", pa.string()), ("prompt_version", pa.string()), ("enriched_at", TS),
])

DEEP_SYSTEM = """You extract facts from a job advertisement. Answer with one JSON object only.
Use exactly these keys and allowed values; use "unknown" when the ad does not say:
  german_required: yes | no | nice_to_have | unknown
  remote_mode: remote | hybrid | onsite | unknown
  visa_sponsorship: yes | no | unknown
  contract_type: permanent | fixed_term | freelance | internship | working_student | apprenticeship | unknown
  work_time: full_time | part_time | full_or_part_time | unknown
  seniority: junior | mid | senior | lead | unknown
  category: one of the category ids given
  skills: list of up to 12 concrete skills or tools named in the ad
  summary: one neutral English sentence describing the role
Do not guess. Do not include people's names or contact details."""


# ---------- category ----------

def _category_vectors() -> dict[str, list[float]]:
    cats = get_settings().categories
    vecs = models.embed([f"{c['label']}: {c['description']}" for c in cats])
    return {c["id"]: v for c, v in zip(cats, vecs, strict=True)}


def classify(title: str, vector: list[float] | None, cat_vectors: dict) -> tuple[str, float, str]:
    # Keyword in the title: the most specific (longest) matching keyword wins, e.g.
    # "DevOps Engineer" -> it_ops_cloud via "devops", not software_development via a generic word.
    title_l = f" {title.lower()} "
    best_len, best_cat = 0, None
    for cat in get_settings().categories:
        for k in cat.get("keywords", []):
            if k.lower() in title_l and len(k.strip()) > best_len:
                best_len, best_cat = len(k.strip()), cat["id"]
    if best_cat:
        return best_cat, 0.9, "rule"
    if vector is None:
        return "other", 0.0, ""
    scores = sorted(((models.cosine(vector, v), cid) for cid, v in cat_vectors.items()), reverse=True)
    best, second = scores[0], scores[1] if len(scores) > 1 else (0.0, "")
    return best[1], round(max(0.0, best[0] - second[0]), 4), "embed"


# ---------- per-row enrichment ----------

def enrich_row(row: dict, vector, cat_vectors: dict, labels: dict) -> dict:
    text = f"{row['title']}\n{row.get('description') or ''}"
    sources: dict[str, str] = {}

    def put(field: str, value_method: tuple):
        value, method = value_method
        out[field] = value if value is not None else "unknown"
        sources[field] = method if value is not None else "none"

    out: dict = {k: row.get(k) for k in ("job_id", "content_hash", "source", "title", "company", "location",
                                         "url", "language", "posted_at")}
    cat, conf, method = classify(row["title"], vector, cat_vectors)
    out.update(category=cat, category_label=labels.get(cat, cat), category_confidence=conf)
    sources["category"] = method or "none"

    put("german_required", rules.german_required(text, row.get("language") or ""))
    put("remote_mode", rules.remote_mode(text, row.get("remote_source")))
    put("visa_sponsorship", rules.visa(text, row.get("visa_source")))
    put("contract_type", rules.contract_type(text, row.get("job_types") or []))
    put("work_time", rules.work_time(text, row.get("job_types") or []))
    put("seniority", rules.seniority(row["title"]))

    sal = rules.salary(text)
    out.update(sal or {"salary_min": None, "salary_max": None, "salary_currency": None,
                       "salary_period": None, "salary_text": None})
    sources["salary"] = "rule" if sal else "none"

    out["skills"] = rules.skills(text)
    sources["skills"] = "rule"
    out["summary"] = None
    out["field_sources"] = json.dumps(sources)
    out["deep_enriched"] = False
    return out


def _fake_deep(user: str) -> dict:
    """Deterministic stand-in for the LLM: fills unknowns conservatively."""
    return {"summary": "A role described in the ad.", "seniority": "mid", "skills": []}


def deep_enrich(out: dict, description: str) -> dict:
    """Ask the local LLM for the fields rules could not determine. Never overwrites source/rule values."""
    sources = json.loads(out["field_sources"])
    cats = get_settings().categories
    prompt = (
        f"Category ids: {', '.join(c['id'] for c in cats)}\n\n"
        f"Title: {out['title']}\nCompany: {out['company']}\nLocation: {out['location']}\n\n"
        f"Ad text:\n{description[:4000]}"
    )
    answer = models.chat_json(DEEP_SYSTEM, prompt, fake=_fake_deep)
    for field, allowed in ENUMS.items():
        value = str(answer.get(field, "unknown")).strip().lower()
        if sources.get(field) == "none" and value in allowed and value != "unknown":
            out[field] = value
            sources[field] = "llm"
    category = str(answer.get("category", "")).strip()
    if sources.get("category") in ("embed", "none") and category in {c["id"] for c in cats}:
        if category != out["category"]:
            out["category"] = category
            out["category_label"] = next(c["label"] for c in cats if c["id"] == category)
            out["category_confidence"] = 0.7
        sources["category"] = "llm"
    llm_skills = [str(s).strip() for s in answer.get("skills") or [] if str(s).strip()][:12]
    if llm_skills:
        merged = list(dict.fromkeys(out["skills"] + llm_skills))
        if len(merged) > len(out["skills"]):
            sources["skills"] = "rule+llm"
        out["skills"] = merged
    if answer.get("summary"):
        out["summary"] = str(answer["summary"])[:400]
        sources["summary"] = "llm"
    out["field_sources"] = json.dumps(sources)
    out["deep_enriched"] = True
    return out


# ---------- step ----------

def run_enrich(deep: bool = False, categories: list[str] | None = None, limit: int | None = None,
               progress=None, embed_progress=None) -> dict:
    settings = get_settings()
    if not storage.exists("silver.jobs"):
        raise RuntimeError("No silver.jobs table yet. Run `jobradar ingest` first.")
    rows = storage.read("silver.jobs").to_pylist()

    previous: dict[str, dict] = {}
    if storage.exists("gold.jobs_enriched"):
        previous = {r["job_id"]: r for r in storage.read("gold.jobs_enriched").to_pylist()}

    model_name = settings.llm_model if settings.provider != "fake" else "fake"
    labels = {c["id"]: c["label"] for c in settings.categories}

    with lineage.run("enrich.jobs", "Classify job ads and extract German, remote, salary, visa, contract, "
                                    "seniority and skills; LLM fills gaps when --deep") as run:
        run.inputs = [lineage.lake_dataset("silver.jobs")]
        vectors, newly_embedded = index.ensure_job_vectors(rows, progress=embed_progress)
        cat_vectors = _category_vectors()
        run.inputs.append(index.vectors_dataset())

        out_rows, deep_count = [], 0
        for i, row in enumerate(rows):
            prev = previous.get(row["job_id"])
            keep_deep = prev and prev["content_hash"] == row["content_hash"] and prev.get("deep_enriched") \
                and prev.get("enrich_model") == model_name
            if keep_deep:
                out_rows.append(prev)
                continue
            out = enrich_row(row, vectors.get(row["job_id"]), cat_vectors, labels)
            wants_deep = deep and (not categories or out["category"] in categories) \
                and (limit is None or deep_count < limit)
            if wants_deep:
                out = deep_enrich(out, row.get("description") or "")
                deep_count += 1
            out.update(enrich_model=model_name if out["deep_enriched"] else "rules+embeddings",
                       prompt_version=models.PROMPT_VERSION, enriched_at=datetime.now(UTC))
            out_rows.append(out)
            if progress:
                progress(i + 1, len(rows))

        storage.overwrite("gold.jobs_enriched", pa.Table.from_pylist(out_rows, schema=GOLD_SCHEMA))
        run.outputs = ["gold.jobs_enriched", index.vectors_dataset()]
        run.output_stats = {"gold.jobs_enriched": len(out_rows)}
        run.run_facets["jobradar_model_embedding"] = models.model_facet("embedding")
        if deep:
            run.run_facets["jobradar_model_llm"] = models.model_facet("extraction")
    return {"jobs": len(out_rows), "newly_embedded": newly_embedded, "deep_enriched_now": deep_count}
