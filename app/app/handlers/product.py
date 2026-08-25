"""Product handler — both template forms.

Two template forms coexist:
- **Uploaded HTML** (user-supplied PDF/DOCX → `uploads/<file_key>.html`).
  Authored once at upload via `generate_template`; consumed by `chat`/`agents`/`docgen`.
- **Built-in schema** (Python dict in `templates/products/<product>.py`).
  Consumed by `intel_agent` for the 15 doctrinal products. Schema fills placeholders
  in `prompts/*.md` at dispatch time — same template document, schema-driven content.

`prompt_for(role, product_name=None)` is the single entry point for the three
agent system prompts in `prompts/`:
  - `operations_expert` — static
  - `intel_agent` — static
  - `product_writer` — schema fills product-specific placeholders

`schema_brief(product_name)` returns the per-product brief that the operations
expert sees as the result of its `set_product_type` tool call. It's a
formatted dump of the schema, not an agent prompt — rendered inline in Python
because there's no static prose to template.
"""
import asyncio
import json
import os
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from templates import PRODUCT_SCHEMAS

logger = logging.getLogger(__name__)

# Canonical-on-disk finalize endpoints — mounted by main.py via the
# include_router pattern. Lives next to the wopi router; both serve the
# editor / SPA lifecycle. The two endpoints below are exercisable via
# curl for verification; the SPA wrapper UI (editor header bar "Mark as
# Final" button + downstream refire modal) lands in a follow-up.
router = APIRouter()

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))
FILE_HOST_API = "/files/"

# Bounded executor for the bg visual-verification renders (preview.png +
# page-NN.png). During parallel walks (V14: ~30 products generated in
# parallel) the previous one-daemon-thread-per-artifact pattern blew past
# the OS native-thread limit and raised
# `RuntimeError: can't start new thread` — those artifacts then shipped
# without any page-NN.png files. A fixed-size pool serializes the renders
# when threads are saturated rather than dropping them. Sized for the
# typical concurrent emit fan-out (~8) without overwhelming the Collabora
# sidecar (which itself serializes convert-to/pdf).
_BG_RENDER_MAX_WORKERS = int(os.environ.get("PYTHO_BG_RENDER_WORKERS", "8"))
_bg_render_executor = ThreadPoolExecutor(
    max_workers=_BG_RENDER_MAX_WORKERS,
    thread_name_prefix="bg-render",
)

_STATIC_ROLES = {"operations_expert", "operations_expert_manual", "intel_agent"}
_SCHEMA_ROLES = {"product_writer"}


def _classify_no_artifact_reason(result: dict) -> str:
    """Classify why the runner ended without registering an artifact.

    Reads the operations-expert `answer` text + result metadata to bucket
    the failure into one of:
      - `intel_agent_no_tools` — intel_agent emitted Message 2 with empty
        cougaar_call_summary. Writer had nothing to consume.
      - `intel_agent_tool_loop` — intel_agent looped on tool calls and blew
        the per-dispatch iteration cap.
      - `writer_vllm_timeout` — product_writer LLM call hit the vllm
        ReadTimeout ceiling (writer payload too large for the per-call
        budget).
      - `writer_max_iterations` — product_writer hit max iteration cap before
        producing a clean emit_artifact preview.
      - `writer_never_dispatched` — intel_agent finished, OE got the result,
        but never dispatched the product_writer (e.g. OE concluded the
        precondition was missing and bailed without writing). Identifiable
        by stage_events: no `dispatch_product_writer` event.
      - `precondition_missing` — writer refused to generate because consumed
        upstream artifact (HVTL, MCOO, Threat COA Statement, …) was not present.
      - `runner_aborted` — catch-all (matches the generic
        "Runner finished without registering an artifact" string).
    """
    if not isinstance(result, dict):
        return "runner_aborted"
    cougaar = (result.get("cougaar_call_summary") or {}) if isinstance(result.get("cougaar_call_summary"), dict) else {}
    answer = (result.get("answer") or "").lower()
    stage_events = result.get("stage_events") or []
    # Check explicit stage signals first — these win over answer-text heuristics.
    saw_writer_dispatch = any(
        (e.get("stage") or "") == "dispatch_product_writer" for e in stage_events
    )
    saw_writer_timeout = any(
        (e.get("stage") or "").startswith("llm_product_writer") and
        ("ReadTimeout" in str(e.get("error") or "") or
         "Timeout" in str(e.get("error") or "") or
         (isinstance(e.get("duration_ms"), int) and e.get("duration_ms") >= 290_000))
        for e in stage_events
    )
    if saw_writer_timeout:
        return "writer_vllm_timeout"
    if "readtimeout" in answer or "timed out" in answer or "timeout" in answer:
        return "writer_vllm_timeout"
    if "max iteration" in answer or "maximum processing iteration" in answer or "maximum iteration" in answer:
        return "writer_max_iterations"
    if "trapped in a" in answer and ("loop" in answer or "tool" in answer):
        return "intel_agent_tool_loop"
    if "retrieval loop" in answer or "tool-calling loop" in answer:
        return "intel_agent_tool_loop"
    if not saw_writer_dispatch and cougaar:
        # intel_agent fired tools but OE never dispatched writer — usually a
        # precondition-missing bail. TMP case from v2-31p.
        return "writer_never_dispatched"
    if not cougaar and ("not transferred" in answer or "not been transferred" in answer
                          or "absence of" in answer or "missing" in answer):
        return "precondition_missing"
    if not cougaar:
        return "intel_agent_no_tools"
    return "runner_aborted"


# ============================================================================
# Public API
# ============================================================================

def prompt_for(role: str, product_name: Optional[str] = None) -> str:
    """Render the system prompt for one of the three agent roles."""
    if role not in _STATIC_ROLES and role not in _SCHEMA_ROLES:
        raise ValueError(f"unknown prompt role: {role!r}")
    template = (PROMPTS_DIR / f"{role}.md").read_text()
    if role in _STATIC_ROLES:
        return template
    if not product_name or product_name not in PRODUCT_SCHEMAS:
        raise ValueError(
            f"role {role!r} requires a product_name from {sorted(PRODUCT_SCHEMAS)}, "
            f"got {product_name!r}"
        )
    schema = PRODUCT_SCHEMAS[product_name]
    fields = _product_writer_fields(product_name, schema)
    return _substitute(template, fields)


def schema_brief(product_name: str) -> str:
    """Return the per-product schema brief shown to the operations expert as
    the result of its `set_product_type` tool call.

    This is structured schema data, not an agent prompt — there's no static
    prose body to template, so it's built directly from the schema dict here."""
    if product_name not in PRODUCT_SCHEMAS:
        return f"UNKNOWN_PRODUCT_TYPE: {product_name}. Must be one of {sorted(PRODUCT_SCHEMAS)}"
    s = PRODUCT_SCHEMAS[product_name]
    doctrine = s.get("doctrine") or {}
    output_format = s.get("output_format") or "docx"
    orientation = "landscape" if output_format == "pptx" else "portrait"
    parts = [f"# Schema brief — {product_name}", ""]
    if doctrine.get("purpose"):
        parts.append(f"**Purpose:** {doctrine['purpose']}")
    if doctrine.get("ordering_rule"):
        parts.append(f"\n**Ordering rule:** {doctrine['ordering_rule']}")
    parts.append(
        f"\n**Shape:** `{s.get('shape', '')}`  ·  "
        f"**Output format:** `{output_format}` ({orientation})"
    )
    if s.get("consumes_artifacts"):
        parts.append("\n**Consumes upstream artifacts:** " + ", ".join(s["consumes_artifacts"]))
    parts.append("\n## Header fields (scope to confirm with analyst)\n")
    parts.append(_render_header_brief(s))
    parts.append("\n## Structure — fixed by the schema (do not propose alternatives)\n")
    parts.append(_render_fixed_structure(s))
    if s.get("subtleties"):
        parts.append("\n## Doctrinal subtleties\n")
        parts.append(_render_bullets(s["subtleties"]))
    if s.get("hard_rules"):
        parts.append("\n## Hard rules\n")
        parts.append(_render_bullets(s["hard_rules"]))
    if s.get("data_sources"):
        parts.append("\n## Data sources the intel agent will pull\n")
        parts.append(_render_bullets(s["data_sources"]))
    return "\n".join(parts)


# ============================================================================
# Manual-dispatch path — Generate-button entry point (Req 4)
# ============================================================================

