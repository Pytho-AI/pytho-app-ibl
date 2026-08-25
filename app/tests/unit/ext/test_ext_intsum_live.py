"""LIVE intsum tests — these call the real LLM, exactly as the API endpoints do.

No mocks: the request runs through products._call_llm -> utils.llm
invoke_vllm_text_async -> the configured OpenAI-compat endpoint. Defaults to
the laptop-dev Ollama (gemma4:26b on 127.0.0.1:11434) when VLLM_API is unset;
honors VLLM_API/VLLM_MODEL when they are. Skips (visibly) when no endpoint is
reachable so the suite still runs on machines without an LLM.

Expect ~10-60s per test on gemma4:26b — this is the same latency profile the
real /v1/products intsum endpoint has.
"""
import os
import re

import pytest
import requests

DEFAULT_API = "http://127.0.0.1:11434/v1/chat/completions"
DEFAULT_MODEL = "gemma4:26b"

from .conftest import HEADERS, ingest, point


def _llm_reachable() -> bool:
    api = os.environ.get("VLLM_API", DEFAULT_API)
    base = api.split("/v1/")[0] if "/v1/" in api else api.rstrip("/")
    try:
        return requests.get(f"{base}/v1/models", timeout=3).ok
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _llm_reachable(), reason="no LLM endpoint reachable (start Ollama or set VLLM_API)"
)


@pytest.fixture()
def live_llm(monkeypatch):
    """Point utils.llm at the real endpoint and clear its process-level caches
    (model name + ollama detection), which may hold values from another test's
    environment."""
    monkeypatch.setenv("VLLM_API", os.environ.get("VLLM_API", DEFAULT_API))
    monkeypatch.setenv("VLLM_MODEL", os.environ.get("VLLM_MODEL", DEFAULT_MODEL))
    from utils import llm
    monkeypatch.setattr(llm, "_VLLM_MODEL_CACHE", None)
    monkeypatch.setattr(llm, "_OLLAMA_COMPAT_CACHE", None)


def test_intsum_live_end_to_end(client, live_llm):
    """Full endpoint flow with the real model: ingest -> generate -> poll -> fetch."""
    ingest(
        client,
        point(44.361, 33.312, layer="blue-force", name="1-2 IN BN TOC",
              sidc="SFGPUCI----K---", source="JBC-P", timestamp="2026-08-24T05:00:00Z"),
        point(44.402, 33.355, layer="red-force", name="BTG recon element",
              sidc="SHGPUCR----K---", source="GDT", timestamp="2026-08-24T06:10:00Z"),
    )
    r = client.post(
        "/v1/products",
        json={"product_type": "intsum", "title": "Live test INTSUM",
              "guidance": "assess the recon element relative to the friendly TOC"},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "processing"  # async contract holds on the live path too

    # TestClient runs the background task (the real LLM call) before returning,
    # so by now the job has completed one way or the other.
    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["status"] == "done", f"live LLM generation failed: {job}"

    product = client.get(f"/v1/products/{body['product']['product_id']}", headers=HEADERS).json()
    assert product["status"] == "done"
    assert product["error"] is None

    narrative = product["content"]["narrative"]
    assert len(narrative) > 200, f"narrative implausibly short: {narrative!r}"
    # the model was instructed that every claim must cite [n] — hold it to that
    citations = set(int(n) for n in re.findall(r"\[(\d+)\]", narrative))
    assert citations, "narrative contains no [n] citations"
    valid = {s["n"] for s in product["content"]["sources"]}
    assert citations <= valid, f"narrative cites {citations - valid} not in sources {valid}"
    # stats coexist with the narrative
    assert product["content"]["feature_count"] == 2


def test_intsum_live_empty_observation_set(client, live_llm):
    """Zero observations must still complete cleanly, not hang or hallucinate a picture."""
    r = client.post("/v1/products", json={"product_type": "intsum"}, headers=HEADERS)
    body = r.json()
    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["status"] == "done", f"live LLM generation failed: {job}"
    product = client.get(f"/v1/products/{body['product']['product_id']}", headers=HEADERS).json()
    assert product["status"] == "done"
    assert product["content"]["feature_count"] == 0
    assert product["content"]["sources"] == []
