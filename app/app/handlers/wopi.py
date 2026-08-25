"""WOPI host for live Office editing (XLSX / DOCX / PPTX).

Implements the minimal Web Application Open Platform Interface needed for
the Online Editor (LibreOffice headless inside agent-editor) to open,
edit, and save the per-artifact deliverable files stored under
`uploads/<owner>/<artifact_id>/<filename>`.

Surface:
    POST /api/wopi/token             — mint a per-(user, artifact, format) JWT
    GET  /wopi/files/{token}         — CheckFileInfo (metadata JSON)
    GET  /wopi/files/{token}/contents — GetFile (raw bytes)
    POST /wopi/files/{token}/contents — PutFile (save callback)

Format coverage
---------------
FORMAT_SPEC carries the per-extension MIME and the validity rule. The
mint endpoint resolves which format an artifact's deliverable is via the
schema's `output_formats[0] || output_format`. Each format has its own
ContentType returned on GetFile so LibreOffice dispatches to the right
module (Writer / Calc / Impress).

Feature flag
------------
DOCX and PPTX live-edit is gated by `COLLABORA_NARRATIVE_EDIT` (default
false). XLSX is always available because it has been the only live-edit
target since the WOPI surface landed. With the flag off, a token-mint
request for a docx/pptx artifact returns 403 + a frontend-recognizable
error code so the SPA falls back to the TinyMCE editor without surprise.

Why a token in the URL path
---------------------------
The Online Editor pulls CheckFileInfo + GetFile + PutFile from inside
the docker network and carries no session cookies — only the WOPISrc URL
the browser handed it. Embedding the JWT in the URL is the standard
WOPI access-token pattern. The token carries:

    {user_id, artifact_id, owner_user_id, filename, format, iat, exp}

Different `user_id` values yield distinct tokens → the editor treats them
as distinct collaborators → cursors / selections rendered per user. That
is the bare-minimum multi-user surface — drop-in replaceable with a real
identity provider later.

Storage
-------
Files live where the rest of the artifact lives:
    uploads/<owner>/<artifact_id>/<deliverable_filename>
PutFile overwrites in place via an atomic `.tmp` rename. The deliverable
is the canonical document from generation forward — there is no
derive-from-HTML step on save.

Locks
-----
The editor's collab session is the authoritative concurrency primitive
(Operational Transform + presence cursors). We implement WOPI
SupportsLocks=False — the editor handles co-edit; we do not need to
deconflict on the WOPI side.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import jwt
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

log = logging.getLogger(__name__)

UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))


# JWT secret — set WOPI_SECRET in compose so tokens survive container
# restarts. The random-per-process fallback is convenient for laptop dev
# but rotates on every restart, which invalidates every minted token —
# any open editor session breaks. In production set WOPI_SECRET to a
# stable random value.
def _wopi_secret_load() -> str:
    v = os.environ.get("WOPI_SECRET")
    if v:
        return v
    # Persistent fallback file in /app/data (writable volume) so dev still
    # survives the restart loop even without the env var set.
    try:
        return Path("/app/data/wopi_secret").read_text().strip()
    except Exception:
        log.warning(
            "WOPI_SECRET not set and /app/data/wopi_secret unreadable — "
            "minting a fresh random secret per process (tokens will NOT "
            "survive container restart).",
        )
        return os.urandom(32).hex()


_WOPI_SECRET = _wopi_secret_load()
_TOKEN_TTL_SEC = int(os.environ.get("WOPI_TOKEN_TTL_SEC", "604800"))  # 7 days
_JWT_ALG = "HS256"

# Feature flag — DOCX / PPTX live edit is opt-in. Default off preserves
# the TinyMCE-DocEditor path for those formats so the analyst UX doesn't
# change implicitly.
_COLLABORA_NARRATIVE_EDIT = (
    (os.environ.get("COLLABORA_NARRATIVE_EDIT") or "").strip().lower()
    in ("1", "true", "yes", "on")
)


# ---------------------------------------------------------------------------
# Per-format spec — extension, MIME, narrative-flag gate
# ---------------------------------------------------------------------------
# Format key here matches the schema's `output_format` / `output_formats[0]`
# value. extension is the on-disk suffix the exporter writes; mime is the
# Content-Type returned on GetFile so LibreOffice dispatches to the right
# module (Calc / Writer / Impress). `requires_narrative_flag` gates DOCX
# and PPTX behind COLLABORA_NARRATIVE_EDIT; XLSX is always allowed.
FORMAT_SPEC: dict[str, dict] = {
    "xlsx": {
        "ext": ".xlsx",
        "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "requires_narrative_flag": False,
    },
    "docx": {
        "ext": ".docx",
        "mime": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "requires_narrative_flag": True,
    },
    "pptx": {
        "ext": ".pptx",
        "mime": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "requires_narrative_flag": True,
    },
}


# Per-product opt-out from live edit. Empty after the convert.js
# image-inlining fix (DOCX maps now actually embed in the zip, so
# Threat Model graduates out). Kept as the gate primitive for future
# render-side limitations.
_DISALLOWED_LIVE_EDIT_PRODUCTS: set[str] = set()


def _resolve_artifact_format(product_type: str, schema: dict) -> Optional[str]:
    """Pick the live-editable format for an artifact from its schema.

    Priority:
      1. `output_formats[0]` if set (XLSX-canonical products + any future
         multi-format products).
      2. `output_format` (legacy single-format declaration; DOCX / PPTX).
    Returns the matching FORMAT_SPEC key or None if neither is a
    live-edit-supported format.
    """
    formats = schema.get("output_formats")
    if isinstance(formats, list) and formats:
        candidate = formats[0]
    else:
        candidate = schema.get("output_format") or ""
    candidate = (candidate or "").lower().strip()
    return candidate if candidate in FORMAT_SPEC else None


router = APIRouter()


# ---------------------------------------------------------------------------
# Token mint
# ---------------------------------------------------------------------------
class MintTokenRequest(BaseModel):
    user_id: str
    artifact_id: str
    user_friendly_name: Optional[str] = None
    ttl_sec: Optional[int] = None


class MintTokenResponse(BaseModel):
    token: str
    wopi_src: str
    exp: int
    format: str


@router.post("/api/wopi/token", response_model=MintTokenResponse)
async def mint_token(req: MintTokenRequest, request: Request) -> MintTokenResponse:
    """Mint a WOPI access token for the given (user_id, artifact_id).

    Auth (bare minimum): every request mints. Real auth slots in by
    validating the request's session/JWT before this returns. For
    multi-user testing, the frontend passes a distinct `user_id` per
    browser session so the editor renders per-user cursors.
    """
    artifact_id = (req.artifact_id or "").strip()
    user_id = (req.user_id or "").strip()
    if not artifact_id or not user_id:
        raise HTTPException(status_code=400, detail="user_id and artifact_id required")
    # Deliverable lives at uploads/_autodraft/<artifact_id>/<filename>; the
    # filename is the schema's title_field resolved value (e.g.
    # INTSUM-...xlsx). Resolve from the artifact's schema_state.json so the
    # token encodes a deterministic full path — no glob at request time.
    from templates import PRODUCT_SCHEMAS
    from utils.exporter import artifact_filename_base
    artifact_dir = UPLOAD_DIR / "_autodraft" / artifact_id
    schema_state_path = artifact_dir / "schema_state.json"
    if not schema_state_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id} has no schema_state.json under uploads/_autodraft/",
        )
    import json as _json
    try:
        _ss = _json.loads(schema_state_path.read_text())
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read schema_state: {e}")
    _ptype = _ss.get("product_type") or ""
    _sd = _ss.get("structured_data") or {}
    _schema = PRODUCT_SCHEMAS.get(_ptype, {}) or {}

    fmt = _resolve_artifact_format(_ptype, _schema)
    if fmt is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"product_type={_ptype!r} has no live-edit-supported output_format "
                f"(expected one of {sorted(FORMAT_SPEC.keys())})"
            ),
        )
    spec = FORMAT_SPEC[fmt]
    # Narrative-format gate — DOCX / PPTX require the env flag.
    if spec["requires_narrative_flag"] and not _COLLABORA_NARRATIVE_EDIT:
        raise HTTPException(
            status_code=403,
            detail=(
                f"live edit for {fmt} is disabled "
                f"(set COLLABORA_NARRATIVE_EDIT=true to enable). "
                f"Frontend should fall back to the legacy editor."
            ),
        )
    # Per-product opt-out — see _DISALLOWED_LIVE_EDIT_PRODUCTS docstring
    # for the rationale (currently Threat Model only, because its DOCX
    # render leaves map images as unresolvable URL references).
    if _ptype in _DISALLOWED_LIVE_EDIT_PRODUCTS:
        raise HTTPException(
            status_code=403,
            detail=(
                f"product_type={_ptype!r} is not supported in the live editor "
                f"(render-side limitation). Frontend should fall back to the "
                f"legacy editor."
            ),
        )

    filename = f"{artifact_filename_base(_ptype, _schema, _sd, artifact_id)}{spec['ext']}"
    file_path = artifact_dir / filename
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"{fmt} deliverable not found at {file_path}",
        )
    ttl = min(int(req.ttl_sec) if req.ttl_sec else _TOKEN_TTL_SEC, _TOKEN_TTL_SEC)
    now = int(time.time())
    exp = now + ttl
    payload = {
        "user_id": user_id,
        "artifact_id": artifact_id,
        "user_friendly_name": req.user_friendly_name or user_id,
        "owner_user_id": file_path.parent.parent.name,
        "filename": filename,
        "format": fmt,
        "iat": now,
        "exp": exp,
    }
    token = jwt.encode(payload, _WOPI_SECRET, algorithm=_JWT_ALG)
    wopi_host = os.environ.get("WOPI_INTERNAL_BASE", "http://docker-api:8000")
    # WOPISrc is keyed by artifact_id, NOT by the JWT — every fresh
    # mint produces a different JWT (iat/exp tick every second), so
    # embedding it in the URL meant every page refresh handed coolwsd
    # a "new" document URL and a fresh docbroker / kit cold-start
    # cycle paid every time. Keying by artifact_id makes WOPISrc
    # stable per file, so a refresh attaches to the existing warm
    # docbroker (within `per_document.idle_timeout_secs`, default 1h).
    # The access_token still gates auth — coolwsd passes it as a
    # query param on every WOPI request the endpoints below read.
    wopi_src = f"{wopi_host.rstrip('/')}/wopi/files/{artifact_id}"
    return MintTokenResponse(token=token, wopi_src=wopi_src, exp=exp, format=fmt)


# ---------------------------------------------------------------------------
# Token verification helper
# ---------------------------------------------------------------------------
def _verify_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, _WOPI_SECRET, algorithms=[_JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="WOPI token expired")
    except jwt.InvalidTokenError as e:
        raise HTTPException(status_code=401, detail=f"invalid WOPI token: {e}")
    return payload


def _file_path_for(payload: dict) -> Path:
    """Deterministic path resolution from the token. The token was minted
    against `uploads/<owner>/<artifact_id>/<filename>`; every parameter
    rides the JWT. No glob, no schema re-read."""
    artifact_id = payload["artifact_id"]
    owner = payload.get("owner_user_id") or "_autodraft"
    filename = payload.get("filename") or "artifact.xlsx"
    path = UPLOAD_DIR / owner / artifact_id / filename
    if path.exists():
        return path
    raise HTTPException(status_code=404, detail=f"deliverable not found at {path}")


def _mime_for_path(path: Path) -> str:
    """Resolve the right Content-Type for GetFile from the file extension.
    Falls back to octet-stream — LibreOffice keys off BaseFileName so even
    a wrong MIME usually opens; the explicit MIME is just doctrinally
    correct + helps any non-LibreOffice WOPI client."""
    suffix = path.suffix.lower()
    for spec in FORMAT_SPEC.values():
        if spec["ext"] == suffix:
            return spec["mime"]
    return "application/octet-stream"


# ---------------------------------------------------------------------------
# Auth helper — extract + verify the access_token query param against
# the path artifact_id. Centralizes the check shared by the three
# WOPI endpoints below. Path segment is now the stable artifact_id
# (was a per-mint JWT) so coolwsd can attach to the existing warm
# docbroker on every refresh; the JWT moves to the access_token
# query param coolwsd already passes per WOPI spec.
# ---------------------------------------------------------------------------
def _verify_access(artifact_id: str, access_token: Optional[str]) -> dict:
    if not access_token:
        raise HTTPException(status_code=401, detail="access_token query param required")
    payload = _verify_token(access_token)
    if str(payload.get("artifact_id")) != str(artifact_id):
        raise HTTPException(
            status_code=403,
            detail=f"access_token artifact_id mismatch (token={payload.get('artifact_id')!r} path={artifact_id!r})",
        )
    return payload


# ---------------------------------------------------------------------------
# CheckFileInfo
# ---------------------------------------------------------------------------
@router.get("/wopi/files/{artifact_id}")
async def check_file_info(artifact_id: str,
                            access_token: Optional[str] = Query(None)) -> JSONResponse:
    """WOPI CheckFileInfo. The Online Editor pings this first to learn
    file metadata + caller identity. Auth is via the access_token
    query param coolwsd appends on every WOPI request.
    """
    payload = _verify_access(artifact_id, access_token)
    file_path = _file_path_for(payload)
    stat = file_path.stat()
    info = {
        "BaseFileName": file_path.name,
        "Size": stat.st_size,
        "Version": str(int(stat.st_mtime * 1000)),  # ms epoch — bumps on PutFile
        "OwnerId": payload.get("owner_user_id") or payload["user_id"],
        "UserId": payload["user_id"],
        "UserFriendlyName": payload.get("user_friendly_name") or payload["user_id"],
        "UserCanWrite": True,
        "UserCanNotWriteRelative": True,
        "SupportsUpdate": True,
        "SupportsLocks": False,         # editor handles co-edit, no WOPI lock
        "SupportsRename": False,
        "DisablePrint": False,
        "DisableExport": False,
        "DisableCopy": False,
        "EnableInsertRemoteImage": False,
        "PostMessageOrigin": "*",
        "HidePrintOption": False,
        "HideSaveOption": False,
        "HideExportOption": False,
    }
    return JSONResponse(content=info)


# ---------------------------------------------------------------------------
# GetFile
# ---------------------------------------------------------------------------
@router.get("/wopi/files/{artifact_id}/contents")
async def get_file(artifact_id: str,
                    access_token: Optional[str] = Query(None)) -> Response:
    """WOPI GetFile. Return raw bytes with format-correct Content-Type."""
    payload = _verify_access(artifact_id, access_token)
    file_path = _file_path_for(payload)
    data = file_path.read_bytes()
    return Response(
        content=data,
        media_type=_mime_for_path(file_path),
        headers={
            "Content-Length": str(len(data)),
            "X-WOPI-ItemVersion": str(int(file_path.stat().st_mtime * 1000)),
        },
    )


# ---------------------------------------------------------------------------
# PutFile (Online Editor save callback)
# ---------------------------------------------------------------------------
@router.post("/wopi/files/{artifact_id}/contents")
async def put_file(artifact_id: str, request: Request,
                    access_token: Optional[str] = Query(None)) -> Response:
    """WOPI PutFile. The Online Editor POSTs the saved bytes here.

    Overwrite-in-place. The deliverable IS canonical; there is no
    derive-from-something step on save. The mtime bump shows up as a
    new X-WOPI-ItemVersion on subsequent CheckFileInfo so any cached
    client invalidates.

    Every save fires an `editor.save` analyst_event with the user,
    format, byte count, and a structural fingerprint diff (tables /
    rows / paragraphs / total chars) so the analytics record the
    editor activity that previously went unlogged.
    """
    payload = _verify_access(artifact_id, access_token)
    file_path = _file_path_for(payload)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    # Snapshot the prior bytes BEFORE overwriting so the diff fingerprint
    # has a baseline. None on first-ever save.
    prior_bytes: Optional[bytes] = None
    try:
        if file_path.exists():
            prior_bytes = file_path.read_bytes()
    except Exception as e:
        log.warning("wopi.put_file: prior-bytes snapshot failed: %s", e)
    # Atomic write: write to .tmp then rename. Avoids a half-written file
    # being read by a concurrent CheckFileInfo / GetFile. The .tmp suffix
    # mirrors the source extension so any sanity-check on filename shape
    # still passes.
    tmp_path = file_path.with_suffix(file_path.suffix + ".tmp")
    tmp_path.write_bytes(body)
    os.replace(tmp_path, file_path)
    mtime = file_path.stat().st_mtime
    new_version = str(int(mtime * 1000))
    log.info("wopi.put_file: %s (%d bytes, version=%s, user=%s, format=%s)",
              file_path, len(body), new_version,
              payload.get("user_id"), payload.get("format"))
    # Fire analyst_event for the save. Best-effort: never let a telemetry
    # failure break the save flow.
    try:
        from utils import events as _events
        fmt = (payload.get("format") or "").lower()
        prior_fp = _structural_fingerprint(prior_bytes, fmt) if prior_bytes else None
        new_fp = _structural_fingerprint(body, fmt)
        delta = _fingerprint_delta(prior_fp, new_fp)
        _events.log_analyst_event(
            user_id=payload.get("user_id") or "",
            event_type="editor.save",
            surface="editor",
            artifact_id=artifact_id,
            value_num=len(body),
            detail={
                "format": fmt,
                "bytes": len(body),
                "prior_bytes": len(prior_bytes) if prior_bytes else 0,
                "wopi_version": new_version,
                "fingerprint": new_fp,
                "delta_vs_prior": delta,
                "session_id": payload.get("session_id") or "",
            },
        )
    except Exception as e:
        log.warning("wopi.put_file: analyst_event log failed: %s", e)
    return JSONResponse(
        content={
            "LastModifiedTime":
                datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat(),
        },
        headers={"X-WOPI-ItemVersion": new_version},
    )


# ---------------------------------------------------------------------------
# Structural fingerprint + diff for analyst-edit telemetry.
# ---------------------------------------------------------------------------
def _structural_fingerprint(blob: Optional[bytes], fmt: str) -> dict:
    """Coarse structural fingerprint of a saved file. Captures per-slide
    table+row counts (PPTX), per-sheet row+col counts (XLSX), and per-
    paragraph counts (DOCX). The diff between two consecutive saves
    shows the analyst's row additions / removals / text deltas even
    when the schema_state.json reconciler can't see un-marked rows.

    Returns {} on parse failure (best-effort)."""
    if not blob:
        return {}
    try:
        if fmt == "pptx":
            import io
            from pptx import Presentation  # type: ignore
            pres = Presentation(io.BytesIO(blob))
            slides = []
            for si, slide in enumerate(pres.slides):
                tables = []
                shapes = []
                for sh_i, sh in enumerate(slide.shapes):
                    sh_name = sh.name or ""
                    if sh.has_table:
                        tbl = sh.table
                        # Full per-cell text snapshot. Stored as a 2D
                        # list of strings so the diff can emit
                        # per-cell BEFORE/AFTER for every changed cell.
                        cells: list = []
                        for row in tbl.rows:
                            row_cells = []
                            for cell in row.cells:
                                row_cells.append(cell.text_frame.text or "")
                            cells.append(row_cells)
                        tables.append({
                            "shape_name": sh_name,
                            "rows": len(tbl.rows),
                            "cols": len(tbl.columns),
                            "cells": cells,
                        })
                    tf = getattr(sh, "text_frame", None)
                    if tf is not None:
                        # Per-paragraph text snapshot — catches narrative
                        # edits in non-table shapes.
                        paras = [(p.text or "") for p in tf.paragraphs]
                        shapes.append({
                            "shape_name": sh_name,
                            "shape_index": sh_i,
                            "paragraphs": paras,
                        })
                slides.append({
                    "slide_index": si,
                    "tables": tables,
                    "shapes": shapes,
                })
            return {"format": "pptx", "slides": slides,
                     "n_slides": len(slides)}
        if fmt == "xlsx":
            import io
            from openpyxl import load_workbook  # type: ignore
            wb = load_workbook(io.BytesIO(blob), data_only=True, read_only=True)
            sheets = []
            for sn in wb.sheetnames:
                ws = wb[sn]
                # Per-cell value grid for the used range. Bounded at
                # 200 rows × 50 cols per sheet so a runaway sheet
                # can't blow the analyst_event detail size; the analyst-
                # facing tables don't approach this.
                rows = []
                max_r = min(ws.max_row or 0, 200)
                max_c = min(ws.max_column or 0, 50)
                for r in range(1, max_r + 1):
                    row_vals = []
                    for c in range(1, max_c + 1):
                        v = ws.cell(row=r, column=c).value
                        row_vals.append("" if v is None else str(v))
                    rows.append(row_vals)
                sheets.append({
                    "sheet": sn,
                    "rows": ws.max_row,
                    "cols": ws.max_column,
                    "cells": rows,
                })
            return {"format": "xlsx", "sheets": sheets, "n_sheets": len(sheets)}
        if fmt == "docx":
            import io
            from docx import Document  # type: ignore
            doc = Document(io.BytesIO(blob))
            # Per-paragraph text array — full content for line-by-line
            # diff.
            paragraphs = [(p.text or "") for p in doc.paragraphs]
            tables = []
            for ti, t in enumerate(doc.tables):
                cells = []
                for row in t.rows:
                    cells.append([(cell.text or "") for cell in row.cells])
                tables.append({
                    "table_index": ti,
                    "rows": len(t.rows),
                    "cols": len(t.columns),
                    "cells": cells,
                })
            return {
                "format": "docx",
                "n_paragraphs": len(paragraphs),
                "n_tables": len(tables),
                "paragraphs": paragraphs,
                "tables": tables,
            }
    except Exception as e:
        log.warning("structural_fingerprint(%s) failed: %s", fmt, e)
        return {}
    return {}


def _fingerprint_delta(prior: Optional[dict], new: Optional[dict]) -> dict:
    """Diff two structural fingerprints. Returns a compact summary the
    analyst-action dashboard can read off the analyst_event row."""
    if not prior or not new:
        return {"baseline": prior is None,
                 "n_chars_new": (new or {}).get("total_text_chars", 0)}
    delta: dict = {}
    fmt = new.get("format") or prior.get("format")
    delta["format"] = fmt
    if fmt == "pptx":
        prior_slides = prior.get("slides") or []
        new_slides = new.get("slides") or []
        delta["text_chars_delta"] = (new.get("total_text_chars") or 0) - (
            prior.get("total_text_chars") or 0
        )
        per_slide: list = []
        for si in range(max(len(prior_slides), len(new_slides))):
            p_sl = prior_slides[si] if si < len(prior_slides) else {}
            n_sl = new_slides[si] if si < len(new_slides) else {}
            p_tables = p_sl.get("tables") or []
            n_tables = n_sl.get("tables") or []
            table_changes = []
            for ti in range(max(len(p_tables), len(n_tables))):
                p_t = p_tables[ti] if ti < len(p_tables) else {}
                n_t = n_tables[ti] if ti < len(n_tables) else {}
                if (p_t.get("rows") != n_t.get("rows")
                    or p_t.get("row_char_counts") != n_t.get("row_char_counts")
                ):
                    table_changes.append({
                        "table_name": n_t.get("name") or p_t.get("name") or "",
                        "rows_before": p_t.get("rows"),
                        "rows_after":  n_t.get("rows"),
                        "row_chars_before": p_t.get("row_char_counts") or [],
                        "row_chars_after":  n_t.get("row_char_counts") or [],
                    })
            if (table_changes
                or p_sl.get("text_chars") != n_sl.get("text_chars")):
                per_slide.append({
                    "slide_index": si,
                    "text_chars_before": p_sl.get("text_chars"),
                    "text_chars_after":  n_sl.get("text_chars"),
                    "table_changes": table_changes,
                })
        delta["slides_changed"] = per_slide
        return delta
    if fmt == "xlsx":
        prior_sheets = prior.get("sheets") or []
        new_sheets = new.get("sheets") or []
        changes = []
        for si in range(max(len(prior_sheets), len(new_sheets))):
            p_s = prior_sheets[si] if si < len(prior_sheets) else {}
            n_s = new_sheets[si] if si < len(new_sheets) else {}
            if (p_s.get("rows") != n_s.get("rows")
                or p_s.get("cols") != n_s.get("cols")
            ):
                changes.append({
                    "sheet": n_s.get("sheet") or p_s.get("sheet"),
                    "rows_before": p_s.get("rows"),
                    "rows_after":  n_s.get("rows"),
                    "cols_before": p_s.get("cols"),
                    "cols_after":  n_s.get("cols"),
                })
        delta["sheets_changed"] = changes
        return delta
    if fmt == "docx":
        delta["paragraphs_before"] = prior.get("n_paragraphs")
        delta["paragraphs_after"]  = new.get("n_paragraphs")
        delta["tables_before"]    = prior.get("n_tables")
        delta["tables_after"]     = new.get("n_tables")
        delta["text_chars_delta"] = (new.get("total_text_chars") or 0) - (
            prior.get("total_text_chars") or 0
        )
        return delta
    return delta


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------
@router.get("/api/wopi/health")
async def wopi_health() -> JSONResponse:
    """Diag: confirm the router is mounted + report config. Never returns
    the JWT secret."""
    return JSONResponse(content={
        "status": "ok",
        "upload_dir": str(UPLOAD_DIR),
        "internal_base": os.environ.get("WOPI_INTERNAL_BASE", "http://docker-api:8000"),
        "token_ttl_sec": _TOKEN_TTL_SEC,
        "collabora_narrative_edit": _COLLABORA_NARRATIVE_EDIT,
        "secret_source": (
            "WOPI_SECRET env" if os.environ.get("WOPI_SECRET")
            else "/app/data/wopi_secret" if Path("/app/data/wopi_secret").exists()
            else "random-per-process"
        ),
        "supported_formats": sorted(FORMAT_SPEC.keys()),
    })
