from pathlib import Path

import pytest

from jobradar import storage

APP = str(Path(__file__).parent.parent / "app" / "streamlit_app.py")


def test_apply_page_generates_documents(pipeline):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    job = storage.read("gold.jobs_enriched").to_pylist()[0]
    at = AppTest.from_file(APP, default_timeout=60)
    at.session_state["page"] = "Apply"
    at.session_state["apply_job"] = {k: job[k] for k in ("job_id", "title", "company", "country", "url")}
    at.run()
    assert not at.exception
    next(b for b in at.button if b.label == "Generate").click()
    at.run()
    assert not at.exception
    assert "# [Your name]" in at.session_state["cv_md"]
    assert "Application:" in at.session_state["letter_md"]


def test_match_card_button_opens_apply_page(pipeline):
    AppTest = pytest.importorskip("streamlit.testing.v1").AppTest
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    next(b for b in at.button if b.label == "Find matches").click()
    at.run()
    buttons = [b for b in at.button if b.label.startswith("Create CV")]
    assert buttons, "each match card has a create button"
    buttons[0].click()
    at.run()
    assert at.session_state["page"] == "Apply"
    assert at.session_state["apply_job"]["job_id"]
