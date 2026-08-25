"""Embedding service wrapper — talks to the ollama service in docker-compose.

The ollama service serves `embeddinggemma:300m` (Google's EmbeddingGemma 300M)
on port 11434. docker-api reaches it via Docker DNS at ollama:11434 — the
OLLAMA_HOST env var is set by docker-compose.yml. For host-side testing
(e.g. via SSH into spark-dev) the legacy SERVICE_HOST:11434 path still
works as a fallback when OLLAMA_HOST is unset.

Used by utils/search.py for both:
  - corpus indexing (insert_by_file_id chunks + embeds + writes to ES)
  - query retrieval (top_k_similar_chunks embeds the query, runs kNN on
    the doctrine_chunks / documents ES index)

Embedding dimensionality and embedding-space MUST match what the
existing ES index was built with — switching models invalidates every
indexed vector. EmbeddingGemma 300M outputs 768-dim vectors; the
`doctrine_chunks` ES index's `embedding` field must declare
`dims: 768` (utils/search.py:_ensure_index probes the dim from the
first chunk's embedding and creates the index accordingly).
"""
import os
import requests
from tenacity import retry, wait_random_exponential, stop_after_attempt

EMBEDDING_MODEL = "embeddinggemma:300m"


def _embedding_api_url() -> str:
    """Resolve the ollama /api/embed endpoint.

    Priority:
      1. OLLAMA_HOST env (e.g. "ollama:11434" — Docker DNS in compose).
         Accepts bare host:port OR a full http(s):// URL.
      2. SERVICE_HOST env + literal :11434 (legacy path; falls through
         to host.docker.internal:11434 when neither is set so unit
         tests on a laptop with a host-side ollama still work).
    """
    h = os.getenv("OLLAMA_HOST", "").strip()
    if h:
        if "://" in h:
            return h.rstrip("/") + "/api/embed"
        return f"http://{h}/api/embed"
    service_host = os.getenv("SERVICE_HOST", "host.docker.internal")
    return f"http://{service_host}:11434/api/embed"


@retry(wait=wait_random_exponential(min=1, max=20), stop=stop_after_attempt(6))
def get_embedding(text):
    request = {
        "model": EMBEDDING_MODEL,
        "input": text,
    }
    resp = requests.post(_embedding_api_url(), json=request, timeout=60)
    resp.raise_for_status()
    return resp.json()["embeddings"][0]
