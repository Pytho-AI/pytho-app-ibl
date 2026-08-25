"""Inbound data: GeoJSON features, document files, and job-status lookup."""
import logging
import os
import re
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

from core.config import UPLOAD_DIR
from schemas.ext import (
    DocumentIngestResult,
    FeatureCollection,
    FeatureIngestResult,
    IngestJob,
)
from utils import database
from utils.ext_models import ExtFeature, ExtJob
from .common import geometry_bbox, to_naive_utc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["ext-ingest"])

MAX_REPORTED_ERRORS = 20


@router.post("/features", response_model=FeatureIngestResult, summary="Ingest GeoJSON features")
async def ingest_features(
    collection: FeatureCollection,
    layer: str = Query("default", description="Default layer for features that don't set properties.layer"),
    db: Session = Depends(database.get_db),
):
    """Ingest a GeoJSON FeatureCollection (units, tracks, events, graphics).

    Each feature may set `properties.layer`, `sidc` (MIL-STD-2525), `timestamp`,
    `source`, and `name`; unknown properties are preserved and returned on read.
    Features with unusable geometry are rejected individually — one bad feature
    does not fail the batch. Returns a `job_id` queryable at `/ingest/jobs/{job_id}`.
    """
    job = ExtJob(kind="features", status="processing")
    db.add(job)
    db.flush()

    accepted, errors = 0, []
    for i, feature in enumerate(collection.features):
        try:
            bbox = geometry_bbox(feature.geometry)
        except ValueError as e:
            if len(errors) < MAX_REPORTED_ERRORS:
                errors.append(f"feature[{i}]: {e}")
            continue
        props = feature.properties
        raw_props = props.model_dump(mode="json", exclude_none=True)
        db.add(ExtFeature(
            layer=props.layer or layer,
            name=props.name,
            sidc=props.sidc,
            source=props.source,
            timestamp=to_naive_utc(props.timestamp),
            geometry=feature.geometry.model_dump(exclude_none=True),
            properties=raw_props,
            min_lon=bbox[0], min_lat=bbox[1], max_lon=bbox[2], max_lat=bbox[3],
            job_id=job.id,
        ))
        accepted += 1

    rejected = len(collection.features) - accepted
    job.status = "done" if accepted or not rejected else "error"
    job.detail = {"accepted": accepted, "rejected": rejected, "errors": errors}
    job.completed_at = datetime.utcnow()
    db.commit()
    logger.info("ext ingest job %s: %d accepted, %d rejected", job.id, accepted, rejected)
    return FeatureIngestResult(
        job_id=job.id, status=job.status, accepted=accepted, rejected=rejected, errors=errors,
    )


@router.post("/documents", response_model=DocumentIngestResult, summary="Ingest document files")
async def ingest_documents(
    files: List[UploadFile] = File(..., description="One or more files (reports, PDFs, docs)"),
    db: Session = Depends(database.get_db),
):
    """Store uploaded files under the app's upload area. Stored paths are served
    read-only under `/files/`. Returns a `job_id` queryable at `/ingest/jobs/{job_id}`.
    """
    job = ExtJob(kind="documents", status="processing")
    db.add(job)
    db.flush()

    dest_dir = os.path.join(UPLOAD_DIR, "ext", job.id)
    os.makedirs(dest_dir, exist_ok=True)
    stored = []
    for f in files:
        # Basename + whitelist keeps path traversal out of the filename
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename or "unnamed"))
        dest = os.path.join(dest_dir, safe_name)
        with open(dest, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
        stored.append(f"ext/{job.id}/{safe_name}")

    job.status = "done"
    job.detail = {"stored": stored}
    job.completed_at = datetime.utcnow()
    db.commit()
    logger.info("ext document job %s: %d files stored", job.id, len(stored))
    return DocumentIngestResult(job_id=job.id, status=job.status, stored=stored)


@router.get("/jobs/{job_id}", response_model=IngestJob, summary="Get job status")
async def get_job(job_id: str, db: Session = Depends(database.get_db)):
    """Status handle for any ingest or report-generation job."""
    job = db.query(ExtJob).filter(ExtJob.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return IngestJob(
        job_id=job.id, kind=job.kind, status=job.status, detail=job.detail,
        created_at=job.created_at, completed_at=job.completed_at,
    )