def generate(*, product_type: str, project_id: str, user_id: str,
              session_id: Optional[str] = None, context: Optional[str] = None,
              db=None) -> dict:
    """Manual-dispatch path for the Generate-button UI in ProductLibraryModal.

    Wraps `run_operations_expert_loop` with a synthesized analyst message so
    the analyst doesn't have to type "Build me a WEM for AO ALPHA-NORTH" into
    chat. The runner is reused as-is; this function only synthesizes the
    prompt, threads the State through, persists the turn via `save_turn`, and
    renders the resulting artifact to HTML + DOCX/PPTX so the UI can open it
    in DocEditor.

    Parameters
    ----------
    product_type : str
        Schema key from PRODUCT_SCHEMAS (e.g. "WEM", "HVTL"). MUST match.
    project_id : str
        Pytho project_id — scopes session retrieval and artifact ownership.
    user_id : str
        Pytho user_id — written into the SessionDetail row.
    session_id : Optional[str]
        Reuse an existing session_id to thread additional generations through
        the same operations-expert conversation. Omit/None to start fresh
        (new uuid).
    context : Optional[str]
        Free-form analyst context appended to the synthesized prompt
        ("Additional context: ..."). Used to inject AO scope, valid window,
        analyst-preferred sources, etc. without the analyst having to chat.
    db : Session, optional
        SQLAlchemy session. Created with database.SessionLocal() if omitted.

    Returns
    -------
    dict with: session_id, artifact_id, status, htmlLink, docxLink_or_pptxLink

    Notes
    -----
    Does NOT modify `run_operations_expert_loop` — that's shared with `run_turn`
    via WebSocket /ws/intel_agent. This is a pure wrapper. The exporter call
    happens AFTER the runner finishes; nothing inside the runner cares.
    """
    if product_type not in PRODUCT_SCHEMAS:
        return {
            "status": "error",
            "error": f"unknown product_type {product_type!r}; expected one of {sorted(PRODUCT_SCHEMAS)}",
        }

    # Local imports to avoid the circular handlers/intel_agent.py → handlers/product.py
    # link at module-load time (intel_agent imports prompt_for from this file).
    # Exporter calls now live inside render_artifact_files (factored out so
    # intel_agent.main can call it directly over the WS path).
    from handlers import intel_agent
    from utils import database

    owns_db = False
    if db is None:
        db = database.SessionLocal()
        owns_db = True

    try:
        if not session_id:
            session_id = str(uuid.uuid4())

        prompt = f"Generate a {product_type}."
        if context:
            prompt += f" Additional context: {context}"

        # Manual-dispatch path: uses a dedicated OE prompt that has NO
        # set_product_type tool and NO scope-confirmation step. The product
        # type is pre-set in state, OE goes directly to dispatch_intel_agent
        # then dispatch_product_writer.
        result = intel_agent.run_turn(
            prompt,
            session_id=session_id,
            project_id=project_id,
            user_id=user_id,
            db=db,
            title=f"Generate {product_type}",
            manual_product_type=product_type,
        )

        artifacts = result.get("artifacts") or []

        # run_turn returns the artifacts created on this single turn. The
        # Generate-button UI dispatches one product per click, so there should
        # be exactly one — but the operations expert sometimes chains
        # dispatches (HVTL implicitly consumes SITEMP/Threat Overlay/Threat COA Statement), so prefer
        # the artifact whose type matches the requested product_type, falling
        # back to the most recent one.
        target = None
        for a in artifacts:
            if a.get("type") == product_type:
                target = a
                break
        if target is None and artifacts:
            target = artifacts[-1]

        if not target:
            # No artifact was registered. Persist a forensic dump beside where
            # the artifact would have lived so post-mortem can see what tools
            # fired, which stages completed, and how long each leg took.
            # Without this dump the failure path is a black box — the driver
            # and frontend only see `aid=` empty.
            failure_dump = {
                "status": "no_artifact",
                "product_type": product_type,
                "session_id": session_id,
                "project_id": project_id,
                "user_id": user_id,
                "turn_total_ms": result.get("turn_total_ms"),
                "answer": result.get("answer", ""),
                "stage_events": (result.get("stage_events") if isinstance(result, dict) else None) or [],
                "cougaar_call_summary": (result.get("cougaar_call_summary") if isinstance(result, dict) else None) or {},
                "intel_agent_data": (result.get("intel_agent_data") if isinstance(result, dict) else None) or {},
                # Best-effort failure-reason classification. Specific signatures
                # win over the generic catch-all so the driver / frontend can
                # tell "writer hit iteration cap" apart from "intel agent never
                # fired tools" apart from "writer refused due to missing deps".
                "failure_reason": _classify_no_artifact_reason(result),
            }
            try:
                owner = (user_id or "anonymous").strip() or "anonymous"
                fail_dir = UPLOAD_DIR / owner / f"_failed_{product_type}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
                fail_dir.mkdir(parents=True, exist_ok=True)
                (fail_dir / "schema_state.json").write_text(json.dumps(failure_dump, indent=2, default=str))
            except Exception as e:
                logger.warning(f"generate(): failed-path dump write failed for {product_type}: {e}")
            return {
                "status": "no_artifact",
                "session_id": session_id,
                "artifact_id": "",
                "htmlLink": None,
                "docxLink": None,
                "pptxLink": None,
                "answer": result.get("answer", ""),
                "turn_total_ms": result.get("turn_total_ms"),
                "stage_events": failure_dump["stage_events"],
                "cougaar_call_summary": failure_dump["cougaar_call_summary"],
                "failure_reason": failure_dump["failure_reason"],
                "error": (
                    "Runner finished without registering an artifact. "
                    f"failure_reason={failure_dump['failure_reason']}. "
                    "Inspect the operations-expert answer for the failure mode."
                ),
            }

        artifact_id = target["artifact_id"]
        artifact_type = target["type"]
        gaps = target.get("gaps", []) or []

        rendered = render_artifact_files(
            session_id=session_id,
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            gaps=gaps,
            db=db,
        )
        if rendered.get("status") != "ok":
            return {
                "status": "error",
                "session_id": session_id,
                "artifact_id": artifact_id,
                "error": rendered.get("error", "render failed"),
            }

        # Augment schema_state.json with the render-side stages so the dump
        # beside the artifact carries the full trigger → artifact-persist
        # timeline. The WS path does the same; this is the legacy
        # /api/products/generate equivalent.
        try:
            from handlers.intel_agent import (
                stage_events_snapshot as _stage_events_snapshot,
                cougaar_call_summary_snapshot as _cougaar_summary_snapshot,
                _augment_schema_states,
            )
            _augment_schema_states(
                [{"artifact_id": artifact_id, "htmlLink": rendered["htmlLink"]}],
                _stage_events_snapshot(), _cougaar_summary_snapshot(),
            )
        except Exception as e:
            logger.warning(f"generate(): schema_state augment failed for {artifact_id}: {e}")

        return {
            "status": "ok",
            "session_id": session_id,
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "htmlLink": rendered["htmlLink"],
            "docxLink": rendered["docxLink"],
            "pptxLink": rendered["pptxLink"],
            "xlsxLink": rendered.get("xlsxLink"),
            "gaps": gaps,
            "answer": result.get("answer", ""),
            "turn_total_ms": result.get("turn_total_ms"),
            "stage_events": (result.get("stage_events") if isinstance(result, dict) else None) or [],
            "cougaar_call_summary": (result.get("cougaar_call_summary") if isinstance(result, dict) else None) or {},
        }
    finally:
        if owns_db:
            db.close()


