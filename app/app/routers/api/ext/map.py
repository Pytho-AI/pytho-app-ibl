"""Gaia-style map access: enumerate layers, pull features as GeoJSON."""
import logging
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from schemas.ext import FeatureCollection, LayerInfo
from utils import database
from utils.ext_models import ExtFeature
from .common import apply_feature_filters, parse_bbox_param, row_to_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/map", tags=["ext-map"])

MAX_FEATURES = 5000


@router.get("/layers", response_model=List[LayerInfo], summary="List map layers")
async def list_layers(db: Session = Depends(database.get_db)):
    """Enumerate every layer with feature count, combined bbox, time range, and sources."""
    rows = (
        db.query(
            ExtFeature.layer,
            func.count(ExtFeature.id),
            func.min(ExtFeature.min_lon), func.min(ExtFeature.min_lat),
            func.max(ExtFeature.max_lon), func.max(ExtFeature.max_lat),
            func.min(ExtFeature.timestamp), func.max(ExtFeature.timestamp),
        )
        .group_by(ExtFeature.layer)
        .all()
    )
    layers = []
    for layer, count, min_lon, min_lat, max_lon, max_lat, earliest, latest in rows:
        sources = [
            s for (s,) in db.query(ExtFeature.source)
            .filter(ExtFeature.layer == layer, ExtFeature.source.isnot(None))
            .distinct().all()
        ]
        bbox = [min_lon, min_lat, max_lon, max_lat] if min_lon is not None else None
        layers.append(LayerInfo(
            layer=layer, feature_count=count, bbox=bbox,
            earliest=earliest, latest=latest, sources=sources,
        ))
    return layers


@router.get(
    "/layers/{layer_id}/features",
    response_model=FeatureCollection,
    response_model_exclude_none=True,  # keep the GeoJSON clean of null members
    summary="Get layer features as GeoJSON",
)
async def get_layer_features(
    layer_id: str = Path(..., description="Layer name as returned by /map/layers"),
    bbox: Optional[str] = Query(None, description="Spatial filter: minLon,minLat,maxLon,maxLat"),
    since: Optional[datetime] = Query(None, description="Only features with timestamp >= since (ISO 8601)"),
    until: Optional[datetime] = Query(None, description="Only features with timestamp <= until (ISO 8601)"),
    sidc: Optional[str] = Query(None, description="MIL-STD-2525 SIDC prefix filter, e.g. 'SFG' for friendly ground"),
    limit: int = Query(MAX_FEATURES, ge=1, le=MAX_FEATURES, description="Max features returned, newest first"),
    db: Session = Depends(database.get_db),
):
    """GeoJSON FeatureCollection for one layer — importable directly by Gaia,
    MapLibre, ArcGIS, or anything else that speaks RFC 7946."""
    try:
        bbox_vals = parse_bbox_param(bbox)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    query = apply_feature_filters(
        db.query(ExtFeature), layers=[layer_id],
        since=since, until=until, bbox=bbox_vals, sidc_prefix=sidc,
    )
    rows = query.order_by(ExtFeature.timestamp.desc().nullslast(), ExtFeature.created_at.desc()).limit(limit).all()
    return FeatureCollection(features=[row_to_feature(r) for r in rows])
