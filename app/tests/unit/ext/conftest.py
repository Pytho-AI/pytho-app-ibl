"""Shared fixtures for the external API (/api/ext/v1) tests.

Tests run against the REAL ext_app (the sub-app mounted at /api/ext in prod),
so the X-API-Key dependency wiring is exercised — only the DB session and the
upload directory are swapped out. Paths therefore have no /api/ext prefix:
the mount point belongs to the parent app.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from utils import database
from routers.api.ext import ext_app

KEY = "test-key-123"
ALT_KEY = "alt-key-456"
HEADERS = {"X-API-Key": KEY}

# Console trace of every request/response a test makes. pytest captures stdout,
# so this shows up on failures by default and on every test with -s:
#   ./virtual/bin/python -m pytest app/tests/unit/ext -s -v
BODY_LIMIT = 500


def _trim(text):
    text = " ".join(str(text).split())
    return text[:BODY_LIMIT] + f" …[{len(text) - BODY_LIMIT} more chars]" if len(text) > BODY_LIMIT else text


@pytest.fixture(autouse=True)
def _announce_test(request):
    print(f"\n{'─' * 78}")
    print(f"▶ {request.node.name}")
    doc = (request.function.__doc__ or "").strip().splitlines()
    if doc:
        print(f"  {doc[0]}")
    yield
    print(f"◀ {request.node.name} finished")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    TestingSession = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    database.Base.metadata.create_all(bind=engine)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    ext_app.dependency_overrides[database.get_db] = override_get_db
    monkeypatch.setenv("EXT_API_KEYS", f"{KEY},{ALT_KEY}")
    monkeypatch.delenv("EXT_API_KEY", raising=False)
    # ingest.py binds UPLOAD_DIR at import; repoint document storage at tmp
    monkeypatch.setattr("routers.api.ext.ingest.UPLOAD_DIR", str(tmp_path / "uploads"))

    test_client = TestClient(ext_app)
    test_client.sessionmaker = TestingSession

    # Trace every call the test makes: method, path, key request fields,
    # response status + body (trimmed). Visible with -s, and on any failure.
    original_request = test_client.request

    def traced_request(method, url, **kwargs):
        line = f"  → {method.upper()} {url}"
        if kwargs.get("params"):
            line += f"  params={dict(kwargs['params'])}"
        if not (kwargs.get("headers") or {}).get("X-API-Key"):
            line += "  (no API key)"
        print(line)
        if kwargs.get("json") is not None:
            print(f"    body: {_trim(json.dumps(kwargs['json']))}")
        if kwargs.get("files"):
            names = [f[1][0] for f in kwargs["files"]] if isinstance(kwargs["files"], list) \
                else [v[0] for v in kwargs["files"].values()]
            print(f"    files: {names}")
        response = original_request(method, url, **kwargs)
        print(f"  ← {response.status_code}  {_trim(response.text)}")
        return response

    test_client.request = traced_request
    yield test_client
    ext_app.dependency_overrides.clear()


def feature(geometry, **props):
    return {"type": "Feature", "geometry": geometry, "properties": props}


def point(lon, lat, **props):
    return feature({"type": "Point", "coordinates": [lon, lat]}, **props)


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def ingest(client, *features, layer=None):
    """POST a collection and return the parsed body, asserting HTTP 200."""
    params = {"layer": layer} if layer else {}
    r = client.post("/v1/ingest/features", json=collection(*features), params=params, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()
