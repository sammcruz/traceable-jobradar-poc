"""jobradar local web page. Run: `jobradar app` (or `streamlit run app/streamlit_app.py`).

Everything stays on this machine: the CV you upload is converted locally and the original
upload is deleted right after; only the Markdown and profile stay in data/private/.
"""

from __future__ import annotations

import json
import tempfile
from collections import Counter
from pathlib import Path

import pandas as pd
import streamlit as st

from jobradar import lineage, storage
from jobradar.config import get_settings

st.set_page_config(page_title="jobradar", page_icon="🧭", layout="wide")
settings = get_settings()
LABELS = {c["id"]: c["label"] for c in settings.categories}
PRETTY = {"yes": "Yes", "no": "No", "nice_to_have": "Nice to have", "unknown": "–", "remote": "Remote",
          "hybrid": "Hybrid", "onsite": "Onsite", None: "–"}


@st.cache_data(ttl=60)
def load_jobs() -> pd.DataFrame:
    if not storage.exists("gold.jobs_enriched"):
        return pd.DataFrame()
    return storage.read("gold.jobs_enriched").to_pandas()


def salary_label(row) -> str:
    if pd.isna(row.get("salary_min")):
        return "–"
    lo, hi, per = row["salary_min"], row["salary_max"], row.get("salary_period") or "year"
    fmt = (lambda v: f"{v / 1000:.0f}k") if per == "year" else (lambda v: f"{v:,.0f}")
    return f"€{fmt(lo)}" + (f"–{fmt(hi)}" if hi and hi != lo else "") + f" / {per}"


# ---------------- sidebar: filters ----------------
st.sidebar.title("🧭 jobradar")
st.sidebar.caption("EU job ads from the last 7 days · local models · traceable")
jobs = load_jobs()

with st.sidebar:
    st.subheader("Filters")
    cats = st.multiselect("Category", options=list(LABELS), format_func=LABELS.get,
                          default=[c for c in ("data", "ai_ml") if c in LABELS])
    no_local_language = st.toggle("Exclude ads that require the local language")
    remote = st.multiselect("Work mode", ["remote", "hybrid", "onsite"], format_func=PRETTY.get)
    salary_only = st.toggle("Salary stated")
    visa_only = st.toggle("Visa sponsorship mentioned")
    contract = st.multiselect("Contract", ["permanent", "fixed_term", "freelance", "internship",
                                           "working_student", "apprenticeship"])
    seniority = st.multiselect("Seniority", ["junior", "mid", "senior", "lead"])
    st.divider()
    model_note = "fake models (offline demo)" if settings.provider == "fake" else \
        f"{settings.llm_model} + {settings.embed_model} @ {settings.llm_base_url}"
    st.caption(f"Model hop: {model_note}")


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    m = pd.Series(True, index=df.index)
    if cats:
        m &= df["category"].isin(cats)
    if no_local_language:
        m &= df["local_language_required"] != "yes"
    if remote:
        m &= df["remote_mode"].isin(remote)
    if salary_only:
        m &= df["salary_min"].notna()
    if visa_only:
        m &= df["visa_sponsorship"] == "yes"
    if contract:
        m &= df["contract_type"].isin(contract)
    if seniority:
        m &= df["seniority"].isin(seniority)
    return df[m]


tab_match, tab_explore, tab_lineage, tab_privacy = st.tabs(["Match my CV", "Explore jobs", "Lineage", "Privacy"])

# ---------------- match ----------------
with tab_match:
    from jobradar.cv import latest_profile, load_cv
    from jobradar.match import Filters, run_match

    if jobs.empty:
        st.info("No jobs yet. Run `jobradar ingest` and `jobradar enrich` (or `jobradar demo`) first.")
    left, right = st.columns([1, 2])
    with left:
        st.subheader("Your CV")
        upload = st.file_uploader("PDF, Markdown or text. Processed locally.", type=["pdf", "md", "txt"])
        if upload and st.button("Read CV", type="primary"):
            suffix = Path(upload.name).suffix
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                tmp.write(upload.getbuffer())
            try:
                with st.spinner("Converting and reading your CV with the local model…"):
                    load_cv(tmp.name)
            finally:
                Path(tmp.name).unlink(missing_ok=True)  # the original upload is not kept
            st.success("CV loaded.")
        profile = latest_profile()
        if profile:
            p = profile["profile"]
            st.markdown(f"**{p.get('headline') or 'Profile'}**")
            st.caption(f"CV {profile['cv_hash'][:12]} · via {profile['converter']}")
            st.write(", ".join(p.get("skills", [])[:20]))
            langs = p.get("languages") or []
            if langs:
                st.caption("Languages: " + ", ".join(f"{x.get('language')} {x.get('level', '')}" for x in langs))
        top = st.slider("Rerank with the LLM", 3, 20, 8)
        go = st.button("Find matches", disabled=not profile or jobs.empty)

    with right:
        if go:
            filters = Filters(categories=cats, exclude_local_language_required=no_local_language, remote_modes=remote,
                              salary_only=salary_only, visa_only=visa_only, contract_types=contract,
                              seniority=seniority)
            bar = st.progress(0.0, text="Matching…")
            results = run_match(filters, shortlist=max(30, top * 3), rerank=top,
                                progress=lambda d, t: bar.progress(d / max(t, 1), text=f"Matching {d}/{t}"))
            bar.empty()
            st.session_state["results"] = results
        results = st.session_state.get("results", [])
        if results:
            st.subheader(f"Best fits ({len(results)} shortlisted)")
        for r in results:
            if r["score"] is None:
                continue
            with st.container(border=True):
                head, score = st.columns([5, 1])
                head.markdown(f"**[{r['title']}]({r['url']})**  \n{r['company']} · {r['location']}")
                score.metric("Fit", f"{r['score']}")
                st.caption(" · ".join([LABELS.get(r["category"], r["category"]),
                                       f"Local language: {PRETTY.get(r['local_language_required'], r['local_language_required'])}",
                                       PRETTY.get(r["remote_mode"], r["remote_mode"]),
                                       salary_label(r), f"visa: {r['visa_sponsorship']}",
                                       r["contract_type"], f"similarity {r['similarity']:.2f}"]))
                if r["reasons"]:
                    st.markdown("✅ " + "  \n✅ ".join(r["reasons"]))
                if r["gaps"]:
                    st.markdown("⚠️ " + "  \n⚠️ ".join(r["gaps"]))

