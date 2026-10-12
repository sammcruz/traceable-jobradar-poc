import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Every test gets its own data folder, fake models and no Marquez."""
    monkeypatch.setenv("JOBRADAR_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JOBRADAR_PROVIDER", "fake")
    monkeypatch.setenv("OPENLINEAGE_URL", "")
    from jobradar import config, models

    config.get_settings.cache_clear()
    models._client.cache_clear()
    yield
    config.get_settings.cache_clear()
    os.environ.pop("JOBRADAR_DATA_DIR", None)


@pytest.fixture
def pipeline():
    """Ingest the sample responses, enrich (with fake LLM), load the sample CV."""
    from jobradar import ingest
    from jobradar.cli import load_sample
    from jobradar.cv import load_cv
    from jobradar.enrich import run_enrich

    sample = FIXTURES / "sample_api_responses.json"
    for source in ("arbeitnow", "bundesagentur"):
        ingest.ingest(source, fetched=load_sample(sample, source), days=3650)
    run_enrich(deep=True)
    record = load_cv(FIXTURES / "sample_cv.md")
    return record