def _write_docx_map_embeds_sidecar(*, artifact_dir: Path, html_str: str,
                                    artifact_id: str) -> None:
    """Emit a slim map_embeds.json for a DOCX-with-maps artifact.

    Per-entry shape (no twips — Writer reflows so position is runtime-only):
      {
        "map_id":      "<schema field name or stamped uuid>",
        "label":       "<schema render_label>",
        "alt_text":    "pytho-map:<map_id>",   ← join key for UNO probe
        "bbox_lonlat": [w,s,e,n] | null,
        "image_filename": "<field>.jpg",
        "artifact_id": "<aid>",
        "twips":       null,
        "page_index":  null,
      }

    The LiveEditor frontend on document-loaded posts CallPythonScript →
    pytho_graphics.get_graphic_objects, gets back per-graphic {Title,
    x_twips, y_twips, w_twips, h_twips}, joins by alt-text == Title to
    these entries → renders overlay buttons at the live twips position.

    convert.js stamps the alt-text on the embedded ImageRun when
    isMapFigure (see docx-converter/convert.js — altText option includes
    title + description + name).
    """
    import re
    # Pull each ibl-map-embed figure tag first, then parse its attributes
    # individually. A single regex that requires data-map-field to appear
    # after data-bbox would mis-fire on figures where attribute order
    # differs — convert.js emits data-artifact-id between bbox and field,
    # and a greedy `[^>]*` swallows the field attr so image_filename
    # falls back to the map_id UUID instead of the on-disk JPG basename.
    figure_re = re.compile(
        r'<figure\b[^>]*class="ibl-map-embed"[^>]*>',
        re.IGNORECASE,
    )
    attr_re = re.compile(
        r'(\bdata-[a-z0-9_-]+)="([^"]*)"',
        re.IGNORECASE,
    )
    out: list[dict] = []
    for fm in figure_re.finditer(html_str or ""):
        attrs = {k.lower(): v for k, v in attr_re.findall(fm.group(0))}
        map_id = (attrs.get("data-map-id") or "").strip()
        bbox_raw = (attrs.get("data-bbox") or "").strip()
        field = (attrs.get("data-map-field") or "").strip() or map_id
        bbox_lonlat = None
        if bbox_raw:
            try:
                parts = [float(x) for x in bbox_raw.split(",")]
                if len(parts) == 4:
                    bbox_lonlat = parts
            except Exception:
                bbox_lonlat = None
        join_key = map_id or field
        out.append({
            "map_id": join_key,
            "label": field,
            "alt_text": f"pytho-map:{join_key}",
            "bbox_lonlat": bbox_lonlat,
            "image_filename": f"{field}.jpg",
            "artifact_id": artifact_id,
            "twips": None,
            "page_index": None,
        })
    sidecar = artifact_dir / "map_embeds.json"
    with open(sidecar, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    logger.info("DOCX map_embeds.json: %d embeds at %s", len(out), sidecar)


def render_artifact_files(*, session_id: str, artifact_id: str,
                           artifact_type: str, gaps: Optional[list] = None,
                           db=None) -> dict:
    """Render a registered artifact to HTML + DOCX/PPTX on disk and return
    the public /files/ links.

    Factored out of `generate()` so the WebSocket path (intel_agent.main) can
    produce the same htmlLink/docxLink the synchronous /api/products/generate
    endpoint produces — without having to re-run the operations expert. The
    artifact is looked up out of the per-session registry; this function
    never modifies state, it only reads structured_data and writes files.

    Parameters
    ----------
    session_id : str
        Pytho session_id whose State holds the artifact in its registry.
    artifact_id : str
        Stable artifact_id minted by the product writer at emit time.
    artifact_type : str
        Schema key (e.g. "WEM", "HVTL"). Used for filename + schema lookup.
    gaps : list, optional
        Per-section gap markers from the product writer's emit, threaded into
        the HTML renderer so callouts render in the right places.
    db : Session, optional
        SQLAlchemy session. Created with database.SessionLocal() if omitted.

    Returns
    -------
    dict with keys: status, htmlLink, docxLink, pptxLink. On error,
    {"status": "error", "error": str, "htmlLink": None, ...}.
    """
    # Lazy imports to avoid the circular handlers/intel_agent.py → product
    # import chain at module-load time.
    from handlers import intel_agent
    from utils import database
    from utils.exporter import render_artifact_html, html_to_docx, render_artifact_pptx

    owns_db = False
    if db is None:
        db = database.SessionLocal()
        owns_db = True
    gaps = gaps or []
    try:
        state_after = intel_agent.load_state(session_id, db)
        reg = state_after.registry_dict()
        items = reg.get(artifact_type, ())
        target_entry = None
        for entry in items:
            if entry.get("artifact_id") == artifact_id:
                target_entry = entry
                break
        if target_entry is None:
            return {
                "status": "error",
                "htmlLink": None,
                "docxLink": None,
                "pptxLink": None,
                "error": (
                    f"artifact {artifact_id} ({artifact_type}) not present in "
                    f"state for session {session_id} after reload"
                ),
            }

        structured_data = target_entry.get("structured_data") or {}
        schema = PRODUCT_SCHEMAS.get(artifact_type, {})
        # Resolve the formats list:
        #   - schema.output_formats (plural list) takes precedence — used by
        #     the new XLSX lane to declare ["xlsx"] (default) with a fallback
        #     to ["pptx"] / ["docx"] under DISABLE_XLSX.
        #   - schema.output_format (singular string) is the legacy field; if
        #     output_formats is absent we promote it to a one-element list.
        # DISABLE_XLSX=1 env var: any "xlsx" in formats is replaced by the
        # schema.legacy_formats list (fall back to original [output_format]
        # if legacy_formats is absent). Lets the analyst kill-switch the
        # new XLSX rendering and fall back to the existing DOCX/PPTX path.
        output_formats = schema.get("output_formats")
        if not output_formats:
            output_formats = [schema.get("output_format") or "docx"]
        if os.environ.get("DISABLE_XLSX") == "1" and "xlsx" in output_formats:
            legacy = schema.get("legacy_formats") or [schema.get("output_format") or "docx"]
            output_formats = [f for f in output_formats if f != "xlsx"] + list(legacy)
            output_formats = list(dict.fromkeys(output_formats))  # de-dupe, preserve order
        # `output_format` (singular) still drives page orientation downstream
        # — pick the first NON-XLSX entry for that purpose since XLSX has
        # no doctrinal orientation.
        first_non_xlsx = next((f for f in output_formats if f != "xlsx"), None) \
            or schema.get("output_format") or "docx"
        output_format = first_non_xlsx
        orientation = "landscape" if output_format == "pptx" else "portrait"
        formats = list(output_formats)
        classification = ((structured_data.get("header") or {}).get("classification")
                          or "UNCLASSIFIED")
        # Doctrinal citation no longer rendered as a per-page footer (analyst
        # feedback: reads as redundant chrome). The schema field is still
        # carried for writer/agent grounding but never reaches the analyst.

        # All files for one product live in one folder:
        #   uploads/<user_id>/<artifact_id>/{artifact.html, artifact.docx,
        #     artifact.pptx, map.png}
        # artifact_id is the deterministic <PRODUCT>_<UTC_TS> id minted at
        # turn start (intel_agent._next_artifact_id), so the path is known
        # before any tool call and tool_render_map writes its PNG into the
        # All autodrafted artifacts live under uploads/_autodraft/<id>/
        # — full stop. There is no per-user owner directory for
        # autodraft output. Prior behavior pulled owner from
        # header.owner_user_id / target_entry.owner_user_id / fell back
        # to "anonymous" — which created junk `uploads/anonymous/...`
        # / `uploads/<email>/...` trees diverged from the in-flight
        # `_autodraft/` artifact dir, breaking the token-mint endpoint
        # and every consumer that looks for schema_state.json under
        # _autodraft/. The autodraft pipeline owns one canonical home.
        owner = "_autodraft"
        artifact_dir = UPLOAD_DIR / owner / artifact_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        rel = f"{owner}/{artifact_id}"
        # HTML is the input to the DOCX/PPTX bridge (convert.js + python-pptx)
        # AND the canonical surface for TinyMCE-DocEditor-edited products.
        # XLSX-canonical products (output_formats=["xlsx"], no docx/pptx in
        # formats) skip HTML entirely — the XLSX is the canonical document,
        # and emitting an htmlLink would route the frontend to the legacy
        # TinyMCE DocEditor instead of the new SheetEditor. The bg-render
        # visual-verification path (preview.png / page-NN.png) is also
        # skipped for XLSX-only since neither preview source applies.
        xlsx_only = formats == ["xlsx"]
        html_str = ""
        html_link = None
        # Title-based filename for the user-facing deliverables (docx /
        # pptx / xlsx). The HTML stays at the fixed `artifact.html` path
        # because the preview / DocEditor headless renderer reads it
        # there. Deliverables get the schema title_field value so analysts
        # see e.g. `INTSUM-291800ZJUN26-300600ZJUN26.docx` instead of
        # `artifact.docx` when they download.
        from utils.exporter import artifact_filename_base
        _schema_for_name = PRODUCT_SCHEMAS.get(artifact_type, {}) or {}
        deliverable_base = artifact_filename_base(
            artifact_type, _schema_for_name, structured_data, artifact_id,
        )
        if not xlsx_only:
            import time as _t_raf
            _t_html = _t_raf.monotonic()
            logger.info("RAF HTML START type=%s aid=%s", artifact_type, artifact_id)
            html_path = artifact_dir / "artifact.html"
            html_str = render_artifact_html(artifact_type, structured_data,
                                              gaps=gaps, artifact_id=artifact_id,
                                              user_id=owner)
            html_path.write_text(html_str, encoding="utf-8")
            html_link = f"{FILE_HOST_API}{rel}/artifact.html"
            logger.info("RAF HTML DONE type=%s aid=%s dur_s=%.1f size=%d",
                          artifact_type, artifact_id, _t_raf.monotonic() - _t_html, len(html_str))

        docx_link = None
        pptx_link = None
        xlsx_link = None
        if "xlsx" in formats:
            try:
                from utils.xlsx_exporter import render_artifact_xlsx
                xlsx_name = f"{deliverable_base}.xlsx"
                xlsx_path = artifact_dir / xlsx_name
                render_artifact_xlsx(artifact_type, structured_data,
                                       str(xlsx_path),
                                       artifact_id=artifact_id, user_id=owner)
                xlsx_link = f"{FILE_HOST_API}{rel}/{xlsx_name}"
            except Exception as e:
                logger.warning(f"render_artifact_files: xlsx render failed for {artifact_id}: {e}")
        if "docx" in formats:
            try:
                docx_name = f"{deliverable_base}.docx"
                docx_path = artifact_dir / docx_name
                # Route through the bounded executor (_bg_render_executor,
                # max_workers=8). html_to_docx forks a Node subprocess via
                # subprocess.run; during parallel walks 30+ concurrent forks
                # blew past the container's pids cap and raised
                # `[Errno 11] Resource temporarily unavailable` (EAGAIN on
                # fork). The executor caps in-flight forks at 8 — bursts
                # queue rather than fail. Submitting + waiting on .result()
                # is safe from both sync and async callers (FastAPI sync
                # endpoint + runtime/dag_executor.run_dag async path).
                _bg_render_executor.submit(
                    html_to_docx, html_str, str(docx_path),
                    classification=classification,
                    orientation=orientation,
                ).result()
                docx_link = f"{FILE_HOST_API}{rel}/{docx_name}"
            except Exception as e:
                logger.warning(f"render_artifact_files: docx render failed for {artifact_id}: {e}")
        if "pptx" in formats:
            try:
                pptx_name = f"{deliverable_base}.pptx"
                pptx_path = artifact_dir / pptx_name
                # Pass user_id so _emit_map_slide can resolve the
                # /files/<uid>/<aid>/<field_name>.jpg map image path.
                # Without this the PPTX silently dropped every shape="map"
                # slide because the path fell back to "anonymous" and the
                # JPEG wasn't where the resolver looked.
                render_artifact_pptx(artifact_type, structured_data,
                                      str(pptx_path), gaps=gaps,
                                      artifact_id=artifact_id, user_id=owner)
                pptx_link = f"{FILE_HOST_API}{rel}/{pptx_name}"
            except Exception as e:
                logger.warning(f"render_artifact_files: pptx render failed for {artifact_id}: {e}")

        # DOCX-with-maps sidecar (slim). Carries the alt-text → map_id
        # join key for runtime enumeration. The image position can't be
        # set at write time (Writer reflows inline images with text), so
        # the LiveEditor frontend dispatches a CallPythonScript postMessage
        # to pytho_graphics.py at document-loaded — the UNO script returns
        # the live twips bbox for every graphic, paired by alt-text to
        # these sidecar entries. PPTX writes its own richer sidecar (with
        # twips) from render_artifact_pptx.
        if "docx" in formats and docx_link:
            try:
                _write_docx_map_embeds_sidecar(
                    artifact_dir=artifact_dir,
                    html_str=html_str,
                    artifact_id=artifact_id,
                )
            except Exception as e:
                logger.warning(
                    f"render_artifact_files: docx map_embeds sidecar failed for {artifact_id}: {e}"
                )

        # ----------------------------------------------------------------
        # Visual-verification renders (BACKGROUND — never block the
        # analyst's artifact response). Two complementary captures land
        # beside the source files:
        #   - preview.png — Playwright drives the headless DocEditor route
        #     (/missioncommand/docs/render) and screenshots the TinyMCE
        #     iframe body. WYSIWYG: matches what the analyst sees in the
        #     editor including page-break chrome. ~5-15s typical, up to
        #     60s on slow first run.
        #   - page-01.png, page-02.png ... — DOCX/PPTX posted to the
        #     Collabora editor sidecar's /cool/convert-to/pdf, then
        #     PyMuPDF rasterizes each PDF page to PNG. ~2-5s typical.
        # Side-by-side diff of these catches HTML/DOCX drift in one glance.
        # Both run in a daemon thread so render_artifact_files returns
        # the htmlLink/docxLink immediately — the analyst sees their
        # artifact the moment the writer emits, and the verification
        # images show up on disk a few seconds later.
        # ----------------------------------------------------------------
        def _bg_render_visual_verification():
            try:
                from utils.render.preview import render_preview_to_png_sync
                preview_path = artifact_dir / "preview.png"
                ok = render_preview_to_png_sync(owner, artifact_id, preview_path,
                                                  orientation=orientation)
                if not ok:
                    logger.warning(
                        f"bg-render: preview.png skipped/failed for {artifact_id}"
                    )
            except Exception as e:
                logger.warning(f"bg-render: preview failed for {artifact_id}: {e}")
            try:
                from utils.render.doc_pages import render_doc_to_pages_safe
                # Use the title-based filenames we wrote above.
                if docx_link:
                    docx_p = artifact_dir / f"{deliverable_base}.docx"
                    render_doc_to_pages_safe(docx_p, artifact_dir, filename_stem="page")
                elif pptx_link:
                    pptx_p = artifact_dir / f"{deliverable_base}.pptx"
                    render_doc_to_pages_safe(pptx_p, artifact_dir, filename_stem="page")
            except Exception as e:
                logger.warning(f"bg-render: page render failed for {artifact_id}: {e}")

        try:
            # Submit to the bounded executor (see _bg_render_executor at the
            # top of this module). The executor's work queue absorbs bursts
            # past the worker cap — no thread-exhaustion failures even when
            # a parallel walk emits 30+ artifacts in quick succession.
            _bg_render_executor.submit(_bg_render_visual_verification)
        except Exception as e:
            logger.warning(f"render_artifact_files: failed to submit bg-render task for {artifact_id}: {e}")

        # ----------------------------------------------------------------
        # Schema-state debug dump (additive — never breaks the pipeline).
        # Beside every artifact we drop a schema_state.json that captures:
        #   - product_type
        #   - spec (the PRODUCT_SCHEMAS[type] doctrine schema)
        #   - intel_agent_data (full bundle of tool responses that fed the
        #     writer, keyed by tool name — what the writer actually saw)
        #   - structured_data (the final structured_data the renderer just
        #     turned into HTML/DOCX/PPTX)
        #   - generated_at (ISO-8601 UTC)
        #   - cougaar_call_summary (per-tool {called, row_count,
        #     supplemented_from_fixture, elapsed_ms, source, error})
        # The dump lets a debugger see exactly what data fed each artifact
        # without re-running the agent loop. Failures here are logged but
        # do not block the artifact from rendering.
        # ----------------------------------------------------------------
        try:
            try:
                intel_agent_data = intel_agent._extract_ia_data(state_after)
            except Exception as e:
                logger.warning(f"render_artifact_files: _extract_ia_data failed for {artifact_id}: {e}")
                intel_agent_data = {}
            try:
                cougaar_call_summary = intel_agent.cougaar_call_summary_snapshot()
            except Exception as e:
                logger.warning(f"render_artifact_files: cougaar_call_summary_snapshot failed for {artifact_id}: {e}")
                cougaar_call_summary = {}
            try:
                stage_events = intel_agent.stage_events_snapshot()
            except Exception as e:
                logger.warning(f"render_artifact_files: stage_events_snapshot failed for {artifact_id}: {e}")
                stage_events = []
            schema_state = {
                "product_type": artifact_type,
                "artifact_id": artifact_id,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "spec": PRODUCT_SCHEMAS.get(artifact_type, {}),
                "intel_agent_data": intel_agent_data,
                "structured_data": structured_data,
                "cougaar_call_summary": cougaar_call_summary,
                "stage_events": stage_events,
                "gaps": gaps,
            }
            schema_state_path = artifact_dir / "schema_state.json"
            schema_state_path.write_text(
                json.dumps(schema_state, default=str, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning(f"render_artifact_files: schema_state.json write failed for {artifact_id}: {e}")

        return {
            "status": "ok",
            "htmlLink": html_link,
            "docxLink": docx_link,
            "pptxLink": pptx_link,
            "xlsxLink": xlsx_link,
        }
    finally:
        if owns_db:
            db.close()


# ---------------------------------------------------------------------------
# Canonical-on-disk finalize endpoints
#
# POST /api/artifacts/{artifact_id}/finalize
#   1. Resolve deliverable path (same convention wopi._file_path_for uses).
#   2. Acquire per-artifact flock on .finalize.lock.
#   3. Snapshot prior schema_state.json to history/schema_state.<ts>.json.
#   4. Invoke import_artifact() to walk markers in the deliverable.
#   5. Validate against PRODUCT_SCHEMAS[product_type]; 422 on failure.
#   6. Atomically overwrite schema_state.json.
#   7. Compute immediate_downstreams via depends_on cross-reference.
#   8. Release lock; return summary + downstream list.
#
# POST /api/artifacts/{artifact_id}/refire-immediate-downstream
#   Body: {product_types: ["SITEMP", "Threat COAs", ...]}.
#   Fires each listed product once against the updated upstream.
#   No cascade — analyst explicitly drives each hop.
#
# Per the user's stated corrections to the architecture spec:
#   - Stateless finalize (no DRAFT/SAVED/FINAL state machine).
#   - No artifact_state key, no state_version, no un-finalize endpoint.
#   - PERMISSIVE downstream policy (no STRICT freshness gating).
#   - Audit log to uploads/_autodraft/<artifact_id>/history/audit.jsonl.
# ---------------------------------------------------------------------------


class RefireBody(BaseModel):
    product_types: list[str] = []


# Strong refs to in-flight fire-and-forget refire tasks so the event loop
# doesn't garbage-collect them mid-run. register_and_run (inside each refire)
# tracks the actual DAG task separately; this set only keeps the dispatch
# coroutine alive until it returns.
_REFIRE_BG_TASKS: set[asyncio.Task] = set()


def _autodraft_dir_for(artifact_id: str) -> Path:
    return UPLOAD_DIR / "_autodraft" / artifact_id


def _deliverable_path_for(artifact_id: str, schema_state: dict) -> Optional[Path]:
    """Resolve the deliverable file under uploads/_autodraft/<artifact_id>/.

    Mirrors handlers/wopi.py:_file_path_for + the artifact_filename_base
    naming convention. Returns None when the file can't be found.

    Format resolution iterates `output_formats` (plural list) first and
    falls back to the singular `output_format` only when the schema
    doesn't declare a list. HPTL/HVTL carry `output_formats=["xlsx"]`
    while history also stamped an obsolete `output_format="pptx"` on the
    same schemas; the singular field is being removed, but the
    resolver tolerates both shapes so old branches and partial migrations
    still resolve. We try each declared format in order and return the
    first that has a file on disk — graceful degradation across
    DISABLE_XLSX rollouts and legacy_formats back-compat.
    """
    from utils.exporter import artifact_filename_base
    ptype = schema_state.get("product_type") or ""
    sd = schema_state.get("structured_data") or {}
    schema = PRODUCT_SCHEMAS.get(ptype) or {}

    # Build the ordered format list. Plural `output_formats` is the
    # canonical declaration (per the multi-format render path at
    # product.py ~line 540); singular `output_format` is the legacy
    # field. If only the legacy field is present we promote it; if
    # both are present plural wins. legacy_formats appended last so a
    # DISABLE_XLSX rollback or a hand-edited xlsx-missing dir still
    # resolves to the historical pptx/docx.
    plural = schema.get("output_formats")
    if isinstance(plural, list) and plural:
        formats = [f for f in plural if isinstance(f, str) and f]
    else:
        singular = schema.get("output_format")
        formats = [singular] if isinstance(singular, str) and singular else []
    for f in (schema.get("legacy_formats") or []):
        if isinstance(f, str) and f and f not in formats:
            formats.append(f)
    if not formats:
        formats = ["docx"]

    ext_for = {"docx": ".docx", "pptx": ".pptx", "xlsx": ".xlsx"}
    fb = artifact_filename_base(ptype, schema, sd, artifact_id)
    aid_dir = _autodraft_dir_for(artifact_id)

    # Try each format in declared order; return the first that resolves.
    for fmt in formats:
        ext = ext_for.get((fmt or "").lower())
        if not ext:
            continue
        cand = aid_dir / f"{fb}{ext}"
        if cand.exists():
            return cand
        # Fallback: any file with matching extension in the artifact dir.
        for p in aid_dir.glob(f"*{ext}"):
            return p
    return None


def _compute_immediate_downstreams(product_type: str,
                                    finalize_mtime: float) -> list[dict]:
    """Walk every schema's depends_on for `artifact.<product_type>` membership.

    Returns one entry per downstream product type:
      {product_type, in_pool_artifact_ids: [...], staleness: "fresh" | "stale"}

    A downstream pool entry is "stale" when its generation precedes
    finalize_mtime. Pool entries are read from uploads/_autodraft/.
    """
    downstreams: dict[str, dict] = {}
    target_dep = f"artifact.{product_type}"
    for cand_type, cand_schema in PRODUCT_SCHEMAS.items():
        if cand_type == product_type:
            continue
        deps_found = False
        # Walk header_fields + paragraph_fields + row_fields depends_on
        for hf in (cand_schema.get("header_fields") or {}).values():
            if target_dep in ((hf or {}).get("depends_on") or []):
                deps_found = True
                break
        if not deps_found:
            for pf in (cand_schema.get("paragraph_fields") or []):
                if target_dep in ((pf or {}).get("depends_on") or []):
                    deps_found = True
                    break
        if not deps_found:
            for rf in (cand_schema.get("row_fields") or {}).values():
                if target_dep in ((rf or {}).get("depends_on") or []):
                    deps_found = True
                    break
        if not deps_found and target_dep[len("artifact."):] in (cand_schema.get("consumes_artifacts") or []):
            deps_found = True
        if not deps_found:
            continue
        # Scan autodraft pool for in-pool entries of this downstream type.
        in_pool_ids = []
        pool_root = UPLOAD_DIR / "_autodraft"
        # Pool layout: uploads/_autodraft/<artifact_id>/schema_state.json
        # The product type is recorded in schema_state.product_type.
        try:
            for aid_dir in pool_root.iterdir():
                if not aid_dir.is_dir():
                    continue
                ss = aid_dir / "schema_state.json"
                if not ss.exists():
                    continue
                try:
                    ss_data = json.loads(ss.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if (ss_data.get("product_type") or "") != cand_type:
                    continue
                in_pool_ids.append({
                    "artifact_id": aid_dir.name,
                    "generated_at": ss_data.get("generated_at") or "",
                    "mtime": ss.stat().st_mtime,
                })
        except FileNotFoundError:
            pass
        staleness = "fresh"
        if in_pool_ids:
            oldest_mtime = min(e["mtime"] for e in in_pool_ids)
            if oldest_mtime < finalize_mtime:
                staleness = "stale"
        downstreams[cand_type] = {
            "product_type": cand_type,
            "in_pool_artifact_ids": [e["artifact_id"] for e in in_pool_ids],
            "staleness": staleness,
        }
    return list(downstreams.values())


def _audit_log_append(artifact_id: str, entry: dict) -> None:
    """Append one JSON line to the per-artifact audit log."""
    try:
        hist_dir = _autodraft_dir_for(artifact_id) / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        log_path = hist_dir / "audit.jsonl"
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception as e:
        logger.warning("audit log append failed for %s: %s", artifact_id, e)


def _capture_edit_signal(artifact_id, product_type, draft_sd, final_sd,
                         approved_by=None, session_id=None) -> None:
    """Core analyst-data-collection signal: diff the agent's DRAFT
    structured_data against the analyst's FINAL (post-edit) structured_data,
    field by field, at finalize. Logs per-field edit magnitude + the
    before/after pair into analyst_event — feeding BOTH the analytics
    fix-list (which fields/products the agent gets wrong) and the
    model-training corpus (draft->final pairs). Best-effort: never raises
    into finalize."""
    import difflib
    try:
        from utils import events as _events
        draft = draft_sd if isinstance(draft_sd, dict) else {}
        final = final_sd if isinstance(final_sd, dict) else {}

        def _norm(v):
            if v is None:
                return ""
            return v if isinstance(v, str) else json.dumps(v, default=str, sort_keys=True)

        all_fields = set(draft) | set(final)
        changed = []
        total_dist = 0
        for key in sorted(all_fields):
            ds = _norm(draft.get(key))
            fs = _norm(final.get(key))
            if ds == fs:
                continue
            sm = difflib.SequenceMatcher(None, ds, fs, autojunk=False)
            dist = sum(max(i2 - i1, j2 - j1)
                       for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
            total_dist += dist
            changed.append(key)
            _events.log_analyst_event(
                user_id=approved_by or None,
                event_type="edit.field",
                surface="editor",
                product_type=product_type,
                session_id=session_id,
                artifact_id=artifact_id,
                value_num=dist,
                detail={
                    "field": key,
                    "draft_chars": len(ds),
                    "final_chars": len(fs),
                    "draft_value": ds,     # agent output (training pair: before)
                    "final_value": fs,     # analyst correction (training pair: after)
                },
            )
        _events.log_analyst_event(
            user_id=approved_by or None,
            event_type="edit.finalized",
            surface="editor",
            product_type=product_type,
            session_id=session_id,
            artifact_id=artifact_id,
            value_num=len(changed),
            detail={
                "fields_changed": changed,
                "field_count": len(all_fields),
                "total_edit_distance": total_dist,
                "approved_by": approved_by or None,
            },
        )
    except Exception:
        logger.warning("analyst edit-signal capture failed for %s",
                       artifact_id, exc_info=True)


@router.post("/api/artifacts/{artifact_id}/finalize")
async def finalize_artifact(
    artifact_id: str, user_id: str = "", approved_by: str = "",
) -> dict:
    """Walk markers in the on-disk deliverable, rebuild structured_data,
    overwrite schema_state.json, flip the artifact's approval status to
    'published' in the DB, and return the list of immediate downstream
    consumers.

    `user_id` / `approved_by` (optional) stamp the approver on the published
    header so "Approved by …" survives a reload; they default to empty for
    programmatic callers.

    Stateless: no lifecycle state machine. The deliverable bytes are
    never touched. A botched importer cannot corrupt analyst work — the
    worst case is a 422 (validation failed) leaving the prior
    schema_state.json intact.
    """
    import fcntl
    import time as _time

    aid_dir = _autodraft_dir_for(artifact_id)
    ss_path = aid_dir / "schema_state.json"
    if not ss_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"artifact {artifact_id} has no schema_state.json under uploads/_autodraft/",
        )
    try:
        ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read schema_state: {e}")
    product_type = ss_data.get("product_type") or ""
    schema = PRODUCT_SCHEMAS.get(product_type) or {}
    if not product_type or not schema:
        raise HTTPException(
            status_code=501,
            detail=f"no importer registered for product_type={product_type!r}",
        )

    deliverable = _deliverable_path_for(artifact_id, ss_data)
    if deliverable is None:
        raise HTTPException(
            status_code=404,
            detail=f"deliverable file not found under {aid_dir}",
        )

    # Per-artifact lock.
    aid_dir.mkdir(parents=True, exist_ok=True)
    lock_path = aid_dir / ".finalize.lock"
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise HTTPException(
                status_code=409,
                detail="finalize already in progress for this artifact",
            )

        # Snapshot prior schema_state.json.
        hist_dir = aid_dir / "history"
        hist_dir.mkdir(parents=True, exist_ok=True)
        snap_path = hist_dir / f"schema_state.{int(_time.time())}.json"
        try:
            snap_path.write_bytes(ss_path.read_bytes())
        except Exception as e:
            logger.warning("schema_state snapshot failed: %s", e)

        # Invoke the importer.
        try:
            from utils.importer import import_artifact
        except ImportError as e:
            raise HTTPException(
                status_code=500,
                detail=f"importer module unavailable: {e}",
            )
        try:
            result = import_artifact(
                artifact_id, product_type,
                on_disk_path=str(deliverable),
            )
        except Exception as e:
            _audit_log_append(artifact_id, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "IMPORTER_FAILED",
                "artifact_id": artifact_id,
                "product_type": product_type,
                "error": str(e),
            })
            raise HTTPException(status_code=500, detail=f"importer crashed: {e}")

        new_sd = result.get("structured_data") or {}
        warnings = result.get("warnings") or []

        # Core analyst-data-collection signal: how the analyst changed the
        # agent's draft, field by field. ss_data is the prior schema_state
        # (the agent's draft on first finalize); new_sd is the analyst's
        # final. Best-effort — never affects the finalize result.
        _capture_edit_signal(
            artifact_id, product_type,
            ss_data.get("structured_data") or {},
            new_sd,
            approved_by=(approved_by or user_id) or None,
            session_id=(ss_data.get("autodraft_session_id") or ss_data.get("session_id")),
        )

        # Validate — fatal warnings short-circuit to 422 BEFORE we
        # overwrite schema_state.json. Fatal means "the importer could
        # not actually read the deliverable, so its new_sd is {} or near-
        # empty; finalizing with that would silently wipe the analyst's
        # prior structured_data":
        #
        #   - `parse_failure` (OOXML/XLSX/PPTX parser threw, e.g. SDT
        #     spans a <w:tc> boundary, malformed zip, unreadable sheet),
        #   - `artifact_not_found` (deliverable missing on disk after
        #     the resolver claimed it exists — race or rename),
        #   - `unsupported_format` (resolver picked a file with an
        #     extension the importer doesn't handle).
        #
        # `missing_required_*` is NOT fatal. A schema field marked
        # `required: True` is guidance for the writer + the analyst —
        # the importer surfaces it as a warning, but does NOT block
        # finalize. Analysts routinely save partial work (draft→final on
        # a deliverable that hasn't been fully filled in yet) and the
        # required tag is the wrong place to gate persistence. Warnings
        # are still returned in the response so the UI can flag them
        # next to the field, but the structured_data is written either
        # way.
        _FATAL_PREFIXES: tuple[str, ...] = ()
        _FATAL_CODES = ("parse_failure", "artifact_not_found", "unsupported_format")
        fatal_warnings = [
            w for w in warnings
            if (w[0] or "").startswith(_FATAL_PREFIXES) or (w[0] or "") in _FATAL_CODES
        ]
        if fatal_warnings:
            _audit_log_append(artifact_id, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "event": "VALIDATION_FAILED",
                "artifact_id": artifact_id,
                "product_type": product_type,
                "warnings": warnings,
            })
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "imported structured_data fails schema validation",
                    "warnings": warnings,
                },
            )

        # Compute schema_state_diff between old and new structured_data.
        old_sd = ss_data.get("structured_data") or {}
        def _flat_keys(d, prefix=""):
            if isinstance(d, dict):
                for k, v in d.items():
                    yield from _flat_keys(v, prefix + ("." if prefix else "") + str(k))
            elif isinstance(d, list):
                for i, v in enumerate(d):
                    yield from _flat_keys(v, prefix + f"[{i}]")
            else:
                yield (prefix, d)
        old_flat = dict(_flat_keys(old_sd))
        new_flat = dict(_flat_keys(new_sd))
        fields_added = sorted(set(new_flat) - set(old_flat))
        fields_removed = sorted(set(old_flat) - set(new_flat))
        fields_modified = []
        for k in sorted(set(old_flat) & set(new_flat)):
            if old_flat[k] != new_flat[k]:
                _o = str(old_flat[k])[:500]
                _n = str(new_flat[k])[:500]
                fields_modified.append({"path": k, "before": _o, "after": _n})

        # Atomically overwrite schema_state.json.
        ss_data["structured_data"] = new_sd
        ss_data["finalized_at"] = datetime.now(timezone.utc).isoformat()
        tmp_path = ss_path.with_suffix(".json.tmp")
        try:
            tmp_path.write_text(
                json.dumps(ss_data, default=str, indent=2),
                encoding="utf-8",
            )
            os.replace(str(tmp_path), str(ss_path))
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"schema_state.json overwrite failed: {e}",
            )

        finalize_mtime = ss_path.stat().st_mtime

        # Persist the approval flip to the DB. The editor's Draft → Final
        # toggle drives this endpoint, but the dashboard reads each row's
        # DRAFT/FINAL badge from the SessionDetail header
        # (product_records._artifact_header_lookup -> header.status). Unless we
        # write header.status="published" here, the "published" state lives
        # only in the editor's local React state and the row reverts to DRAFT
        # the moment the analyst navigates back. Best-effort: a status-write
        # miss must never fail the finalize itself.
        try:
            from utils import database
            from handlers import intel_agent as _ia
            approver = (
                {"id": user_id, "name": approved_by}
                if (user_id or approved_by)
                else None
            )
            _db = database.SessionLocal()
            try:
                pub = _ia.publish_artifact_in_db(_db, artifact_id, approver=approver)
            finally:
                _db.close()
            if not pub.get("ok"):
                logger.warning(
                    "finalize: could not persist published status for %s: %s",
                    artifact_id, pub.get("error"),
                )
        except Exception:
            logger.exception(
                "finalize: publish_artifact_in_db raised for %s", artifact_id,
            )

        # jobs-unification-005 wire-up: drop the artifact header cache so
        # the next /api/products/records read sees the freshly imported
        # structured_data (header.status, approved_by, approved_at).
        # Without this, the dashboard row would keep rendering the
        # pre-finalize cached header until process restart.
        try:
            from handlers import product_records as _pr
            _pr.invalidate_artifact_header_cache(artifact_id)
        except Exception:
            log.exception(
                "invalidate_artifact_header_cache failed post-finalize "
                "artifact_id=%s", artifact_id,
            )
        immediate_downstreams = _compute_immediate_downstreams(
            product_type, finalize_mtime,
        )

        _audit_log_append(artifact_id, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "FINALIZE",
            "artifact_id": artifact_id,
            "product_type": product_type,
            "deliverable_mtime": deliverable.stat().st_mtime,
            "deliverable_size": deliverable.stat().st_size,
            "schema_state_diff": {
                "fields_added": fields_added,
                "fields_removed": fields_removed,
                "fields_modified": fields_modified,
            },
            "warnings": warnings,
        })

        return {
            "artifact_id": artifact_id,
            "product_type": product_type,
            "mtime": finalize_mtime,
            "structured_data_summary": {
                "fields_added": fields_added,
                "fields_removed": fields_removed,
                "fields_modified_count": len(fields_modified),
            },
            "warnings": [{"code": w[0], "field_path": w[1], "detail": w[2]}
                         for w in warnings],
            "immediate_downstreams": immediate_downstreams,
        }
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except Exception:
            pass
        os.close(lock_fd)


