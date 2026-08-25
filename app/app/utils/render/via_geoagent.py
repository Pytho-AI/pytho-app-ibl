"""POST render requests to geo-agent's `/api/v1/render`.

The Playwright driver moved out of pytho-app under Task 6 of the map API
unification plan. pytho-app callers now build a `RenderOptions`
dataclass (unchanged field shape) and hand it to `render_map_dispatch`,
which POSTs to geo-agent and streams the JPG back.

geo-agent is a STANDALONE service reached over its host-published port —
`GEO_API_URL` is the ONE project-wide knob (base URL, no path); the
render endpoint derives from it. No local fallback — geo-agent IS the
renderer.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

import httpx

log = logging.getLogger(__name__)

GEO_AGENT_RENDER_URL = (
    os.environ.get("GEO_API_URL", "http://host.docker.internal:9845").rstrip("/")
    + "/api/v1/render"
)


@dataclass
class RenderOptions:
    """Field-for-field mirror of geo-agent's RenderRequest payload.

    Callers construct this from PRODUCT_SCHEMAS (map_basemap,
    map_overlay_kinds, map_doctrinal_layer_visibility, ...) plus the
    per-fire bbox / layers / dimensions. All schema-driven per-product
    behavior flows through these primitives — no product_type switches
    on the frontend or renderer side.
    """

    width: int = 1920
    height: int = 1080
    bbox: Optional[list[float]] = None
    layers: Optional[list[str]] = None
    timeout_ms: int = 60_000
    pixel_ratio: float = 1.5
    product_type: Optional[str] = None
    basemap: Optional[str] = None
    basemap_layer_visibility: Optional[list[str]] = None
    doctrinal_layer_visibility: Optional[list[str]] = None
    show_topographic_contours: Optional[bool] = None
    cluster_mil_units: Optional[bool] = None
    compact_aa_labels: Optional[bool] = None
    compact_legend_labels: Optional[bool] = None
    # Fast-path flag for the live-edit refresh_map endpoint.
    fast: bool = False


async def render_map_via_geoagent(
    map_uuid: UUID, opts: Optional[RenderOptions] = None
) -> bytes:
    """POST render request to geo-agent, stream JPG bytes back."""
    opts = opts or RenderOptions()
    payload = {
        "product_type": opts.product_type,
        "map_uuid": str(map_uuid),
        "basemap": opts.basemap,
        "width": opts.width,
        "height": opts.height,
        "pixel_ratio": opts.pixel_ratio,
        "wait_ms": opts.timeout_ms,
        "bbox": opts.bbox,
        "layers": opts.layers,
        "basemap_layer_visibility": opts.basemap_layer_visibility,
        "doctrinal_layer_visibility": opts.doctrinal_layer_visibility,
        "show_topographic_contours": opts.show_topographic_contours,
        "cluster_mil_units": opts.cluster_mil_units,
        "compact_aa_labels": opts.compact_aa_labels,
        "compact_legend_labels": opts.compact_legend_labels,
        "fast": opts.fast,
        # Persist on EVERY render (generation included) so geo-agent's
        # GET /render/latest/{map_uuid}.jpg is a valid fetchable endpoint
        # from the first render onward — not only after an edit-session
        # Done. Consumers can pull the current image of any map by
        # endpoint at any time. (geo-agent also stashes the render spec
        # per map on every non-replay render, which is what the edit
        # session's replay re-renders from.)
        "persist": True,
    }
    read_timeout = max(60.0, (opts.timeout_ms + 30_000) / 1000.0)
    log.info(
        "render_map_via_geoagent: POST %s (map=%s product=%s fast=%s)",
        GEO_AGENT_RENDER_URL, map_uuid, opts.product_type, opts.fast,
    )
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=10.0, read=read_timeout, write=30.0, pool=10.0,
        ),
        verify=False,
    ) as client:
        resp = await client.post(GEO_AGENT_RENDER_URL, json=payload)
        resp.raise_for_status()
        return resp.content


async def render_map_dispatch(map_id: UUID, opts: Optional[RenderOptions] = None) -> bytes:
    """Single render entry point for all pytho-app callers. Delegates to
    geo-agent — there is no local driver. Name is kept as
    `render_map_dispatch` for callsite compatibility."""
    return await render_map_via_geoagent(map_id, opts)


def render_map_dispatch_sync(map_id: UUID, opts: Optional[RenderOptions] = None) -> bytes:
    """Sync wrapper for callers running inside `asyncio.to_thread` or an
    otherwise loop-less context (e.g. handlers/map.refresh_artifact_map)."""
    import asyncio
    return asyncio.run(render_map_via_geoagent(map_id, opts))


# Back-compat aliases — some callers still use the legacy names. Keeping
# them thin re-exports lets grep hits keep resolving without churn.
render_map_to_png = render_map_via_geoagent
render_map_to_png_sync = render_map_dispatch_sync
