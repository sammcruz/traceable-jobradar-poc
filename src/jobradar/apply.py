"""Prepare a tailored CV and cover letter for one job. Local models only, no photo, English output.

How it stays honest:
  1. Your CV is read once into structured roles and bullets. A bullet is kept only if its text appears
     in your CV word for word, so the model cannot invent one.
  2. The model only *chooses and orders* those bullets and picks skills from your CV. It writes a short
     profile and the cover letter from that evidence.
  3. Checks flag numbers and skills in the generated text that your CV does not contain.
Your name and contact details are never sent to the model: they live in data/private/identity.json
and are merged into the document at the end.

Everything is recorded in lineage (job, country profile, counts, model) and removed by `jobradar erase`.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from functools import lru_cache

import yaml

from . import lineage, models, rules, storage
from .config import CONFIG_DIR, get_settings
from .cv import get_profile
from .privacy import redact

COUNTRY_KEYS = {"GB": "gb", "UK": "gb", "DE": "de", "AT": "at", "CH": "ch", "NL": "nl", "DK": "dk", "NO": "no",
                "FI": "fi"}
OUTPUT_LANGUAGE = "en"
SECTION_TITLES = {"profile": "Profile", "experience": "Experience", "education": "Education",
                  "skills": "Skills", "languages": "Languages"}
MAX_BULLETS = (5, 4, 3, 2)  # per role, most recent first


# ---------- country profiles and identity ----------

@lru_cache(maxsize=1)
def _countries_file() -> tuple[dict, str]:
    text = (CONFIG_DIR / "countries.yml").read_text()
    return yaml.safe_load(text), hashlib.sha256(text.encode()).hexdigest()[:12]


def countries() -> dict[str, dict]:
    return {k: v for k, v in _countries_file()[0].items() if k != "default"}


def country_profile(code: str | None) -> tuple[str, dict]:
    """Return (key, profile) for a job's country code like 'DE' or a profile key like 'gb'."""
    data = _countries_file()[0]
    key = COUNTRY_KEYS.get((code or "").upper(), (code or "").lower())
    key = key if key in data and key != "default" else "default"
    return key, data[key]


def _identity_path():
    return get_settings().private / "identity.json"


def load_identity() -> dict:
    path = _identity_path()
    return json.loads(path.read_text()) if path.exists() else {}


def save_identity(identity: dict) -> None:
    clean = {k: str(identity.get(k) or "").strip() for k in ("name", "email", "phone", "location", "links")}
    get_settings().private.mkdir(parents=True, exist_ok=True)
    _identity_path().write_text(json.dumps(clean, ensure_ascii=False, indent=1))


# ---------- 1. structured CV ----------