@router.post("/api/artifacts/{artifact_id}/refire-immediate-downstream")
async def refire_immediate_downstream(artifact_id: str, body: RefireBody) -> dict:
    """Fire each listed downstream product once against the updated
    upstream. ONE HOP only — no transitive cascade.

    DISPATCH-ONLY: each refire is scheduled as a background task and this
    endpoint returns immediately with the list of dispatched product types.
    The actual DAG generation (100–150s each) runs async via
    register_and_run, which broadcasts running/complete/failed to the
    dashboard over WebSocket — so the analyst follows progress there instead
    of the request blocking for minutes. `errors` carries only immediate
    dispatch failures (e.g. unknown product_type); generation failures
    surface on the dashboard as failed rows.
    """
    if not body.product_types:
        return {"fired": [], "errors": []}

    aid_dir = _autodraft_dir_for(artifact_id)
    ss_path = aid_dir / "schema_state.json"
    if not ss_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"upstream artifact {artifact_id} has no schema_state.json",
        )
    try:
        ss_data = json.loads(ss_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to read schema_state: {e}")
    upstream_product_type = ss_data.get("product_type") or ""

    # Dispatch each listed product via the existing autodraft refire
    # mechanism (refire_one_product_type fires one fresh artifact for `pt`
    # against the just-finalized upstream). We schedule it as a background
    # task and DO NOT await the generation — register_and_run inside it
    # tracks the DAG task and broadcasts progress to the dashboard.
    dispatched: list[str] = []
    errors = []
    try:
        from handlers import product_autodraft
    except ImportError as e:
        raise HTTPException(
            status_code=500,
            detail=f"product_autodraft module unavailable: {e}",
        )

    for pt in body.product_types:
        if pt not in PRODUCT_SCHEMAS:
            errors.append({"product_type": pt, "error": "unknown product_type"})
            continue
        task = asyncio.create_task(
            product_autodraft.refire_one_product_type(pt, mode="full")
        )
        _REFIRE_BG_TASKS.add(task)
        task.add_done_callback(_REFIRE_BG_TASKS.discard)
        dispatched.append(pt)

    _audit_log_append(artifact_id, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": "REFIRE_IMMEDIATE_DOWNSTREAM",
        "artifact_id": artifact_id,
        "product_type": upstream_product_type,
        "refire_targets": body.product_types,
        "dispatched": dispatched,
        "errors": errors,
    })

    return {"upstream_artifact_id": artifact_id, "dispatched": dispatched, "errors": errors}


