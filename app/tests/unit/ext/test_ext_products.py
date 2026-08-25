"""Product generation (layer_summary sync, intsum via mocked LLM), listing, fetch."""
import pytest

from .conftest import HEADERS, ingest, point


def seed(client):
    ingest(
        client,
        point(44.1, 33.1, layer="blue-force", sidc="SFGPUCI----K---", source="JBC-P",
              name="1-2 IN BN CP", timestamp="2026-08-24T04:00:00Z"),
        point(44.3, 33.3, layer="blue-force", sidc="SFGPUCI----K---", source="GDT",
              name="TOC", timestamp="2026-08-24T06:00:00Z"),
        point(10.0, 20.0, layer="red-force", sidc="SHGPUCI----K---",
              name="BTG recon", timestamp="2026-08-24T05:00:00Z"),
    )


def generate(client, **payload):
    r = client.post("/v1/products", json=payload, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


# --- layer_summary (synchronous) ---

def test_layer_summary_returns_done_job_and_summary(client):
    seed(client)
    body = generate(client, title="Snapshot")
    assert body["status"] == "done"
    assert body["product"]["title"] == "Snapshot"
    assert body["product"]["product_type"] == "layer_summary"
    assert body["product"]["status"] == "done"
    assert sorted(body["product"]["formats"]) == ["geojson", "json"]

    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["kind"] == "product"
    assert job["detail"] == {"product_id": body["product"]["product_id"], "feature_count": 3}


def test_content_counts_bbox_and_time_range(client):
    seed(client)
    pid = generate(client)["product"]["product_id"]
    content = client.get(f"/v1/products/{pid}", headers=HEADERS).json()["content"]
    assert content["feature_count"] == 3
    assert content["by_layer"] == {"blue-force": 2, "red-force": 1}
    assert content["by_sidc"] == {"SFGPUCI----K---": 2, "SHGPUCI----K---": 1}
    assert content["by_source"] == {"JBC-P": 1, "GDT": 1}
    assert content["bbox"] == [10.0, 20.0, 44.3, 33.3]
    assert content["time_range"] == {
        "earliest": "2026-08-24T04:00:00Z", "latest": "2026-08-24T06:00:00Z",
    }
    assert content["snapshot_truncated"] is False


def test_layer_and_time_filters_scope_the_product(client):
    seed(client)
    pid = generate(client, layers=["blue-force"], since="2026-08-24T05:00:00Z")["product"]["product_id"]
    content = client.get(f"/v1/products/{pid}", headers=HEADERS).json()["content"]
    assert content["feature_count"] == 1
    assert content["by_source"] == {"GDT": 1}


def test_bbox_filter_scopes_the_product(client):
    seed(client)
    pid = generate(client, bbox=[9.0, 19.0, 11.0, 21.0])["product"]["product_id"]
    content = client.get(f"/v1/products/{pid}", headers=HEADERS).json()["content"]
    assert content["by_layer"] == {"red-force": 1}


def test_empty_db_product_is_well_formed(client):
    pid = generate(client)["product"]["product_id"]
    r = client.get(f"/v1/products/{pid}", headers=HEADERS).json()
    assert r["content"]["feature_count"] == 0
    assert r["content"]["bbox"] is None
    assert r["content"]["time_range"] == {"earliest": None, "latest": None}
    geo = client.get(f"/v1/products/{pid}", params={"format": "geojson"}, headers=HEADERS).json()
    assert geo == {"type": "FeatureCollection", "features": []}


def test_default_title_is_generated(client):
    body = generate(client)
    assert body["product"]["title"].startswith("layer_summary ")


def test_geojson_format_returns_overlay(client):
    seed(client)
    pid = generate(client, layers=["red-force"])["product"]["product_id"]
    geo = client.get(f"/v1/products/{pid}", params={"format": "geojson"}, headers=HEADERS).json()
    assert geo["type"] == "FeatureCollection"
    assert len(geo["features"]) == 1
    assert geo["features"][0]["properties"]["sidc"] == "SHGPUCI----K---"
    assert "geometries" not in geo["features"][0]["geometry"]


def test_snapshot_capped_and_flagged(client, monkeypatch):
    monkeypatch.setattr("routers.api.ext.products.SNAPSHOT_CAP", 2)
    seed(client)
    pid = generate(client)["product"]["product_id"]
    r = client.get(f"/v1/products/{pid}", headers=HEADERS).json()
    assert r["content"]["snapshot_truncated"] is True
    geo = client.get(f"/v1/products/{pid}", params={"format": "geojson"}, headers=HEADERS).json()
    assert len(geo["features"]) == 2
    assert r["content"]["feature_count"] == 3


def test_list_products_newest_first_with_limit(client):
    first = generate(client, title="first")["product"]["product_id"]
    second = generate(client, title="second")["product"]["product_id"]
    listed = client.get("/v1/products", headers=HEADERS).json()
    assert [p["product_id"] for p in listed] == [second, first]
    limited = client.get("/v1/products", params={"limit": 1}, headers=HEADERS).json()
    assert [p["product_id"] for p in limited] == [second]


def test_unknown_product_404_and_bad_format_422(client):
    assert client.get("/v1/products/nope", headers=HEADERS).status_code == 404
    pid = generate(client)["product"]["product_id"]
    assert client.get(f"/v1/products/{pid}", params={"format": "pdf"}, headers=HEADERS).status_code == 422


def test_unknown_product_type_422(client):
    assert client.post("/v1/products", json={"product_type": "nope"}, headers=HEADERS).status_code == 422


# --- intsum (LLM-backed, background) ---

@pytest.fixture()
def fake_llm(monkeypatch):
    """Capture the prompt; return a canned cited narrative."""
    calls = {}

    async def _fake(prompt):
        calls["prompt"] = prompt
        return "BLUF: friendly posture is stable [1][2]. We assess hostile recon is likely screening [3]."

    monkeypatch.setattr("routers.api.ext.products._call_llm", _fake)
    return calls


def test_intsum_async_contract_and_narrative(client, fake_llm):
    seed(client)
    body = generate(client, product_type="intsum", title="Morning INTSUM",
                    guidance="focus on the recon element")
    # the POST response is written before the background task runs
    assert body["status"] == "processing"
    assert body["product"]["status"] == "processing"

    # TestClient runs background tasks before returning, so the job is now done
    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert (job["kind"], job["status"]) == ("product", "done")

    product = client.get(f"/v1/products/{body['product']['product_id']}", headers=HEADERS).json()
    assert product["status"] == "done"
    assert product["error"] is None
    assert "[3]" in product["content"]["narrative"]
    assert product["content"]["feature_count"] == 3  # stats retained alongside the narrative


def test_intsum_prompt_contains_numbered_cited_observations(client, fake_llm):
    seed(client)
    generate(client, product_type="intsum", guidance="watch the river")
    prompt = fake_llm["prompt"]
    # newest-first numbering: TOC (06:00) is observation 1
    assert "1. [blue-force] TOC" in prompt
    assert "3. [blue-force] 1-2 IN BN CP" in prompt
    assert "sidc=SHGPUCI----K---" in prompt
    assert "MUST cite" in prompt
    assert "watch the river" in prompt


def test_intsum_sources_trace_to_feature_ids(client, fake_llm):
    seed(client)
    pid = generate(client, product_type="intsum")["product"]["product_id"]
    product = client.get(f"/v1/products/{pid}", headers=HEADERS).json()
    sources = product["content"]["sources"]
    assert [s["n"] for s in sources] == [1, 2, 3]
    assert sources[0]["name"] == "TOC"
    assert all(s["feature_id"] for s in sources)
    # every cited feature id resolves to a real ingested feature
    feats = client.get("/v1/map/layers/blue-force/features", headers=HEADERS).json()["features"]
    assert sources[0]["feature_id"] in {f["id"] for f in feats}


def test_intsum_llm_failure_marks_product_error(client, monkeypatch):
    async def _boom(prompt):
        raise RuntimeError("vllm unreachable")

    monkeypatch.setattr("routers.api.ext.products._call_llm", _boom)
    seed(client)
    body = generate(client, product_type="intsum")
    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["status"] == "error"
    assert "vllm unreachable" in job["detail"]["error"]

    product = client.get(f"/v1/products/{body['product']['product_id']}", headers=HEADERS).json()
    assert product["status"] == "error"
    assert "RuntimeError" in product["error"]
    assert "narrative" not in (product["content"] or {})


def test_intsum_observation_cap_noted_in_prompt(client, fake_llm, monkeypatch):
    monkeypatch.setattr("routers.api.ext.products.INTSUM_OBSERVATION_CAP", 2)
    seed(client)
    pid = generate(client, product_type="intsum")["product"]["product_id"]
    assert "coverage is partial" in fake_llm["prompt"]
    assert "\n3. [" not in fake_llm["prompt"].split("Observations:")[1]  # only 2 listed
    sources = client.get(f"/v1/products/{pid}", headers=HEADERS).json()["content"]["sources"]
    assert len(sources) == 2
