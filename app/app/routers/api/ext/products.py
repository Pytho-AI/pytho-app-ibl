"""Outbound intelligence products: request generation, list, fetch as JSON or GeoJSON overlay.

Two product types:
- layer_summary: pure aggregation over ingested observations, completes synchronously.
- intsum: narrative intelligence summary written by the LLM (same vllm path the
  internal SITEMP / Threat COA product chains use, token-gated). Every claim in
  the narrative cites its supporting observation as [n]; the product carries the
  n -> feature id/source mapping so claims trace back to their source report.
  Runs as a background task — poll the returned job_id.
"""
import logging
from collections import Counter
from datetime import datetime
from typing import List, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy.orm import Session, sessionmaker

from schemas.ext import (
    FeatureCollection,
    Product,
    ProductGenerationResult,
    ProductRequest,
    ProductSummary,
)
from utils import database
from utils.ext_models import ExtFeature, ExtJob, ExtProduct
from .common import apply_feature_filters, row_to_feature

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/products", tags=["ext-products"])

SNAPSHOT_CAP = 5000
TOP_N = 25
INTSUM_OBSERVATION_CAP = 200  # newest N observations fed to the LLM


async def _call_llm(prompt: str) -> str:
    """LLM boundary, kept import-lazy so the router imports without the vllm
    stack, and small so tests can monkeypatch it."""
    from utils.llm import invoke_vllm_text_async

    return await invoke_vllm_text_async(
        [
            {"role": "system", "content": (
                "You are an Army all-source intelligence analyst writing for a commander. "
                "You follow ICD 203 analytic standards: use calibrated confidence language "
                "(likely / unlikely / we assess with moderate confidence), distinguish fact "
                "from assessment, and never state a fact you cannot cite."
            )},
            {"role": "user", "content": prompt},
        ],
        gate_label="ext_intsum",
    )


def _summary(p: ExtProduct) -> ProductSummary:
    return ProductSummary(
        product_id=p.id, title=p.title, product_type=p.product_type,
        status=p.status, created_at=p.created_at,
    )


def _stats(rows: List[ExtFeature]) -> dict:
    timestamps = [r.timestamp for r in rows if r.timestamp is not None]
    lons = [v for r in rows for v in (r.min_lon, r.max_lon) if v is not None]
    lats = [v for r in rows for v in (r.min_lat, r.max_lat) if v is not None]
    return {
        "feature_count": len(rows),
        "by_layer": dict(Counter(r.layer for r in rows)),
        "by_sidc": dict(Counter(r.sidc for r in rows if r.sidc).most_common(TOP_N)),
        "by_source": dict(Counter(r.source for r in rows if r.source).most_common(TOP_N)),
        "bbox": [min(lons), min(lats), max(lons), max(lats)] if lons else None,
        "time_range": {
            "earliest": min(timestamps).isoformat() + "Z" if timestamps else None,
            "latest": max(timestamps).isoformat() + "Z" if timestamps else None,
        },
        "snapshot_truncated": len(rows) > SNAPSHOT_CAP,
    }


def _observation_lines(rows: List[ExtFeature]) -> tuple[list[str], list[dict]]:
    """Number the newest observations for the prompt and build the matching
    sources table stored on the product (the [n] -> source-report trace)."""
    lines, sources = [], []
    for n, r in enumerate(rows[:INTSUM_OBSERVATION_CAP], start=1):
        center = None
        if r.min_lon is not None:
            center = f"({(r.min_lon + r.max_lon) / 2:.4f}, {(r.min_lat + r.max_lat) / 2:.4f})"
        parts = [
            f"[{r.layer}] {r.name or 'unnamed'}",
            f"sidc={r.sidc}" if r.sidc else None,
            f"source={r.source}" if r.source else None,
            r.timestamp.isoformat() + "Z" if r.timestamp else None,
            f"{r.geometry.get('type', 'geometry')} at {center}" if center else None,
        ]
        lines.append(f"{n}. " + ", ".join(p for p in parts if p))
        sources.append({
            "n": n, "feature_id": r.id, "layer": r.layer,
            "name": r.name, "sidc": r.sidc, "source": r.source,
            "timestamp": r.timestamp.isoformat() + "Z" if r.timestamp else None,
        })
    return lines, sources


def _intsum_prompt(lines: List[str], stats: dict, guidance: str | None, truncated: int) -> str:
    guidance_block = f"\nCommander's guidance for this summary: {guidance}\n" if guidance else ""
    truncation_note = (
        f"\nNote: only the {INTSUM_OBSERVATION_CAP} newest of {truncated} observations are listed; "
        "state that coverage is partial.\n" if truncated > INTSUM_OBSERVATION_CAP else ""
    )
    return (
        "Write an INTSUM-style narrative assessment of the observations below.\n\n"
        "Structure:\n"
        "- BLUF: one paragraph, the single most decision-relevant judgment.\n"
        "- Activity: what the observations show, grouped by layer/actor.\n"
        "- Assessment: what it means; if hostile activity is present, the most likely "
        "and most dangerous enemy courses of action.\n\n"
        "Rules:\n"
        "- Every factual claim MUST cite its supporting observation(s) by bracketed "
        "number, e.g. [3] or [2][5]. A claim with no citation is not allowed.\n"
        "- Use only the observations provided; do not invent units, locations, or times.\n"
        "- Use ICD 203 confidence language for assessments.\n"
        f"{guidance_block}{truncation_note}\n"
        f"Summary statistics: {stats['feature_count']} observations, layers {list(stats['by_layer'])}, "
        f"window {stats['time_range']['earliest']} to {stats['time_range']['latest']}.\n\n"
        "Observations:\n" + "\n".join(lines)
    )