# ---------------- explore ----------------
with tab_explore:
    view = apply_filters(jobs)
    if jobs.empty:
        st.info("No jobs yet.")
    else:
        a, b, c, d = st.columns(4)
        a.metric("Ads (filtered)", len(view))
        b.metric("Local language not required", int((view["local_language_required"] != "yes").sum()))
        c.metric("Salary stated", int(view["salary_min"].notna().sum()))
        d.metric("Visa sponsorship", int((view["visa_sponsorship"] == "yes").sum()))

        counts = view.groupby("category_label").size().sort_values(ascending=False)
        st.bar_chart(counts, horizontal=True, y_label="", x_label="ads")

        skills = Counter(s for row in view["skills"] for s in (list(row) if row is not None else []))
        if skills:
            st.caption("Most requested skills: " + ", ".join(f"{s} ({n})" for s, n in skills.most_common(15)))

        table = view.assign(salary=view.apply(salary_label, axis=1))[
            ["posted_at", "title", "company", "location", "category_label", "country", "local_language_required", "remote_mode",
             "salary", "visa_sponsorship", "contract_type", "seniority", "url"]
        ].sort_values("posted_at", ascending=False)
        st.dataframe(table, hide_index=True, width="stretch",
                     column_config={"url": st.column_config.LinkColumn("link", display_text="open")})

        st.subheader("Where did a value come from?")
        pick = st.selectbox("Job", view["job_id"], format_func=lambda j: view.set_index("job_id").loc[j, "title"])
        if pick:
            row = view.set_index("job_id").loc[pick]
            st.json({"field_sources": json.loads(row["field_sources"]), "enrich_model": row["enrich_model"],
                     "prompt_version": row["prompt_version"], "content_hash": row["content_hash"][:16]})

# ---------------- lineage ----------------
with tab_lineage:
    events = [e for e in lineage.read_events() if e["eventType"] != "START"]
    st.caption(f"{len(events)} completed or failed runs in {settings.lineage_log}")
    if settings.openlineage_url:
        from urllib.parse import urlparse

        ui = f"http://{urlparse(settings.openlineage_url).hostname}:3000"
        st.markdown(f"Graph view in Marquez: [{ui}]({ui})")
    rows = [{
        "time": e["eventTime"][:19].replace("T", " "), "job": e["job"]["name"], "status": e["eventType"],
        "inputs": ", ".join(d["name"] for d in e.get("inputs", [])),
        "outputs": ", ".join(d["name"] for d in e.get("outputs", [])),
        "models": ", ".join(f["model"] for k, f in e["run"].get("facets", {}).items() if k.startswith("jobradar_model")),
    } for e in reversed(events[-50:])]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

# ---------------- privacy ----------------
with tab_privacy:
    st.subheader("Your data stays here")
    st.markdown(
        f"- CV files and profile: `{settings.private}`\n"
        "- Lineage only stores a hash of your CV, never its content.\n"
        "- Job ads: contact names, e-mails and phone numbers are removed at ingest; ads older than the "
        "retention window are deleted and vacuumed.\n"
        f"- Model endpoint: `{settings.llm_base_url}`"
        + (" (local)" if "localhost" in settings.llm_base_url or settings.provider == "fake" else
           " ⚠️ not local: prompts leave this machine")
    )
    if st.button("Erase my CV and everything derived from it", type="secondary"):
        from jobradar.erase import erase_cv

        report = erase_cv()
        st.session_state.pop("results", None)
        st.success("Erased. Found through lineage:")
        st.json(report)
