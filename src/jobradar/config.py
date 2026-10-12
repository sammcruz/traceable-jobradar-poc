"""Settings from environment variables (.env supported) and YAML config files."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("JOBRADAR_CONFIG_DIR", REPO_ROOT / "config"))


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    provider: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    embed_model: str
    openlineage_url: str
    namespace: str
    sources: dict = field(default_factory=dict)
    categories: list = field(default_factory=list)

    @property
    def lake(self) -> Path:
        return self.data_dir / "lake"

    @property
    def private(self) -> Path:
        """Your CV and anything derived from it. Never leaves this folder."""
        return self.data_dir / "private"

    @property
    def lineage_log(self) -> Path:
        return self.data_dir / "lineage" / "events.jsonl"

    @property
    def vectors(self) -> Path:
        return self.data_dir / "vectors"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    sources = yaml.safe_load((CONFIG_DIR / "sources.yml").read_text())
    categories = yaml.safe_load((CONFIG_DIR / "categories.yml").read_text())["categories"]
    data_dir = Path(os.environ.get("JOBRADAR_DATA_DIR", REPO_ROOT / "data")).resolve()
    return Settings(
        data_dir=data_dir,
        provider=os.environ.get("JOBRADAR_PROVIDER", "openai"),
        llm_base_url=os.environ.get("LLM_BASE_URL", "http://localhost:11434/v1"),
        llm_api_key=os.environ.get("LLM_API_KEY", "ollama"),
        llm_model=os.environ.get("LLM_MODEL", "qwen3:8b"),
        embed_model=os.environ.get("EMBED_MODEL", "bge-m3"),
        openlineage_url=os.environ.get("OPENLINEAGE_URL", ""),
        namespace=os.environ.get("OPENLINEAGE_NAMESPACE", "jobradar"),
        sources=sources,
        categories=categories,
    )