def get_product_config(product_name: str) -> dict:
    """Derive the runner-side config from a schema entry."""
    s = PRODUCT_SCHEMAS.get(product_name)
    if not s:
        return {}
    _output_format = s.get("output_format") or "docx"
    cfg = {
        "name": s.get("name", product_name),
        "shape": s.get("shape", ""),
        "output_format": _output_format,
        "page_orientation": "landscape" if _output_format == "pptx" else "portrait",
        "consumes_artifacts": s.get("consumes_artifacts", []),
        "row_required": [],
        "row_defaults": {},
        "row_analytical": [],
        "row_published": [],
        "row_workpaper": [],
        "row_provenance_required": [],
        "header_required": [],
        "header_analytical": [],
        "header_defaults": {},
        "header_published": [],
        "body_paragraphs": [],
        # Mirrors the `header_required` / `row_required` lists for
        # paragraph_fields. Two schema agents had to work around the missing
        # required-flag honor (TMP promoted `parent_etf_eid` to a header,
        # SITREP added prose to silence `line_17_narrative` gaps). The runner
        # now uses `body_required` when deciding whether to flag an empty
        # paragraph as a gap (see intel_agent._merge_pw_emit).
        "body_required": [],
        "row_source": s.get("row_source"),
        "row_factors": s.get("row_factors", []),
        "group_by": s.get("group_by"),
        "order_by": s.get("order_by"),
        "published_section_title": s.get("published_section_title", ""),
        "matrix_spec": s.get("matrix_spec"),
    }
    for f, spec in s.get("header_fields", {}).items():
        if spec.get("required"):
            cfg["header_required"].append(f)
        if spec.get("source") == "analytical":
            cfg["header_analytical"].append(f)
        if "default" in spec:
            cfg["header_defaults"][f] = spec["default"]
        cfg["header_published"].append(f)
    for f, spec in (s.get("row_fields") or {}).items():
        src = spec.get("source", "")
        if spec.get("required") and src != "kg_id":
            cfg["row_required"].append(f)
        if src == "analytical":
            cfg["row_analytical"].append(f)
            if "default" in spec:
                cfg["row_defaults"][f] = spec["default"]
        v = field_view(spec)
        if v == "published":
            cfg["row_published"].append(f)
        elif v == "workpaper":
            cfg["row_workpaper"].append(f)
        if field_provenance_required(spec):
            cfg["row_provenance_required"].append(f)
    for p in (s.get("paragraph_fields") or []):
        cfg["body_paragraphs"].append(p["name"])
        if p.get("required", True):
            cfg["body_required"].append(p["name"])
    return cfg


