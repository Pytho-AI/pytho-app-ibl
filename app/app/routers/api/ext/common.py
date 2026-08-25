"""Shared helpers for the ext routers: geometry bbox math, row<->GeoJSON
conversion, and the feature filter set used by both /map and /products."""
from datetime import datetime, timezone
from typing import List, Optional

from schemas.ext import FeatureProperties, GeoJSONFeature, GeoJSONGeometry
from utils.ext_models import ExtFeature


def to_naive_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """SQLite stores naive datetimes; normalize aware inputs to naive UTC so
    ingest-time values and filter params compare consistently."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _walk_positions(coords, out: List[List[float]]):
    if isinstance(coords, (list, tuple)) and coords:
        if isinstance(coords[0], (int, float)):
            out.append([float(coords[0]), float(coords[1])])
        else:
            for c in coords:
                _walk_positions(c, out)


def geometry_bbox(geometry: GeoJSONGeometry) -> List[float]:
    """[minLon, minLat, maxLon, maxLat] over every position in the geometry.
    Raises ValueError if no positions are found (empty/malformed coordinates)."""
    positions: List[List[float]] = []
    if geometry.type == "GeometryCollection":
        for g in geometry.geometries or []:
            _walk_positions(g.coordinates, positions)
    else:
        _walk_positions(geometry.coordinates, positions)
    if not positions:
        raise ValueError(f"{geometry.type} geometry has no coordinates")
    lons = [p[0] for p in positions]
    lats = [p[1] for p in positions]
    if not all(-180.0 <= lon <= 180.0 for lon in lons) or not all(-90.0 <= lat <= 90.0 for lat in lats):
        raise ValueError("coordinates out of range (expected lon/lat order, WGS84)")
    return [min(lons), min(lats), max(lons), max(lats)]


def row_to_feature(row: ExtFeature) -> GeoJSONFeature:
    props = dict(row.properties or {})
    props.update({
        "name": row.name,
        "sidc": row.sidc,
        "source": row.source,
        "layer": row.layer,
        "timestamp": row.timestamp.isoformat() + "Z" if row.timestamp else None,
    })
    return GeoJSONFeature(
        id=row.id,
        geometry=GeoJSONGeometry(**row.geometry),
        properties=FeatureProperties(**{k: v for k, v in props.items() if v is not None}),
    )


def apply_feature_filters(
    query,
    layers: Optional[List[str]] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    bbox: Optional[List[float]] = None,
    sidc_prefix: Optional[str] = None,
):
    """Compose the standard filter set onto an ExtFeature query.
    bbox uses rectangle intersection against the precomputed feature bbox."""
    if layers:
        query = query.filter(ExtFeature.layer.in_(layers))
    if since is not None:
        query = query.filter(ExtFeature.timestamp >= to_naive_utc(since))
    if until is not None:
        query = query.filter(ExtFeature.timestamp <= to_naive_utc(until))
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        query = query.filter(
            ExtFeature.max_lon >= min_lon,
            ExtFeature.min_lon <= max_lon,
            ExtFeature.max_lat >= min_lat,
            ExtFeature.min_lat <= max_lat,
        )
    if sidc_prefix:
        query = query.filter(ExtFeature.sidc.startswith(sidc_prefix))
    return query


def parse_bbox_param(bbox: Optional[str]) -> Optional[List[float]]:
    """Parse a 'minLon,minLat,maxLon,maxLat' query string; None passes through."""
    if not bbox:
        return None
    parts = [p.strip() for p in bbox.split(",")]
    if len(parts) != 4:
        raise ValueError("bbox must be 'minLon,minLat,maxLon,maxLat'")
    try:
        return [float(p) for p in parts]
    except ValueError:
        raise ValueError("bbox values must be numbers")