_ROLE = re.compile(r"^\*\*(?P<title>.+?)\*\*[,\s]*(?P<rest>.*)$")
_PERIOD = re.compile(r"\(([^)]*\d{4}[^)]*)\)\s*$")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[*_`#>-]", " ", text)).strip().lower()


def parse_markdown_cv(markdown: str) -> dict:
    """Rule-based reading of a Markdown CV (## sections, **Title**, Company (dates), - bullets)."""
    cv: dict = {"summary": "", "roles": [], "education": [], "skills": [], "languages": []}
    section, role = "", None
    for raw in markdown.splitlines():
        line = raw.strip()
        if not line or line.startswith("# "):
            continue
        if line.startswith("## "):
            title = line[3:].strip().lower()
            section = next((k for k, words in (("summary", ("profile", "summary", "about")),
                                               ("experience", ("experience", "employment", "work")),
                                               ("education", ("education", "studies")),
                                               ("skills", ("skills", "tools", "technologies")),
                                               ("languages", ("languages",))) if any(w in title for w in words)), "")
            role = None
            continue
        bullet = line[2:].strip() if line.startswith(("- ", "* ")) else None
        if section == "summary":
            cv["summary"] = (cv["summary"] + " " + line).strip()
        elif section == "experience":
            m = _ROLE.match(line)
            if m and bullet is None:
                rest = m.group("rest").strip()
                period = _PERIOD.search(rest)
                role = {"id": f"r{len(cv['roles']) + 1}", "title": m.group("title").strip(),
                        "company": (rest[:period.start()] if period else rest).strip(" ,"),
                        "period": period.group(1) if period else "", "bullets": []}
                cv["roles"].append(role)
            elif role is not None:
                text = bullet if bullet is not None else line
                role["bullets"].append({"id": f"{role['id']}b{len(role['bullets']) + 1}", "text": text})
        elif section == "education":
            cv["education"].append(bullet if bullet is not None else line)
        elif section == "skills":
            cv["skills"] += [s.strip() for s in re.split(r"[,;•|]", bullet if bullet is not None else line) if s.strip()]
        elif section == "languages":
            cv["languages"] += [s.strip() for s in re.split(r"[,;•|]", bullet if bullet is not None else line) if s.strip()]
    return cv


EXTRACT_SYSTEM = """You read a CV and copy its content into structured JSON. Copy text exactly as written; do not
rewrite, shorten, translate or add anything. Leave out the person's name, e-mail, phone, address and photo.
Keys:
  summary: the profile paragraph if there is one, else ""
  roles: list, newest first, each {"title", "company", "period", "bullets": [exact bullet texts]}
  education: list of exact lines
  skills: list of skills as written
  languages: list of languages with levels as written"""


def _verify_extraction(data: dict, source: str) -> tuple[dict, int]:
    """Keep only bullets that appear in the CV word for word; give everything stable ids."""
    haystack, dropped = _norm(source), 0
    roles = []
    for r in data.get("roles") or []:
        if not isinstance(r, dict):
            continue
        rid = f"r{len(roles) + 1}"
        bullets = []
        for b in r.get("bullets") or []:
            text = str(b.get("text") if isinstance(b, dict) else b).strip()
            if text and _norm(text) in haystack:
                bullets.append({"id": f"{rid}b{len(bullets) + 1}", "text": text})
            else:
                dropped += 1
        roles.append({"id": rid, "title": str(r.get("title") or "").strip(), "company": str(r.get("company") or "").strip(),
                      "period": str(r.get("period") or "").strip(), "bullets": bullets})
    keep = lambda key: [str(x).strip() for x in data.get(key) or [] if str(x).strip() and _norm(str(x)) in haystack]  # noqa: E731
    cv = {"summary": str(data.get("summary") or "").strip(), "roles": roles, "education": keep("education"),
          "skills": [str(x).strip() for x in data.get("skills") or [] if str(x).strip()],
          "languages": keep("languages")}
    return cv, dropped


def structured_cv(cv_hash: str) -> dict:
    """Read the saved CV Markdown into roles and bullets (cached per CV in data/private/)."""
    settings = get_settings()
    short = cv_hash[:12]
    cache = settings.private / f"cvdata_{short}.json"
    if cache.exists():
        return json.loads(cache.read_text())
    source_path = settings.private / f"cv_{short}.md"
    if not source_path.exists():
        raise RuntimeError("The CV text is not available. Load your CV again.")
    source = source_path.read_text(encoding="utf-8")
    answer = models.chat_json(EXTRACT_SYSTEM, source[:12000], fake=lambda _u: parse_markdown_cv(source),
                              max_tokens=3000, num_ctx=8192)
    cv, dropped = _verify_extraction(answer, source)
    if not any(r["bullets"] for r in cv["roles"]):  # model gave nothing usable: fall back to the rule-based reader
        cv, dropped = _verify_extraction(parse_markdown_cv(source), source)
    cv["skills"] = list(dict.fromkeys(cv["skills"] + rules.skills(source)))
    cv["_dropped_unverified"] = dropped
    cache.write_text(json.dumps(cv, ensure_ascii=False, indent=1))
    return cv


# ---------- 2. tailoring ----------

SELECT_SYSTEM = """You tailor a CV to one job. You may only choose from the CV items you are given.
Answer with one JSON object:
  roles: list of {"id": role id, "bullets": [bullet ids, most relevant first]} (omit roles that do not help)
  skills: up to 14 skills, copied exactly from the CV skill list, most relevant first
  summary: 2 or 3 sentences in English for the top of the CV, using only facts from the CV items
Never add a skill, tool, number, employer or achievement that is not in the CV items."""


def _job_block(job: dict) -> str:
    return (f"Title: {job.get('title')}\nCompany: {job.get('company')}\nLocation: {job.get('location')}\n"
            f"Skills in ad: {', '.join(job.get('skills') or [])}\nSummary: {job.get('summary') or ''}\n\n"
            f"Ad text:\n{(job.get('description') or '')[:3000]}")


def _cv_block(cv: dict) -> str:
    lines = []
    for r in cv["roles"]:
        lines.append(f"ROLE {r['id']}: {r['title']}, {r['company']} ({r['period']})")
        lines += [f"  [{b['id']}] {b['text']}" for b in r["bullets"]]
    lines.append("CV skill list: " + ", ".join(cv["skills"]))
    if cv.get("summary"):
        lines.append("CV profile: " + cv["summary"])
    return "\n".join(lines)


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9+#.]{3,}", text.lower())}


def _fake_select(user: str, cv: dict, job: dict) -> dict:
    """Deterministic stand-in: rank bullets by word overlap with the ad."""
    want = _words(_job_block(job))
    roles = []
    for r in cv["roles"]:
        ranked = sorted(r["bullets"], key=lambda b: len(_words(b["text"]) & want), reverse=True)
        roles.append({"id": r["id"], "bullets": [b["id"] for b in ranked]})
    skills = sorted(cv["skills"], key=lambda s: s.lower() not in want)
    first = cv["summary"].split(". ")[0].rstrip(".") + "." if cv.get("summary") else "Experienced professional."
    return {"roles": roles, "skills": skills[:14], "summary": first}


def select_content(cv: dict, job: dict, limit_roles: int = 4) -> tuple[dict, dict]:
    """Returns (tailored CV dict, stats). Only verified bullets and CV skills survive."""
    prompt = f"JOB\n{_job_block(job)}\n\nCV ITEMS\n{_cv_block(cv)}"
    answer = models.chat_json(SELECT_SYSTEM, prompt, fake=lambda u: _fake_select(u, cv, job),
                              max_tokens=900, num_ctx=8192)
    by_role = {r["id"]: r for r in cv["roles"]}
    by_bullet = {b["id"]: b for r in cv["roles"] for b in r["bullets"]}
    chosen, unknown = [], 0
    for pick in answer.get("roles") or []:
        role = by_role.get(pick.get("id")) if isinstance(pick, dict) else None
        if not role or any(c["id"] == role["id"] for c in chosen):
            unknown += not role
            continue
        ids = [i for i in pick.get("bullets") or [] if i in by_bullet and by_bullet[i] in role["bullets"]]
        unknown += len(pick.get("bullets") or []) - len(ids)
        chosen.append({**{k: role[k] for k in ("id", "title", "company", "period")}, "bullets": ids})
    for role in cv["roles"]:  # keep the full career timeline even if the model skipped a role
        if not any(c["id"] == role["id"] for c in chosen):
            chosen.append({**{k: role[k] for k in ("id", "title", "company", "period")}, "bullets": []})
    chosen.sort(key=lambda c: [r["id"] for r in cv["roles"]].index(c["id"]))
    for n, c in enumerate(chosen):
        cap = MAX_BULLETS[min(n, len(MAX_BULLETS) - 1)]
        ids = c["bullets"] or [b["id"] for b in by_role[c["id"]]["bullets"]][:2]
        c["bullets"] = [by_bullet[i] for i in ids[:cap]]
    known = {s.lower(): s for s in cv["skills"]}
    skills = [known[s.lower()] for s in (str(x) for x in answer.get("skills") or []) if s.lower() in known][:14]
    if not skills:
        skills = cv["skills"][:14]
    tailored = {"summary": str(answer.get("summary") or cv.get("summary") or "").strip(), "roles": chosen,
                "education": cv["education"], "skills": skills, "languages": cv["languages"]}
    return tailored, {"ids_rejected": unknown}


LETTER_SYSTEM = """You write a cover letter body for a job application, in English.
Use ONLY the evidence list for facts about the candidate. Do not invent employers, numbers, skills, degrees
or achievements. If the ad asks for something the evidence does not show, do not claim it.
Do not write the greeting, the sign-off or any names. Answer with one JSON object:
  paragraphs: 3 short paragraphs (why this role and company, 2 concrete strengths from the evidence, a closing line)"""


def _fake_letter(job: dict, tailored: dict, words: int) -> dict:
    top = [b["text"] for r in tailored["roles"] for b in r["bullets"]][:2]
    return {"paragraphs": [
        f"I am writing to apply for the {job.get('title')} position at {job.get('company') or 'your company'}.",
        "In my work so far: " + " ".join(t.rstrip(".") + "." for t in top) if top else
        "My background matches the requirements of the role.",
        "I would be glad to discuss how I can contribute and look forward to hearing from you.",
    ]}


def write_letter(job: dict, tailored: dict, profile: dict, tone: str, words: int) -> list[str]:
    evidence = [f"- {b['text']}" for r in tailored["roles"] for b in r["bullets"]]
    prompt = (f"JOB\n{_job_block(job)}\n\nCANDIDATE EVIDENCE\nHeadline: {profile.get('headline') or ''}\n"
              + "\n".join(evidence) + f"\nSkills: {', '.join(tailored['skills'])}\n\n"
              f"Tone: {tone}. About {words} words in total.")
    answer = models.chat_json(LETTER_SYSTEM, prompt, fake=lambda _u: _fake_letter(job, tailored, words),
                              max_tokens=900, num_ctx=6144, temperature=0.3)
    paras = [str(p).strip() for p in answer.get("paragraphs") or [] if str(p).strip()]
    return paras[:5] or _fake_letter(job, tailored, words)["paragraphs"]


# ---------- 3. checks ----------

_NUMBER = re.compile(r"\d[\d.,]*\s?%?")


def check_text(label: str, text: str, cv_text: str, job: dict) -> list[str]:
    """Flag numbers and skills in generated text that the CV does not contain."""
    flags = []
    allowed = _norm(cv_text)
    for number in dict.fromkeys(m.group().strip(" .,") for m in _NUMBER.finditer(text)):
        if number and _norm(number) not in allowed and number not in (job.get("title") or ""):
            flags.append(f"{label}: the number '{number}' is not in your CV. Check it or remove it.")
    cv_skills = {s.lower() for s in rules.skills(cv_text)}
    for skill in rules.skills(text):
        if skill.lower() not in cv_skills and skill.lower() not in allowed:
            flags.append(f"{label}: mentions '{skill}', which is not in your CV.")
    return flags


# ---------- 4. rendering ----------

def contact_line(identity: dict) -> str:
    return " · ".join(x for x in (identity.get("email"), identity.get("phone"), identity.get("location"),
                                  identity.get("links")) if x)


def render_cv_md(tailored: dict, identity: dict, country_key: str) -> str:
    _, profile = country_profile(country_key)
    out = [f"# {identity.get('name') or '[Your name]'}"]
    if contact_line(identity):
        out.append(contact_line(identity))
    for section in profile["cv_sections"]:
        out += ["", f"## {SECTION_TITLES[section]}"]
        if section == "profile" and tailored["summary"]:
            out.append(tailored["summary"])
        elif section == "experience":
            for r in tailored["roles"]:
                head = f"**{r['title']}**, {r['company']}" + (f" ({r['period']})" if r["period"] else "")
                out += [head] + [f"- {b['text']}" for b in r["bullets"]]
        elif section == "education":
            out += [f"- {e}" for e in tailored["education"]]
        elif section == "skills":
            out.append(", ".join(tailored["skills"]))
        elif section == "languages":
            out.append(", ".join(tailored["languages"]))
    return "\n".join(out).strip() + "\n"


def render_letter_md(paragraphs: list[str], job: dict, identity: dict, country_key: str, today: str | None = None) -> str:
    _, profile = country_profile(country_key)
    name = identity.get("name") or "[Your name]"
    out = [f"# {name}"]
    if contact_line(identity):
        out.append(contact_line(identity))
    out += ["", today or datetime.now(UTC).strftime("%d %B %Y"), "", f"**{job.get('company') or ''}**"]
    if job.get("location"):
        out.append(job["location"])
    out += ["", f"**Application: {job.get('title')}**", "", profile["salutation"], ""]
    for p in paragraphs:
        out += [p, ""]
    out += ["Kind regards,", name]
    return "\n".join(out) + "\n"


# ---------- 5. the whole step ----------

def _job_by_id(job_id: str) -> dict:
    if not storage.exists("gold.jobs_enriched"):
        raise RuntimeError("No enriched jobs yet. Run `jobradar enrich` first.")
    safe = job_id.replace("'", "''")
    rows = storage.query(f"SELECT * FROM gold_jobs_enriched WHERE job_id = '{safe}'", ["gold.jobs_enriched"]).to_pylist()
    if not rows:
        raise RuntimeError("That job is no longer in the data (ads older than the retention window are deleted).")
    job = rows[0]
    desc = storage.query(f"SELECT description FROM silver_jobs WHERE job_id = '{safe}'", ["silver.jobs"]).to_pylist()
    job["description"] = desc[0]["description"] if desc else ""
    return job


def application_path(cv_hash: str, job_id: str):
    return get_settings().private / f"app_{cv_hash[:12]}_{hashlib.sha256(job_id.encode()).hexdigest()[:10]}.json"


def prepare_application(job_id: str, country: str | None = None, cv_hash: str | None = None) -> dict:
    """Select content, write the letter, check both. Returns what the app shows and edits."""
    record = get_profile(cv_hash)
    if not record:
        raise RuntimeError("No CV loaded yet.")
    cv_hash = record["cv_hash"]
    short = cv_hash[:12]
    job = _job_by_id(job_id)
    key, profile = country_profile(country or job.get("country"))
    settings = get_settings()
    cv_text, _ = redact((settings.private / f"cv_{short}.md").read_text(encoding="utf-8"))

    path = application_path(cv_hash, job_id)
    with lineage.run("apply.prepare", "Tailor a CV and cover letter to one job from the candidate's own CV items. "
                                      "Local models, no photo, English.") as run:
        run.inputs = [lineage.private_dataset(f"cv_profile/{short}"), lineage.lake_dataset("gold.jobs_enriched")]
        cv = structured_cv(cv_hash)
        tailored, stats = select_content(cv, job)
        paragraphs = write_letter(job, tailored, record["profile"], profile["letter_tone"], profile["letter_words"])
        flags = (check_text("Profile", tailored["summary"], cv_text, job)
                 + check_text("Letter", " ".join(paragraphs), cv_text, job))
        have = {s.lower() for s in cv["skills"]}
        not_in_cv = sorted({s for s in job.get("skills") or [] if s.lower() not in have})
        result = {"country": key, "country_name": profile["name"], "notes": profile.get("notes", []),
                  "job": {k: job.get(k) for k in ("job_id", "title", "company", "location", "url", "country",
                                                  "local_language_required", "local_language")},
                  "tailored": tailored, "letter": paragraphs, "flags": flags, "not_in_cv": not_in_cv,
                  "created_at": datetime.now(UTC).isoformat()}
        path.write_text(json.dumps(result, ensure_ascii=False, indent=1))

        run.outputs = [lineage.private_dataset(f"cv_structured/{short}"),
                       lineage.private_dataset(f"application/{path.stem}")]
        run.run_facets["jobradar_cv"] = lineage.custom_facet("CvRunFacet", cv_hash=cv_hash, converter=None)
        run.run_facets["jobradar_application"] = lineage.custom_facet(
            "ApplicationRunFacet", job_id=job_id, job_content_hash=job.get("content_hash"), country_profile=key,
            country_profiles_version=_countries_file()[1], output_language=OUTPUT_LANGUAGE, photo_included=False,
            bullets_selected=sum(len(r["bullets"]) for r in tailored["roles"]),
            bullets_dropped_unverified=cv.get("_dropped_unverified", 0), ids_rejected=stats["ids_rejected"],
            flags=len(flags), skills_not_in_cv=len(not_in_cv),
            runs_locally=models.model_facet("apply")["runs_locally"])
        run.run_facets["jobradar_model_llm"] = models.model_facet("apply")
    return result