async def _generate_intsum(session_factory, product_id: str, job_id: str,
                           prompt: str, sources: list) -> None:
    """Background completion of an intsum product. Owns its own DB session —
    the request's session is gone by the time this runs."""
    try:
        narrative = await _call_llm(prompt)
        status, error = "done", None
    except Exception as e:  # surface the failure on the product, don't swallow it silently
        logger.exception("ext intsum %s failed", product_id)
        narrative, status, error = None, "error", f"{type(e).__name__}: {e}"

    db = session_factory()
    try:
        product = db.query(ExtProduct).filter(ExtProduct.id == product_id).first()
        job = db.query(ExtJob).filter(ExtJob.id == job_id).first()
        if product is not None:
            if narrative is not None:
                product.content = {**(product.content or {}), "narrative": narrative, "sources": sources}
            product.status = status
            product.error = error
        if job is not None:
            job.status = status
            job.detail = {**(job.detail or {}), "error": error} if error else job.detail
            job.completed_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


@router.post("", response_model=ProductGenerationResult, summary="Generate a product")
async def generate_product(
    request: ProductRequest,
    background: BackgroundTasks,
    db: Session = Depends(database.get_db),
):
    """Generate an intelligence product over the ingested observations.

    `layer_summary` completes synchronously. `intsum` returns immediately with
    a `processing` job — the LLM writes the narrative in the background; poll
    `/ingest/jobs/{job_id}` and fetch the product when `done`.
    """
    job = ExtJob(kind="product", status="processing")
    db.add(job)
    db.flush()

    query = apply_feature_filters(
        db.query(ExtFeature),
        layers=request.layers, since=request.since, until=request.until, bbox=request.bbox,
    )
    rows = query.order_by(ExtFeature.timestamp.desc().nullslast(), ExtFeature.created_at.desc()).all()

    stats = _stats(rows)
    snapshot = FeatureCollection(
        features=[row_to_feature(r) for r in rows[:SNAPSHOT_CAP]]
    ).model_dump(mode="json", exclude_none=True)
    title = request.title or f"{request.product_type} {datetime.utcnow().strftime('%Y-%m-%d %H:%MZ')}"

    product = ExtProduct(
        title=title,
        product_type=request.product_type,
        status="processing" if request.product_type == "intsum" else "done",
        params=request.model_dump(mode="json", exclude_none=True),
        content=stats,
        geojson=snapshot,
        job_id=job.id,
    )
    db.add(product)
    db.flush()
    job.detail = {"product_id": product.id, "feature_count": len(rows)}

    if request.product_type == "intsum":
        lines, sources = _observation_lines(rows)
        prompt = _intsum_prompt(lines, stats, request.guidance, len(rows))
        # bind the background session to the same engine this request used,
        # so tests (tmp sqlite) and prod (app db) both do the right thing
        session_factory = sessionmaker(autocommit=False, autoflush=False, bind=db.get_bind())
        db.commit()
        background.add_task(_generate_intsum, session_factory, product.id, job.id, prompt, sources)
        logger.info("ext intsum %s queued over %d observations", product.id, len(rows))
    else:
        job.status = "done"
        job.completed_at = datetime.utcnow()
        db.commit()
        logger.info("ext product %s generated over %d features", product.id, len(rows))

    return ProductGenerationResult(job_id=job.id, status=job.status, product=_summary(product))


@router.get("", response_model=List[ProductSummary], summary="List products")
async def list_products(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(database.get_db),
):
    rows = db.query(ExtProduct).order_by(ExtProduct.created_at.desc()).limit(limit).all()
    return [_summary(p) for p in rows]


@router.get("/{product_id}", summary="Fetch a product")
async def get_product(
    product_id: str,
    format: Literal["json", "geojson"] = Query("json", description="json = full product; geojson = the map overlay"),
    db: Session = Depends(database.get_db),
):
    """Fetch a generated product. `format=geojson` returns the FeatureCollection
    overlay, suitable for direct import into a map client."""
    p = db.query(ExtProduct).filter(ExtProduct.id == product_id).first()
    if not p:
        raise HTTPException(status_code=404, detail="Product not found")
    if format == "geojson":
        return p.geojson or {"type": "FeatureCollection", "features": []}
    return Product(
        product_id=p.id, title=p.title, product_type=p.product_type, status=p.status,
        created_at=p.created_at, params=p.params, error=p.error, content=p.content,
    )
