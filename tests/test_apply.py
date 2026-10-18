import io
import json
import zipfile

from jobradar import apply, documents, lineage, storage
from jobradar.config import get_settings
from jobradar.erase import erase_cv


def _job_id():
    rows = storage.read("gold.jobs_enriched").to_pylist()
    return next(r["job_id"] for r in rows if r["category"] == "data") if any(r["category"] == "data" for r in rows) \
        else rows[0]["job_id"]


def test_parse_markdown_cv_reads_roles_and_bullets():
    cv = apply.parse_markdown_cv((__import__("pathlib").Path(__file__).parent / "fixtures" / "sample_cv.md").read_text())
    assert [r["title"] for r in cv["roles"]] == ["Senior Data Engineer", "Data Engineer"]
    assert cv["roles"][0]["period"] == "2021–today"
    assert len(cv["roles"][0]["bullets"]) == 3
    assert "Python" in cv["skills"] and any("Spanish" in x for x in cv["languages"])


def test_unverified_bullets_are_dropped():
    source = "## Experience\n**Dev**, ACME (2020)\n- Built a pipeline\n"
    data = {"roles": [{"title": "Dev", "company": "ACME", "period": "2020",
                       "bullets": ["Built a pipeline", "Led a team of 40 engineers"]}]}
    cv, dropped = apply._verify_extraction(data, source)
    assert [b["text"] for b in cv["roles"][0]["bullets"]] == ["Built a pipeline"]
    assert dropped == 1


def test_prepare_application_is_traced_and_honest(pipeline):
    result = apply.prepare_application(_job_id(), country="de")
    assert result["country"] == "de"
    cv_text = (get_settings().private / f"cv_{pipeline['cv_hash'][:12]}.md").read_text()
    for role in result["tailored"]["roles"]:
        for b in role["bullets"]:
            assert b["text"] in cv_text, "every bullet comes from the CV"
    events = [e for e in lineage.read_events() if e["job"]["name"] == "apply.prepare" and e["eventType"] == "COMPLETE"]
    assert len(events) == 1
    facets = events[0]["run"]["facets"]
    assert facets["jobradar_application"]["photo_included"] is False
    assert facets["jobradar_application"]["runs_locally"] is True
    assert facets["jobradar_cv"]["cv_hash"] == pipeline["cv_hash"]
    text = json.dumps(events[0])
    assert "Senior Data Engineer" not in text and "Example Retail" not in text, "no CV text in lineage"


def test_checks_flag_invented_numbers_and_skills():
    cv_text = "Built pipelines in Python. 7 years of experience."
    job = {"title": "Data Engineer"}
    flags = apply.check_text("Letter", "I cut costs by 40% using Kubernetes and Python over 7 years.", cv_text, job)
    assert any("40%" in f for f in flags)
    assert any("Kubernetes" in f for f in flags)
    assert not any("7" in f for f in flags) and not any("Python" in f for f in flags)


def test_country_profiles_cover_the_chosen_countries():
    assert set(apply.countries()) == {"nl", "de", "at", "ch", "gb", "dk", "no", "fi"}
    assert apply.country_profile("GB")[0] == "gb"
    assert apply.country_profile("SE")[0] == "default"
    for p in apply.countries().values():
        assert "photo" not in " ".join(p["cv_sections"]).lower()


def test_identity_is_merged_only_at_render_time(pipeline):
    result = apply.prepare_application(_job_id(), country="nl")
    stored = (get_settings().private / next(p.name for p in get_settings().private.glob("app_*.json"))).read_text()
    apply.save_identity({"name": "Alex Example", "email": "alex@example.org"})
    assert "Alex Example" not in stored
    md = apply.render_cv_md(result["tailored"], apply.load_identity(), "nl")
    assert md.startswith("# Alex Example") and "alex@example.org" in md
    letter = apply.render_letter_md(result["letter"], result["job"], apply.load_identity(), "nl", today="1 January 2027")
    assert "Dear Hiring Team," in letter and letter.rstrip().endswith("Alex Example")


def test_documents_export_without_images():
    md = "# Ana Łukasz\nana@example.org\n\n## Experience\n**Engineer**, ACME (2020–today)\n- Built **things** ✓\n"
    pdf = documents.to_pdf(md)
    assert pdf.startswith(b"%PDF")
    docx = documents.to_docx(md)
    with zipfile.ZipFile(io.BytesIO(docx)) as z:
        assert not [n for n in z.namelist() if n.startswith("word/media/")], "no photo or images"
        assert "Ana" in z.read("word/document.xml").decode()


def test_erase_removes_applications_and_identity(pipeline):
    apply.prepare_application(_job_id(), country="gb")
    apply.save_identity({"name": "Alex Example"})
    private = get_settings().private
    assert list(private.glob("app_*.json")) and list(private.glob("cvdata_*.json"))
    erase_cv()
    assert not list(private.glob("app_*.json")) and not list(private.glob("cvdata_*.json"))
    assert not (private / "identity.json").exists()
