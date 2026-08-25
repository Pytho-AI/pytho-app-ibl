"""Live-edit map refresh — regenerate one map PNG inside an existing
deliverable (DOCX / PPTX) without re-running the whole product render.

Why this exists
---------------
The LiveEditor (Collabora) co-edit path is canonical: analyst opens the
DOCX/PPTX deliverable, edits text/tables in the iframe, and we never
re-render from structured_data. But maps inside the document need an
out-of-band edit path because Collabora can't trigger our React
GeoAgentMap overlay.

The user flow is:
  1. Frontend overlays an Edit Map button on top of each map in the
     iframe (positioned via map_embeds.json + iframe.contentWindow.app.
     map._twipsToLatLng → latLngToContainerPoint).
  2. Click → open GeoAgentMap overlay (React, parent page).
  3. On Done editing the overlay, frontend POSTs
     /api/artifacts/<aid>/refresh_map/<map_id>.
  4. THIS module:
     a. Pulls the saved overlays + render_params for this map (from the
        artifact dir — written by the original render).
     b. Calls geo-agent's render endpoint to regenerate the PNG.
     c. Opens the DOCX/PPTX zip, replaces the image part by relId, atomic
        rename. Text/table edits preserved.
     d. Bumps the deliverable's mtime so the next CheckFileInfo returns
        a fresh X-WOPI-ItemVersion → the editor reloads.

map_embeds.json sidecar shape
-----------------------------
The exporter writes one entry per map embed:

    [
      {
        "map_id": "<schema-paragraph-field-name>",
        "label": "Combined Obstacle Overlay",
        "slide_or_page_index": 3,
        "twips": [x, y, w, h],
        "bbox_lonlat": [w, s, e, n],
        "image_part": "ppt/media/image1.png",  # relative path inside the zip
        "image_filename": "combined_obstacle_overlay_map.jpg",  # alongside-deliverable filename for the source PNG
        "artifact_id": "MCOO_..."
      },
      ...
    ]

`image_part` is the join key — that's what we replace. Falls back to
filename-stem matching if missing.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

_UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))

# Internal base URL the editor's LibreOffice kit uses to fetch the
# regenerated map PNG for the UNO GraphicProvider swap. Defaults to
# `http://docker-api:8000` — the IDX 26 container name. Parallel stacks
# (V Corps: docker-api-vcorps, future scenarios: docker-api-<slug>) set
# `WOPI_INTERNAL_BASE` to their scoped container hostname so:
#   (a) Docker DNS resolves unambiguously to the stack's own docker-api
#       even when the editor container sits on a shared network with
#       aliases from multiple docker-api instances.
#   (b) The editor's WOPI aliasgroup1 whitelist accepts the URL — the
#       parallel stacks scope aliasgroup1 to their own docker-api-<slug>
#       host, so a URL that names a different stack's docker-api would
#       be rejected by the WOPI allowlist check even if it resolved.
# Reuses the WOPI_INTERNAL_BASE env var the docker-api container sets
# for the same purpose in handlers/wopi.py rather than introducing a
# separate MAP_REFRESH_INTERNAL_BASE — one env var, one policy.
_EDITOR_INTERNAL_BASE = (
    os.environ.get("WOPI_INTERNAL_BASE", "http://docker-api:8000").rstrip("/")
)


def _artifact_dir(artifact_id: str) -> Path:
    """All live-edit artifacts live in the _autodraft owner namespace —
    that's where the autodraft sweep + autofires emit + where LiveEditor
    mints WOPI tokens against."""
    return _UPLOAD_DIR / "_autodraft" / artifact_id


def _read_map_embeds(artifact_id: str) -> list[dict]:
    sidecar = _artifact_dir(artifact_id) / "map_embeds.json"
    if not sidecar.exists():
        raise FileNotFoundError(
            f"no map_embeds.json for artifact {artifact_id} — "
            f"either no maps in this artifact, or the deliverable was "
            f"generated before the sidecar emission landed"
        )
    return json.loads(sidecar.read_text())


def _find_deliverable(artifact_dir: Path) -> Optional[Path]:
    """Pick the live-edit deliverable (DOCX / PPTX) from the artifact dir.
    XLSX is not handled here — XLSX-canonical products don't carry maps.
    """
    for ext in (".docx", ".pptx"):
        matches = list(artifact_dir.glob(f"*{ext}"))
        if matches:
            return matches[0]
    return None


async def _download_rendered_map(artifact_id: str, map_id: str,
                                 image_url: str,
                                 image_filename: Optional[str]) -> Path:
    """Fetch the JPG geo-agent already rendered + persisted (the in-map
    Done-editing flow: geo-agent triggers its OWN render; this side only
    downloads and embeds — never re-renders).

    `image_url` arrives as the browser-relative `/geo-api/v1/...` path
    from the done hook; map it onto the server-reachable GEO_API_URL
    base (`/geo-api/*` → `<GEO_API_URL>/api/*`). Absolute http(s) URLs
    pass through untouched. Writes the bytes over the sidecar image file
    (same on-disk target `_regenerate_map_png` uses) so the dashboard
    preview stays in sync with the in-zip bytes, and returns its path.
    """
    import httpx

    if image_url.startswith("/geo-api/"):
        geo_base = os.environ.get(
            "GEO_API_URL", "http://host.docker.internal:9845",
        ).rstrip("/")
        fetch_url = f"{geo_base}/api/{image_url[len('/geo-api/'):]}"
    elif image_url.startswith(("http://", "https://")):
        fetch_url = image_url
    else:
        raise RuntimeError(f"unsupported image_url shape: {image_url!r}")

    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=5.0)) as client:
        resp = await client.get(fetch_url)
    if resp.status_code != 200 or not resp.content:
        raise RuntimeError(
            f"rendered-map download failed: GET {fetch_url} -> "
            f"{resp.status_code} ({len(resp.content)} bytes)"
        )

    adir = _artifact_dir(artifact_id)
    img_path = adir / (image_filename or f"{map_id}.jpg")
    img_path.write_bytes(resp.content)
    log.info(
        "refresh_map: downloaded persisted render aid=%s map=%s url=%s bytes=%d -> %s",
        artifact_id, map_id, fetch_url, len(resp.content), img_path.name,
    )
    return img_path


async def _regenerate_map_png(artifact_id: str, map_id: str,
                                image_filename: Optional[str]) -> Path:
    """Re-render ONE map PNG by replaying the cached render_params for that
    map. Same primitive `render_map_to_png` the original render used —
    just scoped to a single map_id rather than the whole product.

    Reads `<artifact_dir>/render_params_<map_id>.json` (written by
    `tool_render_map` at original generation). Reconstructs RenderOptions
    from the cached params + the live schema. Writes the new PNG over the
    sidecar image file on disk (so the dashboard preview reflects the new
    map too) AND returns its path for the in-zip replace.
    """
    from uuid import UUID as _UUID
    from templates import PRODUCT_SCHEMAS
    from utils.render import render_map_dispatch, RenderOptions
    from handlers.intel_agent import upsert_geo_agent_map_record

    adir = _artifact_dir(artifact_id)
    params_path = adir / f"render_params_{map_id}.json"
    if not params_path.exists():
        # map_id passed in is map_embeds.json's entry.map_id, set by
        # exporter.py:3708 as `meta.get("map_id") or field_name`. When
        # the schema's structured_data.map_overlays.<field>.map_id is
        # populated (every IDX 26 product — intel_agent.tool_render_map
        # writes the geo-agent map UUID there), map_embeds.map_id IS the
        # UUID. But render_params is written by intel_agent.py:5952 as
        # `render_params_{map_artifact_id}.json` where map_artifact_id is
        # the FIELD NAME, not the UUID — so a UUID-shaped map_id has no
        # direct `render_params_<uuid>.json` on disk. Both files
        # cross-reference the same UUID: map_embeds.map_id == UUID and
        # render_params.map_uuid == UUID. Scan candidates and match.
        for pf in adir.glob("render_params_*.json"):
            try:
                cand = json.loads(pf.read_text())
            except Exception:
                continue
            if str(cand.get("map_uuid") or "") == map_id:
                params_path = pf
                break
    if not params_path.exists():
        n = len(list(adir.glob("render_params_*.json")))
        raise FileNotFoundError(
            f"no cached render params for map_id={map_id!r} in {adir} "
            f"(direct lookup + map_uuid scan across {n} candidate "
            f"render_params files both missed) — regenerate the "
            f"product once with mode=full to populate the cache"
        )
    try:
        params = json.loads(params_path.read_text())
    except Exception as e:
        raise RuntimeError(
            f"{params_path.name} unreadable: {type(e).__name__}: {e}"
        )

    # Resolve target image path (alongside the deliverable) so the
    # dashboard preview thumbnails + the map_embeds.json reference stay
    # in sync with the in-zip image bytes.
    img_relpath = (
        image_filename
        or params.get("image_relpath")
        or f"{map_id}.jpg"
    )
    img_path = adir / img_relpath

    # Schema-derived render options come from the live PRODUCT_SCHEMAS,
    # not the cache — keeps rendering in lockstep with tool_render_map.
    schema_state_path = adir / "schema_state.json"
    pt = ""
    try:
        if schema_state_path.exists():
            pt = json.loads(schema_state_path.read_text()).get("product_type") or ""
    except Exception:
        pass
    pt = params.get("product_type") or pt
    schema = PRODUCT_SCHEMAS.get(pt, {}) or {}
    kinds = schema.get("map_overlay_kinds")

    opts = RenderOptions(
        width=int(params.get("width") or 2560),
        height=int(params.get("height") or 1440),
        bbox=params.get("bbox"),
        layers=list(kinds) if isinstance(kinds, (list, tuple)) and kinds else None,
        product_type=pt,
        basemap=schema.get("map_basemap"),
        basemap_layer_visibility=schema.get("map_basemap_layer_visibility"),
        doctrinal_layer_visibility=schema.get("map_doctrinal_layer_visibility"),
        show_topographic_contours=schema.get("map_show_topographic_contours"),
        cluster_mil_units=schema.get("map_cluster_mil_units"),
        compact_aa_labels=schema.get("map_compact_aa_labels"),
        compact_legend_labels=schema.get("map_compact_legend_labels"),
        # Live-edit refresh hits the fast path: tile wait capped at 5s
        # instead of 32s, skip the 1500ms symbol-flush sleep. The
        # analyst-edit case doesn't need cold-render headroom — basemap
        # tiles are already cached in Chromium's HTTP cache from the
        # last render, and the doctrinal paint is already complete by
        # the time __renderReady fires.
        fast=True,
    )

    # Refresh the geo-agent map record's schema-derived attrs so the next
    # EditMap overlay opens with the right basemap + kind allowlist.
    upsert_geo_agent_map_record(
        map_uuid=str(params["map_uuid"]),
        product_type=pt,
        field_name=map_id,
        bbox=params.get("bbox") or [],
        schema=schema,
    )
    png_bytes = await render_map_dispatch(_UUID(str(params["map_uuid"])), opts)
    img_path.write_bytes(png_bytes)
    log.info("refresh_map: regenerated PNG aid=%s map=%s bytes=%d at %s",
              artifact_id, map_id, len(png_bytes), img_path)
    return img_path


def _resolve_image_part(deliverable: Path, image_part: str) -> str:
    """Match map_embeds.json's image_part to an actual zip member, tolerant
    of extension drift.

    python-pptx writes JPEG image parts as `.jpeg` but older versions /
    different code paths in our exporter recorded `.jpg` in map_embeds.json.
    Same content, different name. Falls back to:
      1. exact match (the common case)
      2. same basename + interchangeable extension (.jpg <-> .jpeg)
      3. unique image part in the zip (when there's only one)

    Raises if no candidate can be resolved.
    """
    with zipfile.ZipFile(deliverable, "r") as z:
        names = z.namelist()
    if image_part in names:
        return image_part
    JPEG_VARIANTS = {".jpg": ".jpeg", ".jpeg": ".jpg"}
    base, ext = os.path.splitext(image_part)
    alt_ext = JPEG_VARIANTS.get(ext.lower())
    if alt_ext:
        alt = base + alt_ext
        if alt in names:
            log.info(
                "refresh_map: image_part %r drifted; matched %r in zip",
                image_part, alt,
            )
            return alt
    # Last resort: if there's exactly one media image in the zip, it has to
    # be the one we're replacing. (True for every IDX 26 product so far —
    # one map per slide, one slide per artifact.)
    media = [n for n in names if n.startswith(("ppt/media/", "word/media/"))
             and not n.endswith("/")]
    if len(media) == 1:
        log.warning(
            "refresh_map: image_part %r missing and basename-flip "
            "didn't match; collapsing to sole media part %r",
            image_part, media[0],
        )
        return media[0]
    raise RuntimeError(
        f"image_part {image_part!r} not found inside {deliverable.name}; "
        f"map_embeds.json may be out-of-date with the live zip "
        f"(zip media members: {media[:5]}{'...' if len(media) > 5 else ''})"
    )


def _replace_image_in_zip(deliverable: Path, image_part: str,
                            new_image_bytes: bytes) -> str:
    """Replace one image part inside a DOCX/PPTX zip atomically.

    deliverable : path to the .docx or .pptx file on disk.
    image_part  : zip member name e.g. "ppt/media/image1.png" or
                  "word/media/image1.png". Tolerant of `.jpg` <-> `.jpeg`
                  drift via `_resolve_image_part`.
    new_image_bytes : raw bytes of the replacement image (matching format —
                  if image_part is .png the bytes must be PNG, etc.).

    Returns the new mtime in ms.
    """
    if not deliverable.exists():
        raise FileNotFoundError(f"deliverable not found: {deliverable}")

    target = _resolve_image_part(deliverable, image_part)

    tmp = deliverable.with_suffix(deliverable.suffix + ".refresh.tmp")
    # Copy → modify → rename. zipfile lacks in-place edit; the only safe
    # way is rewrite the archive with the replaced member.
    with zipfile.ZipFile(deliverable, "r") as zin, \
         zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = (
                new_image_bytes if item.filename == target
                else zin.read(item.filename)
            )
            new_item = zipfile.ZipInfo(
                filename=item.filename,
                date_time=item.date_time,
            )
            new_item.compress_type = item.compress_type
            new_item.external_attr = item.external_attr
            zout.writestr(new_item, data)
    os.replace(tmp, deliverable)
    return str(int(deliverable.stat().st_mtime * 1000))


async def refresh_map(artifact_id: str, map_id: str,
                      image_url: Optional[str] = None) -> dict:
    """Top-level entry — called by /api/artifacts/<aid>/refresh_map/<map_id>.

    Steps:
      1. Read map_embeds.json sidecar; find the entry matching map_id.
      2. Obtain the new image. When `image_url` is provided (the in-map
         "Done editing" hook: geo-agent already rendered + persisted the
         JPG itself), DOWNLOAD it — no render happens here. Without it,
         fall back to regenerating via the map_only replay path.
      3. Replace the embedded image part inside the deliverable's zip.
      4. Return cache_bust + version metadata so the frontend can ask
         the editor to reload.
    """
    artifact_dir = _artifact_dir(artifact_id)
    if not artifact_dir.exists():
        raise FileNotFoundError(f"artifact dir not found: {artifact_dir}")

    embeds = _read_map_embeds(artifact_id)
    entry = next((e for e in embeds if e.get("map_id") == map_id), None)
    if entry is None:
        raise FileNotFoundError(
            f"map_id={map_id!r} not found in {artifact_id}/map_embeds.json "
            f"(known: {sorted(e.get('map_id', '?') for e in embeds)})"
        )

    image_part = entry.get("image_part")
    if not image_part:
        raise RuntimeError(
            f"map_embeds.json entry for {map_id} missing image_part "
            f"(can't locate inside the zip)"
        )

    deliverable = _find_deliverable(artifact_dir)
    if deliverable is None:
        raise FileNotFoundError(
            f"no DOCX/PPTX deliverable in {artifact_dir} — refresh_map "
            f"is only valid for narrative live-edit products"
        )

    if image_url:
        new_png = await _download_rendered_map(
            artifact_id, map_id, image_url, entry.get("image_filename"),
        )
    else:
        new_png = await _regenerate_map_png(
            artifact_id, map_id, entry.get("image_filename"),
        )
    new_bytes = new_png.read_bytes()
    new_version = _replace_image_in_zip(deliverable, image_part, new_bytes)

    log.info(
        "refresh_map: aid=%s map=%s zip=%s part=%s bytes=%d version=%s",
        artifact_id, map_id, deliverable.name, image_part,
        len(new_bytes), new_version,
    )
    # HTTP URL the EDITOR container can reach over the docker network.
    # /uploads is bind-mounted in the editor for completeness, but the LO
    # kit runs inside a chroot jail (Collabora can't bind-mount due to
    # docker overlayfs; jail receives a COPY of systemplate, which does
    # not include /uploads). So file:// URLs from inside the kit don't
    # resolve. The fastapi /files StaticFiles mount at the docker-api
    # container serves the same uploads tree over HTTP — UNO's
    # GraphicProvider accepts http:// URLs and the editor->docker-api
    # hop is in-network. The hostname comes from _EDITOR_INTERNAL_BASE
    # (WOPI_INTERNAL_BASE env var) rather than being hardcoded to
    # `docker-api:8000` — parallel stacks (V Corps: docker-api-vcorps)
    # need the URL to name their scoped container, otherwise the
    # editor's WOPI allowlist rejects it AND Docker DNS routes to the
    # wrong stack's uploads tree when the container_name alias is
    # shared across multiple stacks on the same network.
    cache_bust = int(time.time() * 1000)
    editor_image_url = (
        f"{_EDITOR_INTERNAL_BASE}/files/_autodraft/{artifact_id}/"
        f"{new_png.name}?t={cache_bust}"
    )
    return {
        "artifact_id": artifact_id,
        "map_id": map_id,
        "image_part": image_part,
        "deliverable": deliverable.name,
        "x_wopi_item_version": new_version,
        "editor_image_url": editor_image_url,
        # The on-disk zip has been updated. The editor's in-memory copy
        # still holds the OLD image until the analyst issues a
        # CallPythonScript to pytho_graphics.replace_picture(map_id,
        # editor_image_url). UNO swap is preferred over iframe reload —
        # keeps the analyst's slide position, no boot screen.
        "frontend_action": "uno_swap",
    }