def field_view(spec: dict) -> str:
    """Resolve the `view` tag — defaults to 'published'."""
    return spec.get("view", "published")


def field_provenance_required(spec: dict) -> bool:
    return bool(spec.get("provenance_required", False))


# ============================================================================
# Uploaded-template flow (existing)
# ============================================================================

def generate_template(product_dict):
    """Convert an uploaded PDF template to structured HTML and cache the body."""
    pdf_path = product_dict.get('template_path', '')
    if pdf_path.endswith('.html'):
        pdf_path = pdf_path.replace('.html', '').replace('_body', '')

    if not os.path.exists(pdf_path):
        logger.warning(f"generate_template: PDF not found at {pdf_path}, skipping")
        return

    try:
        from handlers.docgen import _pdf_to_docx_html
        html_body, layout = _pdf_to_docx_html(pdf_path)
        if not html_body:
            logger.warning(f"generate_template: _pdf_to_docx_html returned empty for {pdf_path}")
            return

        with open(product_dict['template_body_path'], "w", encoding="utf-8") as f:
            f.write(html_body)

        logger.info(f"generate_template: saved {len(html_body)} chars to {product_dict['template_body_path']}")

    except Exception as e:
        logger.error(f"generate_template: failed for {pdf_path}: {e}")


# ============================================================================
# Placeholder rendering — schema → strings the prompt template substitutes in
# ============================================================================

