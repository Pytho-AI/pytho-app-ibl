"""Map-related handlers.

Owns the artifact map-refresh path that the DocEditor's Edit-map roundtrip
hits when the analyst returns from OperationsMap. Re-renders the map via
the in-process renderer (utils/render/headless.py) and overwrites
`uploads/<user>/<artifact>/map.png` so DocEditor's `<img src>` cache-bust
picks up the new image.

The route registration lives in `main.py` and is a thin wrapper around
`refresh_artifact_map` below. Pattern mirrors `handlers/intel_agent.py`'s
`tool_render_map` for consistency.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional
from uuid import UUID

from utils import database

logger = logging.getLogger(__name__)

# UUID validation pattern, mirrored from handlers/intel_agent.py:_UUID_RE.
# Used by `refresh_artifact_map` to gate the render call — non-UUID map_ids
# (legacy "M-NNNN" or empty strings) skip the render and fall back to
# cache-bust-only behavior rather than 400ing the analyst.
_MAP_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Mirror main.py's UPLOAD_DIR default. The env override is the same so the
# two surfaces stay aligned.
_UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))


def refresh_artifact_map(artifact_id: str, db: Optional[object] = None,
                         image_url: Optional[str] = None,
                         hook_map_id: Optional[str] = None) -> dict:
    """Re-render the map embedded in artifact `artifact_id` via the
    in-process renderer and return a cache-bust timestamp the DocEditor
    uses to bust its `<img>` cache. The endpoint never 500s on render
    failure — the analyst always gets a 200 with a `cache_bust` token and
    a diagnostic `render_error` / `note` field.

    Called from the async route via `asyncio.to_thread(...)` so the
    `asyncio.run` inside `render_map_to_png_sync` runs on a worker thread
    rather than colliding with the request's event loop.

    Flow:
        1. Walk recent `SessionDetail` rows looking for an
           `agent_trace.product_writer_dispatches[].emit` whose
           `artifact_id` matches. Extract
           `structured_data.header.spatial_overlay_map_id`.
        2. If the artifact is not found OR has no map_id OR the map_id is
           not a UUID, return 200 with a `note` field explaining why.
           The cache_bust is still returned so DocEditor can soft-reload.
        3. Call `render_map_to_png_sync(<map_id>, bbox=AO)` and overwrite
           `uploads/<user>/<artifact>/map.png` with the bytes. Latest-wins.
        4. On any render failure (Playwright crash, ready-signal timeout,
           etc.), log a warning and return 200 with `rendered: false` + a
           `render_error` string. The artifact body did not change — only
           the embedded PNG — so no HTML/DOCX re-emit is required.
    """
    timestamp = int(time.time() * 1000)
    html_link: Optional[str] = None
    docx_link: Optional[str] = None
    map_id: str = ""
    owner_user_id: str = ""
    artifact_found = False

    owns_db = False
    if db is None:
        db = next(database.get_db())
        owns_db = True

    try:
        try:
            details = (
                db.query(database.SessionDetail)
                .order_by(database.SessionDetail.timestamp.desc())
                .limit(500)
                .all()
            )
            for row in details:
                try:
                    payload = json.loads(row.data or "{}")
                except Exception:
                    logger.warning("malformed session_detail row id=%s session=%s — skipping",
                                   getattr(row, "id", "?"), getattr(row, "session_id", "?"))
                    continue
                trace = (payload.get("agent_trace") or {})
                for disp in (trace.get("product_writer_dispatches") or []):
                    emit = (disp or {}).get("emit") or {}
                    if str(emit.get("artifact_id")) == artifact_id:
                        sd = emit.get("structured_data") or {}
                        header_blk = sd.get("header") or {}
                        map_id = header_blk.get("spatial_overlay_map_id") or ""
                        # map_overlays carries the FULL per-field map dict
                        # for multi-map products (GRINTSUM: overview_map +
                        # zoom_inset_map etc.). Each value is
                        # {map_id, bbox}. Used below to refresh + save-back
                        # every map field, not just the header's primary.
                        map_overlays = sd.get("map_overlays") or {}
                        owner_user_id = (
                            header_blk.get("owner_user_id")
                            or emit.get("owner_user_id")
                            or row.user_id
                            or ""
                        )
                        html_link = disp.get("htmlLink") or html_link
                        docx_link = disp.get("docxLink") or docx_link
                        artifact_found = True
                        break
                if artifact_found:
                    break
        except Exception as lookup_err:
            logger.warning(f"refresh_artifact_map lookup failed: {lookup_err}")

        response: dict = {
            "artifact_id": artifact_id,
            "map_id": map_id or None,
            "htmlLink": html_link,
            "docxLink": docx_link,
            "cache_bust": timestamp,
        }

        if not artifact_found:
            response["note"] = "artifact not found in recent SessionDetail rows"
            return response

        if not map_id:
            response["note"] = "artifact has no map"
            return response

        if not _MAP_UUID_RE.match(map_id.strip()):
            response["note"] = f"map_id {map_id!r} is not a UUID; cannot re-render"
            return response

        # PNG re-render per-field. tool_render_map writes each map paragraph
        # to <artifact_dir>/<field_name>.jpg (the file the figure img src
        # points at) AND caches the schema-aware RenderOptions in
        # render_params_<field>.json. Refresh mirrors that: iterate every
        # map field, load its cached params, re-render with the SAME
        # schema-derived primitives (basemap, layers, kinds, etc.) and
        # overwrite the per-field .jpg in place. Falls back to the header's
        # primary map_id when no per-field map_overlays is present.
        owner = (owner_user_id or "").strip() or "_autodraft"
        artifact_dir = _UPLOAD_DIR / owner / artifact_id
        try:
            from utils.render import render_map_to_png_sync, RenderOptions
            from utils import scenario as _scenario
            from templates import PRODUCT_SCHEMAS
            from handlers.intel_agent import upsert_geo_agent_map_record
            artifact_dir.mkdir(parents=True, exist_ok=True)
            # Per-field render list: prefer structured_data.map_overlays
            # (multi-map products land here), fall back to the header's
            # primary map_id as field='map' for single-map back-compat.
            _renders: list[tuple[str, str, Optional[list]]] = []
            for _fname, _meta in (map_overlays or {}).items():
                if not isinstance(_meta, dict):
                    continue
                _mid = (_meta.get("map_id") or "").strip()
                if not (_mid and _MAP_UUID_RE.match(_mid)):
                    continue
                _bbox = _meta.get("bbox")
                _renders.append((_fname, _mid, _bbox if isinstance(_bbox, list) else None))
            if not _renders and map_id:
                _renders.append(("map", map_id, list(_scenario.AO_BBOX)))
            # Resolve the active product schema from the latest autodraft
            # for this artifact_id — it carries the doctrinal primitives
            # (basemap / layers / kind allowlist) the figure was originally
            # rendered with. Falls back to empty schema (analyst-default
            # paint) if no schema can be resolved.
            _schema_for_artifact: dict = {}
            _pt_for_artifact: str = ""
            try:
                # Walk recent autodraft dirs to find this artifact_id and
                # read its render_params_<field>.json for product_type.
                from handlers.product import UPLOAD_DIR as _UD
                from pathlib import Path as _Path
                _adir = _Path(_UD) / "_autodraft" / artifact_id
                if _adir.is_dir():
                    _params_files = sorted(_adir.glob("render_params_*.json"))
                    if _params_files:
                        try:
                            _params = json.loads(_params_files[0].read_text())
                            _pt_for_artifact = (_params.get("product_type") or "").strip()
                            if _pt_for_artifact:
                                _schema_for_artifact = PRODUCT_SCHEMAS.get(_pt_for_artifact, {}) or {}
                        except Exception:
                            pass
            except Exception:
                pass
            _kinds = _schema_for_artifact.get("map_overlay_kinds")
            # In-map Done-editing hook: geo-agent already rendered +
            # persisted the edited map's JPG. Download it ONCE and write
            # it to every field bound to that map; skip re-rendering the
            # untouched fields entirely (they weren't edited).
            _hook_bytes: Optional[bytes] = None
            if image_url and hook_map_id:
                try:
                    import urllib.request as _urlreq
                    if image_url.startswith("/geo-api/"):
                        _geo = os.environ.get(
                            "GEO_API_URL", "http://host.docker.internal:9845",
                        ).rstrip("/")
                        _fetch = f"{_geo}/api/{image_url[len('/geo-api/'):]}"
                    else:
                        _fetch = image_url
                    with _urlreq.urlopen(_fetch, timeout=60) as _resp:
                        _hook_bytes = _resp.read()
                except Exception as _dl_err:
                    logger.warning(
                        "refresh_artifact_map: hook image download failed url=%s err=%s",
                        image_url, _dl_err,
                    )
            _render_results: list[dict] = []
            if _hook_bytes and hook_map_id:
                for _fname, _mid, _bbox in _renders:
                    if _mid != hook_map_id:
                        _render_results.append({
                            "field": _fname, "map_id": _mid, "rendered": False,
                            "skipped": "not the edited map",
                        })
                        continue
                    _img_path = artifact_dir / f"{_fname}.jpg"
                    _img_path.write_bytes(_hook_bytes)
                    _render_results.append({
                        "field": _fname, "map_id": _mid, "rendered": True,
                        "bytes": len(_hook_bytes),
                        "png_url": f"/files/{owner}/{artifact_id}/{_fname}.jpg",
                        "source": "geo-agent persisted render",
                    })
                _renders = []
            for _fname, _mid, _bbox in _renders:
                _img_path = artifact_dir / f"{_fname}.jpg"
                _png_url  = f"/files/{owner}/{artifact_id}/{_fname}.jpg"
                _bbox_for_field = _bbox or list(_scenario.AO_BBOX)
                # Refresh the geo-agent map record's schema attrs so the
                # embedded EditMap overlay opens on the doctrinal state
                # the analyst expects after Done. Cheap idempotent PUT.
                try:
                    upsert_geo_agent_map_record(
                        map_uuid=_mid,
                        product_type=_pt_for_artifact,
                        field_name=_fname,
                        bbox=_bbox_for_field,
                        schema=_schema_for_artifact,
                    )
                except Exception as _attrs_err:
                    logger.warning(
                        "refresh_artifact_map: attrs upsert failed map=%s field=%s err=%s",
                        _mid, _fname, _attrs_err,
                    )
                try:
                    _opts = RenderOptions(
                        bbox=_bbox_for_field,
                        layers=list(_kinds) if isinstance(_kinds, (list, tuple)) and _kinds else None,
                        product_type=(_pt_for_artifact or None),
                        basemap=_schema_for_artifact.get("map_basemap"),
                        basemap_layer_visibility=_schema_for_artifact.get("map_basemap_layer_visibility"),
                        doctrinal_layer_visibility=_schema_for_artifact.get("map_doctrinal_layer_visibility"),
                        show_topographic_contours=_schema_for_artifact.get("map_show_topographic_contours"),
                        cluster_mil_units=_schema_for_artifact.get("map_cluster_mil_units"),
                        compact_aa_labels=_schema_for_artifact.get("map_compact_aa_labels"),
                        compact_legend_labels=_schema_for_artifact.get("map_compact_legend_labels"),
                    )
                    _png_bytes = render_map_to_png_sync(UUID(_mid), _opts)
                    _img_path.write_bytes(_png_bytes)
                    _render_results.append({
                        "field": _fname, "map_id": _mid, "rendered": True,
                        "bytes": len(_png_bytes), "png_url": _png_url,
                    })
                except Exception as _render_err:
                    logger.warning(
                        "refresh_artifact_map: render failed field=%s map=%s err=%s",
                        _fname, _mid, _render_err,
                    )
                    _render_results.append({
                        "field": _fname, "map_id": _mid, "rendered": False,
                        "error": f"{type(_render_err).__name__}: {_render_err}",
                    })
            response["renders"] = _render_results
            # Back-compat keys for the legacy single-map response shape.
            if _render_results:
                _primary = _render_results[0]
                response["rendered"] = bool(_primary.get("rendered"))
                response["render_bytes"] = _primary.get("bytes")
                response["png_url"] = _primary.get("png_url")
        except Exception as render_err:
            logger.warning(
                f"refresh_artifact_map: render setup failed "
                f"(artifact={artifact_id}): "
                f"{type(render_err).__name__}: {render_err}"
            )
            response["rendered"] = False
            response["render_error"] = f"{type(render_err).__name__}: {render_err}"

        # Save-back: snapshot the CURRENT per-product geo-agent map state
        # into the product folder's overlays_<field>.json — captures any
        # analyst edits that landed on the geo-agent map(s) during the
        # operations-map round-trip. Authoritative archive so a container
        # restart that loses transient geo-agent state can re-populate
        # from the product folder.
        #
        # Iterates every map field in structured_data.map_overlays so
        # multi-map products (GRINTSUM: overview_map + zoom_inset_map,
        # etc.) save back ALL their per-field maps, not just the primary
        # one held in header.spatial_overlay_map_id.
        try:
            import requests as _requests
            _geo_base = (
                os.environ.get(
                    "GEO_API_URL", "http://host.docker.internal:9845"
                ).rstrip("/")
                + "/api/v1"
            )
            _saved: list[dict] = []
            # Build the per-field set to save back. Always include the
            # header's primary map (under field_name="map" as a sensible
            # default if no map_overlays exists), plus every entry in
            # structured_data.map_overlays.
            _fields: dict[str, str] = {}
            for _fname, _meta in (map_overlays or {}).items():
                if isinstance(_meta, dict):
                    _mid = (_meta.get("map_id") or "").strip()
                    if _mid and _MAP_UUID_RE.match(_mid):
                        _fields[_fname] = _mid
            if not _fields and map_id:
                _fields["map"] = map_id
            for _fname, _mid in _fields.items():
                try:
                    _r = _requests.get(f"{_geo_base}/maps/{_mid}/state", timeout=5)
                    if _r.ok:
                        _state = _r.json() or {}
                        (artifact_dir / f"overlays_{_fname}.json").write_text(
                            json.dumps({
                                "map_uuid": _mid,
                                "field_name": _fname,
                                "snapshot_source": "refresh_artifact_map",
                                "snapshot_at_ms": int(time.time() * 1000),
                                "overlays": _state.get("overlays") or [],
                                "entities": _state.get("entities") or {},
                            }, default=str, indent=2)
                        )
                        _saved.append({"field": _fname, "map_id": _mid,
                                        "overlay_count": len(_state.get("overlays") or [])})
                    else:
                        logger.info(
                            "refresh_artifact_map: state fetch HTTP %s for map=%s field=%s (skip save-back)",
                            _r.status_code, _mid, _fname,
                        )
                except Exception as _field_err:
                    logger.warning("refresh_artifact_map: save-back failed map=%s field=%s: %s",
                                    _mid, _fname, _field_err)
            response["overlays_saved"] = _saved
        except Exception as _save_err:
            logger.warning("refresh_artifact_map: overlays save-back wrapper failed artifact=%s: %s",
                            artifact_id, _save_err)

        return response
    finally:
        if owns_db:
            try:
                db.close()
            except Exception:
                logger.debug("db.close() failed in map handler finally block", exc_info=True)
