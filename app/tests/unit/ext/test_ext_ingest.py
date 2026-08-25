"""Feature/document ingest and the job-status contract."""
import os

from .conftest import HEADERS, collection, feature, ingest, point


# --- features ---

def test_ingest_accepts_and_reports_job(client):
    body = ingest(client, point(44.3, 33.1, name="A"), point(44.4, 33.2, name="B"))
    assert (body["status"], body["accepted"], body["rejected"]) == ("done", 2, 0)
    assert body["errors"] == []

    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["kind"] == "features"
    assert job["status"] == "done"
    assert job["detail"] == {"accepted": 2, "rejected": 0, "errors": []}
    assert job["completed_at"] is not None


def test_bad_feature_rejected_individually_not_the_batch(client):
    bad = feature({"type": "Point", "coordinates": []}, name="bad")
    body = ingest(client, point(44.3, 33.1), bad, point(44.4, 33.2))
    assert (body["accepted"], body["rejected"]) == (2, 1)
    assert body["status"] == "done"
    assert "feature[1]" in body["errors"][0]


def test_all_rejected_marks_job_error(client):
    body = ingest(client, feature({"type": "Point", "coordinates": []}))
    assert (body["accepted"], body["rejected"], body["status"]) == (0, 1, "error")


def test_empty_collection_is_a_done_noop(client):
    body = ingest(client)
    assert (body["accepted"], body["rejected"], body["status"]) == (0, 0, "done")


def test_out_of_range_coordinates_rejected(client):
    # lat/lon swapped by the sender: lat 44 is fine, "lat" 133 is not
    body = ingest(client, point(33.1, 133.0))
    assert body["rejected"] == 1
    assert "out of range" in body["errors"][0]


def test_error_list_capped_but_counts_exact(client):
    bad = [feature({"type": "Point", "coordinates": []}) for _ in range(25)]
    body = ingest(client, *bad)
    assert body["rejected"] == 25
    assert len(body["errors"]) == 20  # MAX_REPORTED_ERRORS


def test_layer_falls_back_to_query_param(client):
    ingest(client, point(1.0, 1.0), layer="from-query")
    ingest(client, point(2.0, 2.0, layer="from-props"), layer="from-query")
    layers = {l["layer"] for l in client.get("/v1/map/layers", headers=HEADERS).json()}
    assert layers == {"from-query", "from-props"}


def test_unknown_properties_are_preserved_round_trip(client):
    ingest(client, point(44.3, 33.1, layer="l1", name="CP", strength=620, custom={"a": 1}))
    feats = client.get("/v1/map/layers/l1/features", headers=HEADERS).json()["features"]
    assert feats[0]["properties"]["strength"] == 620
    assert feats[0]["properties"]["custom"] == {"a": 1}


def test_timestamp_normalized_to_utc(client):
    ingest(client, point(44.3, 33.1, layer="l1", timestamp="2026-08-24T06:30:00+02:00"))
    feats = client.get("/v1/map/layers/l1/features", headers=HEADERS).json()["features"]
    assert feats[0]["properties"]["timestamp"] == "2026-08-24T04:30:00Z"


def test_malformed_collection_is_422(client):
    r = client.post("/v1/ingest/features", json={"type": "nope", "features": []}, headers=HEADERS)
    assert r.status_code == 422
    r = client.post(
        "/v1/ingest/features",
        json=collection(feature({"type": "Blob", "coordinates": [1, 2]})),
        headers=HEADERS,
    )
    assert r.status_code == 422  # unknown geometry type rejected by the schema


# --- jobs ---

def test_unknown_job_404(client):
    assert client.get("/v1/ingest/jobs/nope", headers=HEADERS).status_code == 404


# --- documents ---

def test_document_upload_stores_files(client, tmp_path):
    r = client.post(
        "/v1/ingest/documents",
        files=[
            ("files", ("sitrep.txt", b"SITREP body", "text/plain")),
            ("files", ("annex a.pdf", b"%PDF-", "application/pdf")),
        ],
        headers=HEADERS,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert len(body["stored"]) == 2
    for rel in body["stored"]:
        assert os.path.isfile(os.path.join(tmp_path / "uploads", rel))

    job = client.get(f"/v1/ingest/jobs/{body['job_id']}", headers=HEADERS).json()
    assert job["kind"] == "documents"
    assert job["detail"]["stored"] == body["stored"]


def test_document_filenames_are_sanitized(client, tmp_path):
    r = client.post(
        "/v1/ingest/documents",
        files=[("files", ("../../etc/passwd", b"x", "text/plain"))],
        headers=HEADERS,
    )
    stored = r.json()["stored"][0]
    assert ".." not in stored and "/etc/" not in stored
    # file landed inside the job directory, nowhere else
    assert os.path.isfile(os.path.join(tmp_path / "uploads", stored))
