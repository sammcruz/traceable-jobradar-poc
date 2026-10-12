"""The model hop, kept swappable: any OpenAI-compatible endpoint (Ollama, vLLM, LiteLLM, a hosted API).

Switch models by environment variables only:
  LLM_BASE_URL, LLM_MODEL, EMBED_MODEL   (JOBRADAR_PROVIDER=fake for tests and offline demos)

Every call site records `model_facet()` in lineage, so you always know which model produced what.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from functools import lru_cache
from urllib.parse import urlparse

import requests

from .config import get_settings
from .lineage import custom_facet

PROMPT_VERSION = "2026-10-12.2"
FAKE_DIM = 256


def model_facet(purpose: str, model: str | None = None) -> dict:
    s = get_settings()
    host = urlparse(s.llm_base_url).hostname or ""
    return custom_facet(
        "ModelRunFacet",
        purpose=purpose,
        provider=s.provider,
        model=model or (s.embed_model if purpose == "embedding" else s.llm_model),
        endpoint_host=host,
        runs_locally=host in ("localhost", "127.0.0.1", "::1") or s.provider == "fake",
        prompt_version=PROMPT_VERSION,
    )


@lru_cache(maxsize=1)
def _client():
    from openai import OpenAI

    s = get_settings()
    return OpenAI(base_url=s.llm_base_url, api_key=s.llm_api_key or "none")


def _strip_reasoning(text: str) -> str:
    """Some open models (e.g. Qwen3) may emit <think>…</think> before the answer."""
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    return match.group(0) if match else text


MAX_ANSWER_TOKENS = 500  # a full answer is ~150 tokens; the cap stops runaway "thinking"


def _ollama_root() -> str | None:
    """Ollama's own API (not the OpenAI-compatible one) lets us switch reasoning off reliably."""
    s = get_settings()
    u = urlparse(s.llm_base_url)
    if u.port == 11434 or os.environ.get("LLM_BACKEND", "").lower() == "ollama":
        return f"{u.scheme}://{u.netloc}"
    return None


def _chat_ollama(root: str, system: str, user: str, temperature: float) -> str:
    s = get_settings()
    body = {
        "model": s.llm_model, "stream": False, "format": "json",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "options": {"temperature": temperature, "num_predict": MAX_ANSWER_TOKENS, "num_ctx": 4096},
        "think": False,  # Qwen3, DeepSeek-R1 & co: answer directly, no hidden reasoning
    }
    resp = requests.post(f"{root}/api/chat", json=body, timeout=600)
    if resp.status_code == 400 and "think" in resp.text.lower():  # model without a thinking switch
        body.pop("think")
        resp = requests.post(f"{root}/api/chat", json=body, timeout=600)
    resp.raise_for_status()
    return (resp.json().get("message") or {}).get("content") or ""


def _chat_openai(system: str, user: str, temperature: float) -> str:
    s = get_settings()
    resp = _client().chat.completions.create(
        model=s.llm_model,
        temperature=temperature,
        max_tokens=MAX_ANSWER_TOKENS,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return resp.choices[0].message.content or ""


def chat_json(system: str, user: str, *, fake: callable | None = None, temperature: float = 0.0) -> dict:
    """Ask the model for a JSON object. `fake` supplies the deterministic stand-in answer."""
    s = get_settings()
    if s.provider == "fake":
        return fake(user) if fake else {}
    root = _ollama_root()
    last_error = None
    for _ in range(2):
        text = _chat_ollama(root, system, user, temperature) if root else _chat_openai(system, user, temperature)
        try:
            return json.loads(_strip_reasoning(text))
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"Model did not return valid JSON: {last_error}")


def _fake_embed(text: str) -> list[float]:
    """Hashed bag of words: no model needed, similar texts still land close together."""
    vec = [0.0] * FAKE_DIM
    for token in re.findall(r"[a-zäöüß0-9+#.]{2,}", text.lower()):
        h = int(hashlib.md5(token.encode()).hexdigest(), 16)
        vec[h % FAKE_DIM] += 1.0 if (h >> 8) % 2 else -1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def embed(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    s = get_settings()
    if s.provider == "fake":
        return [_fake_embed(t) for t in texts]
    out: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        batch = [t[:6000] for t in texts[i : i + batch_size]]
        resp = _client().embeddings.create(model=s.embed_model, input=batch)
        vectors = [d.embedding for d in resp.data]
        out += [_normalise(v) for v in vectors]
    return out


def _normalise(v: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))
