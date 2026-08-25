"""Pydantic models for the external API (/api/ext/v1).

These drive the Swagger docs at /api/ext/docs — every route declares a
response_model from here so partners see real shapes, not free-form dicts.

GeoJSON follows RFC 7946. Features carry a small set of well-known properties
(`name`, `sidc`, `timestamp`, `source`, `layer`); anything else in `properties`
is preserved verbatim so map clients (Gaia importer, MapLibre, ArcGIS) can style
off whatever the source system sends.
"""
from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

GEOMETRY_TYPES = Literal[
    "Point", "MultiPoint", "LineString", "MultiLineString",
    "Polygon", "MultiPolygon", "GeometryCollection",
]


class GeoJSONGeometry(BaseModel):
    """RFC 7946 geometry. `coordinates` is validated structurally at ingest
    (nesting depth per type); `geometries` is only used for GeometryCollection."""
    type: GEOMETRY_TYPES
    coordinates: Optional[Any] = None
    geometries: Optional[List["GeoJSONGeometry"]] = None

    model_config = ConfigDict(json_schema_extra={
        "example": {"type": "Point", "coordinates": [44.361, 33.312]}
    })


class FeatureProperties(BaseModel):
    """Well-known properties; unknown keys are preserved."""
    model_config = ConfigDict(extra="allow")

    name: Optional[str] = Field(None, description="Display name for the feature")
    sidc: Optional[str] = Field(None, description="MIL-STD-2525 symbol identification code, e.g. SFGPUCI----K---")
    timestamp: Optional[datetime] = Field(None, description="Event/observation time (ISO 8601)")
    source: Optional[str] = Field(None, description="Originating system or sensor")
    layer: Optional[str] = Field(None, description="Target map layer; falls back to the request-level default")


class GeoJSONFeature(BaseModel):
    type: Literal["Feature"] = "Feature"
    id: Optional[str] = Field(None, description="Assigned by Pytho on ingest; echoed back on read")
    geometry: GeoJSONGeometry
    properties: FeatureProperties = Field(default_factory=FeatureProperties)

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [44.361, 33.312]},
            "properties": {
                "name": "1-2 IN BN CP",
                "sidc": "SFGPUCI----K---",
                "timestamp": "2026-08-24T06:30:00Z",
                "source": "JBC-P",
                "layer": "blue-force",
            },
        }
    })


class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: List[GeoJSONFeature]


# --- Ingest ---

class IngestJob(BaseModel):
    """Handle + status for an ingest or product-generation request."""
    job_id: str
    kind: Literal["features", "documents", "product"]
    status: Literal["queued", "processing", "done", "error"]
    detail: Optional[dict] = Field(None, description="Counts, produced ids, and per-item errors")
    created_at: datetime
    completed_at: Optional[datetime] = None


class FeatureIngestResult(BaseModel):
    job_id: str
    status: str
    accepted: int
    rejected: int
    errors: List[str] = Field(default_factory=list, description="One entry per rejected feature, capped at 20")


class DocumentIngestResult(BaseModel):
    job_id: str
    status: str
    stored: List[str] = Field(default_factory=list, description="Server paths of stored files, served under /files/")


# --- Map ---

class LayerInfo(BaseModel):
    layer: str
    feature_count: int
    bbox: Optional[List[float]] = Field(None, description="[minLon, minLat, maxLon, maxLat] over the layer")
    earliest: Optional[datetime] = None
    latest: Optional[datetime] = None
    sources: List[str] = Field(default_factory=list)


# --- Products ---

class ProductRequest(BaseModel):
    product_type: Literal["layer_summary", "intsum"] = Field(
        "layer_summary",
        description=(
            "layer_summary: synchronous statistics (counts/bbox/time-range) plus a GeoJSON overlay. "
            "intsum: LLM-generated narrative intelligence summary over the matching observations, "
            "with every claim cited back to a numbered source observation — completes in the "
            "background, poll the returned job_id."
        ),
    )
    title: Optional[str] = None
    guidance: Optional[str] = Field(
        None, description="intsum only: free-text steer for the analyst focus (e.g. 'focus on river crossings')"
    )
    layers: Optional[List[str]] = Field(None, description="Restrict to these layers; omit for all")
    since: Optional[datetime] = Field(None, description="Only features with timestamp >= since")
    until: Optional[datetime] = Field(None, description="Only features with timestamp <= until")
    bbox: Optional[List[float]] = Field(
        None, min_length=4, max_length=4,
        description="[minLon, minLat, maxLon, maxLat] spatial filter",
    )

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "product_type": "intsum",
            "title": "Morning INTSUM — blue force",
            "layers": ["blue-force"],
            "since": "2026-08-24T00:00:00Z",
            "guidance": "focus on changes since the last reporting period",
        }
    })


class ProductSummary(BaseModel):
    product_id: str
    title: str
    product_type: str
    status: Literal["processing", "done", "error"]
    created_at: datetime
    formats: List[str] = Field(default_factory=lambda: ["json", "geojson"])


class Product(ProductSummary):
    params: Optional[dict] = None
    error: Optional[str] = Field(None, description="Failure reason when status is error")
    content: Optional[dict] = Field(
        None,
        description=(
            "layer_summary: counts, bbox, time range, breakdowns. "
            "intsum: statistics immediately; narrative (claims cite observations as [n]) "
            "and sources (the numbered observation → feature id/source mapping) are "
            "added when status reaches done."
        ),
    )


class ProductGenerationResult(BaseModel):
    job_id: str
    status: str
    product: ProductSummary
