"""Layer enumeration and GeoJSON feature reads with the full filter set."""
from .conftest import HEADERS, feature, ingest, point


def seed(client):
    """Two layers: blue-force (3 points, timestamps, sidc/source) + graphics (1 line, no ts)."""
    ingest(
        client,
        point(44.1, 33.1, layer="blue-force", name="old", sidc="SFGPUCI----K---",
              source="JBC-P", timestamp="2026-08-24T04:00:00Z"),
        point(44.2, 33.2, layer="blue-force", name="mid", sidc="SFGPUCR----K---",
              source="JBC-P", timestamp="2026-08-24T05:00:00Z"),
        point(44.3, 33.3, layer="blue-force", name="new", sidc="SHGPUCI----K---",
              source="GDT", timestamp="2026-08-24T06:00:00Z"),
        feature({"type": "LineString", "coordinates": [[10.0, 20.0], [12.0, 22.0]]},
                layer="graphics", name="AXIS"),
    )


# --- layers ---

def test_layers_empty_db(client):
    assert client.get("/v1/map/layers", headers=HEADERS).json() == []


def test_layers_aggregate_count_bbox_timerange_sources(client):
    seed(client)
    layers = {l["layer"]: l for l in client.get("/v1/map/layers", headers=HEADERS).json()}
    blue = layers["blue-force"]
    assert blue["feature_count"] == 3
    assert blue["bbox"] == [44.1, 33.1, 44.3, 33.3]
    assert blue["earliest"] == "2026-08-24T04:00:00"
    assert blue["latest"] == "2026-08-24T06:00:00"
    assert sorted(blue["sources"]) == ["GDT", "JBC-P"]

    graphics = layers["graphics"]
    assert graphics["feature_count"] == 1
    assert graphics["bbox"] == [10.0, 20.0, 12.0, 22.0]
    assert graphics["earliest"] is None
    assert graphics["sources"] == []


# --- features ---

def test_features_newest_first_with_ids(client):
    seed(client)
    feats = client.get("/v1/map/layers/blue-force/features", headers=HEADERS).json()["features"]
    assert [f["properties"]["name"] for f in feats] == ["new", "mid", "old"]
    assert all(f["id"] for f in feats)


def test_features_geojson_has_no_null_members(client):
    seed(client)
    feats = client.get("/v1/map/layers/graphics/features", headers=HEADERS).json()["features"]
    geom = feats[0]["geometry"]
    assert geom == {"type": "LineString", "coordinates": [[10.0, 20.0], [12.0, 22.0]]}
    assert None not in feats[0]["properties"].values()


def test_unknown_layer_returns_empty_collection(client):
    seed(client)
    body = client.get("/v1/map/layers/nope/features", headers=HEADERS).json()
    assert body == {"type": "FeatureCollection", "features": []}


def test_bbox_filter_hit_and_miss(client):
    seed(client)
    url = "/v1/map/layers/blue-force/features"
    hit = client.get(url, params={"bbox": "44.15,33.15,44.25,33.25"}, headers=HEADERS).json()
    assert [f["properties"]["name"] for f in hit["features"]] == ["mid"]
    miss = client.get(url, params={"bbox": "0,0,1,1"}, headers=HEADERS).json()
    assert miss["features"] == []


def test_bbox_intersects_not_contains(client):
    # a line whose bbox only PARTIALLY overlaps the query box must still match
    ingest(client, feature({"type": "LineString", "coordinates": [[0.0, 0.0], [10.0, 10.0]]}, layer="l"))
    body = client.get("/v1/map/layers/l/features", params={"bbox": "9,9,20,20"}, headers=HEADERS).json()
    assert len(body["features"]) == 1


def test_since_until_filters(client):
    seed(client)
    url = "/v1/map/layers/blue-force/features"
    since = client.get(url, params={"since": "2026-08-24T05:00:00Z"}, headers=HEADERS).json()
    assert {f["properties"]["name"] for f in since["features"]} == {"mid", "new"}
    window = client.get(
        url, params={"since": "2026-08-24T04:30:00Z", "until": "2026-08-24T05:30:00Z"}, headers=HEADERS
    ).json()
    assert [f["properties"]["name"] for f in window["features"]] == ["mid"]


def test_sidc_prefix_filter(client):
    seed(client)
    url = "/v1/map/layers/blue-force/features"
    friendly = client.get(url, params={"sidc": "SF"}, headers=HEADERS).json()
    assert {f["properties"]["name"] for f in friendly["features"]} == {"old", "mid"}
    hostile = client.get(url, params={"sidc": "SH"}, headers=HEADERS).json()
    assert {f["properties"]["name"] for f in hostile["features"]} == {"new"}


def test_limit_caps_results_newest_first(client):
    seed(client)
    body = client.get("/v1/map/layers/blue-force/features", params={"limit": 2}, headers=HEADERS).json()
    assert [f["properties"]["name"] for f in body["features"]] == ["new", "mid"]


def test_malformed_bbox_is_422(client):
    url = "/v1/map/layers/x/features"
    assert client.get(url, params={"bbox": "1,2,3"}, headers=HEADERS).status_code == 422
    assert client.get(url, params={"bbox": "a,b,c,d"}, headers=HEADERS).status_code == 422
    assert client.get(url, params={"limit": 0}, headers=HEADERS).status_code == 422
