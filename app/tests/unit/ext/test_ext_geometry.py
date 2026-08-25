"""Pure-helper tests: bbox math, bbox query parsing, datetime normalization."""
from datetime import datetime, timezone, timedelta

import pytest

from routers.api.ext.common import geometry_bbox, parse_bbox_param, to_naive_utc
from schemas.ext import GeoJSONGeometry


def bbox_of(gtype, coords=None, geometries=None):
    return geometry_bbox(GeoJSONGeometry(type=gtype, coordinates=coords, geometries=geometries))


def test_point_bbox():
    assert bbox_of("Point", [44.3, 33.1]) == [44.3, 33.1, 44.3, 33.1]


def test_linestring_bbox():
    assert bbox_of("LineString", [[44.1, 33.2], [44.4, 33.5]]) == [44.1, 33.2, 44.4, 33.5]


def test_polygon_bbox():
    ring = [[10.0, 20.0], [12.0, 20.0], [12.0, 22.0], [10.0, 22.0], [10.0, 20.0]]
    assert bbox_of("Polygon", [ring]) == [10.0, 20.0, 12.0, 22.0]


def test_multipolygon_bbox_spans_all_parts():
    p1 = [[[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 0.0]]]
    p2 = [[[5.0, 5.0], [6.0, 5.0], [6.0, 6.0], [5.0, 5.0]]]
    assert bbox_of("MultiPolygon", [p1, p2]) == [0.0, 0.0, 6.0, 6.0]


def test_geometry_collection_bbox():
    geoms = [
        GeoJSONGeometry(type="Point", coordinates=[1.0, 2.0]),
        GeoJSONGeometry(type="LineString", coordinates=[[3.0, 4.0], [5.0, 6.0]]),
    ]
    assert bbox_of("GeometryCollection", geometries=geoms) == [1.0, 2.0, 5.0, 6.0]


def test_positions_with_altitude_are_accepted():
    assert bbox_of("Point", [44.3, 33.1, 120.5]) == [44.3, 33.1, 44.3, 33.1]


@pytest.mark.parametrize("coords", [None, [], [[]]])
def test_empty_coordinates_raise(coords):
    with pytest.raises(ValueError, match="no coordinates"):
        bbox_of("LineString", coords)


@pytest.mark.parametrize("coords", [[200.0, 10.0], [10.0, 95.0], [-181.0, 0.0]])
def test_out_of_range_coordinates_raise(coords):
    # catches lat/lon-order mistakes as well as garbage values
    with pytest.raises(ValueError, match="out of range"):
        bbox_of("Point", coords)


def test_parse_bbox_param():
    assert parse_bbox_param("1, 2,3 ,4") == [1.0, 2.0, 3.0, 4.0]
    assert parse_bbox_param(None) is None
    assert parse_bbox_param("") is None


@pytest.mark.parametrize("raw", ["1,2,3", "1,2,3,4,5", "a,b,c,d"])
def test_parse_bbox_param_rejects_malformed(raw):
    with pytest.raises(ValueError, match="bbox"):
        parse_bbox_param(raw)


def test_to_naive_utc():
    assert to_naive_utc(None) is None
    naive = datetime(2026, 8, 24, 6, 30)
    assert to_naive_utc(naive) is naive  # already naive: untouched
    aware = datetime(2026, 8, 24, 6, 30, tzinfo=timezone(timedelta(hours=2)))
    assert to_naive_utc(aware) == datetime(2026, 8, 24, 4, 30)
