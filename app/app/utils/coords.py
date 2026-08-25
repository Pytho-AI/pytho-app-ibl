"""Coordinate conversion utilities.

Single source of truth for lat/lon -> MGRS conversion. ATP 2-01.3 SITEMP /
MCOO doctrine prefers MGRS for unit / obstacle / polygon location columns;
upstream sources (Cougaar IDXEnemyOrderOfBattleReport, geo-agent feature
collections) return WGS-84 lat/lon decimal pairs. This module bridges the
two so the writer + exporter can always surface MGRS without each callsite
re-implementing the conversion.

The underlying conversion uses the `mgrs` package (manylinux wheel around
the open-source GEOTRANS library; standard for U.S. Army/NGA tooling).
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# Lazy single-instance MGRS converter. mgrs.MGRS() loads geotrans tables on
# construction; we share one instance across the process.
_mgrs_converter = None


def _converter():
    global _mgrs_converter
    if _mgrs_converter is None:
        import mgrs
        _mgrs_converter = mgrs.MGRS()
    return _mgrs_converter


def mgrs_to_latlon(mgrs_str: str) -> tuple[float, float] | None:
    """Convert an MGRS string to (latitude, longitude) decimal pair.

    Returns None on any conversion failure. Used by the geo-agent push
    pipeline to materialize scenario.TERRAIN entries (avenues_of_approach,
    key_terrain, obstacles) which carry MGRS, not lat/lon, into the
    map-overlay GeoJSON shape MapLibre needs."""
    if not isinstance(mgrs_str, str) or not mgrs_str.strip():
        return None
    try:
        lat, lon = _converter().toLatLon(mgrs_str.strip())
        return float(lat), float(lon)
    except Exception:
        return None


def latlon_to_mgrs(latitude: float, longitude: float, precision: int = 5) -> str:
    """Convert a (latitude, longitude) decimal pair to an MGRS string.

    `precision` is the per-axis digit count (5 = 1m, 4 = 10m, 3 = 100m,
    2 = 1km, 1 = 10km). Default 5 matches the Cougaar location-precision
    floor: enemy unit positions arrive as 4-decimal lat/lon, ~10m precision.

    Returns the MGRS string (e.g. "11SMA6430516043"). Raises ValueError when
    the inputs are not finite numbers or when the underlying converter
    rejects the coordinate (polar regions outside the MGRS grid extents
    [-80, +84] degrees latitude are the typical case).
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as e:
        raise ValueError(f"latlon_to_mgrs: non-numeric input lat={latitude!r} lon={longitude!r}") from e
    # mgrs.toMGRS returns bytes on some package versions; str() normalises.
    return str(_converter().toMGRS(lat, lon, MGRSPrecision=precision))


# Matches "<lat>,<lon>" with optional whitespace. Accepts negative + decimal
# fractions on both axes. Two captured groups: lat, lon.
_LATLON_RE = re.compile(
    r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$"
)


def coerce_to_mgrs(value: str, precision: int = 5) -> str:
    """If `value` looks like a "lat,lon" decimal pair, return its MGRS form;
    otherwise return `value` unchanged.

    Used at the exporter render layer to catch writer-emitted or upstream
    location strings that slipped through as lat/lon — the MCOO / SITEMP
    column headers declare MGRS and the lat/lon shape is doctrinally wrong.

    Returns the original value on any conversion failure (best-effort: a
    failed conversion is still better than dropping the cell).
    """
    if not isinstance(value, str):
        return value
    match = _LATLON_RE.match(value)
    if not match:
        return value
    lat_str, lon_str = match.group(1), match.group(2)
    try:
        return latlon_to_mgrs(float(lat_str), float(lon_str), precision=precision)
    except Exception as exc:
        logger.warning("coerce_to_mgrs: conversion failed for %r: %s", value, exc)
        return value


def point_to_mgrs(point: dict | None, precision: int = 5) -> str:
    """Convert a GeoJSON Point dict to an MGRS string.

    `point` is the shape produced by cougaar_adapters._to_geojson_point:
        {"type": "Point", "coordinates": [longitude, latitude, altitude]}
    Returns "" when the input is missing or malformed.
    """
    if not isinstance(point, dict):
        return ""
    coords = point.get("coordinates")
    if not isinstance(coords, list) or len(coords) < 2:
        return ""
    try:
        longitude = float(coords[0])
        latitude = float(coords[1])
    except (TypeError, ValueError):
        return ""
    try:
        return latlon_to_mgrs(latitude, longitude, precision=precision)
    except Exception as exc:
        logger.warning("point_to_mgrs: conversion failed for %r: %s", point, exc)
        return ""