def _substitute(template: str, fields: dict) -> str:
    out = template
    for key, value in fields.items():
        out = out.replace("{" + key + "}", value)
    return out


def _product_writer_fields(product_name: str, s: dict) -> dict:
    doctrine = s.get("doctrine") or {}
    _output_format = s.get("output_format") or "docx"
    return {
        "PRODUCT_NAME": product_name,
        "PURPOSE": doctrine.get("purpose", "(none)"),
        "ORDERING_RULE": doctrine.get("ordering_rule", "(none)"),
        "SHAPE": s.get("shape", ""),
        "OUTPUT_FORMAT": _output_format,
        "PAGE_ORIENTATION": "landscape" if _output_format == "pptx" else "portrait",
        "CONSUMES_ARTIFACTS": ", ".join(s.get("consumes_artifacts", [])) or "(none)",
        "HEADER_SCHEMA": _render_header_schema(s),
        "BODY_SCHEMA": _render_body_schema(s),
        "SUBTLETIES": _render_bullets(s.get("subtleties")),
        "HARD_RULES": _render_bullets(s.get("hard_rules")),
        "POST_EMIT_SUMMARY": s.get("post_emit_summary", "artifact_id and a salient finding"),
    }


# ============================================================================
# Schema → string helpers
# ============================================================================

def _humanize(field: str) -> str:
    return " ".join(p.title() for p in field.replace("_", " ").split())


def _describe_source(spec: dict) -> str:
    src = spec.get("source", "")
    if src == "kg_id":
        return "(programmatic) entity ID"
    if src.startswith("kg:"):
        return f"(programmatic) from KG record field `{src[3:]}`"
    if src.startswith("header_alias:"):
        return f"(programmatic) mirrors header.{src.split(':', 1)[1]}"
    if src.startswith("ref:"):
        return f"(reference) {src[4:]} artifact_id"
    if src == "session":
        return "(programmatic) session header"
    if src == "constant":
        return "(programmatic) constant"
    if src == "analytical":
        return "(analytical — you fill)"
    return f"(source: {src})"


def _render_bullets(items) -> str:
    if not items:
        return "(none)"
    return "\n".join(f"- {x}" for x in items)


def _render_header_schema(s: dict) -> str:
    prog, ana = [], []
    for name, spec in s.get("header_fields", {}).items():
        line = f"- `{name}`: {_describe_source(spec)}"
        if spec.get("required"):
            line += " (required)"
        if spec.get("provenance_required"):
            line += " (provenance required)"
        if "default" in spec:
            line += f" (default {spec['default']!r})"
        if spec.get("guidance"):
            line += f" — {spec['guidance']}"
        (ana if spec.get("source") == "analytical" else prog).append(line)
    parts = []
    if prog:
        parts.append("**Programmatic (runner pre-fills):**\n" + "\n".join(prog))
    if ana:
        parts.append("**Analytical (fill via `header_analysis`):**\n" + "\n".join(ana))
    return "\n\n".join(parts) if parts else "(none)"


def _render_body_schema(s: dict) -> str:
    parts = []
    if s.get("row_fields"):
        parts.append(_render_row_schema(s))
    if s.get("paragraph_fields"):
        parts.append(_render_paragraph_schema(s))
    if s.get("matrix_spec"):
        parts.append(_render_matrix_schema(s))
    return "\n\n".join(parts) if parts else "(no body — header-only product)"


