"""API-key auth on the ext app: fail-closed, 401/403 split, multi-key, fallback var."""
from .conftest import ALT_KEY, HEADERS


def test_missing_key_is_401(client):
    r = client.get("/v1/map/layers")
    assert r.status_code == 401
    assert "X-API-Key" in r.json()["detail"]


def test_wrong_key_is_403(client):
    assert client.get("/v1/map/layers", headers={"X-API-Key": "nope"}).status_code == 403


def test_every_configured_key_works(client):
    assert client.get("/v1/map/layers", headers=HEADERS).status_code == 200
    assert client.get("/v1/map/layers", headers={"X-API-Key": ALT_KEY}).status_code == 200


def test_unconfigured_api_fails_closed_503(client, monkeypatch):
    monkeypatch.delenv("EXT_API_KEYS")
    r = client.get("/v1/map/layers", headers=HEADERS)
    assert r.status_code == 503
    assert "EXT_API_KEYS" in r.json()["detail"]


def test_singular_ext_api_key_fallback(client, monkeypatch):
    monkeypatch.delenv("EXT_API_KEYS")
    monkeypatch.setenv("EXT_API_KEY", "solo-key")
    assert client.get("/v1/map/layers", headers={"X-API-Key": "solo-key"}).status_code == 200
    assert client.get("/v1/map/layers", headers=HEADERS).status_code == 403


def test_keys_are_stripped_and_empty_entries_ignored(client, monkeypatch):
    monkeypatch.setenv("EXT_API_KEYS", "  spaced-key , ,")
    assert client.get("/v1/map/layers", headers={"X-API-Key": "spaced-key"}).status_code == 200
    # the empty entry must not become a valid "" key
    assert client.get("/v1/map/layers", headers={"X-API-Key": ""}).status_code == 401


def test_all_routers_are_protected(client):
    for path in ("/v1/ingest/jobs/x", "/v1/map/layers", "/v1/products"):
        assert client.get(path).status_code == 401, path
