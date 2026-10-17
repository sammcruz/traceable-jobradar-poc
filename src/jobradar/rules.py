"""Deterministic extraction rules. Fast, explainable, and the first thing every field tries.

Each function returns a value or None ("rules can't tell"); the LLM only fills the Nones.
"""

from __future__ import annotations

import re

UNKNOWN = "unknown"


def _any(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


GERMAN_NO = [r"no german (required|needed)", r"german (is )?not (required|necessary|needed)",
             r"without german", r"keine deutschkenntnisse (erforderlich|notwendig)", r"english[- ]speaking team",
             r"working language is english", r"no german"]
GERMAN_NICE = [r"german (is )?(a )?(plus|bonus|nice to have|advantage)", r"deutsch(kenntnisse)? (sind |ist )?(ein )?"
               r"(plus|von vorteil|wünschenswert)", r"basic german"]
GERMAN_YES = [r"flie(ß|ss)end(e|es)?\s+deutsch", r"deutschkenntnisse", r"sehr gute(s)? deutsch", r"verhandlungssicher(e|es)?\s+deutsch",
              r"deutsch\s*\(?(c1|c2|b2)", r"german\s*\(?(c1|c2|b2)", r"fluent (in )?german", r"(business|professional)[- ]fluent german",
              r"german (is )?(a )?must", r"excellent german", r"german and english"]


def german_required(text: str, language: str) -> tuple[str | None, str]:
    if _any(GERMAN_NO, text):
        return "no", "rule"
    if _any(GERMAN_NICE, text):
        return "nice_to_have", "rule"
    if _any(GERMAN_YES, text):
        return "yes", "rule"
    if language == "de":
        return "yes", "rule:ad_written_in_german"
    return None, ""


# ---- local language: the main language of the ad's country (German in DE/AT, Swedish in SE, ...) ----

COUNTRY_LANGUAGE = {"DE": "de", "AT": "de", "SE": "sv", "NL": "nl", "FR": "fr", "IT": "it", "PL": "pl",
                    "ES": "es", "GB": "en", "IE": "en", "UK": "en"}
LANGUAGE_NAME = {"de": "german", "sv": "swedish", "nl": "dutch", "fr": "french", "it": "italian",
                 "pl": "polish", "es": "spanish"}

# Phrases in the local language itself, plus English templates built from the language name below.
NATIVE = {
    "sv": {"yes": [r"flytande svenska", r"goda kunskaper i svenska", r"svenska i tal och skrift",
                   r"beh(ä|a)rskar svenska", r"svenska (är|ar) ett krav"],
           "nice": [r"svenska (är|ar) meriterande", r"meriterande (med )?svenska"],
           "no": [r"arbetsspr(å|a)ket (är|ar) engelska", r"engelska som arbetsspr(å|a)k"]},
    "nl": {"yes": [r"vloeiend nederlands", r"uitstekende beheersing van (het )?nederlands",
                   r"nederlands in woord en geschrift", r"goede kennis van (het )?nederlands"],
           "nice": [r"nederlands (is )?(een )?(pre|plus)", r"kennis van (het )?nederlands is een pre"],
           "no": [r"werktaal is engels", r"engels als voertaal"]},
    "fr": {"yes": [r"fran(ç|c)ais courant", r"ma(î|i)trise (parfaite )?du fran(ç|c)ais",
                   r"fran(ç|c)ais (est )?(obligatoire|exig(é|e))"],
           "nice": [r"fran(ç|c)ais (est )?un (plus|atout)"],
           "no": [r"langue de travail (est )?l'anglais"]},
    "it": {"yes": [r"italiano (fluente|madrelingua)", r"ottima conoscenza (della lingua )?italiana",
                   r"italiano (è|e') (richiesto|obbligatorio)"],
           "nice": [r"italiano (è|e') (un )?(plus|preferibile)"],
           "no": [r"lingua di lavoro (è|e') l'inglese"]},
    "pl": {"yes": [r"bieg(ł|l)a znajomo(ś|s)(ć|c) j(ę|e)zyka polskiego", r"j(ę|e)zyk polski (jest )?wymagany"],
           "nice": [], "no": []},
    "es": {"yes": [r"espa(ñ|n)ol (nativo|fluido|avanzado)", r"dominio del espa(ñ|n)ol"], "nice": [], "no": []},
}
ENGLISH_WORKING_LANGUAGE = [r"english[- ]speaking team", r"working language is english",
                            r"english is the (working|company) language"]


def _templates(name: str) -> dict:
    return {
        "no": [rf"no {name} (required|needed)", rf"{name} (is )?not (required|necessary|needed)",
               rf"without {name}", rf"no {name}\b"] + ENGLISH_WORKING_LANGUAGE,
        "nice": [rf"{name} (is )?(a )?(plus|bonus|nice to have|advantage)", rf"basic {name}"],
        "yes": [rf"fluent (in )?{name}", rf"{name}\s*\(?(c1|c2|b2)", rf"(business|professional)[- ]fluent {name}",
                rf"{name} (is )?(a )?must", rf"excellent {name}", rf"{name} and english"],
    }


def local_language(country: str | None) -> str | None:
    """Main language of the ad's country, or None when we cannot say (e.g. Switzerland has four)."""
    return COUNTRY_LANGUAGE.get((country or "").upper())


def local_language_required(text: str, ad_language: str, local: str | None,
                            from_source: str | None = None) -> tuple[str | None, str]:
    """Does the ad need the country's local language? yes | no | nice_to_have | None (cannot tell).

    Order: what the source API says, then explicit phrases, then 'the ad itself is written in that language'.
    """
    if not local:
        return None, ""
    if from_source in ("yes", "no", "nice_to_have"):
        return from_source, "source"
    if local == "en":
        return "no", "rule:english_speaking_country"
    if local == "de":
        return german_required(text, ad_language)
    name, native = LANGUAGE_NAME.get(local, local), NATIVE.get(local, {})
    tpl = _templates(name)
    for label, key in (("no", "no"), ("nice_to_have", "nice"), ("yes", "yes")):
        if _any(native.get(key, []) + tpl[key], text):
            return label, "rule"
    if ad_language == local:
        return "yes", f"rule:ad_written_in_{name}"
    return None, ""


def remote_mode(text: str, remote_source: bool | None) -> tuple[str | None, str]:
    if re.search(r"\bhybrid\b|hybrides arbeiten|teilweise (remote|homeoffice)", text, re.IGNORECASE):
        return "hybrid", "rule"
    if remote_source is True or _any([r"100\s*%\s*remote", r"fully remote", r"remote[- ]first", r"full[- ]remote",
                                       r"komplett remote", r"vollständig remote"], text):
        return "remote", "source" if remote_source is True else "rule"
    if _any([r"home\s*office", r"mobiles arbeiten", r"mobile work"], text):
        return "hybrid", "rule"
    if _any([r"\bon[- ]?site\b", r"\bvor ort\b", r"in our office", r"präsenz"], text):
        return "onsite", "rule"
    return None, ""


_NUM = r"(\d{1,3}(?:[.,\s]\d{3})+|\d{2,3}\s?[kK]|\d{4,6})"
SALARY = re.compile(
    rf"(?:€|eur|euro)?\s*{_NUM}\s*(?:€|eur|euro)?\s*(?:[-–]|bis|to)\s*(?:€|eur|euro)?\s*{_NUM}\s*(?:€|eur|euro)"
    rf"|(?:€|eur)\s*{_NUM}(?:\s*(?:[-–]|bis|to)\s*{_NUM})?"
    rf"|{_NUM}\s*(?:€|eur|euro)(?!\w)"
    rf"|(\d{{2,3}}(?:[.,]\d{{2}})?)\s*(?:€|eur|euro)(?!\w)(?=[^\n]{{0,25}}(?:stunde|hour|/\s*h\b|monat|month))",
    re.IGNORECASE,
)


def _to_number(raw: str) -> float | None:
    raw = raw.strip().replace(" ", "")
    if raw.lower().endswith("k"):
        return float(raw[:-1]) * 1000
    digits = re.sub(r"[.,]", "", raw)
    return float(digits) if digits.isdigit() else None


def salary(text: str) -> dict | None:
    for m in SALARY.finditer(text):
        nums = [_to_number(g) for g in m.groups() if g]
        nums = [n for n in nums if n]
        if not nums:
            continue
        window = text[max(0, m.start() - 60): m.end() + 60].lower()
        if re.search(r"stunde|/\s*h\b|per hour|hourly", window):
            period = "hour"
        elif re.search(r"monat|month|mtl", window):
            period = "month"
        else:
            period = "year"
        lo, hi = min(nums), max(nums)
        if period == "year" and hi < 10000:  # "45 €" is probably not an annual salary
            continue
        return {"salary_min": lo, "salary_max": hi, "salary_currency": "EUR", "salary_period": period,
                "salary_text": text[m.start(): m.end()].strip()}
    return None


VISA_NO = [r"no visa sponsorship", r"(not|unable to) (able to )?(provide|offer|sponsor)\s+(a )?visa",
           r"cannot sponsor", r"must (already )?have (a )?(valid )?(eu )?work permit", r"eu work permit required",
           r"gültige arbeitserlaubnis", r"keine visa"]
VISA_YES = [r"visa sponsorship", r"visa support", r"we sponsor", r"sponsor (your )?visa", r"blue card",
            r"blaue karte", r"visum", r"relocation and visa"]


def visa(text: str, visa_source: bool | None) -> tuple[str | None, str]:
    if visa_source is True:
        return "yes", "source"
    if _any(VISA_NO, text):
        return "no", "rule"
    if _any(VISA_YES, text):
        return "yes", "rule"
    return None, ""


def contract_type(text: str, job_types: list[str]) -> tuple[str | None, str]:
    blob = " ".join(job_types) + "\n" + text
    checks = [
        ("working_student", [r"werkstudent", r"working student"]),
        ("internship", [r"praktikum", r"internship", r"\bintern\b", r"pflichtpraktik"]),
        ("apprenticeship", [r"ausbildung zum", r"ausbildung zur", r"apprenticeship", r"\bazubi"]),
        ("freelance", [r"freelance", r"freiberuf", r"contractor", r"selbstständig"]),
        ("permanent", [r"unbefristet", r"permanent", r"festanstellung", r"open[- ]ended"]),
        ("fixed_term", [r"(?<!un)befristet", r"fixed[- ]term", r"temporary", r"elternzeitvertretung"]),
    ]
    source_blob = " ".join(job_types)
    for label, pats in checks:
        if source_blob and _any(pats, source_blob):
            return label, "source"
    for label, pats in checks:
        if _any(pats, blob):
            return label, "rule"
    return None, ""


def work_time(text: str, job_types: list[str]) -> tuple[str | None, str]:
    blob = " ".join(job_types) + "\n" + text
    full = _any([r"vollzeit", r"full[- ]time"], blob)
    part = _any([r"teilzeit", r"part[- ]time"], blob)
    if full and part:
        return "full_or_part_time", "rule"
    if full:
        return "full_time", "rule"
    if part:
        return "part_time", "rule"
    return None, ""


def seniority(title: str) -> tuple[str | None, str]:
    t = title.lower()
    if re.search(r"\b(head of|lead|leiter|leitung|principal|staff|teamlead|teamleiter)\b", t):
        return "lead", "rule"
    if re.search(r"\b(senior|sr\.?)\b", t):
        return "senior", "rule"
    if re.search(r"\b(junior|jr\.?|einsteiger|graduate|trainee|werkstudent|praktik)", t):
        return "junior", "rule"
    return None, ""


SKILLS = [
    "Python", "SQL", "Java", "JavaScript", "TypeScript", "Golang", "Rust", "C#", "C++", "Scala", "Kotlin", "PHP",
    "Ruby", "R", "Spark", "Databricks", "dbt", "Airflow", "Kafka", "Flink", "Delta Lake", "Iceberg",
    "Snowflake", "BigQuery", "Redshift", "Microsoft Fabric", "Power BI", "Tableau", "Looker", "Excel",
    "PostgreSQL", "MySQL", "MongoDB", "Redis", "Elasticsearch", "AWS", "Azure", "GCP", "Kubernetes", "Docker",
    "Terraform", "Ansible", "Linux", "Git", "CI/CD", "React", "Angular", "Vue", "Node.js", "Django", "FastAPI",
    "Spring", ".NET", "PyTorch", "TensorFlow", "scikit-learn", "LLM", "RAG", "MLOps", "NLP", "Computer Vision",
    "Figma", "SAP", "Salesforce", "Jira", "Scrum", "OpenLineage", "Data Modeling", "ETL", "Pandas",
]
_SKILL_PATTERNS = {s: re.compile(r"(?<![\w])" + re.escape(s) + r"(?![\w])", re.IGNORECASE) for s in SKILLS}
_CASE_SENSITIVE = {"R"}  # too ambiguous in lower case


def skills(text: str) -> list[str]:
    found = []
    for s, pat in _SKILL_PATTERNS.items():
        if s in _CASE_SENSITIVE:
            if re.search(r"(?<![\w])" + re.escape(s) + r"(?![\w+#&'-])", text):
                found.append(s)
        elif pat.search(text):
            found.append(s)
    return found
