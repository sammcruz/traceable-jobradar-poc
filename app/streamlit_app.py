"""jobradar local web page. Run: `jobradar app` (or `streamlit run app/streamlit_app.py`).

Everything stays on this machine: the CV you upload is converted locally and the original
upload is deleted right after; only the Markdown and profile stay in data/private/.
Tailored CVs and letters are written by the local model, without a photo, and never leave data/private/.
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


PAGES = ["Match my CV", "Apply", "Explore jobs", "Lineage", "Privacy"]
st.session_state.setdefault("page", PAGES[0])
page = st.segmented_control("Page", PAGES, key="page", label_visibility="collapsed") or PAGES[0]


def open_apply(job: dict) -> None:
    """Button callback on a match card: remember the job and switch to the Apply page."""
    st.session_state["apply_job"] = {k: job.get(k) for k in ("job_id", "title", "company", "country", "url")}
    st.session_state.pop("application", None)
    st.session_state["page"] = "Apply"


# ---------------- match ----------------
if page == "Match my CV":
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
                st.button("Create CV & cover letter for this job", key=f"apply_{r['job_id']}",
                          on_click=open_apply, args=(r,))

# ---------------- apply ----------------
if page == "Apply":
    from urllib.parse import urlparse

    from jobradar import apply as app_mod
    from jobradar import documents
    from jobradar.cv import latest_profile as _latest_profile

    job = st.session_state.get("apply_job")
    cv_record = _latest_profile()
    llm_host = urlparse(settings.llm_base_url).hostname or ""
    model_is_local = settings.provider == "fake" or llm_host in ("localhost", "127.0.0.1", "::1")

    if not job:
        st.info("Pick a job on the **Match my CV** page and press *Create CV & cover letter for this job*.")
    elif not cv_record:
        st.info("Load your CV first (Match my CV page).")
    else:
        st.subheader(f"{job['title']}")
        st.caption(f"{job['company']} · [open the ad]({job['url']})")
        if not model_is_local:
            st.error(f"The model endpoint ({llm_host}) is not on this machine. Documents are only generated with local "
                     "models. Point LLM_BASE_URL at your local Ollama.")

        profiles = app_mod.countries()
        detected, _ = app_mod.country_profile(job.get("country"))
        keys = list(profiles)
        pick = st.selectbox("Country conventions", keys, index=keys.index(detected) if detected in keys else 0,
                            format_func=lambda k: profiles[k]["name"])
        for note in profiles[pick].get("notes", []):
            st.caption("• " + note)
        if detected == "default":
            st.caption(f"The ad's country ({job.get('country') or 'unknown'}) has no profile of its own: choose the closest one.")
        st.caption("Output: English, no photo (even if your CV has one). Nothing is sent outside this machine.")

        with st.expander("Your name and contact details (stored only here, never sent to the model)"):
            ident = app_mod.load_identity()
            c1, c2 = st.columns(2)
            name = c1.text_input("Name", ident.get("name", ""))
            email = c2.text_input("E-mail", ident.get("email", ""))
            phone = c1.text_input("Phone", ident.get("phone", ""))
            place = c2.text_input("Location", ident.get("location", ""))
            links = st.text_input("Links (LinkedIn, GitHub, portfolio)", ident.get("links", ""))
            if st.button("Save contact details"):
                app_mod.save_identity({"name": name, "email": email, "phone": phone, "location": place, "links": links})
                if "application" in st.session_state:  # refresh the header lines of both documents
                    app = st.session_state["application"]
                    st.session_state["cv_md"] = app_mod.render_cv_md(app["tailored"], app_mod.load_identity(), app["country"])
                    st.session_state["letter_md"] = app_mod.render_letter_md(app["letter"], app["job"],
                                                                             app_mod.load_identity(), app["country"])
                st.success("Saved.")

        if st.button("Generate", type="primary", disabled=not model_is_local):
            try:
                with st.spinner("Writing with the local model. This can take a minute…"):
                    app = app_mod.prepare_application(job["job_id"], country=pick, cv_hash=cv_record["cv_hash"])
                st.session_state["application"] = app
                st.session_state["cv_md"] = app_mod.render_cv_md(app["tailored"], app_mod.load_identity(), app["country"])
                st.session_state["letter_md"] = app_mod.render_letter_md(app["letter"], app["job"],
                                                                         app_mod.load_identity(), app["country"])
            except RuntimeError as exc:
                st.error(str(exc))

        app = st.session_state.get("application")
        if app and app["job"]["job_id"] == job["job_id"]:
            if app["job"].get("local_language_required") == "yes":
                st.warning(f"This ad probably requires {app['job'].get('local_language') or 'the local language'}. "
                           "The documents are in English.")
            if app["flags"]:
                st.warning("Check before sending:\n\n" + "\n".join(f"- {f}" for f in app["flags"]))
            else:
                st.success("Every bullet comes word for word from your CV; no unknown numbers or skills found.")
            if app["not_in_cv"]:
                st.caption("Asked for in the ad but not shown in your CV (not claimed): " + ", ".join(app["not_in_cv"]))

            left, right = st.columns(2)
            with left:
                st.markdown("**CV** (edit freely)")
                st.text_area("CV", key="cv_md", height=560, label_visibility="collapsed")
            with right:
                st.markdown("**Cover letter** (edit freely)")
                st.text_area("Letter", key="letter_md", height=560, label_visibility="collapsed")

            base = f"{(job['company'] or 'company').strip().replace(' ', '_')[:30]}"
            try:
                d1, d2 = st.columns(2)
                for col, label, key, stem in ((d1, "CV", "cv_md", "CV"), (d2, "Letter", "letter_md", "Cover_letter")):
                    text = st.session_state[key]
                    a, b, c = col.columns(3)
                    a.download_button(f"{label} .docx", documents.to_docx(text), f"{stem}_{base}.docx", key=f"dl_docx_{key}")
                    b.download_button(f"{label} .pdf", documents.to_pdf(text), f"{stem}_{base}.pdf", key=f"dl_pdf_{key}")
                    c.download_button(f"{label} .md", text, f"{stem}_{base}.md", key=f"dl_md_{key}")
            except ImportError:
                st.info('For Word and PDF export run: pip install -e ".[app]"')
            st.caption("Edits are kept in this browser session only. Generated files are not saved except a small record "
                       "(selected CV items, no contact details) in data/private/, which Erase removes.")

# ---------------- explore ----------------
if page == "Explore jobs":
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
if page == "Lineage":
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
if page == "Privacy":
    st.subheader("Your data stays here")
    st.markdown(
        f"- CV files, profile, contact details and application records: `{settings.private}`\n"
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
