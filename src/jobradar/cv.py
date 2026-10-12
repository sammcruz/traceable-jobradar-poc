"""Your CV: PDF -> Markdown -> profile -> vector. Everything stays in data/private/.

Only a hash of the CV ever appears in lineage, and the profile deliberately leaves out your name
and contact details: matching needs skills and experience, not identity.
PDF conversion uses Docling (LF AI & Data) when installed (`pip install -e ".[cv]"`), else pypdf.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from . import lineage, models, rules
from .config import get_settings
from .privacy import redact

PROFILE_SYSTEM = """You read a CV and describe the candidate's professional profile as one JSON object.
Keys:
  headline: one line, e.g. "Data engineering lead, 8 years, lakehouse and BI"
  years_experience: integer or null
  seniority: junior | mid | senior | lead
  skills: up to 25 concrete skills and tools
  languages: list of {"language": ..., "level": ...} with CEFR level or "native" when stated
  desired_roles: up to 5 job titles this person fits
  domains: up to 5 industries or domains
Never include the person's name, e-mail, phone, address, birth date, nationality or photo description."""


def convert_to_markdown(path: Path) -> tuple[str, str]:
    """Returns (markdown, converter used)."""
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        return path.read_text(encoding="utf-8"), "plain-text"
    if suffix != ".pdf":
        raise ValueError("Please provide a PDF, Markdown or text file.")
    try:
        from docling.document_converter import DocumentConverter

        result = DocumentConverter().convert(str(path))
        return result.document.export_to_markdown(), "docling"
    except ImportError:
        from pypdf import PdfReader

        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)
        return text, "pypdf"


def _fake_profile(user: str) -> dict:
    return {"headline": "Candidate profile (fake model)", "years_experience": None, "seniority": "mid",
            "skills": rules.skills(user), "languages": [], "desired_roles": [], "domains": []}


def profile_text(profile: dict) -> str:
    langs = ", ".join(f"{x.get('language')} {x.get('level', '')}".strip() for x in profile.get("languages") or [])
    return "\n".join([
        profile.get("headline") or "",
        "Roles: " + ", ".join(profile.get("desired_roles") or []),
        "Skills: " + ", ".join(profile.get("skills") or []),
        "Domains: " + ", ".join(profile.get("domains") or []),
        "Languages: " + langs,
    ])


def load_cv(path: str | Path) -> dict:
    path = Path(path).expanduser().resolve()
    settings = get_settings()
    settings.private.mkdir(parents=True, exist_ok=True)
    cv_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    short = cv_hash[:12]

    with lineage.run("cv.load", "Convert the CV to Markdown, extract a profile without identity data, "
                                "embed it. Local only.") as run:
        run.inputs = [lineage.private_dataset(f"cv/{short}")]
        markdown, converter = convert_to_markdown(path)
        clean, _ = redact(markdown)  # e-mails and phone numbers out before any model sees it
        profile = models.chat_json(PROFILE_SYSTEM, clean[:12000], fake=_fake_profile)
        rule_skills = rules.skills(clean)
        profile["skills"] = list(dict.fromkeys((profile.get("skills") or []) + rule_skills))[:30]
        vector = models.embed([profile_text(profile)])[0]

        (settings.private / f"cv_{short}.md").write_text(clean, encoding="utf-8")
        record = {"cv_hash": cv_hash, "converter": converter, "loaded_at": datetime.now(UTC).isoformat(),
                  "profile": profile, "vector": vector}
        (settings.private / f"profile_{short}.json").write_text(json.dumps(record, ensure_ascii=False, indent=1))

        run.outputs = [lineage.private_dataset(f"cv_markdown/{short}"),
                       lineage.private_dataset(f"cv_profile/{short}")]
        run.run_facets["jobradar_cv"] = lineage.custom_facet("CvRunFacet", cv_hash=cv_hash, converter=converter)
        run.run_facets["jobradar_model_llm"] = models.model_facet("cv_profile")
        run.run_facets["jobradar_model_embedding"] = models.model_facet("embedding")
    return record


def latest_profile() -> dict | None:
    files = sorted(get_settings().private.glob("profile_*.json"), key=lambda p: p.stat().st_mtime)
    return json.loads(files[-1].read_text()) if files else None


def get_profile(cv_hash: str | None) -> dict | None:
    if not cv_hash:
        return latest_profile()
    path = get_settings().private / f"profile_{cv_hash[:12]}.json"
    return json.loads(path.read_text()) if path.exists() else None