def _render_row_schema(s: dict) -> str:
    prog, ana = [], []
    for name, spec in s["row_fields"].items():
        v = field_view(spec)
        view_tag = f" [{v}]" if v != "published" else ""
        prov_tag = " (provenance required)" if field_provenance_required(spec) else ""
        req_tag = "" if spec.get("required") else " (optional)"
        default_tag = f" (default {spec['default']!r})" if "default" in spec else ""
        label_tag = f" rendered as '{spec['render_label']}'" if spec.get("render_label") else ""
        enum_tag = f" enum: {spec['enum']}" if spec.get("enum") else ""
        guidance = spec.get("guidance", "(analytical)") if spec.get("source") == "analytical" else _describe_source(spec)
        line = f"- `{name}`{view_tag}: {guidance}{label_tag}{enum_tag}{default_tag}{req_tag}{prov_tag}"
        (ana if spec.get("source") == "analytical" else prog).append(line)
    out = ["### Row schema"]
    if prog:
        out.append("**Programmatic (runner pre-fills from T1 entity by id):**\n" + "\n".join(prog))
    if ana:
        out.append("**Analytical (fill via `rows_analysis`, keyed by id):**\n" + "\n".join(ana))
    out.append(
        "**Field views:** `[workpaper]` fields are analyst working notes (audit appendix only). "
        "Untagged fields are published."
    )
    out.append(
        "**Provenance:** for fields tagged `(provenance required)`, include in your `rows_analysis` row "
        "a `_provenance` dict mapping field name → source citation."
    )
    return "\n\n".join(out)


def _render_paragraph_schema(s: dict) -> str:
    out = ["### Paragraph / structured schema (top-level fields in `structured_data`)"]
    for p in s["paragraph_fields"]:
        shape = p.get("shape", "string")
        v = p.get("view", "published")
        view_tag = f" [{v}]" if v != "published" else ""
        req = "" if p.get("required") else " (optional)"
        prov = " (provenance required)" if p.get("provenance_required") else ""
        label_tag = f" — '{p['render_label']}'" if p.get("render_label") else ""
        enum_tag = f" enum: {p['enum']}" if p.get("enum") else ""
        line = f"- `{p['name']}`{view_tag} ({shape}{req}{prov}{enum_tag}){label_tag}: {p.get('guidance', '')}"
        if p.get("subfields"):
            sf_specs = p.get("subfield_specs") or {}
            rendered = []
            for x in p["subfields"]:
                sf_spec = sf_specs.get(x) or {}
                if sf_spec.get("enum") is not None:
                    rendered.append(f"`{x}` (enum: {sf_spec['enum']})")
                else:
                    rendered.append(f"`{x}`")
            line += "\n  - subfields: " + ", ".join(rendered)
        if p.get("item_subfields"):
            line += "\n  - per-item subfields:"
            for child in p["item_subfields"]:
                line += "\n" + _render_subfield(child, 2)
        out.append(line)
    return "\n".join(out)


def _render_subfield(sf, depth: int = 0) -> str:
    if not isinstance(sf, dict):
        return f"{'  ' * depth}- `{sf}`"
    name = sf.get("name", "")
    shape = sf.get("shape")
    bits = []
    if shape:
        bits.append(shape)
    if sf.get("enum") is not None:
        bits.append(f"enum: {sf['enum']}")
    if sf.get("provenance_required"):
        bits.append("provenance required")
    if sf.get("required") is False:
        bits.append("optional")
    if sf.get("render_label"):
        bits.append(f"label: '{sf['render_label']}'")
    attrs = f" ({'; '.join(bits)})" if bits else ""
    guidance = sf.get("guidance", "")
    line = f"{'  ' * depth}- `{name}`{attrs}"
    if guidance:
        line += f" — {guidance}"
    nested = sf.get("item_subfields") or []
    if nested:
        line += f"\n{'  ' * depth}  per-item subfields:"
        for child in nested:
            line += "\n" + _render_subfield(child, depth + 2)
    if sf.get("subfields"):
        line += f"\n{'  ' * depth}  subfields: " + ", ".join(f"`{x}`" for x in sf["subfields"])
    return line


def _render_matrix_schema(s: dict) -> str:
    """Render the matrix-spec section of the schema brief.

    Some products (TSS/AGM/TSM — FM 3-60 Decide trio; Information Collection
    Matrix — ATP 2-01.3 Fig 6-15) have DYNAMIC rows: the row axis is derived
    at runtime from a consumed upstream artifact (HPTL HPTs, OPORD PIRs).
    Those schemas omit `matrix_spec.row_axis` and may carry a `row_axis_source`
    label naming the upstream. The brief surfaces 'dynamic' so the writer
    knows to derive rows from the consumed artifact rather than expecting a
    fixed enum.
    """
    ms = s["matrix_spec"]
    row_axis = ms.get("row_axis")
    if row_axis:
        rows_repr = repr(row_axis)
    else:
        src = ms.get("row_axis_source")
        rows_repr = f"dynamic (derived from {src})" if src else "dynamic (derived from consumed artifacts)"
    return (
        "### Matrix spec\n\n"
        f"Field: `{ms['field']}` — grid with rows={rows_repr}, cols={ms['col_axis']}"
    )


def _render_header_brief(s: dict) -> str:
    lines = []
    for name, spec in s.get("header_fields", {}).items():
        src = spec.get("source", "")
        label = spec.get("render_label") or _humanize(name)
        marker = ""
        if src == "session":
            marker = " — analyst-supplied or scope-inferred"
        elif src.startswith("ref:"):
            marker = f" — cross-reference to a {src[4:]} artifact"
        elif src == "constant":
            marker = f" — constant ({spec.get('default', '')!r})"
        elif src == "analytical":
            marker = " — writer-supplied (don't confirm with analyst)"
        req = " *(required)*" if spec.get("required") else ""
        lines.append(f"- `{name}`: {label}{marker}{req}")
    return "\n".join(lines) if lines else "(none)"


def _render_fixed_structure(s: dict) -> str:
    parts = []
    shape = s.get("shape")
    if shape == "row":
        parts.append("Row-shape product. The runner derives row entities from the intel agent's responses.")
        if s.get("row_factors"):
            parts.append(f"Doctrinal row set is fixed: `{s['row_factors']}`.")
        if s.get("row_source"):
            parts.append(f"Row derivation: `{s['row_source']}`.")
        if s.get("group_by"):
            parts.append(f"Rows grouped by `{s['group_by']}` in the published view.")
        if s.get("order_by"):
            ob = s["order_by"]
            parts.append(f"Rows ordered by `{ob.get('field')}` ({ob.get('direction', 'asc')}).")
        published_cols = [
            f for f, sp in (s.get("row_fields") or {}).items()
            if (sp or {}).get("view", "published") == "published"
        ]
        if published_cols:
            parts.append(f"Published columns: {', '.join('`' + c + '`' for c in published_cols)}.")
    elif shape == "lines":
        line_count = sum(
            1 for p in (s.get("paragraph_fields") or []) if p.get("name", "").startswith("line_")
        )
        parts.append(f"Lines-shape product. {line_count} numbered lines per FM 6-99.")
    elif shape == "matrix":
        ms = s.get("matrix_spec") or {}
        if ms:
            row_axis = ms.get("row_axis")
            col_axis = ms.get("col_axis") or []
            if row_axis:
                parts.append(
                    f"Matrix-shape product. Field `{ms.get('field')}` is a "
                    f"{len(row_axis)}×{len(col_axis)} grid."
                )
                parts.append(f"Row axis (fixed): `{row_axis}`.")
            else:
                # Dynamic-row matrices (FM 3-60 Decide trio TSS/AGM/TSM, ATP
                # 2-01.3 Information Collection Matrix). Row axis is derived
                # at runtime from a consumed artifact; the writer must pull
                # rows from the upstream identified by row_axis_source (or by
                # the schema's consumes_artifacts list).
                src = ms.get("row_axis_source") or "consumed upstream artifact"
                parts.append(
                    f"Matrix-shape product. Field `{ms.get('field')}` is a "
                    f"dynamic-row × {len(col_axis)}-col grid."
                )
                parts.append(
                    f"Row axis (dynamic): derived from {src}. "
                    f"Writer pulls rows from the consumed upstream artifact "
                    f"(see consumes_artifacts)."
                )
            parts.append(f"Col axis (fixed): `{col_axis}`.")

    if s.get("paragraph_fields"):
        parts.append("\nParagraph / structured fields (top-level analytical sections):")
        for p in s["paragraph_fields"]:
            name = p.get("name")
            label = p.get("render_label") or _humanize(name)
            psh = p.get("shape", "")
            req = " *(required)*" if p.get("required") else ""
            extra = ""
            if p.get("enum"):
                extra = f" enum: `{p['enum']}`"
            parts.append(f"- `{name}` ({psh}){req}: {label}{extra}")
            for sf in (p.get("item_subfields") or []):
                if isinstance(sf, dict) and sf.get("enum"):
                    parts.append(f"    - `{sf['name']}` (per-item) enum-fixed: `{sf['enum']}`")
    return "\n".join(parts) if parts else "(none)"
