"""External API (/api/ext/v1) models — inbound features, ingest jobs, generated reports.

Kept separate from the internal tables: everything here is written/read only by the
ext facade, so it can evolve (or be dropped) without touching UI-facing schema.

Geometry is stored as the raw GeoJSON dict plus a precomputed bounding box
(min/max lon/lat columns) so bbox filtering works with plain SQL — no PostGIS.
"""
from datetime import datetime

from sqlalchemy import Column, DateTime, Float, JSON, String, Text

from utils.database import Base, generate_uuid


class ExtFeature(Base):
    """One ingested GeoJSON feature, drawable on a map (Gaia-style layers)."""
    __tablename__ = "ext_features"

    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    layer = Column(String, nullable=False, index=True, default="default")
    name = Column(String, nullable=True)
    sidc = Column(String, nullable=True, index=True)  # MIL-STD-2525 symbol code
    source = Column(String, nullable=True, index=True)  # originating system/sensor
    timestamp = Column(DateTime, nullable=True, index=True)  # event time from payload, not ingest time
    geometry = Column(JSON, nullable=False)  # raw GeoJSON geometry dict
    properties = Column(JSON, nullable=True)  # full properties dict as received
    min_lon = Column(Float, nullable=True)
    min_lat = Column(Float, nullable=True)
    max_lon = Column(Float, nullable=True)
    max_lat = Column(Float, nullable=True)
    job_id = Column(String, nullable=True, index=True)  # ingest job that created it
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ExtJob(Base):
    """Handle for an ingest or product-generation request (the async job contract)."""
    __tablename__ = "ext_jobs"

    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    kind = Column(String, nullable=False)  # features | documents | product
    status = Column(String, nullable=False, default="queued")  # queued | processing | done | error
    detail = Column(JSON, nullable=True)  # counts, errors, produced ids
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class ExtProduct(Base):
    """A generated outbound intelligence product.

    layer_summary products complete synchronously; LLM-backed types (intsum)
    complete in the background — `status` tracks it, mirrored on the job row.
    """
    __tablename__ = "ext_products"

    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    title = Column(String, nullable=False)
    product_type = Column(String, nullable=False, default="layer_summary")
    status = Column(String, nullable=False, default="done")  # processing | done | error
    error = Column(Text, nullable=True)  # populated when status == error
    params = Column(JSON, nullable=True)  # the ProductRequest that produced it
    content = Column(JSON, nullable=True)  # stats, and for intsum: narrative + sources
    geojson = Column(JSON, nullable=True)  # FeatureCollection snapshot (the overlay)
    job_id = Column(String, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
