"""Intel agent — three-agent runner for the 15 doctrinal intel products.

Agents:
- Operations expert  — analyst-facing orchestrator. Thinking enabled.
- Intel agent        — tool hands, gathers raw data. Thinking disabled.
- Product writer     — per-product specialist, loaded by artifact_type. Thinking enabled.

Each analyst message = one turn. Within a turn the operations expert may dispatch
the intel agent and/or the product writer; the runner handles the sub-loops and
the message threading. State persists to Session/SessionDetail rows; one
SessionDetail per turn carries the full per-agent trace for audit.

Public entry points:
- `async def main(data, manager, websocket)` — WebSocket handler (analyst dispatches).
- `def run_turn(question, session_id, project_id, user_id, db) -> dict` — synchronous
   in-process driver used by the testing harness against the same code path.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from utils import database, scenario
from utils import oob
from utils import llm as llm_module
from templates import PRODUCT_SCHEMAS, PRODUCTS
from handlers.product import prompt_for, get_product_config, schema_brief

log = logging.getLogger(__name__)

# ============================================================================
# PER-TURN INSTRUMENTATION — thread-local so concurrent run_turn invocations
# (one per executor worker thread) don't clobber each other's state.
#
# The WebSocket entry point dispatches each turn via loop.run_in_executor,
# which lands the turn on its own ThreadPoolExecutor thread. The cougaar
# tools are async-native (each one `await`s its cougaar adapter directly);
# the OE dispatcher in this module wraps async handlers with `asyncio.run`
# from the OE worker thread (no parent loop on that thread, so it's safe).
# The DAG executor (which IS the parent loop on the WS path) `await`s the
# same async tools without any wrapping. Thread-local storage stays per-turn
# either way.
#
# Two thread-local payloads:
#   - cougaar_call_summary : {tool_name -> {called,row_count,...}} captured at
#                            the end of every cougaar-backed tool invocation.
#                            Consumed by product.render_artifact_files() right
#                            after the writer's emit (same thread, same turn)
#                            and written into schema_state.json beside the
#                            artifact.
#   - stage_events         : ordered list of stage_event dicts emitted by the
#                            `stage(...)` context manager wrapping every
#                            pipeline boundary (tool fetch, LLM call, render,
#                            export, etc.). Also surfaced into schema_state
#                            and pushed live over the WebSocket via on_event.
#   - on_event             : callable supplied by the WS handler (or None for
#                            the harness path) used by `stage(...)` and the
#                            various dispatch points to push frames to the
#                            client. Stored on the thread-local so `stage`
#                            doesn't have to be threaded through every layer.
# ============================================================================
_TURN_LOCAL = threading.local()


def _ensure_turn_local() -> None:
    """Defensive init — first access on a fresh worker thread sees no attrs."""
    if not hasattr(_TURN_LOCAL, "cougaar_call_summary"):
        _TURN_LOCAL.cougaar_call_summary = {}
    if not hasattr(_TURN_LOCAL, "stage_events"):
        _TURN_LOCAL.stage_events = []
    if not hasattr(_TURN_LOCAL, "on_event"):
        _TURN_LOCAL.on_event = None
    if not hasattr(_TURN_LOCAL, "session_id"):
        _TURN_LOCAL.session_id = ""


def _reset_turn_local(session_id: str, on_event) -> None:
    """Called at the top of run_turn — zero out previous-turn residue on the
    same worker thread (the executor reuses threads across turns)."""
    _TURN_LOCAL.cougaar_call_summary = {}
    _TURN_LOCAL.stage_events = []
    _TURN_LOCAL.on_event = on_event
    _TURN_LOCAL.session_id = session_id or ""


def _record_cougaar_call(tool_name: str, *, called: bool, row_count: int,
                          supplemented_from_fixture: bool, elapsed_ms: int,
                          source: str, error: Optional[str] = None) -> None:
    _ensure_turn_local()
    _TURN_LOCAL.cougaar_call_summary[tool_name] = {
        "called": called,
        "row_count": row_count,
        "supplemented_from_fixture": supplemented_from_fixture,
        "elapsed_ms": elapsed_ms,
        "source": source,
        "error": error,
    }


def cougaar_call_summary_snapshot() -> dict:
    """Return a deep-ish copy of the current turn's per-tool call summary,
    safe to serialize into schema_state.json. Consumed by
    product.render_artifact_files immediately after the writer emit so the
    captured numbers reflect the cougaar fetch that fed the artifact."""
    _ensure_turn_local()
    return {k: dict(v) for k, v in _TURN_LOCAL.cougaar_call_summary.items()}


def stage_events_snapshot() -> list:
    """Return a copy of this turn's ordered stage_event list."""
    _ensure_turn_local()
    return list(_TURN_LOCAL.stage_events)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def stage(stage_name: str, **extra):
    """Per-stage latency emitter.

    Wraps any pipeline boundary (tool fetch, LLM dispatch, render, export)
    with a structured stage_event:

        {
          "type": "stage_event",
          "session_id": ...,
          "stage": stage_name,
          "started_at": ISO-8601 UTC,
          "completed_at": ISO-8601 UTC,
          "duration_ms": int,
          "extra": { ... call-site fields ... }
        }

    The event is (a) appended to the thread-local stage_events list so it
    ends up in schema_state.json, (b) pushed over on_event() if a WS
    handler registered one, and (c) logged at INFO so docker logs carry
    the breakdown even when no WS client is listening.

    Failures inside the wrapped block re-raise — but a stage_event is still
    emitted with `error` set and the (partial) duration so the analyst sees
    where the turn died.

    Extras supplied per call site (rows_returned, supplemented_from_fixture,
    artifact_type, model, tokens, etc.) flow through verbatim and are the
    primary diagnostic surface — keep them small and JSON-serialisable.
    """
    _ensure_turn_local()
    session_id = _TURN_LOCAL.session_id
    started_at = _now_iso()
    t0 = time.monotonic()
    err = None
    try:
        yield
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        raise
    finally:
        duration_ms = int((time.monotonic() - t0) * 1000)
        event = {
            "type": "stage_event",
            "session_id": session_id,
            "stage": stage_name,
            "started_at": started_at,
            "completed_at": _now_iso(),
            "duration_ms": duration_ms,
            "extra": {k: v for k, v in extra.items() if v is not None},
        }
        if err is not None:
            event["error"] = err
        _TURN_LOCAL.stage_events.append(event)
        log.info("[stage] %s duration_ms=%d session=%s extra=%s%s",
                 stage_name, duration_ms, session_id,
                 event["extra"], f" error={err}" if err else "")
        cb = _TURN_LOCAL.on_event
        if cb is not None:
            try:
                cb(event)
            except Exception:
                log.debug("stage_event on_event callback raised", exc_info=True)


# Default AO scope per XX Corps OPORD 26-01 "RESTORE BORDERS" (IDX 26 OPORD
# in scratch/docs/ibl/event_data/FIRST_DATA_DUMP/IDX Order.docx). The
# canonical OPORD label for the scenario AO is "AO BALTIC RESTORE" (the
# Valga / Estonia-Latvia border sector). Used by tool_get_oob and other
# AO-scoped lookups so the label has a single source of truth.
_DEFAULT_AO_LABEL = "AO BALTIC RESTORE"

# Master maps are PER PROJECT/EVENT: the event map's uuid IS the
# projectId (pytho-vite 2a606a2 is the canonical linkage — geo-agent
# auto-creates the map on first open, every project member lands on the
# same operating picture). Every push/read site resolves its map through
# _project_map_uuid(state.project_id or an explicitly threaded
# project_id) — never a deployment-wide env var (the retired
# PYTHO_MASTER_MAP_UUID pattern is exactly how the 2CR stack ended up
# reading IDX 26's map), never a per-user id, and never a legacy
# fallback map (pre-unification data MIGRATES onto its event's map).
# A blank project id at a resolution site is a BUG — the caller must
# thread the project — and raises rather than silently writing
# somewhere shared. Render paths are a different axis: each product
# instance has its OWN deterministic per-product map UUID (map_uuid_for
# below) so analyst edits on one product don't bleed into another.


def _project_map_uuid(project_id) -> str:
    """The master map FOR A PROJECT is the project id itself.

    project_id must already be a uuid (CreateProject mints them with
    crypto.randomUUID()). Blank / placeholder ids raise: there is no
    shared fallback map, only unthreaded callers to fix.
    """
    pid = str(project_id or "").strip()
    if not pid or pid.lower() in ("anon", "_autodraft", "default"):
        raise ValueError(
            f"master-map resolution requires a project id (got {project_id!r}) "
            "— thread the PROJECT through this caller"
        )
    return pid


def active_project_ids() -> list:
    """Every non-deleted project/event id — THE fan-out axis for
    context-free background work (the 30s OOB sync, the autodraft and
    signal-report sweeps): each project has its own master map and its
    own artifact pool, so background producers run once PER PROJECT.
    Reads the same sqlite rows the /api/projects surface serves. An
    empty list (no events created yet) means background producers have
    nothing to produce for — they no-op until an event exists."""
    try:
        from utils.database import SessionLocal, Project
        db = SessionLocal()
        try:
            rows = (
                db.query(Project.project_id)
                .filter((Project.is_deleted.is_(None))
                        | (Project.is_deleted == False))  # noqa: E712
                .all()
            )
            return [r[0] for r in rows if r[0]]
        finally:
            db.close()
    except Exception:
        log.exception("active_project_ids: projects query failed")
        return []

# The former import-time aliases (scenario.KG = scenario.KG etc.) are GONE:
# scenario data is per project/event, resolved lazily through the
# utils.scenario proxy against the ACTIVE project (utils.project_context)
# at each read. An import-time snapshot would bake one event's data into
# every event's products.

# ============================================================================
# DOCTRINAL CYCLE — what product comes after this one per FM/ATP/JP
# ============================================================================
# Operations expert reads this to surface a doctrinal-next recommendation in
# its receipt content message after a successful product-writer dispatch.
DOCTRINAL_NEXT = {
    # ATP 2-01.3 Ch. 3 — Step 1 (Operational Environment Definition) is the
    # FIRST product in any IPOE analytical chain. MCOO is the doctrinal-next
    # default; analyst can pivot to CCA, WEM, or Threat Overlay as scenario
    # demands.
    "Operational Environment Definition": "MCOO",
    "HVTL":     "CARVER",     # FM 3-60 Aug 2023 — HVTL → CARVER (target value analysis)
    "CARVER":   "HPTL",       # FM 3-60 Aug 2023 — CARVER ranking feeds HPTL nomination
    # FM 3-60 (Aug 2023) Decide-phase targeting trio per App. D Tables D-2 –
    # D-4. HPTL → TSS → AGM → TSM is the working-group deliverable set.
    "HPTL":     "TSS",
    "TSS":      "AGM",
    "AGM":      "TSM",
    "TSM":      "TMP",
    # FM 3-60 Ch. 2 ASSESS — BDA → CA. MEA findings and Reattack Recommendation
    # are CA sections per FM 3-60 para 2-70 (CA elements = BDA + MEA + RR),
    # not separate products. CA closes back to HPTL for re-prioritization.
    "BDA":      "CA",
    "INTSUM":   "GRINTSUM",
    "WEM":      "MCOO",       # WEM mobility modifier feeds MCOO
    "TEM":      "MCOO",
    "Threat Overlay": "Threat Model",  # Step-2 overlay → Step-3 doctrinal threat description
    # ATP 2-01.3 Step-3 family (paras 5-45 – 5-71). Threat Model contains the
    # Threat Template as a sub-section per para 5-45 (three Threat Model
    # activities: graphic + tactics + HVT-ID); not a separate product.
    "Threat Model":                "Threat Capability Statement",
    "Threat Capability Statement": "SITEMP",
    # ATP 2-01.3 Step-4 IC trio (paras 6-64 – 6-72; Figs 6-11 – 6-15).
    "Event Template":                "Decision Support Template",
    "Decision Support Template":     "Information Collection Matrix",
    # Red (threat-perspective) DST — independent Step-4 analytical product
    # built off the same Threat COA Statement / SITEMP / Event Template
    # upstream as the blue DST. Terminal in the doctrinal-next chain.
    "Threat Decision Support Template": None,
    "Information Collection Matrix": None,
    "Threat COA Statement":     "INTSUM",     # Threat COA Statement feeds INTSUM Para 4
    "MCOO":     "Threat COA Statement",
    "SITEMP":   "Threat COA Statement",
    "TMP":      "BDA",        # TMP produces pre-strike JDPI → BDA references it as miss-distance baseline
    "GRINTSUM": "SITREP",
    "SITREP":   None,
    "CCA":      None,
    # CA closes the Assess sub-loop back to HPTL (re-prio targets).
    "CA":  "HPTL",
    # TSA ARCHIVED 2026-06-08 — theater/J-2 product, not corps-authored.
    # See templates/products/_archived/README.md. The TSA→HVTL chain
    # entry was retired with the schema.
    # ATP 2-33.4 App. E intel estimates.
    "Intelligence Estimate":         "INTSUM",
    "Intelligence Running Estimate": None,
    # JP 3-60 + CJCSI 3122.06 / 3160.01D legal/ROE pre-strike gates.
    "NSL": None,
    "RTL": None,
    "CDE": None,
    # Tier 1 single-INT analytical products. The doctrinal sensor-message
    # atomics (TACELINT, IIR (GEOINT), HUMINT IIR, RECCEXREP, INTREP) are
    # USMTF / IIR records already in the Cougaar scenario.KG and consumed by Tier 1
    # rollups + Tier 2 fusion directly via get_recent_intel — they are not
    # analyst-authored products in this catalog. Doctrinal next chains for
    # the products we DO publish:
    #   SIGINT side: KLIEGLIGHT → SITREP (perishable, event-driven), SIGSUM → INTSUM
    #   GEOINT side: CCM → MCOO, LOS → MCOO
    #   HUMINT side: HUMINTSUM → INTSUM
    #   OSINT side:  OSIR → INTSUM
    "SIGSUM":              "INTSUM",
    "KLIEGLIGHT":          "SITREP",
    "CCM":                 "MCOO",
    "LOS":                 "MCOO",
    "HUMINTSUM":           "INTSUM",
    "OSIR":                "INTSUM",
    # Tier 2 fused additions surfaced by FINAL_DOCTRINE_AUDIT.
    # ICSM closes the Step-4 IC trio; DSM is the DST's tabulated companion.
    # TIF/ETF is intentionally not a product — see templates/__init__.py.
    "ICSM":                None,
    "DSM":                 None,
}

SUMMARIZER_PROMPT = """Summarize the conversation log that produced an intel artifact.
Output 2-4 sentences in natural prose: analyst request, emitted artifact (id/type/dtg),
salient inputs consulted, doctrinal notes. No bullets, no JSON."""


# ============================================================================
# STATE — in-memory, frozen dataclass; turn-deltas tracked separately
# ============================================================================

@dataclass(frozen=True)
class State:
    session_id: str
    operations_expert_messages: tuple
    intel_agent_messages: tuple
    product_writer_messages: tuple
    artifact_registry: tuple
    seen_ids: frozenset
    iteration: int
    product_writer_skeleton_json: str = ""
    product_writer_last_emit_json: str = ""  # cached structured_data from the most recent successful emit_artifact preview
    active_product_type: str = ""
    # Owner of artifacts emitted during this turn. Threaded in from run_turn so
    # _build_skeleton can stamp header.owner_user_id and _project_docs_index can
    # filter draft artifacts to the current analyst (Req 3 — drafts vs published
    # doc namespace). Defaults to "" so legacy callers / load_state don't break.
    user_id: str = ""
    # PROJECT/EVENT this turn serves — THE scoping axis for everything
    # analyst-facing: the event master map's uuid IS this id
    # (_project_map_uuid), artifact pools are per project, and pushes
    # target the project's map. Stamped by load_state from its
    # project_id argument; "" resolves to the legacy master map with a
    # loud log until every caller threads the project.
    project_id: str = ""
    # Pre-minted artifact id for the current turn. Minted at turn start so the
    # value is available BEFORE any tool call — tool_render_map writes its PNG
    # to uploads/<user_id>/<pending_artifact_id>/map.png, and the writer's emit
    # adopts it instead of minting fresh. Format is <PRODUCT>_<UTC_TS>; see
    # _next_artifact_id. Empty string for legacy / non-manual flows.
    pending_artifact_id: str = ""
    # Per-COA SITEMP fan-out binding (intel_agent.product_autodraft.
    # _autodraft_sitemp_fanout). When set carries {coa_id, coa_label, rank,
    # most_dangerous, threat_coa_statement_artifact_id} for the COA this SITEMP is being
    # built against. The DAG header_field resolver reads it and stamps
    # header_analysis.coa_id / coa_label / most_dangerous / threat_coa_statement_artifact_id
    # WITHOUT going through the writer LLM — these are mechanical
    # transforms (the runner already knows the values from the fan-out
    # dispatch). Empty dict for non-SITEMP-fan-out flows.
    coa_focus: dict = field(default_factory=dict)

    @staticmethod
    def fresh(session_id: str, user_id: str = "",
              manual_product_type: str = "",
              coa_focus: dict | None = None,
              project_id: str = "") -> "State":
        """Build a fresh State. `manual_product_type` switches the operations
        expert into manual-dispatch mode: loads `operations_expert_manual.md`
        as the system prompt and pre-sets `active_product_type` so OE has no
        `set_product_type` step and no scope-confirmation step. Used by the
        Generate-button path (handlers/product.py:generate)."""
        if manual_product_type:
            operations_expert_role = "operations_expert_manual"
            active = manual_product_type
        else:
            operations_expert_role = "operations_expert"
            active = ""
        # Manual-dispatch: mint the artifact_id NOW so tool_render_map can
        # write its PNG to uploads/<user_id>/<artifact_id>/map.png in this same
        # turn (the writer emit later adopts the pre-minted id). Non-manual
        # flows defer minting — the writer's emit calls _next_artifact_id when
        # it actually produces an artifact.
        pending_aid = _next_artifact_id(active) if manual_product_type else ""
        return State(
            session_id=session_id,
            operations_expert_messages=({"role": "system", "content": prompt_for(operations_expert_role)},),
            intel_agent_messages=(),
            product_writer_messages=(),
            artifact_registry=(),
            seen_ids=frozenset(),
            iteration=0,
            product_writer_skeleton_json="",
            product_writer_last_emit_json="",
            active_product_type=active,
            user_id=user_id,
            project_id=str(project_id or ""),
            pending_artifact_id=pending_aid,
            coa_focus=dict(coa_focus or {}),
        )

    def append_operations_expert(self, msg):
        return replace(self, operations_expert_messages=self.operations_expert_messages + (msg,))

    def append_intel_agent(self, msg):
        return replace(self, intel_agent_messages=self.intel_agent_messages + (msg,))

    def append_product_writer(self, msg):
        return replace(self, product_writer_messages=self.product_writer_messages + (msg,))

    def reset_intel_agent(self):
        return replace(self, intel_agent_messages=())

    def reset_product_writer(self):
        return replace(self, product_writer_messages=())

    def with_artifact(self, artifact_type, entry):
        existing = [v for k, v in self.artifact_registry if k == artifact_type]
        existing_list = existing[0] if existing else ()
        new_list = existing_list + (entry,)
        new_reg = tuple([(k, v) for k, v in self.artifact_registry if k != artifact_type]) + (
            (artifact_type, new_list),
        )
        return replace(self, artifact_registry=new_reg)

    def add_seen(self, ids):
        return replace(self, seen_ids=self.seen_ids | frozenset(ids))

    def registry_dict(self):
        return dict(self.artifact_registry)

    def publish_artifact(self, artifact_id):
        """Flip header.status from 'draft' to 'published' for the artifact with
        matching artifact_id. Returns (new_state, found_atype_or_None). The
        registry is a frozen tuple of (atype, tuple-of-entries) tuples; we
        rebuild the whole structure with the matched entry's structured_data
        header mutated. No-op if the artifact isn't in the registry.
        """
        new_reg = []
        found_atype = None
        for atype, entries in self.artifact_registry:
            new_entries = []
            for entry in entries:
                if entry.get("artifact_id") == artifact_id:
                    found_atype = atype
                    sd = dict(entry.get("structured_data") or {})
                    header = dict(sd.get("header") or {})
                    header["status"] = "published"
                    sd["header"] = header
                    new_entry = dict(entry)
                    new_entry["structured_data"] = sd
                    new_entries.append(new_entry)
                else:
                    new_entries.append(entry)
            new_reg.append((atype, tuple(new_entries)))
        return replace(self, artifact_registry=tuple(new_reg)), found_atype


# ============================================================================
# DB-BACKED STATE LOAD / SAVE
# ============================================================================

def _decode(text: Optional[str]) -> dict:
    if not text:
        return {}
    try:
        return json.loads(text)
    except Exception:
        return {}


def load_state(session_id: str, db, user_id: str = "", project_id: str = "",
                manual_product_type: str = "") -> State:
    """Reconstruct in-memory State from this session's SessionDetail rows.

    The operations expert's full conversation history is reassembled by walking
    every prior turn's `agent_trace.operations_expert_messages` and the analyst's
    `question` from each row. seen_ids and artifact_registry are derived from
    the cumulative trace.

    `user_id` is the analyst running THIS turn — stamped onto draft artifacts
    via _build_skeleton and used by _project_docs_index to filter other
    analysts' drafts out of the visible catalog.

    `project_id` enables cross-session visibility for the projectdocs
    namespace (Req 3): published artifacts emitted in sibling sessions of the
    same project are merged into artifact_registry so this session's analyst
    sees them via list_project_docs / get_project_doc. Drafts from sibling
    sessions are NOT merged regardless of owner — privatedocs are scoped to
    the session that produced them. Pass an empty project_id to disable
    cross-session merge (legacy / single-session test paths).
    """
    rows = (
        db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id == session_id)
        .order_by(database.SessionDetail.timestamp.asc())
        .all()
    )
    session_row = (
        db.query(database.Session)
        .filter(database.Session.session_id == session_id)
        .first()
    )

    state = State.fresh(session_id, user_id=user_id,
                         manual_product_type=manual_product_type,
                         project_id=project_id)
    if session_row:
        sd = _decode(session_row.data)
        active = (sd.get("agent") or {}).get("active_product_type", "")
        # manual_product_type wins over session-stored active_product_type.
        # Without this, a shared session_id across multiple Generate-button
        # calls (driver test pattern) inherited the LAST product as the
        # active one — e.g. MCOO request dispatched intel_agent for "OED"
        # because OED was the prior product in the same session.
        if active and not manual_product_type:
            state = replace(state, active_product_type=active)

    seen_ids = set()
    registry = []  # list of (artifact_type, [entry, ...])
    last_ia_messages: tuple = ()

    for row in rows:
        d = _decode(row.data)
        question = d.get("question") or ""
        answer = d.get("answer") or ""
        trace = d.get("agent_trace") or {}

        # Replay the analyst's user message
        if question:
            state = state.append_operations_expert({"role": "user", "content": question})

        # Replay the operations expert's assistant/tool messages from this turn
        for msg in trace.get("operations_expert_messages") or []:
            state = state.append_operations_expert(msg)

        # Walk intel-agent dispatches → harvest seen_ids from any tool responses
        # AND keep the most recent dispatch's messages so the next PW dispatch
        # in this build can read the IA's raw retrievals (the runner extracts
        # those from state.intel_agent_messages when building the PW skeleton).
        for disp in trace.get("intel_agent_dispatches") or []:
            disp_msgs = disp.get("messages") or []
            for m in disp_msgs:
                if m.get("role") == "tool":
                    try:
                        seen_ids.update(harvest_seen_ids(json.loads(m.get("content") or "{}")))
                    except Exception:
                        log.debug("seen_ids harvest skipped — tool message JSON parse failed", exc_info=True)
            if disp_msgs:
                last_ia_messages = tuple(disp_msgs)

        # Walk product-writer dispatches → recover artifacts + seen_ids
        for disp in trace.get("product_writer_dispatches") or []:
            emit = disp.get("emit") or {}
            artifact_id = emit.get("artifact_id")
            if artifact_id:
                seen_ids.add(artifact_id)
                entry = {
                    "artifact_id": artifact_id,
                    "source": "pytho",
                    "dtg": emit.get("dtg") or "",
                    "structured_data": emit.get("structured_data") or {},
                    "summary": emit.get("summary") or "",
                    "gaps": emit.get("gaps") or [],
                }
                # Append to registry by type
                atype = disp.get("artifact_type") or emit.get("artifact_type") or ""
                if atype:
                    found = False
                    for i, (t, items) in enumerate(registry):
                        if t == atype:
                            registry[i] = (t, items + [entry])
                            found = True
                            break
                    if not found:
                        registry.append((atype, [entry]))

    # Cross-session merge: pull PUBLISHED artifacts from sibling sessions in
    # the same project so projectdocs visibility works across analysts. Drafts
    # are intentionally excluded — privatedocs are session-scoped.
    if project_id:
        own_artifact_ids = {
            entry.get("artifact_id")
            for _atype, items in registry for entry in items
        }
        sibling_rows = (
            db.query(database.SessionDetail)
            .filter(database.SessionDetail.project_id == project_id)
            .filter(database.SessionDetail.session_id != session_id)
            .order_by(database.SessionDetail.timestamp.asc())
            .all()
        )
        for row in sibling_rows:
            d = _decode(row.data)
            trace = d.get("agent_trace") or {}
            for disp in trace.get("product_writer_dispatches") or []:
                emit = disp.get("emit") or {}
                artifact_id = emit.get("artifact_id")
                if not artifact_id or artifact_id in own_artifact_ids:
                    continue
                sd = emit.get("structured_data") or {}
                header = sd.get("header") or {}
                if header.get("status") != "published":
                    continue  # drafts from other sessions stay invisible
                entry = {
                    "artifact_id": artifact_id,
                    "source": "pytho",
                    "dtg": emit.get("dtg") or "",
                    "structured_data": sd,
                    "summary": emit.get("summary") or "",
                    "gaps": emit.get("gaps") or [],
                }
                seen_ids.add(artifact_id)
                atype = disp.get("artifact_type") or emit.get("artifact_type") or ""
                if not atype:
                    continue
                found = False
                for i, (t, items) in enumerate(registry):
                    if t == atype:
                        registry[i] = (t, items + [entry])
                        found = True
                        break
                if not found:
                    registry.append((atype, [entry]))

    state = state.add_seen(seen_ids)
    state = replace(state, artifact_registry=tuple((t, tuple(items)) for t, items in registry))
    if last_ia_messages:
        state = replace(state, intel_agent_messages=last_ia_messages)
    return state


def set_artifact_status_in_db(db, artifact_id: str, target_status: str,
                               approver: Optional[dict] = None) -> dict:
    """Walk every SessionDetail row, find the product-writer dispatch whose
    emit.artifact_id matches, and set emit.structured_data.header.status to
    `target_status` ('published' to approve/finalize, 'draft' to revert).
    Returns the matched row's session_id + artifact_type (+ approval
    metadata), or {"ok": False, "error": ...} if not found.

    Direct DB rewrite is necessary because load_state rebuilds
    artifact_registry from these persisted emits — purely-in-memory flips
    would not survive a session reload. `already` is True when the artifact
    was already at the target status (no write performed).

    Approval lives entirely in this status flip: the file is NOT moved out
    of the autodraft pool — only its lifecycle status changes (draft ⇄
    published). On publish we stamp `approved_by` / `approved_by_id` /
    `approved_at` (ms) from `approver`; on revert we clear them. The
    dashboard catalog + DRAFT/FINAL badge + editor toggle read these.
    """
    rows = (
        db.query(database.SessionDetail)
        .order_by(database.SessionDetail.timestamp.asc())
        .all()
    )
    for row in rows:
        try:
            payload = json.loads(row.data or "{}")
        except Exception:
            log.warning("malformed session_detail row id=%s session=%s — skipping",
                        getattr(row, "id", "?"), getattr(row, "session_id", "?"))
            continue
        trace = payload.get("agent_trace") or {}
        dispatches = trace.get("product_writer_dispatches") or []
        dirty = False
        matched = False
        matched_type = ""
        approved_by = ""
        approved_at = 0
        for disp in dispatches:
            emit = disp.get("emit") or {}
            if str(emit.get("artifact_id")) != artifact_id:
                continue
            matched = True
            matched_type = (disp.get("artifact_type")
                            or emit.get("artifact_type") or "")
            sd = emit.get("structured_data") or {}
            header = sd.get("header") or {}
            # Surface current approval metadata in the response even on a
            # no-op (already at target).
            approved_by = header.get("approved_by", "") or ""
            approved_at = header.get("approved_at", 0) or 0
            if (header.get("status") or "draft") == target_status:
                continue  # already at target — nothing to write
            header["status"] = target_status
            if target_status == "published":
                approved_by = (approver or {}).get("name", "") or ""
                approved_at = int(time.time() * 1000)
                header["approved_by"] = approved_by
                header["approved_by_id"] = (approver or {}).get("id", "") or ""
                header["approved_at"] = approved_at
            else:
                # Revert to draft — clear stale approval metadata.
                header.pop("approved_by", None)
                header.pop("approved_by_id", None)
                header.pop("approved_at", None)
                approved_by = ""
                approved_at = 0
            sd["header"] = header
            emit["structured_data"] = sd
            disp["emit"] = emit
            dirty = True
        if matched:
            if dirty:
                payload["agent_trace"]["product_writer_dispatches"] = dispatches
                row.data = database.null_safe_json_dumps(payload)
                db.add(row)
                db.commit()
            return {"ok": True, "artifact_id": artifact_id,
                    "artifact_type": matched_type, "session_id": row.session_id,
                    "status": target_status, "already": not dirty,
                    "approved_by": approved_by, "approved_at": approved_at}
    return {"ok": False, "error": f"artifact {artifact_id} not found"}


def publish_artifact_in_db(db, artifact_id: str,
                            approver: Optional[dict] = None) -> dict:
    """Approve/finalize an artifact: flip header.status to 'published' and
    stamp the approver. Thin wrapper over `set_artifact_status_in_db`;
    preserves the legacy `already_published` key for tool_publish_artifact
    + the POST /api/artifacts/{id}/publish endpoint.
    """
    result = set_artifact_status_in_db(db, artifact_id, "published", approver=approver)
    if result.get("ok"):
        result["already_published"] = result.pop("already", False)
    return result


def revert_artifact_in_db(db, artifact_id: str) -> dict:
    """Un-approve an artifact: flip header.status back to 'draft'. The file
    stays in place; only the status changes. Powers the editor's approval
    toggle (POST /api/artifacts/{id}/revert)."""
    return set_artifact_status_in_db(db, artifact_id, "draft")


def delete_artifact_in_db(db, artifact_id: str) -> dict:
    """Permanently delete a single artifact: drop its product-writer
    dispatch from the owning SessionDetail row (and delete the Session +
    SessionDetail rows entirely when that artifact was the session's only
    dispatch — the autodraft case, where each session holds exactly one
    artifact), then remove its on-disk files. Returns
    {ok, artifact_id, artifact_type, session_id} or {ok: False, error}.

    Powers the dashboard's per-row trash button (DELETE /api/artifacts/{id})
    so analysts can prune the autodrafted library.
    """
    import shutil as _shutil
    from pathlib import Path as _Path

    rows = (
        db.query(database.SessionDetail)
        .order_by(database.SessionDetail.timestamp.asc())
        .all()
    )
    for row in rows:
        try:
            payload = json.loads(row.data or "{}")
        except Exception:
            continue
        trace = payload.get("agent_trace") or {}
        dispatches = trace.get("product_writer_dispatches") or []
        idx = None
        atype = ""
        owner = ""
        for i, disp in enumerate(dispatches):
            emit = disp.get("emit") or {}
            if str(emit.get("artifact_id")) == artifact_id:
                idx = i
                atype = (disp.get("artifact_type")
                         or emit.get("artifact_type") or "")
                header = (emit.get("structured_data") or {}).get("header") or {}
                owner = (header.get("owner_user_id") or "").strip()
                break
        if idx is None:
            continue

        session_id = row.session_id
        remaining = [d for j, d in enumerate(dispatches) if j != idx]
        if remaining:
            # Session has other artifacts — rewrite without this one.
            trace["product_writer_dispatches"] = remaining
            payload["agent_trace"] = trace
            row.data = database.null_safe_json_dumps(payload)
            db.add(row)
        else:
            # Sole artifact in the session — drop the whole session.
            db.delete(row)
            sess = (db.query(database.Session)
                    .filter(database.Session.session_id == session_id)
                    .one_or_none())
            if sess is not None:
                db.delete(sess)
        db.commit()

        # On-disk cleanup: uploads/<owner>/<artifact_id>/. owner comes from
        # the header; fall back to the autodraft pool + anonymous.
        try:
            from handlers.product import UPLOAD_DIR
            for o in dict.fromkeys([owner, "_autodraft", "anonymous"]):
                if not o:
                    continue
                adir = _Path(UPLOAD_DIR) / o / artifact_id
                if adir.is_dir():
                    _shutil.rmtree(adir, ignore_errors=True)
        except Exception:
            log.exception("delete_artifact_in_db: disk cleanup failed for %s",
                          artifact_id)

        return {"ok": True, "artifact_id": artifact_id,
                "artifact_type": atype, "session_id": session_id}
    return {"ok": False, "error": f"artifact {artifact_id} not found"}


def list_autodraft_artifacts_by_type(product_type: str, db) -> list:
    """Return the latest autodraft for `product_type` in the same shape
    `list_session_artifacts` produces (artifact_id + render links).

    Walks SessionDetail rows whose session_id starts `autodraft-`,
    finds the first dispatch for the requested product type (rows are
    timestamp-desc, so first match = latest), and delegates to
    list_session_artifacts to assemble the full per-artifact entry
    with htmlLink/docxLink/pptxLink/xlsxLink.

    Powers `GET /api/products/autodraft_latest?product_type=X` — the
    cache-read DocEditorPage uses on doc-open: if an autodraft exists
    for the requested product type, load it directly instead of firing
    a full DAG run for an already-warm product.
    """
    rows = (
        db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id.like("autodraft-%"))
        .order_by(database.SessionDetail.timestamp.desc())
        .all()
    )
    for row in rows:
        payload = _decode(row.data)
        trace = payload.get("agent_trace") or {}
        for disp in trace.get("product_writer_dispatches") or []:
            atype = (disp.get("artifact_type")
                     or (disp.get("emit") or {}).get("artifact_type"))
            if atype != product_type:
                continue
            # Found the latest matching autodraft session — assemble the
            # full artifact entry list for it (a single autodraft session
            # typically holds exactly one product dispatch).
            return list_session_artifacts(row.session_id, db)
    return []


def list_session_artifacts(session_id: str, db) -> list:
    """Walk every SessionDetail row for `session_id` (ordered by timestamp asc),
    collect every product-writer dispatch that successfully emitted an
    artifact, render the files via `product.render_artifact_files` so the
    `/files/...` links exist on disk, and return a list of:

        {
          "artifact_id":   "...",
          "artifact_type": "WEM" | "MCOO" | ...,
          "htmlLink":      "/files/WEM_<id>.html" | None,
          "docxLink":      "/files/WEM_<id>.docx" | None,
          "pptxLink":      "/files/WEM_<id>.pptx" | None,
          "timestamp":     <ms epoch>,
          "gaps":          [...]
        }

    The returned shape matches the per-artifact entry the `/ws/intel_agent`
    socket emits in `intel_agent.artifacts[...]` (see `main()` below), so the
    UI can feed each one into the same `onChatArtifact` path that the live WS
    flow uses.

    Used by `GET /api/sessions/<session_id>/artifacts` to let DocEditorPage
    detect "this session already has a product" on mount and SKIP firing a
    duplicate Generate-button WS message after a page refresh / nav.

    Errors rendering a single artifact do NOT block the rest — that entry's
    htmlLink/docxLink/pptxLink stay None and a `render_error` field is set.
    """
    # Lazy import to avoid the product → intel_agent → product cycle at
    # module-load time.
    from handlers import product as product_handler

    rows = (
        db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id == session_id)
        .order_by(database.SessionDetail.timestamp.asc())
        .all()
    )
    out = []
    for row in rows:
        payload = _decode(row.data)
        trace = payload.get("agent_trace") or {}
        for disp in trace.get("product_writer_dispatches") or []:
            emit = disp.get("emit") or {}
            artifact_id = emit.get("artifact_id")
            if not artifact_id:
                continue
            artifact_type = (disp.get("artifact_type")
                             or emit.get("artifact_type")
                             or "")
            gaps = emit.get("gaps") or []
            _hdr = (emit.get("structured_data") or {}).get("header") or {}
            status = _hdr.get("status") or "draft"
            entry = {
                "artifact_id": str(artifact_id),
                "artifact_type": artifact_type,
                # Lifecycle status (draft / published) + approver so the
                # editor's approval toggle boots into the right state and
                # can show who approved it on load.
                "status": status,
                "approved_by": _hdr.get("approved_by", "") or "",
                "approved_at": _hdr.get("approved_at", 0) or 0,
                "htmlLink": None,
                "docxLink": None,
                "pptxLink": None,
                "xlsxLink": None,
                "timestamp": row.timestamp,
                "gaps": gaps,
            }
            try:
                rendered = product_handler.render_artifact_files(
                    session_id=session_id,
                    artifact_id=str(artifact_id),
                    artifact_type=artifact_type,
                    gaps=gaps,
                    db=db,
                )
                if rendered.get("status") == "ok":
                    entry["htmlLink"] = rendered.get("htmlLink")
                    entry["docxLink"] = rendered.get("docxLink")
                    entry["pptxLink"] = rendered.get("pptxLink")
                    entry["xlsxLink"] = rendered.get("xlsxLink")
                else:
                    entry["render_error"] = rendered.get("error", "render failed")
            except Exception as e:  # noqa: BLE001 — surface to UI, don't drop the row
                entry["render_error"] = f"render exception: {e}"
            out.append(entry)
    return out


def list_autodraft_catalog(db) -> list:
    """Catalog of every artifact in the shared autodraft pool, for the
    Intelligence Operations dashboard table.
    """
    from templates import PRODUCT_SCHEMAS
    from utils.exporter import (
        PRODUCT_CATEGORIES,
        PRODUCT_CATEGORY_ORDER,
        PRODUCT_LONG_NAMES,
    )

    # Canonical category → sort index. Unknown / uncategorized sorts last.
    cat_index = {c: i for i, c in enumerate(PRODUCT_CATEGORY_ORDER)}
    unknown_index = len(cat_index)

    def _meta(product_type: str) -> dict:
        cat_entry = PRODUCT_CATEGORIES.get(product_type)
        category, cat_order, step = cat_entry if cat_entry else (None, 0, None)
        schema = PRODUCT_SCHEMAS.get(product_type, {}) or {}
        ofmts = list(
            schema.get("output_formats")
            or [schema.get("output_format") or "docx"]
        )
        is_xlsx = ofmts == ["xlsx"]
        # Primary deliverable format for the row badge. XLSX-canonical
        # products are "xlsx"; everything else is the schema's singular
        # output_format (docx → portrait page, pptx → landscape slide).
        fmt = "xlsx" if is_xlsx else (schema.get("output_format") or "docx")
        return {
            "longName": PRODUCT_LONG_NAMES.get(product_type, product_type),
            "category": category,
            "categoryOrder": cat_order,
            "categoryIndex": cat_index.get(category, unknown_index),
            "stepLabel": step,
            # XLSX-canonical products open in SheetEditor, not the doc
            # editor; the frontend badges them and routes the open click.
            "isXlsx": is_xlsx,
            # Format badge shown on every row/rail item for consistency.
            "format": fmt,
        }

    catalog: list = []
    seen_success_types: set = set()
    seen_artifact_ids: set = set()

    rows = (
        db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id.like("autodraft-%"))
        .order_by(database.SessionDetail.timestamp.desc())
        .all()
    )
    for row in rows:
        payload = _decode(row.data)
        trace = payload.get("agent_trace") or {}
        for disp in trace.get("product_writer_dispatches") or []:
            emit = disp.get("emit") or {}
            artifact_id = emit.get("artifact_id")
            if not artifact_id:
                continue  # dispatch never emitted — not a real artifact
            artifact_type = (disp.get("artifact_type")
                             or emit.get("artifact_type") or "")
            if not artifact_type:
                continue
            meta = _meta(artifact_type)
            # Only surface products that carry a dashboard category. This
            # naturally drops any stray internal type while keeping all 45
            # doctrinal products (every one is categorized).
            if meta["category"] is None:
                continue
            # Dedupe by artifact_id. The same artifact can land on disk
            # exactly once but be referenced by multiple SessionDetail
            # rows (e.g. an autodraft re-fire that pulled the cached
            # artifact instead of regenerating, or the publish/revert
            # status-flip path writing a sibling row). Without this
            # dedupe the dashboard renders N table rows for the same
            # file, all sharing row.artifactId as their React key —
            # which collides during filter-change reconciliation and
            # leaves "sticky" rows visible after the filter switches
            # them out. Keep the FIRST seen (descending-timestamp
            # query order → newest entry wins).
            aid_str = str(artifact_id)
            if aid_str in seen_artifact_ids:
                continue
            seen_artifact_ids.add(aid_str)
            sd = emit.get("structured_data") or {}
            header = sd.get("header") or {}
            status = header.get("status") or "draft"
            seen_success_types.add(artifact_type)
            catalog.append({
                "artifactId": aid_str,
                "sessionId": row.session_id,
                "productType": artifact_type,
                "status": status,
                "approvedBy": header.get("approved_by", "") or "",
                "approvedAt": header.get("approved_at", 0) or 0,
                "timestamp": row.timestamp,
                "failed": False,
                **meta,
            })

    # Sort: most-recent-first, globally. A freshly generated draft (newest
    # timestamp) lands at the very top of the table regardless of category.
    # The rail still groups by category for filtering; the table is a flat
    # recency feed (matches the original IO mockup).
    catalog.sort(key=lambda r: -(r.get("timestamp") or 0))
    return catalog


# ============================================================================
# Per-segment persistence (incremental chat history)
# ============================================================================
# SessionDetail has a compound PK of (session_id, timestamp). One row per
# turn (save_turn) gives an audit-grade per-turn record; one row per
# segment (append_segment) gives a chat-history record that survives
# disconnect at any point during the turn. We write BOTH:
#   - save_turn rows: type="intel_agent", carry full agent_trace + answer.
#   - append_segment rows: type="message_segment", carry one of
#       user_msg | assistant_msg | tool_call | tool_result.
# `load_state` ignores rows it doesn't recognize, so adding segment rows
# is backward-compatible with the existing turn-rebuild path.
# /api/sessions/{session_id}/messages reads ONLY the segment rows and
# projects them to chat-message shape for the frontend.
def append_segment(db, *, session_id: str, project_id: str, user_id: str,
                     segment_type: str, payload: dict,
                     timestamp_ms: Optional[int] = None) -> int:
    """Write one chat-history segment row. Returns the timestamp_ms used
    as the row's PK part so the caller can use it as the message id."""
    if not (session_id or "").strip():
        session_id = "anon-" + str(uuid.uuid4())
    if not (project_id or "").strip():
        project_id = "anon"
    ts = timestamp_ms if timestamp_ms is not None else math.floor(time.time() * 1000)
    # SessionDetail's PK is (session_id, timestamp_ms). If two segments land in
    # the same millisecond (back-to-back user_msg/assistant_msg on a fast
    # turn), bump by 1 until unique. Cheap — collisions are rare.
    while True:
        existing = (
            db.query(database.SessionDetail)
            .filter(database.SessionDetail.session_id == session_id,
                    database.SessionDetail.timestamp == ts)
            .first()
        )
        if existing is None:
            break
        ts += 1
    data = {
        "type": "message_segment",
        "_segment_type": segment_type,
        "sessionId": session_id,
        "projectId": project_id,
        "userId": user_id,
        "timestamp": ts,
        **payload,
    }
    row = database.SessionDetail(
        session_id=session_id,
        timestamp=ts,
        project_id=project_id,
        data=database.null_safe_json_dumps(data),
    )
    db.add(row)
    db.commit()
    return ts


def list_session_messages(db, *, session_id: str, since_ts: int = 0,
                           limit: int = 1000) -> list[dict]:
    """Read chat-history segment rows for a session, ordered by timestamp
    asc. `since_ts` filters to rows strictly after the given timestamp_ms
    (use 0 for full history). Each returned dict is a projected chat
    message: {id, role, content, segment_type, created_at_ms, ...metadata}.
    """
    rows = (
        db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id == session_id)
        .filter(database.SessionDetail.timestamp > since_ts)
        .order_by(database.SessionDetail.timestamp.asc())
        .limit(limit)
        .all()
    )
    messages: list[dict] = []
    for r in rows:
        try:
            d = json.loads(r.data) if r.data else {}
        except Exception:
            continue
        # Only emit segment rows. Skip legacy turn rows (type=="intel_agent")
        # — they're audit records, not chat-history. If a session pre-dates
        # the segment refactor, /messages returns empty; the legacy turn
        # row still exists for load_state's agent-context rebuild.
        if d.get("type") != "message_segment":
            continue
        seg = d.get("_segment_type") or ""
        role = {
            "user_msg": "user",
            "assistant_msg": "assistant",
            "oe_thinking": "assistant",      # OE chain-of-thought, prefaces a tool_call or final reply
            "tool_call": "assistant",        # OE invokes a tool
            "tool_result": "tool",           # tool returns; included for full audit
        }.get(seg, "system")
        msg = {
            "id": r.timestamp,           # ms-precision PK doubles as message id
            "created_at_ms": r.timestamp,
            "role": role,
            "segment_type": seg,
            "content": d.get("content") or "",
        }
        # Carry through optional fields the frontend may want to render.
        # tool_args/tool_result_data are the structured payloads; tool_name
        # identifies the tool for both call + result; artifact_id/type
        # surface when a PW emit landed; dur_ms is timing for the segment.
        for k in (
            "artifact_id", "artifact_type", "job_id",
            "tool_name", "tool_args", "tool_call_id", "tool_result_data",
            "product_type", "status", "exit_reason",
            "sources_consulted", "gaps", "artifacts",
            "ok", "dur_ms", "title",
        ):
            if k in d:
                msg[k] = d[k]
        messages.append(msg)
    return messages


def save_turn(
    db,
    *,
    session_id: str,
    project_id: str,
    user_id: str,
    timestamp: int,
    question: str,
    answer: str,
    title: str,
    state_after: State,
    state_before_oe_msg_count: int,
    intel_agent_dispatches: list,
    product_writer_dispatches: list,
    iter_timings: list,
    turn_total_ms: int,
):
    """Write one SessionDetail row (the turn) and update Session.data when the
    active product type changed. Creates the Session row on the first turn."""
    # Guard against empty session_id / project_id from chat-UI requests that
    # haven't selected a project. The SQLite schema has NOT NULL on session_id
    # so an empty string raises IntegrityError, killing the turn and leaving
    # the user with the "Standing by" hallucinated answer instead of dispatching.
    # Generate a uuid for the missing field so the turn can persist.
    if not (session_id or "").strip():
        session_id = "anon-" + str(uuid.uuid4())
    if not (project_id or "").strip():
        project_id = "anon"
    new_oe_msgs = list(state_after.operations_expert_messages[state_before_oe_msg_count:])
    # Trim the trailing analyst-user message (already covered by `question`)
    if new_oe_msgs and new_oe_msgs[0].get("role") == "user":
        new_oe_msgs = new_oe_msgs[1:]

    detail_data = {
        "userId": user_id,
        "projectId": project_id,
        "sessionId": session_id,
        "timestamp": timestamp,
        "type": "intel_agent",
        "title": title,
        "question": question,
        "answer": answer,
        "agent_trace": {
            "operations_expert_messages": new_oe_msgs,
            "intel_agent_dispatches": intel_agent_dispatches,
            "product_writer_dispatches": product_writer_dispatches,
        },
        "iter_timings": iter_timings,
        "turn_total_ms": turn_total_ms,
    }

    detail = database.SessionDetail(
        session_id=session_id,
        timestamp=timestamp,
        project_id=project_id,
        data=database.null_safe_json_dumps(detail_data),
    )
    db.add(detail)

    # Session row: create on first turn, update if active_product_type changed
    session_row = (
        db.query(database.Session)
        .filter(database.Session.session_id == session_id)
        .first()
    )
    session_data = {
        "userId": user_id,
        "projectId": project_id,
        "sessionId": session_id,
        "timestamp": timestamp,
        "type": "intel_agent",
        "title": title,
        "agent": {"active_product_type": state_after.active_product_type},
    }
    if session_row is None:
        session_row = database.Session(
            session_id=session_id,
            timestamp=timestamp,
            project_id=project_id,
            data=database.null_safe_json_dumps(session_data),
        )
        db.add(session_row)
    else:
        existing = _decode(session_row.data)
        existing.update({"agent": {"active_product_type": state_after.active_product_type}})
        session_row.data = database.null_safe_json_dumps(existing)

    db.commit()


# ============================================================================
# ID HARVESTING (for fabrication detection on artifact emit)
# ============================================================================
ID_PATTERNS = [
    re.compile(r"\bT-[A-Z]{2}\d{3,5}\b"),
    # {2,40} not {2,15}: Donovian unit names like
    # U-DN-624UTILITYHESLOSQDN (21 chars after U-) exceed 15 → regex backs
    # off, finds the \b at U-DN- and reports U-DN- as a "match". seen_ids
    # has the full id (harvest uses pat.match which captures the stored
    # string), so the truncated U-DN- is then flagged as fabricated even
    # though the full id IS in seen_ids. 40-char cap covers IDX 26's
    # longest (U-DN-624UTILITYHELOSQDN, U-DN-HQWESTERNARMY, etc.) with
    # headroom.
    re.compile(r"\bU-[A-Z0-9-]{2,40}\b"),
    re.compile(r"\bE-\d{3,5}\b"),
    # Same backoff hazard as the U- pattern — bumped to {3,40}.
    re.compile(r"\bI-[A-Z0-9-]{3,40}\b"),
    re.compile(r"\bEV-\d{3,5}\b"),
    re.compile(r"\bSPOT-\d{3,5}\b"),
    re.compile(r"\bINTREP-\d{3,5}\b"),
    re.compile(r"\bC-\d{3,5}\b"),
    re.compile(r"\bA-\d{4,6}\b"),
]


def harvest_seen_ids(response_obj):
    found = set()

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("id", "artifact_id", "target_id", "linked_unit") and isinstance(v, str):
                    if any(pat.match(v) for pat in ID_PATTERNS):
                        found.add(v)
                walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(response_obj)
    return found


def detect_fabricated_ids(structured_data, seen_ids):
    s = json.dumps(structured_data, default=str)
    referenced = set()
    for pat in ID_PATTERNS:
        for m in pat.findall(s):
            referenced.add(m)
    return sorted(referenced - seen_ids)


# Redaction-token leakage detection — Gemma's training corpus is full of
# real-world INTSUMs / SIGSUMs that have been [REDACTED] before public
# release, so the model emits redaction tokens when it can't anchor a value.
# IBL / IDX 26 is UNCLASSIFIED — there is nothing to redact, and these
# tokens are user-visible cruft that fails the artifact. The runner enforces
# this hard so the writer is forced to substitute a real scenario value (or
# leave the field empty so the gap surfaces).
# Match the placeholder SHAPE (frame + keyword), not the keyword alone, so we
# don't false-positive on legitimate values like "UNCLASSIFIED" (the canonical
# classification banner), "declassified" prose, the SYSTEM_REACTION enum, etc.
# The token must appear inside one of these placeholder frames:
#   - [TOKEN], (TOKEN), {TOKEN}, <TOKEN>   (brackets / parens / braces / chevrons)
#   - **TOKEN**                            (markdown-bold redaction)
#   - --TOKEN--                            (dashed redaction)
#   - bare REDACTED with word boundaries   (no legitimate prose usage)
_REDACTION_KEYWORD = (
    r"(?:REDACTED|CLASSIFIED|CONFIDENTIAL|"
    r"SECRET(?://[A-Z/]+)?|TOP\s+SECRET(?://[A-Z/]+)?|TS//SCI)"
)
_REDACTION_TOKEN_RE = re.compile(
    r"(?:"
        r"[\[\(\{<]\s*\**\s*-{0,2}\s*" + _REDACTION_KEYWORD + r"\s*-{0,2}\s*\**\s*[\]\)\}>]"
        r"|"
        r"\*\*\s*" + _REDACTION_KEYWORD + r"\s*\*\*"
        r"|"
        r"--\s*" + _REDACTION_KEYWORD + r"\s*--"
        r"|"
        r"(?<![A-Z])REDACTED(?![A-Z])"
    r")",
    re.IGNORECASE,
)


def detect_redaction_tokens(structured_data) -> list[str]:
    """Walk structured_data and return a list of `<field-path>: <snippet>`
    descriptors for every string value containing a redaction-style placeholder.
    Returns an empty list when clean. Field paths use dotted/bracketed shape
    (header_analysis.threat_coa, rows_analysis[2].justification,
    system_components[0].entities[1]) so the writer can locate the bad value."""
    findings: list[str] = []

    def walk(value, path: str):
        if isinstance(value, str):
            m = _REDACTION_TOKEN_RE.search(value)
            if m:
                snippet = value.strip()
                if len(snippet) > 120:
                    # Center the window on the match so the writer sees the
                    # context (e.g. "[REDACTED] grid coordinates near ...").
                    start = max(0, m.start() - 40)
                    end = min(len(value), m.end() + 40)
                    snippet = ("..." if start > 0 else "") + value[start:end] + ("..." if end < len(value) else "")
                findings.append(f"{path or '<root>'}: {snippet}")
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                walk(item, f"{path}[{i}]")

    walk(structured_data, "")
    return findings


# ============================================================================
# T1 NORMALIZATION (mock-adapter behaviour)
# ============================================================================
# Production puts these mappings in the Cougaar adapter; the harness mocks
# mirror them here so the intel agent's tool returns are T1-shaped.
CLASS_TO_CATEGORY = {
    "hq": "command_and_control", "armor_hq": "command_and_control",
    "sam_hq": "command_and_control", "recon_hq": "reconnaissance_and_surveillance",
    "corps_hq": "command_and_control", "div_hq": "command_and_control",
    "mech_inf": "maneuver", "armor": "maneuver", "infantry": "maneuver",
    "aviation": "maneuver", "at": "maneuver",
    "sam_short": "air_defense_artillery", "ew": "cyberspace_electromagnetic_activities",
    "arty": "fire_support",
    "engineer": "engineer",
    "sustainment": "sustainment", "medical": "sustainment",
    "signal": "command_and_control",
    "chemical": "chemical_biological_radiological_nuclear",
}
CLASS_TO_TYPE_FIVE_O = {
    "sam_short": "equipment",
}

TARGET_CAT_TO_CATEGORY = {
    "ADA": "air_defense_artillery", "C2": "command_and_control", "FS": "fire_support",
    "Maneuver": "maneuver", "CEMA": "cyberspace_electromagnetic_activities",
    "ENG": "engineer", "RECON": "reconnaissance_and_surveillance", "SUSTAIN": "sustainment",
    "LIFT": "lift", "INFO": "information",
    "CBRN": "chemical_biological_radiological_nuclear", "LOC": "line_of_communication",
}
TARGET_CAT_TO_TYPE_FIVE_O = {
    "ADA": "equipment", "FS": "equipment", "C2": "facility", "Maneuver": "organization",
    "CEMA": "equipment", "RECON": "organization", "ENG": "equipment", "SUSTAIN": "facility",
    "LIFT": "equipment", "INFO": "facility", "CBRN": "equipment", "LOC": "facility",
}


def _t1_unit(u: dict, force_type: str | None = None) -> dict:
    cls = u.get("class", "")
    sidc = u.get("sidc") or ""
    # MIL-STD-2525C position-2 = affiliation. The scenario.KG.unit_red fixture
    # encodes them as `SN...` (Neutral) regardless of force_type — that
    # was a one-time XLSX import default. ATP 2-01.3 says enemy OOB must
    # render hostile (red diamond), so we normalize the digit to match
    # the force_type the caller declared. Friendly OOB gets `F`, neutral
    # left as-is. Skip if SIDC isn't a recognised 15-char 2525B/C string.
    if sidc and len(sidc) >= 2 and force_type:
        target_affil = {
            "HOSTILE":  "H",
            "FRIENDLY": "F",
            "NEUTRAL":  "N",
        }.get(force_type)
        if target_affil:
            sidc = sidc[0] + target_affil + sidc[2:]
    return {
        "id": u.get("id"),
        "name": u.get("name"),
        "category": CLASS_TO_CATEGORY.get(cls, ""),
        "type_five_o": CLASS_TO_TYPE_FIVE_O.get(cls, "organization"),
        "echelon": u.get("echelon"),
        "location_mgrs": u.get("mgrs"),
        "doi": u.get("last_obs"),
        "size_posture_activity": u.get("activity", ""),
        "strength": u.get("strength", ""),
        "parent": u.get("parent"),
        # Affiliation-normalized 2525C SIDC. mil-sym-ts colors by the
        # position-2 character (H red, F blue, N green).
        "sidc": sidc or None,
        "force_type": force_type,
        # Pass through composition_rollup so the SITEMP range-fan
        # extractor (_extract_writer_range_fans) can derive one fan per
        # unique organic equipment piece per templated unit on the
        # scenario-fixture-floor path. Cougaar-resolved units get this
        # added explicitly by tool_get_oob (intel_agent line ~1703);
        # without including it here, fixture-only stacks (V Corps with
        # COUGAAR_BASE_URL unset) silently lose every range fan.
        "composition_rollup": u.get("composition_rollup") or {},
    }


# ----------------------------------------------------------------------------
# Cougaar → T1 shim. The Cougaar adapter returns Unit records with kbID uuid,
# integer ref_id, SIDC, GeoJSON location and {composition: {...}} block. The
# downstream skeleton builder + writer expect T1 shape ({id, name, category,
# type_five_o, echelon, location_mgrs, doi, size_posture_activity, strength,
# parent}). This shim adapts. Notes:
#   - `id`: the kbID uuid (string). Stable across calls, accepted by
#     get_target_by_id, and what _entity_lookup_from_ia indexes on.
#   - `category` / `type_five_o`: left blank — Cougaar's class taxonomy is
#     SIDC-driven and not a 1:1 with the legacy class enum. The writer
#     reads `name` + `description` + `sidc` to classify; leaving these blank
#     avoids miscategorisation and is no worse than the legacy scenario.KG enum
#     coverage (which itself often missed types).
#   - `location_mgrs`: converted from the GeoJSON Point (lon,lat) to an MGRS
#     string via utils.coords.point_to_mgrs. Per ATP 2-01.3 SITEMP/MCOO
#     doctrine the location column is MGRS — never lat/lon. Empty string
#     when the source point is missing or outside the MGRS grid.
#   - `doi`: maps from cougaar `last_updated` (ISO-8601). The writer renders
#     it as the DTG-style "date of information" column.
#   - `size_posture_activity`: from `unit_status` (enum literal).
#   - `strength`: from `personnel_status` (a free-text string in cougaar).
#   - `parent`: from `superior_ref_id` (integer link to data__ID). Preserved
#     so the HVTL composition rollup can chain children. We DO carry the
#     full composition.equipment_list_with_counts so downstream HVTL/CARVER
#     code can produce a Tier-2 equipment line beneath the parent unit.
# ----------------------------------------------------------------------------
def _cougaar_unit_to_t1(u: dict) -> dict:
    from utils.coords import point_to_mgrs
    location_mgrs = point_to_mgrs(u.get("location"))
    name = u.get("name") or u.get("unit_long_name") or u.get("unit_short_name") or ""
    desc = (u.get("description") or "")
    if len(desc) > 240:
        desc = desc[:240] + "..."
    # Composition rollup carried but trimmed to top-K equipment types — full
    # rollups (50+ nomenclatures per unit) blow past the agent prompt budget.
    comp = u.get("composition") or {}
    eq_list = (comp.get("equipment_list_with_counts") or [])[:15]
    composition = {
        "personnel_count": comp.get("personnel_count"),
        "equipment_list_with_counts": eq_list,
    }
    return {
        "id": u.get("uuid") or "",
        "ref_id": u.get("ref_id"),
        "name": name,
        "category": "",
        "type_five_o": "organization",
        "echelon": u.get("echelon") or "",
        "location_mgrs": location_mgrs,
        "doi": u.get("last_updated") or "",
        "size_posture_activity": u.get("unit_status") or "",
        "strength": u.get("personnel_status") or "",
        "parent": u.get("superior_ref_id"),
        "sidc": u.get("sidc") or "",
        "force_type": u.get("force_type") or "",
        "description": desc,
        "composition": composition,
    }


def _cougaar_target_to_t1(f: dict) -> dict:
    """Map a Cougaar FusedObservation (IMINT-fused target) into the T1
    target row shape consumed by HVTL/CARVER/HPTL row builders."""
    from utils.coords import point_to_mgrs
    raw = (f.get("raw_records") or [{}])[0] or {}
    location_mgrs = point_to_mgrs(f.get("location"))
    return {
        "id": (f.get("source_kbids") or [None])[0] or f.get("observation_id"),
        "name": f.get("entity_name") or f.get("name") or "",
        "description": raw.get("status_activity") or f.get("evidence_text") or "",
        "category": "",
        "type_five_o": "facility",
        "location_mgrs": location_mgrs,
        "doi": f.get("reported_time") or "",
        "size_posture_activity": raw.get("status_activity") or "",
        "parent_target_system": "",
        "be_number": f.get("entity_key") or raw.get("be_number") or "",
        "linked_unit": "",
        "priority": None,
        "status": raw.get("status_activity") or "",
        "country": raw.get("country") or "",
        "target_function_code": raw.get("target_function_code") or "",
        # Internal tag (underscore prefix — never rendered) used by the HPTL
        # skeleton row builder to stamp the correct `kg:list_targets`
        # provenance label on Cougaar-UUID-keyed rows. Without this the
        # provenance switch falls back to a `T-` prefix heuristic that
        # mis-tags Cougaar kbID rows as `kg:get_oob`.
        "_source_tool": "list_targets",
    }


def _t1_target(t: dict, unit_by_id: Optional[dict] = None) -> dict:
    cat = t.get("cat", "")
    status = t.get("status", "")
    if status.startswith("DEFEATED"):
        posture = "defeated"
    elif status:
        posture = status
    else:
        posture = ""
    doi = ""
    if "DEFEATED" in status:
        parts = status.split()
        if len(parts) >= 2:
            doi = parts[-1]
    if not doi:
        doi = t.get("last_obs", t.get("doi", ""))
    if not doi and unit_by_id:
        linked = t.get("linked_unit")
        if linked and linked in unit_by_id:
            doi = unit_by_id[linked].get("last_obs", "")
    return {
        "id": t.get("id"),
        "name": t.get("name", ""),
        "description": t.get("description", ""),
        "category": TARGET_CAT_TO_CATEGORY.get(cat, ""),
        "type_five_o": TARGET_CAT_TO_TYPE_FIVE_O.get(cat, "organization"),
        "location_mgrs": t.get("mgrs", t.get("location_mgrs", "")),
        "doi": doi,
        "size_posture_activity": posture,
        "parent_target_system": t.get("parent_target_system", ""),
        "be_number": t.get("be_number", ""),
        "linked_unit": t.get("linked_unit", ""),
        "priority": t.get("priority"),
        "status": status,
        # Internal tag (underscore prefix — never rendered). Drives the HPTL
        # skeleton row provenance label to `kg:list_targets` regardless of
        # whether the id is a `T-...` fixture key or a Cougaar kbID.
        "_source_tool": "list_targets",
    }


# ============================================================================
# INTEL-AGENT TOOLS
# ============================================================================
async def tool_get_oob(args, state):
    """Force-structure OOB for the IDX 26 AO.

    RED source (per § 12 of per-coa-product-scope.md, 2026-06-09):
        - PRIMARY: `oob.get_red_oob_resolved()` — the on-disk resolved
          snapshot from the last fusion pass (the Cougaar feed is dead
          and disconnected; the snapshot IS the source). Every named
          unit carries composition rollup, FM 3-60 category breakdown,
          attributed sub-positions, and a derived rate-of-march from
          the slowest-element rule.
        - Scenario fixture (`scenario.KG.unit_red`) is used ONLY as a SIDC + activity
          enrichment source by name match — never as a fallback when
          the snapshot has fuseable data. The 50-row fixture is
          doctrine-floor metadata that fills in missing SIDC /
          disposition / activity for the named units the snapshot shares.
        - The fixture-only floor still applies when the snapshot is
          missing or carries fewer than 10 named units.

    BLUE: scenario fixture only. The legacy `World.Organizations.Unit`
    vertex holds 122 NTC California rows (wrong scenario, off-limits).

    `_source` is one of:
        - `"cougaar_resolved"`     — live IDXRedUnit + fusion (the default)
        - `"scenario_authored"`    — fixture floor when cougaar is dark
    """
    force_type = (args.get("force_type") or "RED").upper()
    if force_type not in ("RED", "BLUE"):
        force_type = "RED"
    t0 = time.monotonic()
    error = None
    units: list = []
    resolved_payload: dict | None = None

    # BLUE has no live source — always fixture.
    if force_type != "RED":
        affil = "FRIENDLY"
        units = [_t1_unit(u, force_type=affil) for u in scenario.KG.get("unit_blue", [])]
        source = "scenario_authored"
    else:
        # RED: cougaar resolved is authoritative when reachable.
        cougaar_named: list = []
        try:
            with stage("tool_get_oob_resolved", force_type="RED"):
                resolved_payload = await oob.get_red_oob_resolved()
            cougaar_named = resolved_payload.get("named_units") or []
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.warning(
                "get_red_oob_resolved raised %s; falling through to scenario fixture", error
            )
            resolved_payload = None

        # Threshold for "cougaar has fuseable data": at least 10 named units
        # with location. Below that we treat cougaar as dark and use the
        # scenario floor exactly as before — but we DO NOT do the silent
        # "scenario always wins" thing the prior version did when cougaar
        # was actually present.
        usable_named = [
            n for n in cougaar_named
            if n.get("primary_position", {}).get("lat") is not None
        ]
        if len(usable_named) >= 10:
            # Cougaar IS the source. Build T1-shaped units from cougaar
            # primary + attributed positions, then enrich SIDC/category/
            # activity from scenario.KG.unit_red by NAME MATCH where the
            # cougaar row lacks them.
            affil = "HOSTILE"
            scenario_by_name = {
                (u.get("name") or "").upper().strip(): u
                for u in scenario.KG.get("unit_red", [])
                if u.get("name")
            }
            units = []
            for nu in usable_named:
                nm = (nu.get("name") or "").strip()
                match = scenario_by_name.get(nm.upper())
                # Cougaar primary position → MGRS via point_to_mgrs
                pos = nu.get("primary_position") or {}
                lat, lon = pos.get("lat"), pos.get("lon")
                mgrs_str = ""
                if lat is not None and lon is not None:
                    try:
                        from utils.coords import latlon_to_mgrs
                        mgrs_str = latlon_to_mgrs(lat, lon) or ""
                    except Exception:
                        mgrs_str = ""
                # Echelon → class fallback for SIDC derivation
                echelon = (nu.get("echelon") or "").upper()
                sidc = nu.get("sidc") or (match.get("sidc") if match else None) or ""
                if not sidc:
                    # Derive a minimal hostile SIDC from echelon — enough for
                    # mil-sym-ts to render as a red diamond.
                    ech_token = {
                        "DIVISION": "XX",
                        "DIV":      "XX",
                        "BRIGADE":  "X",
                        "BDE":      "X",
                        "BATTALION": "II",
                        "BN":       "II",
                        "COMPANY":  "I",
                        "CO":       "I",
                        "BATTERY":  "I",
                        "BTRY":     "I",
                    }.get(echelon, "")
                    sidc = ""  # leave empty; mil-sym-ts handles missing SIDC by name
                    _ = ech_token  # placeholder for future SIDC builder
                # Determine class from composition or scenario match for category
                cls = (match.get("class") if match else "") or ""
                category = CLASS_TO_CATEGORY.get(cls, "")
                if not category:
                    # Derive from primary FM 3-60 category in composition rollup
                    cats = nu.get("composition_by_fm360_category", {}) or {}
                    if cats:
                        top_cat = max(cats.items(), key=lambda x: x[1])[0]
                        _fm_to_doctrine = {
                            "AirDefense":  "air_defense_artillery",
                            "FireSupport": "fire_support",
                            "EW_REC":      "cyberspace_electromagnetic_activities",
                            "C2_Signal":   "command_and_control",
                            "Engineer":    "engineer",
                            "RISTA_UAV":   "reconnaissance_and_surveillance",
                            "Lift":        "lift",
                            "Maneuver":    "maneuver",
                            "Radar":       "reconnaissance_and_surveillance",
                        }
                        category = _fm_to_doctrine.get(top_cat, "")
                t1 = {
                    "id":                  nu.get("kg_id") or "",
                    "name":                nm,
                    "category":            category,
                    "type_five_o":         CLASS_TO_TYPE_FIVE_O.get(cls, "organization"),
                    "echelon":             echelon or (match.get("echelon") if match else ""),
                    "location_mgrs":       mgrs_str,
                    "doi":                 (match.get("last_obs") if match else None)
                                            or pos.get("last_seen") or "",
                    "size_posture_activity": nu.get("current_activity")
                                              or (match.get("activity") if match else "")
                                              or "",
                    "strength":            nu.get("strength_pct_manned")
                                            or (match.get("strength") if match else "")
                                            or "",
                    "parent":              nu.get("parent_unit")
                                            or (match.get("parent") if match else ""),
                    "sidc":                sidc or None,
                    "force_type":          affil,
                    # § 12 NEW FIELDS — preserved for downstream consumers
                    # (HVTL composition rollup, range fans, multi-position view)
                    "rate_of_march_kmh":   nu.get("rate_of_march_kmh"),
                    "composition_rollup":  nu.get("composition_rollup") or {},
                    "composition_by_fm360_category": nu.get("composition_by_fm360_category") or {},
                    "attributed_positions": nu.get("attributed_positions") or [],
                    "primary_position":    pos,
                    "disposition":         nu.get("disposition"),
                    "hvt_worthy_count":    nu.get("hvt_worthy_count", 0),
                    "_source_vertex":      "IDXRedUnit",
                    "_source_kg_id":       nu.get("kg_id"),
                    # Position provenance — 'raw' (cougaar reported the
                    # position directly), 'attributed' (cluster
                    # attribution), 'children_avg' (BDE rolled up to
                    # centroid of known BNs), or 'frontage:<role>@<deg>'
                    # (doctrinal-frontage offset from parent BDE along
                    # threat avenue-of-approach tangent). Threaded down
                    # to entity attrs so the analyst panel can flag
                    # inferred positions.
                    "position_source":     nu.get("position_source") or "raw",
                }
                # Apply SIDC affiliation normalization (re-use _t1_unit's
                # hostility-position-2 rule) when scenario provided a SIDC.
                if match and match.get("sidc"):
                    raw_sidc = match.get("sidc") or ""
                    if len(raw_sidc) >= 2:
                        t1["sidc"] = raw_sidc[0] + "H" + raw_sidc[2:]
                units.append(t1)
            source = "cougaar_resolved"
            log.info(
                "tool_get_oob RED: cougaar_resolved is authoritative — "
                "%d named units (of %d total cougaar named), %d sub-positions "
                "attributed, %d unidentified formations flagged for PIR loop.",
                len(units),
                len(cougaar_named),
                resolved_payload.get("fusion_stats", {}).get("n_attributed_sub_positions", 0),
                resolved_payload.get("fusion_stats", {}).get("n_orphan_clusters_unidentified", 0),
            )
        else:
            # No usable resolved snapshot — the project scenario's KG
            # floor IS the RED order of battle. (The old
            # PYTHO_DISABLE_SCENARIO_FIXTURES kill switch is GONE: its
            # "cougaar is the only source" semantics died with the
            # cougaar disconnect, and under per-project scenario data
            # the document is always authoritative.)
            affil = "HOSTILE"
            units = [_t1_unit(u, force_type=affil) for u in scenario.KG.get("unit_red", [])]
            source = "scenario_authored"
            log.info(
                "tool_get_oob RED: no usable resolved snapshot (%d named) — "
                "project scenario KG floor (%d units).",
                len(usable_named), len(units),
            )

    # 3. Merge OPORD-derived threat disposition (RED only). scenario_ingest
    # writes `scenario.KG.opord_derived_threat` with the Donovian build-up timeline
    # from Road-to-War. These are valid RED OOB enrichment entries — per
    # doctrine Threat COA Statement / Threat Model / Threat Capability Statement / SITEMP /
    # Threat Overlay all reason over the full enemy laydown including
    # historical movements that produced the current disposition.
    if force_type == "RED":
        derived = scenario.KG.get("opord_derived_threat") or []
        for d in derived:
            if not isinstance(d, dict):
                continue
            units.append({
                "id": d.get("id") or d.get("name") or "",
                "name": d.get("name") or "",
                "echelon": d.get("echelon"),
                "class": d.get("class"),
                "loc": d.get("loc") or d.get("destination_loc") or d.get("origin_loc"),
                "mgrs": d.get("mgrs") or d.get("destination_mgrs") or d.get("origin_mgrs"),
                "strength": d.get("strength"),
                "activity": d.get("activity"),
                "last_obs": d.get("dtg"),
                "parent": d.get("parent"),
                "_source_vertex": "OPORD_DERIVED",
                "_source_doc_tag": d.get("source_doc_tag"),
                "_source_fragment": d.get("source_fragment"),
                "_origin_loc": d.get("origin_loc"),
                "_origin_mgrs": d.get("origin_mgrs"),
                "_destination_loc": d.get("destination_loc"),
                "_destination_mgrs": d.get("destination_mgrs"),
                "_dtg": d.get("dtg"),
            })

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _record_cougaar_call("get_oob", called=True, row_count=len(units),
                         supplemented_from_fixture=(source == "scenario_authored"),
                         elapsed_ms=elapsed_ms, source=source, error=error)
    note = (
        "RED OOB is fused live from cougaar IDXRedUnit + Equipment + "
        "Equipment_RedUnit edges (see § 12 of per-coa-product-scope.md). "
        "Each unit carries composition_rollup, FM 3-60 category breakdown, "
        "rate_of_march_kmh, and any attributed_positions resolved from "
        "Unknown Red Unit stubs. unidentified_formations are orphan "
        "observations that couldn't be attributed — they surface as PIR "
        "candidates for the next intel cycle. BLUE remains scenario-authored "
        "until an IDX 26 BLUE vertex exists."
    )
    payload = {
        "units": units,
        "count": len(units),
        "ao": _DEFAULT_AO_LABEL,
        "force_type": force_type,
        "summary": (
            f"{len(units)} {force_type} unit records "
            f"(source={source}); IDX 26 {_DEFAULT_AO_LABEL}."
        ),
        "_supplemented_from_fixture": (source == "scenario_authored"),
        "_source": source,
        "_note": note,
        "_error": error,
    }
    # § 12 resolved-OOB extensions — only present on RED + cougaar-resolved path.
    if resolved_payload is not None:
        payload["unidentified_formations"] = resolved_payload.get("unidentified_formations") or []
        payload["fusion_stats"] = resolved_payload.get("fusion_stats") or {}
        payload["raw_summary"] = resolved_payload.get("raw_summary") or {}
        payload["snapshot_path"] = resolved_payload.get("snapshot_path")
        payload["snapshot_ts_ms"] = resolved_payload.get("snapshot_ts_ms")
    return payload


async def tool_list_targets(args, state):
    """Target collection for the active scenario AO.

    Source: scenario data (scenario.KG.target) — the scenario-authored target
    list with canonical MGRS positions. The Cougaar IMINT target feed
    is dead and fully disconnected (2026-07-09); its rows never carried
    plottable geometry anyway (every data__location was null).
    """
    flt = args.get("filter") or {}
    t0 = time.monotonic()
    unit_by_id = {u.get("id"): u for u in scenario.KG.get("unit_red", []) if u.get("id")}
    rows = [_t1_target(t, unit_by_id) for t in scenario.KG.get("target", [])]
    if flt:
        rows = [r for r in rows if all(r.get(k) == v for k, v in flt.items())]
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _record_cougaar_call("list_targets", called=False, row_count=len(rows),
                         supplemented_from_fixture=True,
                         elapsed_ms=elapsed_ms, source="scenario_authored")
    return {
        "targets": rows,
        "count": len(rows),
        "filter_applied": flt,
        "_supplemented_from_fixture": True,
        "_source": "scenario_authored",
        "_no_geometry": False,
        "_note": ("Scenario-authored target set (scenario.KG.target) with canonical "
                   f"MGRS positions for {_DEFAULT_AO_LABEL}."),
        "_error": None,
    }


async def tool_get_target(args, state):
    """Single-target deep dive. target_id may be a kg id (uuid form,
    resolved against the on-disk resolved-OOB snapshot) OR a legacy scenario.KG
    fixture id (T-DM###/U-#### form). Falls through to the scenario.KG fixture
    so the runner stays compatible with prompts that hand-roll fixture
    IDs."""
    target_id = args.get("target_id", "")
    if not target_id:
        _record_cougaar_call("get_target", called=False, row_count=0,
                             supplemented_from_fixture=False, elapsed_ms=0,
                             source="none", error="missing target_id")
        return {"error": "target_id required"}
    t0 = time.monotonic()
    error = None
    # kg ids look like UUIDs (hyphenated hex). Legacy fixture IDs are
    # short prefixed strings (T-DM001 / U-DM-COMD-001). uuid-shaped ids
    # resolve against the on-disk resolved-OOB snapshot's named_units —
    # the same identity namespace the map's source_kbid and the TIDAT
    # per-target dispatch use.
    looks_uuid = "-" in target_id and len(target_id) >= 30
    if looks_uuid:
        try:
            resolved = await oob.get_red_oob_resolved()
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            for nu in (resolved.get("named_units") or []):
                if (nu.get("kg_id") or nu.get("uuid")) == target_id:
                    _record_cougaar_call("get_target", called=False, row_count=1,
                                         supplemented_from_fixture=False,
                                         elapsed_ms=elapsed_ms,
                                         source="oob_snapshot")
                    return {"target": nu, "_source": "oob_snapshot"}
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            log.warning("get_target(%s): resolved-OOB snapshot read failed "
                        "(%s); falling back to scenario.KG fixture", target_id, error)
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    unit_by_id = {u.get("id"): u for u in scenario.KG.get("unit_red", []) if u.get("id")}
    for r in scenario.KG.get("target", []):
        if r.get("id") == target_id:
            _record_cougaar_call("get_target", called=True, row_count=1,
                                 supplemented_from_fixture=True,
                                 elapsed_ms=elapsed_ms, source="fixture", error=error)
            return {"target": _t1_target(r, unit_by_id),
                    "_supplemented_from_fixture": True, "_source": "fixture"}
    for u in scenario.KG.get("unit_red", []):
        if u.get("id") == target_id:
            _record_cougaar_call("get_target", called=True, row_count=1,
                                 supplemented_from_fixture=True,
                                 elapsed_ms=elapsed_ms, source="fixture", error=error)
            return {"target": _t1_unit(u),
                    "_supplemented_from_fixture": True, "_source": "fixture"}
    _record_cougaar_call("get_target", called=True, row_count=0,
                         supplemented_from_fixture=False,
                         elapsed_ms=elapsed_ms, source="none", error=error or "not_found")
    return {"error": f"target {target_id} not found"}


async def tool_get_recent_intel(args, state):
    """Multi-INT report stream — scenario data only.

    The Cougaar fusion layer is dead and fully disconnected
    (2026-07-09). Reports / engagements / imagery / OSINT all come from
    the scenario data (scenario.KG.report / scenario.KG.engagement / scenario.KG.imagery); rows
    carry per-event bodies authored with the scenario. The writer must
    NOT over-interpret absent fields — absent means unknown.
    """
    window_hours = args.get("window_hours", 24)
    types = args.get("types") or ["report", "engagement", "imagery"]
    t0 = time.monotonic()
    reports = list(scenario.KG.get("report") or [])
    engagements = list(scenario.KG.get("engagement") or [])
    imagery = list(scenario.KG.get("imagery") or [])
    # OSINT rows in scenario.KG['report'] carry type='OSINT' (per-event,
    # rich body — see scenario.py OSINT-DIV3ID-26-0501..0508).
    fixture_osint = [r for r in reports
                     if (r.get("type") or "").upper() == "OSINT"]
    bundle = {
        "reports": reports,
        "engagements": engagements,
        # Singular alias preserved for BDA + other schemas that filter
        # on "engagement".
        "engagement": engagements,
        "imagery": imagery,
        "osint": fixture_osint,
        "window_hours": window_hours,
        "types_requested": types,
        "_supplemented_from_fixture": True,
        "_source": "scenario_authored",
        "_note": (
            "Scenario-authored multi-INT stream (scenario.KG.report / "
            "KG.engagement / scenario.KG.imagery). Treat absent fields as "
            "unknown — do not synthesize content."
        ),
        "_total_count": len(reports) + len(engagements) + len(imagery),
        "_fixture_engagement_count": len(engagements),
        "_fixture_osint_count": len(fixture_osint),
    }
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _record_cougaar_call("get_recent_intel", called=False,
                         row_count=bundle["_total_count"],
                         supplemented_from_fixture=True,
                         elapsed_ms=elapsed_ms, source="scenario_authored")
    return bundle


def tool_get_spatial_overlay(args, state):
    """Operations spatial overlay (phase lines, boundaries, axes of advance,
    NAIs / TAIs, NOGO / SLOWGO regions) for the current AO.

    Priority chain (per audit 2026-05-20):
        1. OPORD-ingested overlay — populated by
           `scenario_ingest.parse_and_apply_opord` writing to geo-agent via
           the master-map sync endpoint. Pulled here from the in-memory
           `scenario.OPORD_OVERLAY` mirror so the intel-agent doesn't have
           to re-fetch from geo-agent on every tool call. Empty until the
           analyst triggers the OPORD re-parse from the operations map UI.
        2. Cougaar `World.Informations.SpatialControl` family — verified
           NTC California operation (control measure names: LEXINGTON,
           KENTUCKY, FRANKFORT, 2-69 AR, 3-15 IN) AND every record's
           `data__coordinates` is null. Wrong scenario AND no geometry —
           we NEVER return Cougaar SpatialControl from this tool.

    Returns an empty `control_measures` list when no OPORD overlay has been
    ingested, plus a `_note` flag the writer can surface in the product.
    """
    t0 = time.monotonic()
    # Pull from the in-memory mirror that scenario_ingest writes to. Until
    # the OPORD parse runs in this process the attr is absent — getattr
    # default keeps the call inexpensive.
    overlay = getattr(scenario, "OPORD_OVERLAY", None) or {}
    control_measures = list(overlay.get("control_measures") or [])
    source = "scenario_authored" if control_measures else "no_overlay"
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    _record_cougaar_call("get_spatial_overlay", called=False,
                         row_count=len(control_measures),
                         supplemented_from_fixture=True,
                         elapsed_ms=elapsed_ms, source=source)
    note = (
        "No IDX 26 operations overlay loaded; trigger the green refresh "
        "icon on the operations map to parse OPORD/FRAGOs via Gemma4 and "
        "populate the overlay. Cougaar SpatialControl rows (56 total) are "
        "the wrong scenario (NTC California) AND carry no geometry — never "
        "returned from this tool."
        if not control_measures else
        "OPORD-derived overlay (last ingested via /api/scenario/"
        "regenerate-from-opord). Phase lines, NAIs, axes of advance, "
        "boundaries — all cited back to source OPORD/FRAGO fragments."
    )
    return {
        "control_measures": control_measures,
        "count": len(control_measures),
        "ao": _DEFAULT_AO_LABEL,
        "_supplemented_from_fixture": True,
        "_source": source,
        "_note": note,
    }


def tool_get_environmental(args, state):
    date = args.get("date") or scenario.WEATHER.get("current_dtg", "281200Z")
    illum_map = scenario.WEATHER.get("light_illumination", {})
    astro = illum_map.get(date) or (next(iter(illum_map.values())) if illum_map else None)
    _record_cougaar_call("get_environmental", called=False, row_count=1,
                         supplemented_from_fixture=True, elapsed_ms=0,
                         source="scenario_authored")
    # The scenario.WEATHER fixture for IDX 26 is climatology-modeled (NOT a live SWO
    # forecast). `weather.source` carries the citation for the underlying
    # climatology dataset; `weather.swo_attribution` is the fixture SWO
    # product id (the writer cites both — product id in header.swo_attribution,
    # climatology source in _provenance — see WEM template hard rules).
    return {
        "date": date,
        "weather": scenario.WEATHER,
        "astronomical": astro,
        "_source": "scenario_authored",
        "data_source": scenario.WEATHER.get("source", ""),
        "data_source_type": scenario.WEATHER.get("source_type", ""),
        "source_note": (
            f"Pre-event climatology / planning weather for {_DEFAULT_AO_LABEL}; "
            "not live observation. Underlying values from " +
            (scenario.WEATHER.get("source") or "regional climatology") + "."
        ),
    }


# Geo-agent overlay `kind` values that carry terrain semantics. These are
# the IDX 26 paint-layer kinds (CATEGORY_IBL_OPERATIONAL in geo-agent's
# constants/overlays.py). Mapped to the scenario.TERRAIN sub-layers they
# correspond to so the merge step knows which scenario layer each geo-agent
# overlay augments / overrides.
#
# Mapping rationale:
#   avenue_of_approach           → scenario.TERRAIN.avenues_of_approach
#   mobility_corridor            → scenario.TERRAIN.avenues_of_approach (lower echelon)
#   mobility_go                  → scenario.TERRAIN.mobility_classifications (unrestricted)
#   restricted_terrain           → scenario.TERRAIN.mobility_classifications (restricted/SLOW-GO)
#   severely_restricted_terrain  → scenario.TERRAIN.mobility_classifications (severely_restricted/NO-GO)
#   key_terrain                  → scenario.TERRAIN.key_terrain
#   obstacle_point               → scenario.TERRAIN.obstacles
#   obstacle_belt                → scenario.TERRAIN.obstacles
#   hydrology_obstacle           → scenario.TERRAIN.obstacles (natural water)
_GEO_TERRAIN_KIND_TO_LAYER = {
    "avenue_of_approach":          "avenues_of_approach",
    "mobility_corridor":           "avenues_of_approach",
    "mobility_go":                 "mobility_classifications",
    "restricted_terrain":          "mobility_classifications",
    "severely_restricted_terrain": "mobility_classifications",
    "key_terrain":                 "key_terrain",
    "obstacle_point":              "obstacles",
    "obstacle_belt":               "obstacles",
    "hydrology_obstacle":          "obstacles",
}


def _fetch_geo_agent_terrain_layers(map_uuid: str) -> dict:
    """Pull terrain-classified overlays from the geo-agent master map and
    group them by scenario.TERRAIN sub-layer.

    Endpoint: `GET /api/v1/map/state?map_id=<UUID>&kinds=<comma-separated>`
    Returns the standard FeatureCollection-style payload (entities + overlays);
    we read overlays only — IDX 26 terrain features are stored as overlays
    via the /maps/{id}/sync endpoint from cougaar_adapters + scenario_ingest.

    Returns:
      {
        "avenues_of_approach":      [{id, name, kind, geom, attrs}, ...],
        "mobility_classifications": [...],
        "key_terrain":              [...],
        "obstacles":                [...],
        "_geo_agent_map_id":        "<uuid>",
        "_geo_agent_fetched":       True,
      }
    On any failure returns {"_geo_agent_fetched": False, "_error": "..."}.
    """
    out: dict = {
        "avenues_of_approach": [],
        "mobility_classifications": [],
        "key_terrain": [],
        "obstacles": [],
        "_geo_agent_map_id": map_uuid,
        "_geo_agent_fetched": False,
    }
    kinds = list(_GEO_TERRAIN_KIND_TO_LAYER.keys())
    try:
        r = requests.get(
            f"{_GEO_AGENT_API_BASE}/map/state",
            params={"map_id": map_uuid, "kinds": ",".join(kinds)},
            timeout=10,
        )
    except Exception as e:
        out["_error"] = f"transport: {type(e).__name__}: {e}"
        return out
    if not r.ok:
        out["_error"] = f"HTTP {r.status_code}"
        return out
    try:
        state_payload = r.json()
    except Exception as e:
        out["_error"] = f"json decode: {e}"
        return out
    overlay_features = (
        (state_payload.get("overlays") or {}).get("features")
        or state_payload.get("overlays")
        or []
    )
    if not isinstance(overlay_features, list):
        out["_error"] = "overlays not a list"
        return out
    for f in overlay_features:
        if not isinstance(f, dict):
            continue
        props = f.get("properties") or {}
        kind = props.get("kind") or ""
        layer = _GEO_TERRAIN_KIND_TO_LAYER.get(kind)
        if not layer:
            continue
        out[layer].append({
            "id": f.get("id"),
            "kind": kind,
            "name": props.get("name") or props.get("description") or "",
            "geom": f.get("geometry"),
            "attrs": {k: v for k, v in props.items()
                       if k not in ("kind", "name")},
            "source": props.get("source") or "geo-agent",
        })
    out["_geo_agent_fetched"] = True
    out["_geo_agent_overlay_count"] = sum(
        len(out[k]) for k in (
            "avenues_of_approach", "mobility_classifications",
            "key_terrain", "obstacles",
        )
    )
    return out


def _merge_terrain_scenario_with_geo_agent(scenario_terrain: dict,
                                              geo_terrain: dict) -> dict:
    """Merge `scenario.TERRAIN` (authored fixture) with geo-agent overlays.

    Strategy:
      - For each sub-layer in geo_terrain, if geo-agent returned overlays we
        prefer those (geo-agent is the live operational source).
        scenario.TERRAIN stays as a FALLBACK reference for layers the
        geo-agent doesn't have.
      - Both are surfaced in the output: published consumers (writer LLM)
        see `geo_agent_<layer>` for the live data and the original
        `<layer>` for the fixture. The writer can cite whichever is most
        specific — but the doctrinal preference is geo-agent.
      - Layers that geo-agent doesn't carry (observation_and_fields_of_fire,
        cover_and_concealment) are unchanged from scenario.TERRAIN.

    The merged dict is the same shape the legacy scenario-only payload
    returned plus the `geo_agent_<layer>` siblings and `_provenance` hints.
    """
    merged = dict(scenario_terrain)
    if not geo_terrain.get("_geo_agent_fetched"):
        merged["_geo_agent_fetched"] = False
        merged["_geo_agent_error"] = geo_terrain.get("_error", "unknown")
        return merged
    for layer in ("avenues_of_approach", "mobility_classifications",
                  "key_terrain", "obstacles"):
        overlays = geo_terrain.get(layer) or []
        if overlays:
            merged[f"geo_agent_{layer}"] = overlays
    merged["_geo_agent_fetched"] = True
    merged["_geo_agent_map_id"] = geo_terrain.get("_geo_agent_map_id")
    merged["_geo_agent_overlay_count"] = geo_terrain.get("_geo_agent_overlay_count", 0)
    return merged


def tool_get_terrain(args, state):
    layer = args.get("layer")
    # Geo-agent integration: pull terrain overlays preloaded for the master
    # map (IDX 26 AO BALTIC RESTORE). Cougaar adapters + scenario_ingest push
    # mobility classifications, key terrain, obstacles, avenues of
    # approach into the geo-agent master map at /maps/{id}/sync. The
    # geo-agent is the AUTHORITATIVE source for these layers;
    # scenario.TERRAIN is the fallback for layers the geo-agent doesn't
    # carry.
    map_uuid = _project_map_uuid(getattr(state, "project_id", ""))
    geo_terrain = _fetch_geo_agent_terrain_layers(map_uuid)
    merged = _merge_terrain_scenario_with_geo_agent(scenario.TERRAIN, geo_terrain)
    _record_cougaar_call("get_terrain", called=bool(geo_terrain.get("_geo_agent_fetched")),
                         row_count=geo_terrain.get("_geo_agent_overlay_count", 1) or 1,
                         supplemented_from_fixture=True, elapsed_ms=0,
                         source=("geo_agent_plus_scenario"
                                 if geo_terrain.get("_geo_agent_fetched")
                                 else "scenario_authored"))
    source_note = (
        f"Geo-agent terrain layers merged with scenario-authored fallback for "
        f"{_DEFAULT_AO_LABEL}. Geo-agent overlays: "
        f"{geo_terrain.get('_geo_agent_overlay_count', 0)} "
        f"(fetched={'ok' if geo_terrain.get('_geo_agent_fetched') else 'failed: ' + str(geo_terrain.get('_error'))}). "
        f"Cite per-row provenance as `get_terrain.<layer>.<feature_name_or_id>`."
    )
    if layer:
        # Per-layer query — prefer geo-agent overlays for that layer if
        # they exist, otherwise fall back to scenario.TERRAIN.
        geo_layer_key = f"geo_agent_{layer}"
        if geo_layer_key in merged and merged[geo_layer_key]:
            return {
                "layer": layer,
                "data": merged[geo_layer_key],
                "fallback_scenario_data": scenario.TERRAIN.get(layer),
                "_source": "geo_agent",
                "source_note": source_note,
            }
        if layer in scenario.TERRAIN:
            return {
                "layer": layer,
                "data": scenario.TERRAIN[layer],
                "_source": "scenario_authored",
                "source_note": source_note,
            }
        return {"error": f"unknown layer: {layer}",
                "available_layers": [k for k in scenario.TERRAIN.keys() if k not in ("aoi", "summary")]}
    return {**merged,
            "_source": ("geo_agent_plus_scenario"
                         if merged.get("_geo_agent_fetched")
                         else "scenario_authored"),
            "source_note": source_note}


def tool_get_civil(args, state):
    layer = args.get("layer")
    # Merge static scenario.KG.civil with scenario_ingest's OPORD-derived civil entries.
    # The OPORD's Civil Considerations annex names additional structures
    # (Tartu power, Valga rail), populations (displaced, sympathetic), NGOs
    # (ICRC) etc. that Gemma4 extracts into `scenario.KG.opord_derived_civil`. CCA /
    # NSL / RTL / OED / SITREP Line 15 / IRE civil paragraph all reason over
    # the full civil picture, so merging both is doctrinally correct.
    rows = list(scenario.KG.get("civil") or [])
    derived = scenario.KG.get("opord_derived_civil") or []
    for d in derived:
        if not isinstance(d, dict):
            continue
        rows.append({
            "id": d.get("id") or d.get("name") or "",
            "name": d.get("name") or "",
            "ascope": d.get("ascope"),
            "pmesii": d.get("pmesii"),
            "type": d.get("type"),
            "mgrs": d.get("mgrs"),
            "status": d.get("status"),
            "details": d.get("details"),
            "_source": "opord_derived",
            "_source_doc_tag": d.get("source_doc_tag"),
            "_source_fragment": d.get("source_fragment"),
        })
    if layer:
        rows = [r for r in rows if r.get("ascope") == layer or r.get("pmesii") == layer]
    _record_cougaar_call("get_civil", called=False, row_count=len(rows),
                         supplemented_from_fixture=True, elapsed_ms=0,
                         source="scenario_authored")
    return {
        "civil_entities": rows,
        "count": len(rows),
        "layer_filter": layer,
        "_source": "scenario_authored",
        "_opord_derived_count": len(derived),
        "source_note": (
            "Civil considerations — static scenario.KG.civil entries (NSL-protected "
            "infrastructure, civilian population centers) merged with "
            "scenario_ingest's OPORD-derived civil entries (Gemma4-extracted "
            "ASCOPE/PMESII rows from the latest OPORD/FRAGO). Live OSINT "
            "injects populate this layer during Crisis phase."
        ),
    }


def tool_get_phases(args, state):
    """Phased concept of operations — written by `scenario_ingest.parse_and_apply_opord`
    from the OPORD + every FRAGO. Each phase entry carries id, name, start_dtg,
    end_dtg, concept, decisive_point, phase_objectives, plus source provenance.

    Doctrinally consumed by:
      - SITREP (phase reference in Line 9 situation overview)
      - GRINTSUM (phase block)
      - Intelligence Running Estimate (phase-keyed running narrative)
      - Intelligence Estimate (phase-aware framing)
      - DST / Decision Support Template (decision points anchored to phases)
      - Event Template (NAIs tied to phase windows)
      - Information Collection Matrix / ICSM (collection windows per phase)
      - INTSUM (current-phase situation framing)

    Returns the ordered phase list with provenance preserved so writers can
    cite the source OPORD/FRAGO fragment.
    """
    phases = list(scenario.KG.get("opord_derived_phases") or [])
    t0 = time.monotonic()
    _record_cougaar_call("get_phases", called=False, row_count=len(phases),
                         supplemented_from_fixture=True,
                         elapsed_ms=int((time.monotonic() - t0) * 1000),
                         source="scenario_authored")
    return {
        "phases": phases,
        "count": len(phases),
        "_source": "scenario_authored",
        "_note": (
            "OPORD-derived phase concept (Gemma4-extracted via "
            "scenario_ingest). Empty until the analyst triggers the green "
            "refresh icon on the operations map. Each phase carries "
            "start_dtg/end_dtg/concept/decisive_point/phase_objectives + "
            "source_doc_tag/source_fragment for provenance."
        ),
    }


def _project_docs_index(state):
    """Build the catalog visible to the operations expert + intel agent.

    Two namespaces (Req 3):
      - projectdocs    — static seed catalog (scenario.PROJECT_DOCS) + every analyst's
                          PUBLISHED artifacts. Visible to all analysts on the
                          project.
      - privatedocs[user_id] — the CURRENT analyst's draft artifacts. Other
                          analysts' drafts are invisible.

    Ownership / status live in artifact.structured_data["header"] — stamped by
    _build_skeleton at emit time, flipped by tool_publish_artifact / the
    publish endpoint. Legacy artifacts without these fields are treated as
    published (everyone sees them) — a conservative migration default that
    matches pre-Req-3 behavior.
    """
    catalog = {}
    # 1. Static seed catalog (OPORD / OOB / FRAGOs / Road-to-War / Annex L).
    for tag, doc in scenario.PROJECT_DOCS.items():
        catalog[tag] = doc

    current_user_id = state.user_id if state is not None else ""
    reg = state.registry_dict() if state else {}
    for atype, items in reg.items():
        for art in items:
            aid = art.get("artifact_id", "")
            if not aid:
                continue
            header = (art.get("structured_data") or {}).get("header") or {}
            status = header.get("status", "")
            owner = header.get("owner_user_id", "")
            # Visibility gate:
            #   published     → always visible (projectdocs)
            #   draft + owner → visible only to the owner (privatedocs)
            #   draft + other → hidden
            #   legacy ("")   → visible (pre-Req-3 compat)
            if status == "draft" and owner and owner != current_user_id:
                continue
            tag = f"{atype}_{aid}"
            catalog[tag] = {
                "title": f"{atype} {aid}",
                "body": art.get("structured_data", {}),
                "type": atype,
                "dtg": art.get("dtg", ""),
                "artifact_id": aid,
                "status": status or "published",
                "owner_user_id": owner,
                "source": "pytho-published" if status != "draft" else "pytho-draft",
            }
    return catalog


def tool_list_project_docs(args, state):
    flt = args.get("filter") or {}
    items = []
    catalog = _project_docs_index(state)
    for tag, doc in catalog.items():
        title = doc.get("title", tag) if isinstance(doc, dict) else tag
        item = {"tag": tag, "title": title}
        if isinstance(doc, dict) and doc.get("type"):
            item["type"] = doc["type"]
        elif tag.startswith("FRAGO"):
            item["type"] = "FRAGO"
        elif tag == "OPORD":
            item["type"] = "OPORD"
        elif tag == "OOB":
            item["type"] = "OOB"
        elif tag == "ANNEX_L_INTEL":
            item["type"] = "ANNEX"
        elif tag == "RoadToWar":
            item["type"] = "RoadToWar"
        else:
            item["type"] = "other"
        if isinstance(doc, dict) and doc.get("dtg"):
            item["dtg"] = doc["dtg"]
        if isinstance(doc, dict) and doc.get("artifact_id"):
            item["artifact_id"] = doc["artifact_id"]
        if flt and item.get("type") != flt.get("type"):
            continue
        items.append(item)
    return {"docs": items, "count": len(items)}


def tool_get_project_doc(args, state):
    tag = args.get("tag", "")
    catalog = _project_docs_index(state)
    if tag in catalog:
        return catalog[tag]
    norm = tag.replace(" ", "_").upper()
    for k, v in catalog.items():
        if k.replace(" ", "_").upper() == norm or norm in k.replace(" ", "_").upper():
            return v
    return {"error": "unknown tag", "available_tags": list(catalog.keys())}


# Where rendered PNGs land. Matches main.py UPLOAD_DIR + the StaticFiles mount
# at /files. Created on first render so a fresh checkout doesn't 500.
_UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))

# Master-map resolution lives near _DEFAULT_AO_LABEL at the top of the
# module (_project_map_uuid / active_project_ids) so callers that fire
# before any tool_render_map (e.g. the recent-intel poll callback
# `on_poll_sync_oob`) can resolve their project's map.

# Stable namespace for deterministic per-product map UUIDs (uuid5). Treat
# as a constant — changing this invalidates every existing per-product
# geo-agent map mapping.
_MAP_UUID_NAMESPACE = uuid.UUID('5b3c7d2e-1f4a-4e8c-9d6b-7a8e3f2c1b9d')


def map_uuid_for(product_type: str, artifact_id: str,
                  field_name: str = "map") -> str:
    """Deterministic per-product map UUID. Each map paragraph in each
    product instance gets its own geo-agent map identifier, derived as
    uuid5(NAMESPACE, "<product_type>/<artifact_id>/<field_name>").

    Stable across regenerations of the same artifact_id (so editor edits
    survive a re-render). Different artifact_ids (different sessions) get
    different UUIDs (so analysts don't collide). Multi-map products
    (GRINTSUM: overview_map + zoom_inset_map) get one UUID per field.
    """
    name = f"{(product_type or '').strip()}/{(artifact_id or '').strip()}/{(field_name or 'map').strip()}"
    return str(uuid.uuid5(_MAP_UUID_NAMESPACE, name))

# Where the geo-agent service lives. geo-agent is a STANDALONE service
# reached over its host-published port (9845) — never a shared docker
# network. GEO_API_URL is the ONE project-wide knob (base URL, no path);
# every consumer derives its endpoint from it.
_GEO_AGENT_API_BASE = (
    os.environ.get("GEO_API_URL", "http://host.docker.internal:9845").rstrip("/")
    + "/api/v1"
)

# Per-(map, route-spec-id) cache of terrain-snapped avenue / corridor paths.
# The geo-agent A* planner runs on a tile graph weighted by slope, land
# cover, weather and threat costs — single calls take several seconds.
# Caching by map + route-spec id means we plan once per session and reuse
# across renders. Reset when the scenario fixture is reloaded (manual
# restart).
_AVENUE_TERRAIN_PATH_CACHE: dict[str, list[list[float]]] = {}


def upsert_geo_agent_map_record(
    *,
    map_uuid: str,
    product_type: str,
    field_name: str,
    bbox: list,
    schema: dict,
) -> None:
    """Create/update the per-product map record on the geo-agent with the
    schema-derived attrs the embedded EditMap overlay reads at mount time.

    Writes:
      - name / bbox_* / center_* / zoom — viewport
      - attrs.product_type — for editor-mode product gating
      - attrs.base_map_style — schema.map_basemap (GeoAgentProvider editor basemap)
      - attrs.allowed_kinds — schema.map_overlay_kinds (loadMapState filter)
      - attrs.basemap_layers / doctrinal_layers — schema visibility presets

    The geo-agent's PUT /maps/{id} merges attrs (preserving other keys), so
    analyst-set fields outside this allowlist survive.

    Called from tool_render_map AND from product_autodraft.refire_product_maps
    so map_only iteration also keeps the editor in sync with the latest
    schema state.
    """
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return
    try:
        _w, _s, _e, _n = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return
    attrs: dict[str, Any] = {"product_type": product_type or ""}
    _bm = schema.get("map_basemap")
    if _bm:
        attrs["base_map_style"] = _bm
    _kinds = schema.get("map_overlay_kinds")
    if isinstance(_kinds, (list, tuple)) and _kinds:
        attrs["allowed_kinds"] = list(_kinds)
    _bm_vis = schema.get("map_basemap_layer_visibility")
    if isinstance(_bm_vis, (list, tuple)) and _bm_vis:
        attrs["basemap_layers"] = list(_bm_vis)
    _doc_vis = schema.get("map_doctrinal_layer_visibility")
    if isinstance(_doc_vis, (list, tuple)) and _doc_vis:
        attrs["doctrinal_layers"] = list(_doc_vis)
    name = f"{product_type or 'Map'} — {field_name}"
    try:
        r = requests.put(
            f"{_GEO_AGENT_API_BASE}/maps/{map_uuid}",
            params={"user_id": "_autodraft"},
            json={
                "name": name,
                "bbox_west": _w, "bbox_south": _s,
                "bbox_east": _e, "bbox_north": _n,
                "center_lng": (_w + _e) / 2,
                "center_lat": (_s + _n) / 2,
                "zoom": 8,
                "attrs": attrs,
            },
            timeout=10,
        )
        if r.status_code >= 400:
            log.warning(
                "upsert_geo_agent_map_record: PUT failed map=%s status=%s body=%s",
                map_uuid, r.status_code, r.text[:200],
            )
    except Exception as e:
        log.warning("upsert_geo_agent_map_record: raised map=%s err=%s", map_uuid, e)


def _plan_terrain_route(
    map_id: str, route_spec: dict, p_from: tuple, p_to: tuple,
) -> dict | None:
    """Run the geo-agent A* terrain planner for one terrain-routed segment.

    `route_spec` carries metadata about the thing being planned — typically
    an avenue of approach (built either from scenario.TERRAIN.avenues_of_
    approach or from the MCOO doctrinal-discovery enumeration) but the
    function is shape-agnostic: any dict with `id`, `name`, `source`,
    `from`, `to`, `echelon` will plan. Sub-corridor stubs inside a larger
    avenue also call through here (writer extractor's per_corridor_terrain_
    plan branch builds planner_route_spec dicts on the fly).

    Returns {"coords": [[lon, lat], ...], "analysis": {...}} on success —
    `coords` is the routed centerline, `analysis` carries the planner's
    per-path metadata (distance_mi, max_slope_pct, threat_exposure_mi,
    corridor_width_m + corridor_widths_sampled_m for echelon derivation,
    etc.). Returns None on any planner failure — caller falls back to the
    straight 2-point line so the route still renders.

    Cached per (map_id, route_spec['id'], source, endpoints).
    """
    # Cache key must capture the endpoints AND the source — writer-emitted
    # avenues legitimately reuse scenario IDs ("AA1"/"AA2"/"AA3") with
    # different endpoints. Hashing only (map_id, id) returns the stale
    # scenario path for a writer-emit with the same label.
    cache_key = (
        f"{map_id}:{route_spec.get('id') or route_spec.get('name')}:"
        f"{route_spec.get('source') or 'scenario'}:"
        f"{route_spec.get('from') or ''}->{route_spec.get('to') or ''}"
    )
    if cache_key in _AVENUE_TERRAIN_PATH_CACHE:
        return _AVENUE_TERRAIN_PATH_CACHE[cache_key]

    # Echelon-driven slope tolerance. Brigade-on-roads tolerates ~25 %
    # max grade; battalion off-road can push ~35 %; company recon up to
    # ~50 %. Bigger formations also avoid restricted terrain harder via
    # threat_weight. These are ATP 2-01.3 mobility-class defaults, not
    # vehicle-specific limits.
    echelon = (route_spec.get("echelon") or "").lower()
    if "div" in echelon or "brigade" in echelon or "bde" in echelon:
        max_slope = 25.0
    elif "bn" in echelon or "battalion" in echelon:
        max_slope = 35.0
    else:
        max_slope = 50.0

    route_label = route_spec.get("id") or route_spec.get("name") or "unknown"
    body = {
        "name": f"route-{route_label}",
        "start_geom": {"type": "Point", "coordinates": [p_from[1], p_from[0]]},
        "end_geom": {"type": "Point", "coordinates": [p_to[1], p_to[0]]},
        # mixed — tile graph (slope + land cover + threats + no-go) joined
        # with the road graph via bridge edges. Roads load from the offline
        # OSM JSON dump (geo-agent's roads._load_offline_roads, gated by
        # OFFLINE_MODE=true). Mixed mode is doctrinally correct for avenues
        # of approach: formations use the road network where available and
        # traverse off-road through corridors of trafficable terrain
        # elsewhere. Threat avenues anchored on Donovian HQ → friendly LD
        # rely on this because the off_road-only graph can't bridge water/
        # lake pinch points that road bridges cross.
        "route_mode": "mixed",
        "attrs": {
            "slope_weight": 1.0,
            "threat_weight": 1.0,
            "concealment_weight": 0.3,
            "max_slope_pct": max_slope,
            "rate_of_advance": 0,
        },
    }
    try:
        r = requests.post(
            f"{_GEO_AGENT_API_BASE}/routes/plan",
            params={"map_id": map_id},
            json=body,
            timeout=60,
        )
    except Exception as exc:
        log.warning("Route %s plan transport failed: %s", route_label, exc)
        return None
    if r.status_code >= 400:
        log.warning(
            "Route %s plan HTTP %s: %s",
            route_label, r.status_code, r.text[:300],
        )
        return None
    try:
        result = r.json()
    except Exception as exc:
        log.warning("Route %s plan response decode: %s", route_label, exc)
        return None
    paths = result.get("paths") or []
    if not paths:
        log.warning("Route %s plan returned zero paths", route_label)
        return None
    # Pick the selected path; fall back to rank=1.
    best = next((p for p in paths if p.get("selected")), None)
    if best is None:
        best = sorted(paths, key=lambda p: p.get("rank") or 99)[0]
    geom = best.get("geom") or {}
    coords = geom.get("coordinates") or []
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    analysis = best.get("analysis") or {}
    plan_result = {"coords": coords, "analysis": analysis}
    _AVENUE_TERRAIN_PATH_CACHE[cache_key] = plan_result
    # PROVENANCE RULE (2026-07-06): auto-generated routes must never
    # enter the map's paintable route stream — geo-agent's saved-route
    # painter draws every route in the stream, on the live map AND in
    # product renders, and only ANALYST-authored routes belong there.
    # /routes/plan persists the route+paths server-side; this caller
    # only needs the winning path's coords+analysis, so delete the
    # stored route immediately after extraction. Failure is loud but
    # non-fatal (the avenue geometry itself is already in hand).
    route_id = result.get("id")
    if route_id:
        try:
            dr = requests.delete(
                f"{_GEO_AGENT_API_BASE}/routes/{route_id}", timeout=15)
            if dr.status_code >= 400:
                log.warning(
                    "Route %s cleanup DELETE HTTP %s — pipeline route row "
                    "left on map %s (will paint until removed)",
                    route_label, dr.status_code, map_id,
                )
        except Exception as exc:
            log.warning(
                "Route %s cleanup DELETE failed: %s — pipeline route row "
                "left on map %s (will paint until removed)",
                route_label, exc, map_id,
            )
    log.info(
        "Route %s plan: %d waypoints (rank=%s) corridor_width_m=%s",
        route_label, len(coords), best.get("rank"),
        analysis.get("corridor_width_m"),
    )
    return plan_result


def _classify_range_fan_subkind(label: str) -> str | None:
    """Classify a range fan by weapon family for renderer styling +
    sub-toggle filtering. Mirrors drawRangeFan._rangeFanStyle's classes."""
    if not label:
        return None
    u = label.upper()
    if any(k in u for k in ("ISKANDER", "KALIBR", "SS-21", "SCUD", "TOCHKA")):
        return "long_range"
    if any(k in u for k in ("SA-", "S-300", "S-400", "PATRIOT", "STINGER", "HAWK",
                              "NASAMS", "IGLA", "VERBA", "TOR", "TUNGUSKA",
                              "PANTSIR", "STRELA", "ADA")):
        return "ada"
    if any(k in u for k in ("BM-21", "BM-27", "BM-30", "GRAD", "SMERCH", "URAGAN",
                              "MRL", "MLRS", "FA-BDE", "HOW ", "2S", "TORNADO",
                              "120MM", "152MM", "MORTAR")):
        return "indirect_fire"
    if any(k in u for k in ("KONKURS", "KORNET", "FAGOT", "METIS", "AT-",
                              "9M", "ATGM")):
        return "antitank"
    return None


def _push_scenario_terrain_to_geo_agent(
    map_id: str,
    *,
    avenue_of_approach_source: str = "all",
    show_friendly_axis_of_advance: bool = True,
    engagement_area_perspective: str = "all",
    coa_engaged_friendly_avenue_ids: Optional[set[str]] = None,
    show_friendly_phase_lines: bool = True,
    show_friendly_boundaries: bool = True,
) -> dict:
    """Materialize scenario.TERRAIN avenues_of_approach + key_terrain +
    obstacles into the geo-agent master map so the doctrinal SVG paint
    (drawAvenueChevron, key-terrain glyph, obstacle-point symbol) has
    features to render.

    Without this push the MCOO renders with NO chevrons over the fixture
    avenues of approach (AA1 Axis HAWK, AA2 Axis FALCON, AA3 Western
    ridge) — the only arrow-shaped overlays visible are the OPORD-derived
    axis_of_advance direction-of-attack arrows, which are a separate
    doctrinal concept (the avenues are the terrain analysis, axes are the
    maneuver plan; both
    should render).

    POSTs to `/maps/{id}/sync?source=scenario_terrain` so this batch lives
    next to (and is replaced independently from) the cougaar-sourced
    live entities. Idempotent — every call atomically replaces the
    scenario_terrain layer.

    Schema-driven per-product gates (forwarded by tool_render_map from
    the active product schema):
      avenue_of_approach_source:
        "all"  — push every scenario.TERRAIN avenue of approach (MCOO, Threat
                 Overlay default).
        "none" — skip the AA push entirely (SITEMP — the per-COA threat
                 scheme of maneuver is depicted by the hostile axis-of-
                 advance arrows from the writer's axis_role_assignments
                 extractor; rendering the avenue chevron underneath
                 would duplicate the corridor symbol AND can paint with
                 the fixture-canonical arrowhead in the WRONG direction
                 for COAs that use direction=reverse).
      show_friendly_axis_of_advance:
        True  — push scenario.OPERATIONS.axes_of_advance (the OPORD
                friendly maneuver plan: AXIS BLUE / RED / WHITE).
        False — skip them entirely (SITEMP — the SITEMP is the THREAT's
                plan, not the friendly plan; the friendly maneuver
                scheme does NOT appear here).
      engagement_area_perspective:
        "all"      — push every scenario.TERRAIN.engagement_areas_catalog entry
                     (MCOO / Threat Overlay default — both friendly
                     prospective + threat-perspective EAs are terrain
                     analysis artifacts).
        "threat"   — push only side=threat entries (SITEMP — the
                     threat's fires plan; friendly prospective EAs do
                     not belong on a threat-perspective product).
        "friendly" — push only side=friendly entries (friendly DSM /
                     OPORD overlay — blue prospective fires plan; the
                     threat's plan does not belong on a friendly
                     product).
      coa_engaged_friendly_avenue_ids:
        None     — no per-COA filtering (default); every EA passing the
                   perspective gate paints.
        {AA1, …} — paint ONLY EAs whose `linked_avenue_id` is in this
                   set. SITEMP passes the set of friendly-avenue ids
                   the writer's axis_role_assignments engage for THIS
                   specific COA (derived from each axis row's
                   referenced catalog entry: friendly-side avenues
                   contribute their own id; threat-side counter-attack
                   avenues contribute their `targets_friendly_avenue_id`).
                   COA-2 ("Full Positional Defense") with one
                   counter-attack on AA-THR-623CTRATK-RONGU-FLANK ⇒
                   {AA2}, so only EA-HARGLA-CENTRAL paints. The other
                   two threat-side EAs (VALGA NORTH, ANTSLA SOUTH) are
                   for friendly avenues this COA does not actively
                   engage in its scheme of maneuver and would mislead
                   the analyst about which corridors are in play.
    """
    base = _GEO_AGENT_API_BASE
    from utils.coords import mgrs_to_latlon
    overlays: list[dict] = []

    # Avenues of approach: ask the geo-agent A* planner to interpolate a
    # multi-point polyline that follows valley bottoms / roads between
    # the from→to MGRS endpoints. Falls back to a 2-point straight line
    # if planning fails, so the avenue still renders (just as a straight
    # chevron instead of a terrain-snapped one).
    for avenue in (scenario.TERRAIN.get("avenues_of_approach") or []):
        if not isinstance(avenue, dict):
            continue
        a, b = avenue.get("from"), avenue.get("to")
        p_from = mgrs_to_latlon(a) if a else None
        p_to = mgrs_to_latlon(b) if b else None
        if not (p_from and p_to):
            continue
        plan = _plan_terrain_route(map_id, avenue, p_from, p_to)
        coords = plan["coords"] if plan else None
        snapped = bool(coords and len(coords) > 2)
        if not coords:
            # GeoJSON order is [lon, lat]; mgrs_to_latlon returns (lat, lon).
            coords = [
                [p_from[1], p_from[0]],
                [p_to[1], p_to[0]],
            ]
        overlays.append({
            "kind": "avenue_of_approach",
            "name": avenue.get("name") or avenue.get("id") or "AA",
            "geom": {
                "type": "LineString",
                "coordinates": coords,
            },
            "attrs": {
                "source": "scenario_terrain",
                "avenue_of_approach_id": avenue.get("id"),
                "echelon": avenue.get("echelon"),
                "width_km": avenue.get("width_km"),
                "route": avenue.get("route"),
                "from_mgrs": a,
                "to_mgrs": b,
                "terrain_snapped": snapped,
                "waypoint_count": len(coords),
            },
        })

    # Key terrain: Point at MGRS, glyph rendered by drawKeyTerrain.
    for kt in (scenario.TERRAIN.get("key_terrain") or []):
        if not isinstance(kt, dict):
            continue
        m = kt.get("mgrs")
        p = mgrs_to_latlon(m) if m else None
        if not p:
            continue
        overlays.append({
            "kind": "key_terrain",
            "name": kt.get("name") or "K",
            "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
            "attrs": {
                "source": "scenario_terrain",
                "decisive": bool(kt.get("decisive")),
                "value": kt.get("value"),
                "mgrs": m,
            },
        })

    # Obstacles: Point at MGRS, glyph rendered by drawObstaclePoint.
    for ob in (scenario.TERRAIN.get("obstacles") or []):
        if not isinstance(ob, dict):
            continue
        m = ob.get("mgrs")
        p = mgrs_to_latlon(m) if m else None
        if not p:
            continue
        overlays.append({
            "kind": "obstacle_point",
            "name": ob.get("name") or "obstacle",
            "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
            "attrs": {
                "source": "scenario_terrain",
                "obstacle_type": ob.get("type"),
                "tactical_effect": ob.get("tactical_effect"),
                "mobility_class": ob.get("mobility_class"),
                "details": ob.get("details"),
                "mgrs": m,
            },
        })

    # Range fans: Point at MGRS with bands carried as attrs. The geo-
    # agent SVG layer reads attrs.bands (list of {radius_m, label?}) OR
    # attrs.radius_m for single-ring fans, draws concentric red rings
    # via drawRangeFan. Per ATP 2-01.3 Fig 6-3 ("3,500 meters" rings).
    # Range fans are doctrinally per-product (SITEMP / Threat Template /
    # Threat Overlay depict unit weapon envelopes). They do NOT belong
    # in the scenario_terrain bucket which is the MCOO basemap layer
    # (no analyst-facing toggle). Tag them under the Threat Model
    # source so the "Threat Template" sidebar toggle shows them —
    # that's the per-product overlay the IPB Step-3 doctrinal threat
    # picture publishes, and it's the closest fit for the
    # scenario-fixture range fans (which aren't COA-attributed and so
    # can't go under a per-COA SITEMP bucket).
    for rf in (scenario.TERRAIN.get("range_fans") or []):
        if not isinstance(rf, dict):
            continue
        m = rf.get("mgrs")
        p = mgrs_to_latlon(m) if m else None
        if not p:
            continue
        overlays.append({
            "kind": "range_fan",
            "name": rf.get("name") or rf.get("id") or "RANGE",
            "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
            "attrs": {
                # Source stays as scenario_terrain so the sync REPLACE
                # semantics align cleanly with the URL param's bucket
                # key. Visibility is bypassed in useMapInstance's
                # _filterBySource (range_fan kind is always shown when
                # the layer's sub-toggle isn't set to hide it). Per-
                # product writers (SITEMP/Threat Template) that emit
                # their own range_fans tag them under their own
                # source — those flow with the per-product toggle.
                "source": "scenario_terrain",
                "rf_id": rf.get("id"),
                "bands": rf.get("bands"),
                "radius_m": rf.get("radius_m"),
                "range_label": rf.get("range_label"),
                "rationale": rf.get("rationale"),
                "mgrs": m,
                # subkind classification drives drawRangeFan styling +
                # the SITEMP / Threat Template sub-toggle "Range fan
                # classes" weapon-family checkboxes.
                "subkind": (rf.get("subkind")
                            or _classify_range_fan_subkind(rf.get("name") or rf.get("id") or "")),
            },
        })

    # Threat time-phase lines (H+N forecast PLs) are NO LONGER pre-
    # authored as a scenario fixture (scenario.TERRAIN.enemy_advance_lines was
    # deleted 2026-06-01). H+N lines are PER-COA — they depend on
    # which axes are engaged in THIS specific COA's scheme of maneuver
    # and on the unit rate of march. They are derived at SITEMP render
    # time from the writer's axis_role_assignments + the catalog
    # avenue polylines + a doctrinal mechanized rate (see
    # _derive_threat_time_phase_lines in this module). MCOO / Threat
    # Overlay (non-COA-specific products) carry no time-phase lines at
    # all — there is no canonical "every COA's H+N" line to put on
    # them.

    # Threat objectives: Point at MGRS, glyph rendered by drawThreat-
    # Objective (filled red circle, distinct from friendly OBJ open ring).
    for to_ in (scenario.TERRAIN.get("threat_objectives") or []):
        if not isinstance(to_, dict):
            continue
        m = to_.get("mgrs")
        p = mgrs_to_latlon(m) if m else None
        if not p:
            continue
        overlays.append({
            "kind": "threat_objective",
            "name": to_.get("name") or to_.get("id") or "THR OBJ",
            "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
            "attrs": {
                "source": "scenario_terrain",
                "to_id": to_.get("id"),
                "intent": to_.get("intent"),
                "rationale": to_.get("rationale"),
                "mgrs": m,
            },
        })

    # Engagement areas: scenario.TERRAIN.engagement_areas_catalog (canonical, was
    # mismoved to observation_and_fields_of_fire.likely_engagement_areas
    # — that legacy field is deleted as of this commit). Each entry has
    # id + name + side + polygon + linked_avenue_id + linked_unit +
    # firing_formation + default_intent. Emit Polygon geom when `polygon`
    # is present so the renderer's closed_polygon_with_label primitive
    # paints the kill box. Fall back to Point only if polygon is missing.
    #
    # Per-product perspective gate (engagement_area_perspective):
    #   "all"      — every catalog entry (MCOO / Threat Overlay).
    #   "threat"   — only side=threat (SITEMP — threat's fires plan).
    #   "friendly" — only side=friendly (friendly DSM / OPORD overlay).
    # Caller-driven schema gate, not renderer-side filtering — the
    # renderer paints whatever it is given. Side mismatch is silently
    # skipped here so callers cannot accidentally render the wrong
    # perspective.
    _ea_perspective = (engagement_area_perspective or "all").strip().lower()
    _ea_engaged_set = (
        set(coa_engaged_friendly_avenue_ids)
        if coa_engaged_friendly_avenue_ids is not None else None
    )
    for ea in (scenario.TERRAIN.get("engagement_areas_catalog") or []):
        if not isinstance(ea, dict):
            continue
        ea_side = (ea.get("side") or "").strip().lower()
        if _ea_perspective == "threat" and ea_side != "threat":
            continue
        if _ea_perspective == "friendly" and ea_side != "friendly":
            continue
        # Per-COA filter — SITEMP passes the set of friendly avenue ids
        # the writer's axis_role_assignments engage for THIS specific COA.
        # An EA whose linked_avenue_id is not in that set belongs to a
        # corridor this COA does not actively engage and would mislead
        # the analyst — skip it. When the caller does not pass a set
        # (None), no per-COA filtering applies and every EA passing the
        # perspective gate paints.
        if _ea_engaged_set is not None:
            ea_linked = (ea.get("linked_avenue_id") or "").strip()
            if not ea_linked or ea_linked not in _ea_engaged_set:
                continue
        polygon = ea.get("polygon")
        geom = None
        if isinstance(polygon, list) and len(polygon) >= 4:
            geom = {"type": "Polygon", "coordinates": [polygon]}
        else:
            m = ea.get("mgrs")
            p = mgrs_to_latlon(m) if m else None
            if p:
                geom = {"type": "Point", "coordinates": [p[1], p[0]]}
        if not geom:
            continue
        # Map catalog side → renderer attrs.side token. The renderer's
        # engagement_area overlay paint is a single red-outline primitive
        # today (overlays.py "engagement_area" stroke #b91c1c); the side
        # attr is metadata for legends / future paint branching but does
        # not change the current stroke. Use "hostile" for threat-side
        # and "friendly" for blue-prospective so future branching can
        # split them.
        _ea_attr_side = "hostile" if ea_side == "threat" else (
            "friendly" if ea_side == "friendly" else "unknown"
        )
        overlays.append({
            "kind": "engagement_area",
            "name": ea.get("name") or "EA",
            "geom": geom,
            "attrs": {
                "source": "scenario_terrain",
                "ea_id": ea.get("id"),
                "subkind": ea.get("subkind"),
                "linked_avenue_id": ea.get("linked_avenue_id"),
                "linked_unit": ea.get("linked_unit"),
                "firing_formation": ea.get("firing_formation"),
                "default_intent": ea.get("default_intent"),
                "linked_phase_line": ea.get("linked_phase_line"),
                "rationale": ea.get("rationale"),
                "mgrs": ea.get("mgrs"),
                "side": _ea_attr_side,
            },
        })

    # Avenues of approach — hand-authored fixture in
    # scenario.TERRAIN.avenues_of_approach (friendly + threat directions). Each
    # entry has central_axis_polyline + envelope_polygon +
    # bundle_halfwidth_m so the renderer's drawAvenueChevron primitive
    # can paint the chevron envelope directly without algorithm-emitted
    # geometry.
    #
    # Per-product AA source gate (schema map_avenue_of_approach_source,
    # forwarded by tool_render_map). MCOO / Threat Overlay get every AA
    # ("all"); SITEMP gets none ("none") — the SITEMP renders the
    # threat scheme of maneuver via the writer-emitted hostile axis-of-
    # advance overlays (see _extract_writer_axis_role_assignments).
    # Painting the avenue chevron underneath those overlays duplicates
    # the corridor symbol AND can paint with the fixture-canonical
    # arrowhead in the WRONG direction (COA-3 / COA-4 use AA1 / AA2 /
    # AA3 with direction=reverse, but the chevron always tracks the
    # fixture's central_axis_polyline orientation). Caller-driven
    # schema gate, not renderer-side filtering — the renderer still
    # renders whatever it is given.
    if avenue_of_approach_source == "none":
        _aa_iter: list[dict] = []
    else:
        _aa_iter = list(scenario.TERRAIN.get("avenues_of_approach") or [])
    for av in _aa_iter:
        if not isinstance(av, dict):
            continue
        central = av.get("central_axis_polyline") or []
        if not (isinstance(central, list) and len(central) >= 2):
            continue
        envelope = av.get("envelope_polygon") or []
        overlays.append({
            "kind": "avenue_of_approach",
            "name": av.get("id") or "AA",
            "geom": {
                "type": "LineString",
                "coordinates": [list(p) for p in central],
            },
            "attrs": {
                "source": "scenario_terrain",
                "side": av.get("side") or "friendly",
                "echelon": av.get("echelon") or "division",
                "designation": av.get("id"),
                "origin_name": av.get("origin_name"),
                "objective_name": av.get("objective_name"),
                "envelope_polygon": [list(p) for p in envelope]
                                    if isinstance(envelope, list) else [],
                "tail_coords": [],
                "bundle_halfwidth_m": float(av.get("bundle_halfwidth_m") or 5000.0),
                "rationale": av.get("rationale") or "",
                "synthesized": False,  # hand-authored fixture
            },
        })

    # Defensible terrain — hand-authored fixture in scenario.TERRAIN.defensible_terrain
    # (Donovian defensive positions covering doctrinal terrain features).
    # Same polygon → Polygon geom / Point fallback pattern as
    # engagement_areas_catalog above.
    for d in (scenario.TERRAIN.get("defensible_terrain") or []):
        if not isinstance(d, dict):
            continue
        polygon = d.get("polygon")
        geom = None
        if isinstance(polygon, list) and len(polygon) >= 4:
            geom = {"type": "Polygon", "coordinates": [polygon]}
        else:
            m = d.get("mgrs")
            p = mgrs_to_latlon(m) if m else None
            if p:
                geom = {"type": "Point", "coordinates": [p[1], p[0]]}
        if not geom:
            continue
        overlays.append({
            "kind": "defensible_terrain",
            "name": d.get("name") or "DEF",
            "geom": geom,
            "attrs": {
                "source": "scenario_terrain",
                "def_id": d.get("id"),
                "note": d.get("note"),
                "mgrs": d.get("mgrs"),
                "side": "friendly",  # Donovian defender's position;
                                     # renderer paints as friendly-coded
                                     # defensive overlay
            },
        })

    # Named areas of interest (NAI catalog). SITEMP / Event Template /
    # DST / ICM all reference these by name; the geo-agent paints them
    # as labeled boxes per ATP 2-01.3 Fig 6-3.
    for nai in (scenario.TERRAIN.get("nai_catalog") or []):
        if not isinstance(nai, dict):
            continue
        m = nai.get("mgrs")
        p = mgrs_to_latlon(m) if m else None
        if not p:
            continue
        overlays.append({
            "kind": "named_area_of_interest",
            "name": nai.get("name") or nai.get("id") or "NAI",
            "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
            "attrs": {
                "source": "scenario_terrain",
                "nai_id": nai.get("id"),
                "tied_to_aa": nai.get("tied_to_aa"),
                "threat_branch": nai.get("threat_branch"),
                "watching_for": nai.get("watching_for"),
                "mgrs": m,
            },
        })

    # Friendly axes of advance — scheme-of-maneuver control measures
    # from OPORD v2 §3.b/c. OPERATIONS.axes_of_advance carries the
    # MGRS endpoints; SVG paints them as the friendly blue arrow per
    # the ATP 2-01.3 Fig 6-3 axis style.
    #
    # Per-product gate (schema map_show_friendly_axis_of_advance,
    # forwarded by tool_render_map). MCOO / Threat Overlay show them
    # (the OPORD friendly maneuver plan IS part of those products);
    # SITEMP does NOT (the SITEMP is the THREAT's plan — friendly
    # maneuver belongs on the friendly Course of Action overlay, not
    # here). Caller-driven schema gate.
    operations = getattr(scenario, "OPERATIONS", None) or {}
    _opord_axes_iter = (operations.get("axes_of_advance") or []) if show_friendly_axis_of_advance else []
    for ax in _opord_axes_iter:
        if not isinstance(ax, dict):
            continue
        a, b = ax.get("from"), ax.get("to")
        p_from = mgrs_to_latlon(a) if a else None
        p_to = mgrs_to_latlon(b) if b else None
        if not (p_from and p_to):
            continue
        overlays.append({
            "kind": "axis_of_advance",
            "name": ax.get("name") or ax.get("id") or "AXIS",
            "geom": {"type": "LineString",
                       "coordinates": [[p_from[1], p_from[0]],
                                       [p_to[1], p_to[0]]]},
            "attrs": {
                "source": "scenario_terrain",
                "axis_id": ax.get("id"),
                "along_avenue": ax.get("along_avenue"),
                "side": ax.get("side") or "friendly",
                "designation": ax.get("designation"),
                "echelon": ax.get("echelon"),
                "owning_unit": ax.get("owning_unit"),
                "route": ax.get("route"),
                "task": ax.get("task"),
                "from_mgrs": a,
                "to_mgrs": b,
            },
        })

    # Scenario-authored friendly phase lines (PL ALPHA / PL BRAVO from
    # OPERATIONS). These are FRIENDLY OPORD control measures (LD, phase
    # objective) — they belong on a friendly DSM / OPORD overlay, NOT on
    # a threat-perspective SITEMP. Gated by `show_friendly_phase_lines`
    # (default True so MCOO / Threat Overlay continue to render them;
    # SITEMP schema sets the gate to False).
    if show_friendly_phase_lines:
        for pl in (operations.get("phase_lines") or []):
            if not isinstance(pl, dict):
                continue
            coords = pl.get("geom")
            if not (isinstance(coords, list) and len(coords) >= 2):
                continue
            overlays.append({
                "kind": "phase_line",
                "name": pl.get("name") or pl.get("id") or "PL",
                "geom": {"type": "LineString", "coordinates": coords},
                "attrs": {
                    "source": "scenario_terrain",
                    "phase_line_id": pl.get("id"),
                    "role": pl.get("role"),
                    "note": pl.get("note"),
                },
            })

    # Division boundaries — vertical PL BDRY lines between adjacent axes.
    # These are FRIENDLY C2 control graphics (1ID / 1AD division boundary,
    # etc.) and belong on a friendly DSM / OPORD overlay, NOT on a threat-
    # perspective SITEMP. Gated by `show_friendly_boundaries` (default
    # True; SITEMP schema sets it False).
    if show_friendly_boundaries:
        for bnd in (operations.get("boundaries") or []):
            if not isinstance(bnd, dict):
                continue
            coords = bnd.get("geom")
            if not (isinstance(coords, list) and len(coords) >= 2):
                continue
            overlays.append({
                "kind": "boundary",
                "name": bnd.get("name") or bnd.get("id") or "BDRY",
                "geom": {"type": "LineString", "coordinates": coords},
                "attrs": {
                    "source": "scenario_terrain",
                    "boundary_id": bnd.get("id"),
                    "subkind": bnd.get("subkind") or "BDRY",
                    "note": bnd.get("note"),
                },
            })

    if not overlays:
        return {"overlays_sent": 0, "skipped": "no scenario.TERRAIN features"}
    try:
        r = requests.post(
            f"{base}/maps/{map_id}/sync",
            params={"source": "scenario_terrain"},
            json={"overlays": overlays, "entities": []},
            timeout=30,
        )
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}: {r.text[:400]}",
                     "overlays_sent": len(overlays)}
        return r.json()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                 "overlays_sent": len(overlays)}


# ---------------------------------------------------------------------------
# Writer-emitted geometry → geo-agent overlays.
#
# Per ATP 2-01.3 the SITEMP / Threat COA Statement / MCOO writer authors per-COA (or per-
# product) graphics — engagement areas, NAIs, time phase lines, threat
# objectives, threat avenues of approach, key terrain. The scenario fixture
# layer provides DOCTRINAL FOUNDATION (PL ALPHA at the border, division
# boundaries from OPORD, generic terrain avenues of approach) but the
# COA-SPECIFIC kill zones / collection plans / templated unit positions
# are writer output and
# must appear on the per-product map.
#
# `attrs.source = "writer"` so this batch lives on its own layer; the
# scenario_terrain + cougaar layers stay intact. Re-pushing per-product
# atomically replaces only the writer-source rows for that map_id.
# ---------------------------------------------------------------------------


def _writer_geometry_to_geojson(value, *, want: str = "Polygon"):
    """Coerce a writer-emitted geometry value into a GeoJSON geometry dict.

    The writer's polygon/geometry fields are typed as 'Polygon (MGRS /
    GeoJSON)' or 'phase-line geometry (MGRS line or GeoJSON)' so we accept
    several shapes:
      - GeoJSON geometry dict: {"type": "Polygon"|"LineString"|"Point",
                                  "coordinates": [...]}
      - GeoJSON Feature dict: {"type": "Feature", "geometry": {...}}
      - List of [lon, lat] coordinate pairs (interpreted as a single
        polygon ring if `want=="Polygon"`, or a LineString if want=="LineString")
      - List of MGRS strings (vertices) — converted via mgrs_to_latlon
      - Single MGRS string — converted to Point geom
    Returns None for unparseable values (caller skips the feature).
    """
    if not value:
        return None
    if isinstance(value, dict):
        if value.get("type") == "Feature":
            return value.get("geometry") or None
        if value.get("type") in ("Polygon", "LineString", "Point", "MultiPolygon"):
            return value
        return None
    from utils.coords import mgrs_to_latlon
    if isinstance(value, str):
        try:
            ll = mgrs_to_latlon(value.strip())
        except Exception:
            return None
        if not ll:
            return None
        return {"type": "Point", "coordinates": [ll[1], ll[0]]}
    if isinstance(value, list) and value:
        first = value[0]
        if isinstance(first, str):
            # List of MGRS vertices.
            coords: list[list[float]] = []
            for v in value:
                try:
                    ll = mgrs_to_latlon(str(v).strip())
                except Exception:
                    ll = None
                if ll:
                    coords.append([ll[1], ll[0]])
            if not coords:
                return None
            if want == "Polygon":
                if coords[0] != coords[-1]:
                    coords.append(list(coords[0]))
                return {"type": "Polygon", "coordinates": [coords]}
            return {"type": "LineString", "coordinates": coords}
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            # List of [lon, lat] pairs. Heuristic for the lat/lon order: at
            # IDX 26 latitudes (~57-58) longitudes (~24-28) the second value
            # is always > first, so [lon, lat] is correct. If the sample is
            # clearly swapped (first > second), flip pairwise.
            coords = [[float(p[0]), float(p[1])] for p in value
                       if isinstance(p, (list, tuple)) and len(p) >= 2]
            if not coords:
                return None
            if want == "Polygon":
                if coords[0] != coords[-1]:
                    coords.append(list(coords[0]))
                return {"type": "Polygon", "coordinates": [coords]}
            return {"type": "LineString", "coordinates": coords}
    return None


# ATP 2-01.3 (INCL Change 3 May 2025) Table 4-2 — typical mobility-corridor
# widths for a mechanized force, used to derive an avenue of approach's
# accommodating echelon from its narrowest perpendicular cross-section.
# Per ¶4-27 these ranges are not absolute — they reflect the relative
# spacing of terrain features. Per ¶4-25 requirements scale with force
# type; mechanized is the table's stated baseline. Tuples are
# (echelon_label, min_width_km), ordered from largest formation to
# smallest so the first match wins.
#
# Sub-company widths return "sub-company" — not a doctrinal avenue echelon for
# a maneuvering ground formation. The MCOO renderer can still chevron it,
# but the analyst should treat that as a degraded corridor.
_ATP_2_01_3_TABLE_4_2_MECHANIZED = (
    ("division",  6.0),
    ("brigade",   3.0),
    ("battalion", 1.5),
    ("company",   0.5),
)
# Dismounted infantry / SOF tolerate narrower corridors; the typical-widths
# table doesn't enumerate dismounted explicitly, but ATP 2-01.3 ¶4-26 notes
# dismounted forces "might be able to use more restrictive corridors." Halve
# the mechanized thresholds as the conservative dismounted default. Heavy
# (armored division-scale) is the mechanized table as-is.
_ATP_2_01_3_TABLE_4_2_DISMOUNTED = tuple(
    (lbl, w / 2.0) for lbl, w in _ATP_2_01_3_TABLE_4_2_MECHANIZED
)


def _corridor_echelon_from_width_km(
    width_km: float, force_type: str = "mechanized"
) -> str:
    """Map a mobility-corridor pinch width (km) to its accommodating echelon
    per ATP 2-01.3 Table 4-2. Force-type-aware: mechanized uses the doctrinal
    table verbatim, dismounted halves the thresholds per ¶4-26 note that
    dismounted forces can use more restrictive corridors. The pinch width
    (narrowest sampled cross-section along the route) bounds the largest
    formation the corridor can sustain.
    """
    if width_km is None:
        return "unknown"
    try:
        w = float(width_km)
    except (TypeError, ValueError):
        return "unknown"
    table = (
        _ATP_2_01_3_TABLE_4_2_DISMOUNTED
        if (force_type or "").lower().startswith(("dismount", "infantry", "sof", "special"))
        else _ATP_2_01_3_TABLE_4_2_MECHANIZED
    )
    for label, min_km in table:
        if w >= min_km:
            return label
    return "sub-company"


def _discover_doctrinal_mobility_corridors(
    *, map_id: str, bbox: list, force_type: str = "mechanized"
) -> list[dict]:
    """Call geo-agent's doctrinal corridor-discovery endpoint and return
    the discovered mobility corridors for this AO.

    Per ATP 2-01.3 §4-24 corridors are connected regions of trafficable
    terrain bounded by obstacles. The geo-agent runs the connected-
    component analysis on the cost surface with morphological closing
    and median-width measurement, returning non-overlapping doctrinal
    corridors classified per Table 4-2.

    Returns [] on transport failure (logged). bbox is [W, S, E, N]."""
    if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
        return []
    try:
        w, s, e, n = [float(v) for v in bbox]
    except (TypeError, ValueError):
        return []
    base = _GEO_AGENT_API_BASE
    try:
        r = requests.post(
            f"{base}/routes/discover-corridors",
            params={
                "bbox": f"{w},{s},{e},{n}",
                "force_type": force_type,
                "min_class": "battalion",
                "map_id": map_id,
            },
            timeout=120,
        )
        if r.status_code >= 400:
            log.warning(
                "discover-corridors HTTP %s: %s",
                r.status_code, r.text[:300],
            )
            return []
        result = r.json()
    except Exception as exc:
        log.warning("discover-corridors transport failed: %s", exc)
        return []
    corridors = result.get("corridors") or []
    log.info(
        "discover-corridors map=%s returned %d corridors (force_type=%s)",
        map_id, len(corridors), force_type,
    )
    return corridors


def _point_in_polygon(lon: float, lat: float, polygon_coords: list) -> bool:
    """Ray-cast point-in-polygon for [lon, lat] coord rings. Used to map
    key_terrain MGRS points to discovered mobility corridors."""
    if not polygon_coords or not isinstance(polygon_coords, list):
        return False
    # polygon_coords format depends on Polygon vs MultiPolygon shape:
    #  Polygon: [[[x,y],[x,y],...]]   (outer ring + holes)
    #  MultiPolygon: [[[[x,y],...]], ...]  (list of polygons)
    # Normalize to a flat list of rings.
    rings = []
    try:
        first = polygon_coords[0]
        if isinstance(first[0], (int, float)):
            # Single ring [[x,y],...]
            rings = [polygon_coords]
        elif isinstance(first[0][0], (int, float)):
            # Polygon: [[[x,y],...], ...]
            rings = polygon_coords
        else:
            # MultiPolygon: [[[[x,y],...],...], ...] — flatten one level
            for poly in polygon_coords:
                rings.extend(poly)
    except (IndexError, TypeError):
        return False
    if not rings:
        return False
    # Test against the outer ring only (ignore holes — sufficient for
    # KT containment check on doctrinal corridor polygons).
    ring = rings[0]
    n = len(ring)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > lat) != (yj > lat)) and \
                (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _route_primary_corridor(
    route_coords: list, discovered_corridors: list[dict],
) -> dict | None:
    """For a planner-routed avenue's waypoint list, return the discovered
    mobility corridor whose polygon contains the most route waypoints.

    Used to anchor each avenue of approach to a labeled mobility corridor
    (¶4-22 "an avenue of approach consists of a series of mobility
    corridors") for the doc table and chevron-overlay attrs. Returns None
    if no corridor contains at least 3 waypoints — too little overlap to
    claim primary anchorage; the avenue is then labeled cross-country.
    """
    if not route_coords or not discovered_corridors:
        return None
    best_corridor = None
    best_count = 2  # strict >: require >= 3 waypoints inside
    for corridor in discovered_corridors:
        polygon_coords = (corridor.get("polygon") or {}).get("coordinates") or []
        if not polygon_coords:
            continue
        inside = 0
        for c in route_coords:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                if _point_in_polygon(c[0], c[1], polygon_coords):
                    inside += 1
        if inside > best_count:
            best_count = inside
            best_corridor = corridor
    return best_corridor


def _build_doctrinal_avenues_from_corridors(
    discovered_corridors: list[dict],
    key_terrain: list[dict],
    axes_of_advance: list[dict],
    *,
    map_id: str,
    force_type: str = "mechanized",
) -> list[dict]:
    """Enumerate the doctrinal avenue-of-approach set for the AO.

    Algorithm (MCOO_AA_HANDOFF.md Step 2 + Step 5):

    1. FRIENDLY avenues come straight from OPORD axes_of_advance — one
       avenue per friendly axis (3 total for IDX 26: BLUE/RED/WHITE).
       The axis's `from` MGRS is the origin (LD anchor / FCP), the `to`
       MGRS is the objective (enemy DIV HQ).

    2. THREAT avenues defend each contested objective (the friendly
       axes' destinations). For each contested objective, enumerate
       candidate origins from every threat-side key_terrain entry EXCEPT
       the one at the objective itself (no degenerate origin==objective
       routes — in-place defense isn't an avenue of approach, it IS the
       defensive position). Each threat formation HQ that has a
       planner-feasible route to a contested objective gets one threat
       avenue toward that objective.

    3. For every candidate (origin, objective) pair, plan a terrain
       route via _plan_terrain_route. If the planner returns no path,
       the pair is "literally blocked off" per user spec and the avenue
       is dropped.

    4. The successful route's coords are stamped on the avenue as
       `_route_coords` — this is what the writer extractor fast-path
       paints as the chevron geometry (Step 5: the chevron follows the
       planner route from origin KT to objective KT, NOT the discovered-
       corridor centerline long-axis approximation that produced the
       stub-chevron renders).

    5. Each avenue is anchored to the discovered mobility corridor whose
       polygon contains the most route waypoints (_route_primary_corridor
       helper). That corridor's centerline / class / width get stamped
       into the avenue's mobility_corridors[0] for the doc table + the
       per-row provenance string.

    Pairwise overlap detection + multi-tail merge runs separately in
    Step 6 (handoff TODO).
    """
    if not discovered_corridors or not map_id:
        log.warning(
            "_build_doctrinal_avenues_from_corridors: empty input "
            "(corridors=%d, map_id=%s); returning no avenues",
            len(discovered_corridors or []), bool(map_id),
        )
        return []

    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon

    # Key-terrain lookup by MGRS prefix. Axis from/to MGRS is 15-char
    # precise; KT MGRS is typically 13-char. Match on the first 10
    # chars (1km zone-quad precision) — enough to identify the named
    # locality without false collisions across AOs.
    def _mgrs_key(m):
        return (m or "").strip()[:10]
    kt_by_mgrs = {
        _mgrs_key(kt.get("mgrs")): kt
        for kt in (key_terrain or []) if isinstance(kt, dict) and kt.get("mgrs")
    }
    threat_kts = [
        kt for kt in (key_terrain or [])
        if isinstance(kt, dict) and (kt.get("side") or "").lower() == "threat"
           and kt.get("mgrs")
    ]

    # ------------------------------------------------------------------
    # Candidate enumeration — every (avenue_side, origin, objective)
    # pair we'll ask the planner about. Each candidate carries enough
    # metadata to label the resulting avenue without re-deriving.
    # ------------------------------------------------------------------
    candidates: list[dict] = []

    # Friendly: one avenue per OPORD axis_of_advance.
    for axis in (axes_of_advance or []):
        if not isinstance(axis, dict):
            continue
        if (axis.get("side") or "").lower() != "friendly":
            continue
        from_mgrs = (axis.get("from") or "").strip()
        to_mgrs = (axis.get("to") or "").strip()
        if not from_mgrs or not to_mgrs:
            continue
        route_str = axis.get("route") or ""
        route_parts = [p.strip() for p in route_str.split("->") if p.strip()]
        origin_name = route_parts[0] if route_parts else "LD"
        # Prefer the named KT at the objective MGRS for objective_name.
        objective_kt = kt_by_mgrs.get(_mgrs_key(to_mgrs))
        objective_name = (
            objective_kt.get("name") if objective_kt
            else (route_parts[-1] if route_parts else "objective")
        )
        candidates.append({
            "avenue_side": "friendly",
            "role": "ATTACK",
            "origin_name": origin_name,
            "origin_mgrs": from_mgrs,
            "objective_name": objective_name,
            "objective_mgrs": to_mgrs,
            "echelon": axis.get("echelon") or "",
            "owning_unit": axis.get("owning_unit") or "",
            "axis_id": axis.get("id"),
        })

    # Threat: from every threat HQ to every contested objective (with
    # origin != objective). Contested objectives = each friendly axis's
    # destination, deduplicated.
    seen_objective_mgrs: set[str] = set()
    contested_objectives: list[tuple[str, str]] = []  # (mgrs, name)
    for axis in (axes_of_advance or []):
        if not isinstance(axis, dict):
            continue
        if (axis.get("side") or "").lower() != "friendly":
            continue
        to_mgrs = (axis.get("to") or "").strip()
        if not to_mgrs or _mgrs_key(to_mgrs) in seen_objective_mgrs:
            continue
        seen_objective_mgrs.add(_mgrs_key(to_mgrs))
        objective_kt = kt_by_mgrs.get(_mgrs_key(to_mgrs))
        objective_name = (
            objective_kt.get("name") if objective_kt
            else (axis.get("route") or "").split("->")[-1].strip() or "objective"
        )
        contested_objectives.append((to_mgrs, objective_name))

    for obj_mgrs, obj_name in contested_objectives:
        for threat_kt in threat_kts:
            origin_mgrs = (threat_kt.get("mgrs") or "").strip()
            if not origin_mgrs:
                continue
            # Skip degenerate origin==objective (the in-place defender;
            # their own defensive position is captured by the OTHER
            # divisions' avenues converging here).
            if _mgrs_key(origin_mgrs) == _mgrs_key(obj_mgrs):
                continue
            origin_name = threat_kt.get("name") or origin_mgrs
            # Role: if origin is the operational-reserve HQ (Tartu /
            # Western Army), call it COUNTERATTACK (deep reserve);
            # otherwise it's a peer-DIV lateral reinforcement.
            role = (
                "COUNTERATTACK"
                if "army" in origin_name.lower() or "tartu" in origin_name.lower()
                else "REINFORCE"
            )
            candidates.append({
                "avenue_side": "threat",
                "role": role,
                "origin_name": origin_name,
                "origin_mgrs": origin_mgrs,
                "objective_name": obj_name,
                "objective_mgrs": obj_mgrs,
                "echelon": "division",
                "owning_unit": origin_name,
                "axis_id": None,
            })

    log.info(
        "_build_doctrinal_avenues_from_corridors: %d candidates queued "
        "(%d friendly axes, %d threat reinforce/counterattack pairs)",
        len(candidates),
        sum(1 for c in candidates if c["avenue_side"] == "friendly"),
        sum(1 for c in candidates if c["avenue_side"] == "threat"),
    )

    # ------------------------------------------------------------------
    # Plan each candidate via terrain planner; drop the unroutable ones.
    # ------------------------------------------------------------------
    avenues: list[dict] = []
    for cand in candidates:
        try:
            p_origin = _mgrs_to_latlon(cand["origin_mgrs"])
            p_objective = _mgrs_to_latlon(cand["objective_mgrs"])
        except Exception:
            log.warning(
                "Avenue candidate %s → %s: MGRS resolve failed; skipping",
                cand["origin_mgrs"], cand["objective_mgrs"],
            )
            continue
        if not (p_origin and p_objective):
            continue
        side_tag = "FRD" if cand["avenue_side"] == "friendly" else "THR"
        origin_tag = (cand["origin_name"] or cand["origin_mgrs"]).split()[0][:8].upper()
        objective_tag = (cand["objective_name"] or cand["objective_mgrs"]).split()[0][:8].upper()
        avenue_id = f"AA-{side_tag}-{origin_tag}-TO-{objective_tag}"
        planner_route_spec = {
            "id": avenue_id,
            "name": f"{cand['origin_name']} → {cand['objective_name']}",
            "echelon": cand["echelon"],
            "source": "mcoo_doctrinal_avenue",
            "from": cand["origin_mgrs"],
            "to": cand["objective_mgrs"],
        }
        plan = _plan_terrain_route(map_id, planner_route_spec, p_origin, p_objective)
        if not plan:
            log.info(
                "Avenue %s → %s: planner returned no path (blocked off); "
                "dropping",
                cand["origin_name"], cand["objective_name"],
            )
            continue
        route_coords = plan.get("coords") or []
        analysis = plan.get("analysis") or {}

        # Anchor the avenue to whichever discovered corridor the route
        # traverses most. Used for labeled provenance + the per-corridor
        # detail row in the doc table.
        primary_corridor = _route_primary_corridor(route_coords, discovered_corridors)

        # Width / class / echelon derivation per ¶4-26 (off-road extent
        # vs. road-supported, take the BETTER per the analyst's actual
        # capability) → ¶4-23 (avenue echelon rolls UP one level from
        # the best constituent corridor class).
        width_m = analysis.get("corridor_width_m")
        if width_m and width_m > 0:
            width_km = round(width_m / 1000.0, 2)
            off_road_class = _corridor_echelon_from_width_km(
                width_km, force_type=force_type
            )
        else:
            width_km = None
            off_road_class = "unknown"
        road_class = analysis.get("road_supported_class") or "unknown"
        corridor_class = _better_corridor_class(off_road_class, road_class)
        avenue_echelon = _CORRIDOR_CLASS_TO_AVENUE_ECHELON.get(
            corridor_class, "unknown"
        )

        rationale_bits = [
            f"{'Friendly' if cand['avenue_side'] == 'friendly' else 'Threat'} "
            f"avenue of approach from {cand['origin_name']} to "
            f"{cand['objective_name']}."
        ]
        if cand["axis_id"]:
            rationale_bits.append(
                f"Mirrors OPORD {cand['axis_id']} ({cand['owning_unit']})."
            )
        elif cand["role"] == "REINFORCE":
            rationale_bits.append(
                f"{cand['owning_unit']} shifts laterally to reinforce "
                f"{cand['objective_name']}."
            )
        elif cand["role"] == "COUNTERATTACK":
            rationale_bits.append(
                f"{cand['owning_unit']} counterattacks from operational depth."
            )
        primary_corridor_id = (
            primary_corridor.get("corridor_id") if primary_corridor else None
        )
        rationale_bits.append(
            f"Terrain-planned via geo-agent A* (mixed mode); "
            f"{width_km if width_km else '?'} km pinch width, "
            f"{corridor_class}-class"
            + (
                f", primary corridor {primary_corridor_id}."
                if primary_corridor_id
                else ", cross-country (no primary corridor anchor)."
            )
        )

        avenues.append({
            "id": avenue_id,
            "side": cand["avenue_side"],
            "role": cand["role"],
            "origin_name": cand["origin_name"],
            "origin_mgrs": cand["origin_mgrs"],
            "objective_name": cand["objective_name"],
            "objective_mgrs": cand["objective_mgrs"],
            "owning_unit": cand["owning_unit"],
            "echelon": avenue_echelon,
            "rationale": " ".join(rationale_bits),
            "mobility_corridors": [{
                "corridor_id": (
                    primary_corridor_id or f"{avenue_id}-cross-country"
                ),
                "from_name": cand["origin_name"],
                "from_mgrs": cand["origin_mgrs"],
                "width_km": width_km,
                "class": corridor_class,
                "off_road_class": off_road_class,
                "road_class": road_class,
                "centerline": (
                    primary_corridor.get("centerline") if primary_corridor else None
                ),
                "polygon": (
                    primary_corridor.get("polygon") if primary_corridor else None
                ),
            }],
            # Step 5: stamp the planner-routed avenue geometry on the row
            # so the writer extractor's fast path uses these as the
            # chevron coords. Without this the chevron rendered the
            # discovered-corridor centerline (long-axis stub) instead of
            # the actual origin → objective route.
            "_route_coords": route_coords,
            "_discovered_corridor_id": primary_corridor_id,
        })

    log.info(
        "_build_doctrinal_avenues_from_corridors: %d candidates → %d "
        "planned avenues (%d friendly, %d threat)",
        len(candidates), len(avenues),
        sum(1 for a in avenues if a["side"] == "friendly"),
        sum(1 for a in avenues if a["side"] == "threat"),
    )
    return avenues


def _build_synthesis_terminal_pairs() -> list[dict]:
    """Build the (origin, objective) terminal pair list for the geo-agent
    /synthesize-mcoo call.

    Friendly terminals: each axis_of_advance from scenario.OPERATIONS
    (one avenue per OPORD axis).

    Threat terminals: each threat key_terrain entry that is NOT itself a
    contested objective gets paired with each contested objective the
    planner believes it can reach. Contested objectives = friendly axes'
    destination MGRSs (Kopu, Rongu, Osula for IDX 26).
    """
    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
    operations = getattr(scenario, "OPERATIONS", None) or {}
    axes = operations.get("axes_of_advance") or []
    kts = scenario.TERRAIN.get("key_terrain") or []

    def _resolve(mgrs):
        try:
            return _mgrs_to_latlon(mgrs)
        except Exception:
            return None

    def _mgrs_key(m): return (m or "").strip()[:10]

    kt_by_mgrs = {
        _mgrs_key(kt.get("mgrs")): kt
        for kt in kts if isinstance(kt, dict) and kt.get("mgrs")
    }
    threat_kts = [
        kt for kt in kts
        if isinstance(kt, dict) and (kt.get("side") or "").lower() == "threat"
            and kt.get("mgrs")
    ]
    pairs: list[dict] = []
    seen_obj_keys: set[str] = set()
    contested_objectives: list[tuple[str, str, tuple[float, float]]] = []

    # Friendly avenues from OPORD axes
    for axis in axes:
        if (axis.get("side") or "").lower() != "friendly":
            continue
        from_mgrs = (axis.get("from") or "").strip()
        to_mgrs = (axis.get("to") or "").strip()
        if not from_mgrs or not to_mgrs:
            continue
        p_o = _resolve(from_mgrs)
        p_d = _resolve(to_mgrs)
        if not (p_o and p_d):
            continue
        route_parts = [
            p.strip() for p in (axis.get("route") or "").split("->") if p.strip()
        ]
        origin_name = route_parts[0] if route_parts else "LD"
        obj_kt = kt_by_mgrs.get(_mgrs_key(to_mgrs))
        objective_name = (
            obj_kt.get("name") if obj_kt
            else (route_parts[-1] if route_parts else "objective")
        )
        pairs.append({
            "origin":    {"name": origin_name, "lon": p_o[1], "lat": p_o[0],
                          "side": "friendly"},
            "objective": {"name": objective_name, "lon": p_d[1], "lat": p_d[0],
                          "side": "threat"},
        })
        if _mgrs_key(to_mgrs) not in seen_obj_keys:
            contested_objectives.append((to_mgrs, objective_name, p_d))
            seen_obj_keys.add(_mgrs_key(to_mgrs))

    # Threat avenues: each non-in-place threat HQ -> each contested objective
    for obj_mgrs, obj_name, p_obj in contested_objectives:
        for threat_kt in threat_kts:
            origin_mgrs = (threat_kt.get("mgrs") or "").strip()
            if _mgrs_key(origin_mgrs) == _mgrs_key(obj_mgrs):
                continue
            p_o = _resolve(origin_mgrs)
            if not p_o:
                continue
            origin_name = threat_kt.get("name") or origin_mgrs
            pairs.append({
                "origin":    {"name": origin_name, "lon": p_o[1], "lat": p_o[0],
                              "side": "threat"},
                "objective": {"name": obj_name, "lon": p_obj[1], "lat": p_obj[0],
                              "side": "threat"},
            })
    return pairs


def _synthesize_mcoo_via_terrain_circuit(
    structured_data: dict, *,
    map_id: str, force_type: str, writer_rows: list[dict],
) -> dict:
    """Call geo-agent /routes/synthesize-mcoo and replace the writer's
    avenues_of_approach with the synthesized multi-tail bundles. Also
    stashes engagement areas + defensible terrain + all mobility
    corridors for the writer extractor to push as overlays.

    Returns {"synthesized": True, ...stats} on success, {"synthesized":
    False, "reason": "..."} otherwise (caller falls back to legacy path).
    """
    try:
        from utils import scenario as _scen
        ao_bbox = _scen.AO_BBOX  # [W, S, E, N]
    except Exception:
        return {"synthesized": False, "reason": "no AO_BBOX in scenario"}

    pairs = _build_synthesis_terminal_pairs()
    if not pairs:
        return {"synthesized": False, "reason": "no terminal pairs available"}

    body = {
        "bbox": list(ao_bbox),
        "force_type": force_type,
        "primary_echelon": "division",
        "cell_m": 200,  # coarser than default 100 for full-AO speed
        "terminal_pairs": pairs,
    }
    try:
        r = requests.post(
            f"{_GEO_AGENT_API_BASE}/routes/synthesize-mcoo",
            params={"map_id": map_id}, json=body, timeout=1800,
        )
    except Exception as exc:
        log.warning("synthesize-mcoo transport failed: %s", exc)
        return {"synthesized": False, "reason": f"transport: {exc}"}
    if r.status_code >= 400:
        log.warning("synthesize-mcoo HTTP %s: %s", r.status_code, r.text[:300])
        return {"synthesized": False, "reason": f"HTTP {r.status_code}"}
    try:
        result = r.json()
    except Exception as exc:
        log.warning("synthesize-mcoo response decode: %s", exc)
        return {"synthesized": False, "reason": f"decode: {exc}"}

    raw_avenues = result.get("avenues_of_approach") or []
    raw_corridors = result.get("mobility_corridors") or []
    raw_eas = result.get("engagement_areas") or []
    raw_def = result.get("defensible_terrain") or []
    log.info(
        "synthesize-mcoo OK: %d avenues, %d corridors, %d EAs, %d defensible "
        "regions (writer-emitted %d avenues replaced)",
        len(raw_avenues), len(raw_corridors), len(raw_eas), len(raw_def),
        len(writer_rows),
    )

    # Convert each avenue bundle into the schema row shape used by the
    # writer extractor. The envelope_polygon is the multi-tail hollow
    # outline; the central_axis_coords is the spine the renderer
    # paints inside.
    corridor_by_id = {c["id"]: c for c in raw_corridors}

    def _coords_concat(corridor_ids):
        if not corridor_ids:
            return []
        # Naive concatenation in id order; renderer will smooth.
        seen = set()
        out: list[list[float]] = []
        for cid in corridor_ids:
            if cid in seen:
                continue
            seen.add(cid)
            corridor = corridor_by_id.get(cid)
            if not corridor:
                continue
            for c in corridor.get("coords") or []:
                if not out or out[-1] != list(c):
                    out.append(list(c))
        return out

    # Doctrinal-label assignment: friendly avenues numbered AA1, AA2,
    # ... in the order they appear (matches FM 1-02.2 / ATP 2-01.3 Fig
    # 4-13 convention of short alphanumeric AA designations along the
    # arrow body). Threat avenues prefixed THR (AA-THR1, AA-THR2, ...)
    # so analysts can tell at a glance which side is moving.
    friendly_counter = 0
    threat_counter = 0
    schema_avenues = []
    for av in raw_avenues:
        # Prefer the simplified central_axis_polyline (Douglas-Peucker
        # output of the GVD-edge path); fall back to concatenating the
        # raw central-axis corridor coords if not present.
        central_coords = av.get("central_axis_polyline") or []
        if not central_coords:
            central_coords = _coords_concat(av.get("central_axis_corridor_ids") or [])
        objective_lonlat = av.get("objective_lonlat") or [0, 0]
        origin_lonlat = av.get("origin_lonlat") or [0, 0]
        side = av.get("side") or "neutral"
        if side == "friendly":
            friendly_counter += 1
            short_label = f"AA{friendly_counter}"
        elif side == "threat":
            threat_counter += 1
            short_label = f"AA-THR{threat_counter}"
        else:
            short_label = f"AA-N{len(schema_avenues) + 1}"
        # The Rationale column text is composed HERE, in the product
        # layer, from geo-agent's structured facts — geo-agent emits
        # geometry + facts only and never authors document prose. Plain
        # analytic English: no doctrine citations, no method internals.
        n_central = len(av.get("central_axis_corridor_ids") or [])
        n_tails = len(av.get("tail_corridor_ids") or [])
        n_flank = max(n_tails - n_central, 0)
        group_km = 2.0 * float(av.get("bundle_halfwidth_m") or 0.0) / 1000.0
        side_word = {"friendly": "Friendly", "threat": "Threat"}.get(
            side, "Unattributed")
        echelon_word = str(av.get("echelon") or "").strip()
        rationale_text = (
            f"{side_word} avenue of approach from "
            f"{av.get('origin_name') or 'origin'} to "
            f"{av.get('objective_name') or 'objective'}: central axis "
            f"follows {n_central} mobility corridor segment(s)"
        )
        if n_flank:
            rationale_text += f", with {n_flank} flanking corridor(s)"
            if group_km > 0:
                rationale_text += f" grouped within {group_km:.1f} km of the axis"
        if echelon_word:
            rationale_text += f"; supports {echelon_word}-echelon movement"
        rationale_text += "."
        schema_avenues.append({
            "id": short_label,
            "long_id": av.get("id"),
            "side": av.get("side"),
            "objective_name": av.get("objective_name"),
            "origin_name": av.get("origin_name"),
            "echelon": av.get("echelon"),
            "rationale": rationale_text,
            # mobility_corridors per the existing schema: list with
            # one entry per grouped tail corridor.
            "mobility_corridors": [
                {
                    "corridor_id": str(cid),
                    "from_name": av.get("origin_name") if cid in (av.get("central_axis_corridor_ids") or []) else "neighbour",
                    "from_mgrs": None,
                    "width_km": round((corridor_by_id.get(cid) or {}).get("width_m", 0) / 1000.0, 2),
                    "class": (corridor_by_id.get(cid) or {}).get("class", "unknown"),
                }
                for cid in (av.get("tail_corridor_ids") or [])
            ],
            "_route_coords": central_coords,
            "_envelope_polygon": av.get("envelope_polygon") or [],
            "_bundle_halfwidth_m": float(av.get("bundle_halfwidth_m") or 0.0),
            "_tail_corridor_coords": [
                (corridor_by_id.get(cid) or {}).get("coords") or []
                for cid in (av.get("tail_corridor_ids") or [])
            ],
            "_synthesized": True,
        })

    structured_data["_writer_emitted_avenues_of_approach"] = writer_rows
    structured_data["avenues_of_approach"] = schema_avenues
    structured_data["_synthesized_mobility_corridors"] = raw_corridors
    structured_data["_synthesized_engagement_areas"] = raw_eas
    structured_data["_synthesized_defensible_terrain"] = raw_def

    return {
        "synthesized": True,
        "avenue_count": len(schema_avenues),
        "corridor_count": len(raw_corridors),
        "engagement_area_count": len(raw_eas),
        "defensible_count": len(raw_def),
        "writer_emitted_replaced": len(writer_rows),
    }


def _enrich_mcoo_avenues_of_approach(
    structured_data: dict, *, map_id: str | None, force_type: str = "mechanized"
) -> dict:
    """Enumerate mobility corridors per writer-emitted avenue of approach,
    stamp the list + derived per-corridor width/class + avenue-level echelon
    onto each row.

    Per ATP 2-01.3 ¶4-22 an avenue of approach is "a series of mobility
    corridors through which a maneuvering force must pass to reach its
    objective." The writer emits avenue-level fields (id, side,
    objective_mgrs, objective_name, rationale) — one judgment call per
    avenue about WHICH objective the avenue converges on and WHICH side
    the maneuvering force is. The pipeline then enumerates the constituent
    mobility corridors by walking scenario.TERRAIN.key_terrain for entries
    whose `side` matches the avenue's side (friendly origins for friendly
    avenues, threat origins for threat avenues — the maneuvering force
    originates from its own side), planning a route from each origin to
    the avenue's objective, and computing per-corridor width via the
    planner's corridor_width_m sample. Per-corridor class maps from width
    via Table 4-2; avenue echelon rolls up via the Table 4-2 nesting
    (Division avenue contains Brigade-class corridors, Brigade avenue
    contains Battalion-class, etc. — so the avenue echelon is one level
    UP from its narrowest constituent corridor's class).

    Idempotent. Returns a stats dict for logging."""
    rows = (structured_data or {}).get("avenues_of_approach") or []
    if not isinstance(rows, list) or not map_id:
        return {"enriched": 0, "skipped": "no rows or no map_id"}

    # PRIMARY PATH: doctrinal GVD + circuit-theory MCOO synthesis. Calls
    # geo-agent /routes/synthesize-mcoo which computes the FULL Modified
    # Combined Obstacle Overlay product set (avenues of approach,
    # mobility corridors, engagement areas, defensible terrain) in one
    # pass per the "Terrain-Based Information Fusion and Inference"
    # algorithm (Glinton et al., CMU/UPitt 2004) — validated against
    # SME analysts. Generalized Voronoi Diagram of NO-GO obstacles +
    # Kirchhoff current solution on the corridor circuit per terminal
    # pair. Each (origin, objective) terminal is a friendly OPORD axis
    # or a threat-DIV → contested-objective candidate.
    #
    # COMEX SHIP STATE: synth algorithm DISABLED. The 12 avenues + 6
    # defensible regions are hand-authored in scenario.TERRAIN and
    # pushed by _push_scenario_terrain_to_geo_agent. Synth path
    # retained for post-COMEX rewrite.
    return {
        "enriched": len(rows),
        "skipped": "synth disabled — scenario fixtures authoritative for COMEX",
    }
    # TODO: WIP — restore post-COMEX. The block below is the original
    # synthesis / doctrinal-discovery path. Currently unreachable because
    # the COMEX ship gates above return early. Kept in-source (as a string
    # literal, NOT live code) so the algorithm + provenance shape is
    # preserved verbatim for when the synth path is re-enabled.
    _UNREACHABLE_SYNTH_PATH = """
    synth_stats = _synthesize_mcoo_via_terrain_circuit(
        structured_data, map_id=map_id, force_type=force_type, writer_rows=rows,
    )
    if synth_stats.get("synthesized"):
        return synth_stats

    # Doctrinal corridor discovery via geo-agent's connected-component
    # cost-surface analysis. When this succeeds the discovered corridor
    # set + its derived avenues of approach SUPERSEDE the writer's emitted
    # avenues_of_approach[] list. The writer was best-effort guessing
    # routes; the pipeline knows the actual non-overlapping mobility
    # corridors per ATP 2-01.3 §4-24, so the pipeline's enumeration is
    # authoritative. Writer's per-avenue rationale prose still gets used
    # as fallback if the discovered set is empty (e.g., geo-agent
    # unreachable).
    try:
        from utils import scenario as _scen
        ao_bbox = _scen.AO_BBOX  # [W, S, E, N]
    except Exception:
        ao_bbox = None
    discovered_corridors: list[dict] = []
    if ao_bbox:
        discovered_corridors = _discover_doctrinal_mobility_corridors(
            map_id=map_id, bbox=ao_bbox, force_type=force_type,
        )
    if discovered_corridors:
        operations = getattr(scenario, "OPERATIONS", None) or {}
        derived_avenues = _build_doctrinal_avenues_from_corridors(
            discovered_corridors,
            scenario.TERRAIN.get("key_terrain") or [],
            operations.get("axes_of_advance") or [],
            map_id=map_id,
            force_type=force_type,
        )
        if derived_avenues:
            # Stash the writer's emit for debug + replace with derived.
            structured_data["_writer_emitted_avenues_of_approach"] = rows
            structured_data["avenues_of_approach"] = derived_avenues
            # Also stash the full discovered corridor set for renderer
            # consumption (mobility_corridor overlay layer per ATP
            # 2-01.3 Fig 4-6 doctrinal symbology — all discovered
            # corridors painted, not just those that anchor avenues of
            # approach).
            structured_data["_discovered_mobility_corridors"] = discovered_corridors
            log.info(
                "MCOO avenues of approach superseded by doctrinal "
                "discovery: %d discovered corridors → %d derived avenues "
                "(writer emitted %d)",
                len(discovered_corridors), len(derived_avenues), len(rows),
            )
            return {
                "doctrinal_discovery": True,
                "discovered_corridors": len(discovered_corridors),
                "derived_avenues": len(derived_avenues),
                "writer_emitted_dropped": len(rows),
            }
        log.info(
            "MCOO doctrinal discovery returned %d corridors but mapped to "
            "0 avenues of approach (no key_terrain reachable from any "
            "corridor) — falling back to writer-emit enrichment",
            len(discovered_corridors),
        )
    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
    stats = {
        "avenue_count": 0,
        "corridors_planned": 0,
        "corridors_failed": 0,
        "corridors_no_width": 0,
        "avenue_enriched": 0,
    }
    # Origin pool: scenario.TERRAIN.key_terrain entries with explicit `side`.
    # Pre-resolve once so all avenues share the same candidate pool. Tartu's
    # decisive=False entry is included as a candidate threat-side origin
    # because doctrinally any positioned threat formation can originate
    # a counterattack regardless of decisive-terrain flag.
    _kt_pool = (scenario.TERRAIN.get("key_terrain") or [])
    _origins_by_side: dict[str, list[dict]] = {"friendly": [], "threat": [], "neutral": []}
    for kt in _kt_pool:
        if not isinstance(kt, dict):
            continue
        side = (kt.get("side") or "").strip().lower()
        if side in _origins_by_side:
            _origins_by_side[side].append(kt)
    for row in rows:
        if not isinstance(row, dict):
            continue
        stats["avenue_count"] += 1
        avenue_side = (row.get("side") or "").strip().lower()
        objective_mgrs = (row.get("objective_mgrs") or "").strip()
        if not objective_mgrs or avenue_side not in ("friendly", "threat", "neutral"):
            continue
        try:
            p_obj = _mgrs_to_latlon(objective_mgrs)
        except Exception:
            p_obj = None
        if not p_obj:
            continue
        # The avenue's OWN side's key_terrain entries are the corridor
        # origins — per ATP 2-01.3 ¶4-21 an avenue of approach is the
        # path used by the MANEUVERING force leading TO its objective.
        # Friendly forces originate from friendly KT and converge on
        # threat KT; threat forces originate from threat KT and converge
        # on friendly KT. Origin side == avenue side; objective side ==
        # opposite. For neutral avenues (rare — corridors usable by
        # either side) pool both sides as origins.
        if avenue_side == "neutral":
            origin_pool = _origins_by_side["friendly"] + _origins_by_side["threat"]
        else:
            origin_pool = _origins_by_side[avenue_side]
        # Drop the objective itself from the origin pool — an avenue of
        # approach doesn't have a corridor that starts at its own
        # destination.
        origin_pool = [
            o for o in origin_pool
            if (o.get("mgrs") or "").strip() != objective_mgrs
        ]
        corridors: list[dict] = []
        corridor_classes: list[str] = []
        for idx, origin in enumerate(origin_pool, start=1):
            origin_mgrs = (origin.get("mgrs") or "").strip()
            if not origin_mgrs:
                continue
            try:
                p_orig = _mgrs_to_latlon(origin_mgrs)
            except Exception:
                p_orig = None
            if not p_orig:
                continue
            planner_route_spec = {
                "id": f"{row.get('id') or 'AA'}-C{idx}",
                "echelon": row.get("echelon") or "",
                "name": f"{row.get('id') or 'AA'} corridor {idx}",
                "source": "mcoo_corridor_enum",
                "from": origin_mgrs,
                "to": objective_mgrs,
            }
            plan = _plan_terrain_route(map_id, planner_route_spec, p_orig, p_obj)
            if not plan:
                stats["corridors_failed"] += 1
                continue
            stats["corridors_planned"] += 1
            analysis = plan.get("analysis") or {}
            # Off-road extent: perpendicular pinch against canalization
            # criterion (Table 4-2 widths).
            width_m = analysis.get("corridor_width_m")
            if width_m is None or width_m <= 0:
                stats["corridors_no_width"] += 1
                width_km = None
                off_road_class = "unknown"
            else:
                width_km = round(width_m / 1000.0, 2)
                off_road_class = _corridor_echelon_from_width_km(
                    width_km, force_type=force_type
                )
            # Road-supported class: narrowest OSM highway class along the
            # route's on-road portion mapped to ATP echelon per ¶4-26.
            road_class = analysis.get("road_supported_class") or "unknown"
            # The corridor's accommodating echelon is the BETTER of off-road
            # extent and road-supported class. Either dimension on its own
            # can sustain the formation per ¶4-26: a wide cropland valley
            # carries Division on cross-country; a Brigade-class road
            # through forest carries Brigade in column. Pick the bigger of
            # the two so the analyst sees the corridor's actual capability.
            corridor_class = _better_corridor_class(off_road_class, road_class)
            corridors.append({
                "corridor_id": planner_route_spec["id"],
                "from_name": origin.get("name") or origin_mgrs,
                "from_mgrs": origin_mgrs,
                "width_km": width_km,
                "class": corridor_class,
                "off_road_class": off_road_class,
                "road_class": road_class,
            })
            corridor_classes.append(corridor_class)
        row["mobility_corridors"] = corridors
        row["echelon"] = _avenue_echelon_from_corridor_classes(
            corridor_classes, force_type=force_type
        )
        prov = row.setdefault("_provenance", {})
        prov["mobility_corridors"] = (
            f"derived: enumerated from "
            f"{'either-side' if avenue_side == 'neutral' else avenue_side}-side "
            f"key_terrain origins converging on {objective_mgrs}; "
            f"{len(corridors)} corridor(s) planned via geo-agent A* terrain planner"
        )
        prov["echelon"] = (
            f"derived: ATP 2-01.3 Table 4-2 nesting roll-up from "
            f"{len(corridor_classes)} corridor class(es) "
            f"({force_type}) → {row['echelon']}"
        )
        stats["avenue_enriched"] += 1
    return stats
"""
    # end TODO: WIP — restore post-COMEX block above (string literal, dead code).


# ATP 2-01.3 Table 4-2 NESTING: a Division avenue of approach contains
# Brigade-class mobility corridors; a Brigade avenue contains Battalion-
# class corridors; a Battalion avenue contains Company-class corridors.
# So an avenue's accommodating echelon is ONE LEVEL UP from its narrowest
# constituent corridor's class. This rollup table maps
# corridor-class → avenue-of-approach echelon.
_CORRIDOR_CLASS_TO_AVENUE_ECHELON = {
    "division":    "division",   # division-wide corridor implies the avenue can host a Division (no further upgrade)
    "brigade":     "division",   # Brigade-class corridors nest within a Division avenue
    "battalion":   "brigade",    # Battalion-class corridors nest within a Brigade avenue
    "company":     "battalion",  # Company-class corridors nest within a Battalion avenue
    "sub-company": "company",
    "unknown":     "unknown",
}


def _better_corridor_class(a: str, b: str) -> str:
    """Return the larger of two corridor classes per ATP 2-01.3 Table 4-2
    ordering. Used to merge off-road extent vs road-supported class per
    ¶4-26 — the corridor's accommodating echelon is whichever dimension
    can sustain the larger formation. 'unknown' loses to any known class."""
    rank = {
        "division": 5, "brigade": 4, "battalion": 3,
        "company": 2, "sub-company": 1, "unknown": 0,
    }
    return a if rank.get(a, 0) >= rank.get(b, 0) else b


def _avenue_echelon_from_corridor_classes(
    corridor_classes: list[str], force_type: str = "mechanized"
) -> str:
    """Roll up per-corridor classes to the avenue of approach's
    accommodating echelon per ATP 2-01.3 Table 4-2 nesting + ¶4-23
    "avenues of approach should be prioritized based on how well each
    supports the ability to meet the desired end state." The avenue
    accommodates the largest formation that ANY single constituent
    corridor can carry — one Brigade-class corridor in the avenue is
    sufficient for the avenue to host a Division (using that corridor
    + the others as supporting routes). Mixed-class avenues take the
    BEST available class, not the worst.

    force_type accepted for symmetry with the per-corridor classifier
    (the corridor classes themselves already reflect force_type via the
    width / road-class derivation steps)."""
    if not corridor_classes:
        return "unknown"
    rank = {
        "division": 5, "brigade": 4, "battalion": 3,
        "company": 2, "sub-company": 1, "unknown": 0,
    }
    best = max(corridor_classes, key=lambda c: rank.get(c, 0))
    return _CORRIDOR_CLASS_TO_AVENUE_ECHELON.get(best, "unknown")


# Per-product manifest: which writer-emit fields carry geometry, what
# overlay kind they map to, and what GeoJSON shape we expect. Extend this
# as MCOO/Threat COA Statement/Event Template writer prompts start authoring geometry.
_WRITER_GEOMETRY_MANIFEST: dict[str, list[dict]] = {
    "SITEMP": [
        # engagement_areas moved off the polygon-emit manifest — same
        # anti-hallucination pattern as named_areas_of_interest /
        # objectives. Writer picks an id from
        # scenario.TERRAIN.engagement_areas_catalog (threat-side only)
        # and optionally overrides intent; backend _extract_writer_ea_picks
        # builds the overlay from the catalog's canonical polygon.
        # Without this migration the writer emitted only {id,
        # intent_override} but the manifest extractor still read
        # row["polygon"] which doesn't exist — net effect: zero EAs
        # ever landed in the SITEMP:<coa_id> master-map bucket.
        #
        # time_phase_lines REMOVED from the writer manifest — Gemma has no
        # spatial awareness and was authoring AO-spanning horizontal lines
        # at arbitrary latitudes. Threat time-phase lines are now DERIVED
        # at render time from axis_role_assignments + catalog avenue
        # polylines + doctrinal mechanized rate (see
        # _derive_threat_time_phase_lines).
        # named_areas_of_interest + objectives moved off this manifest —
        # the writer now PICKS ids from scenario.TERRAIN.nai_catalog /
        # .threat_objectives (anti-hallucination), and the dedicated
        # extractors _extract_writer_nai_picks /
        # _extract_writer_threat_objective_picks build the overlays from
        # the catalog's canonical MGRS. Writer no longer emits polygons
        # for those fields.
    ],
    # MCOO writer authors IPB Step-2 terrain analysis. Only AAs go through
    # the writer-overlay layer — key_terrain and obstacles already render
    # from scenario_terrain (cougaar.get_terrain) and the writer's emit was
    # a 1:1 duplicate of those (same MGRS, same names), producing
    # double-painted glyphs at every point. Writer's text-table output for
    # key_terrain / obstacles is still produced for the deliverable but
    # the geometry push is owned by scenario_terrain alone.
    "MCOO": [
        {"emit_field": "avenues_of_approach", "kind": "avenue_of_approach",
         # Per ATP 2-01.3 ¶4-22 each AA is a SERIES of mobility corridors
         # converging on one objective. The writer emits AA-level fields
         # (id, side, objective_mgrs, objective_name, rationale); the
         # post-merge enrichment hook populates row.mobility_corridors[]
         # with one entry per opposite-side key_terrain origin (per
         # _enrich_mcoo_avenues_of_approach). Each corridor becomes one
         # chevron on the map sharing the AA's id + side (for visual
         # grouping), planned via the existing terrain planner cache.
         "routing": "per_corridor_terrain_plan",
         "corridors_subfield": "mobility_corridors",
         "corridor_from_subfield": "from_mgrs",
         "to_subfield": "objective_mgrs",
         "side_subfield": "side",
         "id_subfield": "id", "name_template": "{id}",
         "attr_subfields": ["echelon", "objective_name"]},
    ],
    # Threat COA Statement / Threat Overlay / Event Template — empty until their writer
    # prompts author geometry into structured_data. See handoff.
}


def _polygon_around_mgrs(mgrs: str, *, half_lon_deg: float, half_lat_deg: float) -> dict | None:
    """Build a 4-corner rectangle Polygon (lon/lat ring, first==last) around a
    catalog MGRS. Used by the NAI / threat-objective ID-pickers so writers
    never invent positions — the catalog publishes the anchor MGRS and the
    extractor synthesizes a doctrine-sized rectangle around it.

    half_lon_deg / half_lat_deg are HALF-extents in degrees.
    Typical sizes (rough at lat 58 where 1° lon ≈ 58.8 km, 1° lat ≈ 111 km):
      - NAI (1.5×1 km):       half_lon=0.012, half_lat=0.0045
      - DIV-HQ HOLD (3×2 km): half_lon=0.025, half_lat=0.009
      - SEIZE (3×2 km):       half_lon=0.025, half_lat=0.009
    Returns None when the MGRS is unparseable."""
    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
    ll = _mgrs_to_latlon(mgrs)
    if not ll:
        return None
    lat, lon = ll
    ring = [
        [lon - half_lon_deg, lat - half_lat_deg],
        [lon + half_lon_deg, lat - half_lat_deg],
        [lon + half_lon_deg, lat + half_lat_deg],
        [lon - half_lon_deg, lat + half_lat_deg],
        [lon - half_lon_deg, lat - half_lat_deg],
    ]
    return {"type": "Polygon", "coordinates": [ring]}


def _extract_writer_nai_picks(
    product_type: str,
    structured_data: dict,
) -> list[dict]:
    """SITEMP-only: translate the writer's `named_areas_of_interest` rows
    (each picking ONE id from scenario.TERRAIN.nai_catalog) into geo-agent
    `named_area_of_interest` overlays anchored at the CATALOG's MGRS.

    Anti-hallucination guarantee: the writer cannot invent NAI positions.
    Schema enum-restricts the id to known catalog entries; backend reads
    the canonical MGRS from the catalog; rectangle synthesized at the
    catalog's anchor with doctrinal 1.5×1 km extent (per ATP 2-01.3 ¶6-20
    "small collection area sized to a single collection window").

    The writer can override the catalog's default watching_for prose
    per-COA — that prose flows into attrs.watching_for. Unknown id =
    writer hallucination → skipped + warned (the schema enum should
    catch this upstream, but the runtime guard is belt-and-suspenders).
    """
    # Broadened beyond SITEMP — Event Template also uses NAI catalog
    # picks (audit found Event Template had hallucinated MGRS for
    # NAIs because the extractor wasn't running for it). DST, Event
    # Matrix, and any future product that emits named_areas_of_interest
    # with the catalog-id pick shape goes here too.
    if product_type not in ("SITEMP", "Event Template", "Event_Template"):
        return []
    rows = (structured_data or {}).get("named_areas_of_interest") or []
    if not isinstance(rows, list) or not rows:
        return []
    catalog: dict[str, dict] = {}
    for n in (scenario.TERRAIN.get("nai_catalog") or []):
        if isinstance(n, dict) and n.get("id"):
            catalog[n["id"]] = n
    overlays: list[dict] = []
    skipped_unknown = 0
    skipped_no_geom = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        nid = (row.get("id") or "").strip()
        if not nid:
            continue
        cat = catalog.get(nid)
        if not cat:
            skipped_unknown += 1
            log.warning(
                "named_areas_of_interest: unknown id %r — writer hallucination "
                "(schema enum should have rejected it). Valid ids: %s",
                nid, sorted(catalog.keys()),
            )
            continue
        mgrs = cat.get("mgrs") or ""
        geom = _polygon_around_mgrs(mgrs, half_lon_deg=0.012, half_lat_deg=0.0045)
        if not geom:
            skipped_no_geom += 1
            continue
        watching = (row.get("watching_for") or "").strip() or (cat.get("watching_for") or "")
        overlays.append({
            "kind": "named_area_of_interest",
            "name": cat.get("name") or nid,
            "geom": geom,
            "attrs": {
                "source": "writer",
                "writer_field": "named_areas_of_interest",
                "nai_id": nid,
                "designation": nid,
                "watching_for": watching,
                "tied_to_aa": cat.get("tied_to_aa") or "",
                "threat_branch": cat.get("threat_branch") or "",
                "side": "neutral",  # NAIs are friendly-collection neutral
                "catalog_mgrs": mgrs,
            },
        })
    log.info(
        "named_areas_of_interest: SITEMP catalog ID-picker — input=%d "
        "overlays_built=%d skipped_unknown=%d skipped_no_geom=%d",
        len(rows), len(overlays), skipped_unknown, skipped_no_geom,
    )
    return overlays


def _extract_writer_ea_picks(
    product_type: str,
    structured_data: dict,
) -> list[dict]:
    """SITEMP-only: translate the writer's `engagement_areas` rows (each
    picking ONE id from scenario.TERRAIN.engagement_areas_catalog) into
    geo-agent `engagement_area` overlays anchored at the CATALOG's polygon.

    Anti-hallucination guarantee: writer cannot invent EA positions. The
    schema enum-restricts the id to known catalog entries; backend reads
    the canonical polygon + side + linked_avenue_id + firing_formation
    from the catalog. Writer may override the catalog's default_intent
    per-COA via intent_override (e.g., switching EA-VALGA-NORTH from
    mass_fires_on_attacker to disrupt for a defensive COA).

    Side comes from the catalog (threat-side EAs are the threat fires
    plan; friendly-side EAs would be the US fires plan and are NOT
    pushed by SITEMP — the SITEMP renders threat-perspective only).
    """
    if product_type != "SITEMP":
        return []
    rows = (structured_data or {}).get("engagement_areas") or []
    if not isinstance(rows, list) or not rows:
        return []
    catalog: dict[str, dict] = {}
    for e in (scenario.TERRAIN.get("engagement_areas_catalog") or []):
        if isinstance(e, dict) and e.get("id"):
            catalog[e["id"]] = e
    overlays: list[dict] = []
    skipped_unknown = 0
    skipped_no_geom = 0
    skipped_wrong_side = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        eid = (row.get("id") or "").strip()
        if not eid:
            continue
        cat = catalog.get(eid)
        if not cat:
            skipped_unknown += 1
            log.warning(
                "engagement_areas: unknown id %r — writer hallucination "
                "(schema enum should have rejected it). Valid ids: %s",
                eid, sorted(catalog.keys()),
            )
            continue
        # SITEMP is threat-perspective. Skip friendly-side EAs (those
        # belong on DSM / OPORD overlay, not SITEMP). The catalog's side
        # field is authoritative; writer cannot override it.
        cat_side = (cat.get("side") or "").strip().lower()
        if cat_side and cat_side != "threat":
            skipped_wrong_side += 1
            continue
        polygon = cat.get("polygon") or []
        if not (isinstance(polygon, list) and len(polygon) >= 3):
            skipped_no_geom += 1
            continue
        # Catalog polygons are stored as [[lon,lat], ...] ring without
        # the closing point; geom expects GeoJSON Polygon (outer ring
        # closed, first==last). Close it defensively.
        ring = [list(p) for p in polygon if isinstance(p, (list, tuple)) and len(p) == 2]
        if ring and ring[0] != ring[-1]:
            ring.append(list(ring[0]))
        geom = {"type": "Polygon", "coordinates": [ring]}
        intent = (
            (row.get("intent_override") or cat.get("default_intent") or "engage")
            .strip()
        )
        cat_name = cat.get("name") or eid
        overlays.append({
            "kind": "engagement_area",
            "name": cat_name,
            "geom": geom,
            "attrs": {
                "source": "writer",
                "writer_field": "engagement_areas",
                "engagement_area_id": eid,
                "designation": cat_name,
                "intent": intent,
                "linked_avenue_id": cat.get("linked_avenue_id") or "",
                "linked_unit": cat.get("linked_unit") or "",
                "firing_formation": cat.get("firing_formation") or "",
                "linked_phase_line": cat.get("linked_phase_line") or "",
                "rationale": cat.get("rationale") or "",
                "side": "hostile",  # threat fires plan
                "catalog_mgrs": cat.get("mgrs") or "",
            },
        })
    log.info(
        "engagement_areas: SITEMP catalog ID-picker — input=%d "
        "overlays_built=%d skipped_unknown=%d skipped_wrong_side=%d "
        "skipped_no_geom=%d",
        len(rows), len(overlays), skipped_unknown,
        skipped_wrong_side, skipped_no_geom,
    )
    return overlays


def _extract_writer_threat_objective_picks(
    product_type: str,
    structured_data: dict,
) -> list[dict]:
    """SITEMP-only: translate the writer's `objectives` rows (each picking
    ONE id from scenario.TERRAIN.threat_objectives) into geo-agent
    `threat_objective` overlays anchored at the CATALOG's MGRS.

    Anti-hallucination guarantee: writer cannot invent threat-objective
    positions. Schema enum-restricts the id; backend reads canonical MGRS
    + default_intent from the catalog; writer may override the intent verb
    via intent_override (e.g., re-taking THR-OBJ-VALGA from default DENY
    to SEIZE for an offensive COA). Polygon size: 3×2 km doctrine-sized
    rectangle so drawThreatObjective (polygon path) paints a clearly
    visible filled red AOI per the IPSR03 reference exhibit.

    Name format: "THR OBJ <VERB> <catalog name>" so drawThreatObjective's
    label-strip regex emits a single "THR OBJ ..." prefix.
    """
    if product_type != "SITEMP":
        return []
    rows = (structured_data or {}).get("objectives") or []
    if not isinstance(rows, list) or not rows:
        return []
    catalog: dict[str, dict] = {}
    for o in (scenario.TERRAIN.get("threat_objectives") or []):
        if isinstance(o, dict) and o.get("id"):
            catalog[o["id"]] = o
    overlays: list[dict] = []
    skipped_unknown = 0
    skipped_no_geom = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        oid = (row.get("id") or "").strip()
        if not oid:
            continue
        cat = catalog.get(oid)
        if not cat:
            skipped_unknown += 1
            log.warning(
                "threat_objectives: unknown id %r — writer hallucination "
                "(schema enum should have rejected it). Valid ids: %s",
                oid, sorted(catalog.keys()),
            )
            continue
        mgrs = cat.get("mgrs") or ""
        geom = _polygon_around_mgrs(mgrs, half_lon_deg=0.025, half_lat_deg=0.009)
        if not geom:
            skipped_no_geom += 1
            continue
        intent = (
            (row.get("intent_override") or cat.get("default_intent") or "hold")
            .strip().upper()
        )
        cat_name = cat.get("name") or oid
        # Strip leading "HOLD "/"DENY "/"SEIZE " from the catalog name when
        # building the display label — older catalog entries had the verb
        # baked into the name ("HOLD Tartu (HQ WESTERN ARMY)"). New entries
        # don't. Use the override-or-default verb either way.
        import re as _re
        bare_name = _re.sub(r"^(HOLD|DENY|SEIZE|RETAIN)\s+", "", cat_name,
                              flags=_re.IGNORECASE)
        display_name = f"THR OBJ {intent} {bare_name}"
        overlays.append({
            "kind": "threat_objective",
            "name": display_name,
            "geom": geom,
            "attrs": {
                "source": "writer",
                "writer_field": "objectives",
                "objective_id": oid,
                "designation": display_name,
                "intent": intent.lower(),
                "rationale": cat.get("rationale") or "",
                "side": "hostile",
                "catalog_mgrs": mgrs,
            },
        })
    log.info(
        "threat_objectives: SITEMP catalog ID-picker — input=%d "
        "overlays_built=%d skipped_unknown=%d skipped_no_geom=%d",
        len(rows), len(overlays), skipped_unknown, skipped_no_geom,
    )
    return overlays


def _extract_writer_dp_picks(
    product_type: str,
    structured_data: dict,
) -> list[dict]:
    """Event-Template / SITEMP: translate the writer's `decision_points`
    rows (each picking ONE id from scenario.TERRAIN.dp_catalog) into
    geo-agent `decision_point` overlays anchored at the CATALOG's MGRS.

    Anti-hallucination guarantee: the writer cannot invent friendly-DP
    text, owning-commander, ltiov, or position. Schema enum-restricts
    the id to known catalog entries (DP-1..DP-5); backend reads the
    canonical friendly_decision / owning_commander / phase / ltiov /
    triggering_indicator / linked_nais / options + anchor MGRS from the
    catalog. Writer-invented DP rows were producing enemy-decision text
    in the friendly-DP column (e.g., "Commit 604 TANK BDE to
    counterattack") which is doctrinally wrong — friendly DPs are
    FRIENDLY-commander decisions triggered BY enemy activity.

    Writer may supply context_override prose to refine / scope the
    catalog default (e.g., bounding the DP to a single COA branch);
    that prose flows into attrs.context_override and is surfaced
    alongside the catalog's canonical friendly_decision.

    DP catalog entry SHOULD carry an `mgrs` anchor; if absent we fall
    through to the first `linked_nais` entry's MGRS by looking it up
    in the NAI catalog. If neither anchor resolves, the row is logged
    and skipped (no silent geometry hallucination).

    Half-extents 0.005×0.003 deg ≈ 600×330 m — DP marker sized smaller
    than NAI rectangles (NAIs are 1.5×1 km collection areas; DPs are
    point-decisions rendered as a compact marker).
    """
    if product_type not in ("SITEMP", "Event Template", "Event_Template"):
        return []
    rows = (structured_data or {}).get("decision_points") or []
    if not isinstance(rows, list) or not rows:
        return []
    dp_catalog: dict[str, dict] = {}
    for d in (scenario.TERRAIN.get("dp_catalog") or []):
        if isinstance(d, dict) and d.get("id"):
            dp_catalog[d["id"]] = d
    nai_catalog: dict[str, dict] = {}
    for n in (scenario.TERRAIN.get("nai_catalog") or []):
        if isinstance(n, dict) and n.get("id"):
            nai_catalog[n["id"]] = n
    overlays: list[dict] = []
    skipped_unknown = 0
    skipped_no_geom = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        did = (row.get("id") or "").strip()
        if not did:
            continue
        cat = dp_catalog.get(did)
        if not cat:
            skipped_unknown += 1
            log.warning(
                "decision_points: unknown id %r — writer hallucination "
                "(schema enum should have rejected it). Valid ids: %s",
                did, sorted(dp_catalog.keys()),
            )
            continue
        # Anchor MGRS resolution: catalog `mgrs` is preferred; fall back
        # to the first linked NAI's catalog MGRS (the DP is doctrinally
        # observed AT the NAI that triggers it, so the NAI anchor is a
        # defensible secondary). No silent invention beyond that.
        mgrs = (cat.get("mgrs") or "").strip()
        anchor_source = "dp_catalog.mgrs"
        if not mgrs:
            linked = (cat.get("linked_nais") or "")
            first_nai = (linked.split(",")[0] or "").strip() if linked else ""
            if first_nai and first_nai in nai_catalog:
                mgrs = (nai_catalog[first_nai].get("mgrs") or "").strip()
                anchor_source = f"nai_catalog[{first_nai}].mgrs"
        if not mgrs:
            skipped_no_geom += 1
            log.warning(
                "decision_points: id %r has no anchor mgrs (catalog mgrs "
                "missing and no linked NAI resolved); skipping. Add "
                "`mgrs` to scenario.TERRAIN.dp_catalog[%r] to fix.",
                did, did,
            )
            continue
        geom = _polygon_around_mgrs(mgrs, half_lon_deg=0.005, half_lat_deg=0.003)
        if not geom:
            skipped_no_geom += 1
            continue
        context_override = (row.get("context_override") or "").strip()
        friendly_decision = (cat.get("friendly_decision") or "").strip()
        overlays.append({
            "kind": "decision_point",
            "name": cat.get("name") or did,
            "geom": geom,
            "attrs": {
                "source": "writer",
                "writer_field": "decision_points",
                "dp_id": did,
                "designation": did,
                "owner": "friendly",
                "owning_commander": cat.get("owning_commander") or "",
                "friendly_decision": friendly_decision,
                "context_override": context_override,
                "phase": cat.get("phase") or "",
                "ltiov": cat.get("ltiov") or "",
                "latest_time": cat.get("ltiov") or "",
                "triggering_indicator": cat.get("triggering_indicator") or "",
                "linked_nais": cat.get("linked_nais") or "",
                "associated_nais": cat.get("linked_nais") or "",
                "options": cat.get("options") or "",
                "side": "friendly",
                "catalog_mgrs": mgrs,
                "anchor_source": anchor_source,
            },
        })
    log.info(
        "decision_points: catalog ID-picker — product=%s input=%d "
        "overlays_built=%d skipped_unknown=%d skipped_no_geom=%d",
        product_type, len(rows), len(overlays), skipped_unknown, skipped_no_geom,
    )
    return overlays


# Closed enum of doctrinal tactical-mission-task roles the per-COA
# axis-of-advance painter recognizes. Per FM 1-02.2 Ch 5 + FM 3-90
# (May 2023). 'demonstration' is the FM 3-90 May 2023 form-of-the-
# offense companion to 'feint' (show of force without contact vs feint's
# show of force WITH contact). Rows whose role is not in this set are
# logged + skipped (a writer-emitted unknown role is a writer bug; the
# painter has no dispatch for it).
_AXIS_ROLE_ALLOW_LIST = frozenset({
    "main_effort",
    "supporting",
    "feint",
    "demonstration",
    "counter_attack",
})


def _coa_engaged_friendly_avenue_set(structured_data: dict) -> set[str]:
    """Derive the set of friendly avenue-of-approach ids the writer's
    `axis_role_assignments` rows engage for this specific COA. Used by
    the scenario-terrain push to filter scenario-canonical threat
    engagement areas: an EA whose `linked_avenue_id` is not in this
    set is for a corridor THIS COA does not actively engage — painting
    it on the SITEMP would imply contact on a corridor the COA's
    scheme of maneuver does not include and would mislead the analyst.
    Returns an empty set if the writer emitted no axis_role_assignments
    rows or none resolve to a known catalog avenue.

    Resolution per row:
      - if the catalog avenue has side=friendly (a friendly corridor
        the threat is using in reverse / spoiling on), the avenue's
        own id IS the engaged friendly avenue.
      - if the catalog avenue has side=threat (a threat counter-attack
        axis), read its `targets_friendly_avenue_id` — that names which
        friendly avenue this threat counter-attack is countering.
        Required on every threat-side catalog avenue.
    """
    rows = (structured_data or {}).get("axis_role_assignments") or []
    if not isinstance(rows, list) or not rows:
        return set()
    aas_by_id: dict[str, dict] = {}
    for av in (scenario.TERRAIN.get("avenues_of_approach") or []):
        if isinstance(av, dict) and av.get("id"):
            aas_by_id[av["id"]] = av
    engaged: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        axis_id = (row.get("axis_id") or "").strip()
        if not axis_id:
            continue
        av = aas_by_id.get(axis_id)
        if not av:
            continue
        av_side = (av.get("side") or "").strip().lower()
        if av_side == "friendly":
            engaged.add(axis_id)
        else:
            tgt = (av.get("targets_friendly_avenue_id") or "").strip()
            if tgt:
                engaged.add(tgt)
    return engaged


def _extract_writer_axis_role_assignments(
    product_type: str,
    structured_data: dict,
) -> list[dict]:
    """SITEMP-only: translate the writer's `axis_role_assignments` rows
    into geo-agent `axis_of_advance` overlays for the per-COA threat
    scheme of maneuver. Each row LOOKS UP an existing avenue of approach
    by id in scenario.TERRAIN.avenues_of_approach. Polyline orientation
    is determined AUTOMATICALLY from the catalog avenue's `side` field —
    a friendly-side corridor (AA1/AA2/AA3) re-used by the threat is
    REVERSED on push (the friendly-native polyline goes south→north, so
    the threat using it for a south-bound spoiling attack needs the
    arrow head at the southern end). A threat-side corridor (AA-THR-*)
    is left as-is — those polylines are already stored in threat-attack
    direction (rear→objective). The writer NEVER picks direction; that
    is a runtime decision.

    `side='hostile'` is forced on the resulting overlay regardless of
    the source avenue's home affiliation — the role-assignment is the
    threat's plan, not the terrain corridor's natural side.

    Returns an empty list for non-SITEMP products or when the writer
    emitted nothing. Rows whose role is not in `_AXIS_ROLE_ALLOW_LIST`
    are logged + skipped (closed enum per the schema's
    axis_role_assignments.role definition). Rows with unknown axis_id
    are logged + skipped (writer hallucination)."""
    if product_type != "SITEMP":
        return []
    rows = (structured_data or {}).get("axis_role_assignments") or []
    if not isinstance(rows, list) or not rows:
        return []
    aas_by_id: dict[str, dict] = {}
    for av in (scenario.TERRAIN.get("avenues_of_approach") or []):
        if isinstance(av, dict) and av.get("id"):
            aas_by_id[av["id"]] = av
    overlays: list[dict] = []
    skipped_unknown = 0
    skipped_no_geom = 0
    skipped_unknown_role = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        axis_id = (row.get("axis_id") or "").strip()
        role = (row.get("role") or "").strip().lower()
        unit = (row.get("unit_designation") or "").strip()
        narrative_basis = row.get("narrative_basis") or ""
        if not (axis_id and role):
            continue
        if role not in _AXIS_ROLE_ALLOW_LIST:
            skipped_unknown_role += 1
            log.warning(
                "axis_role_assignments: unknown role %r (axis_id=%s); "
                "closed enum — skipping. Valid roles: %s",
                role, axis_id, sorted(_AXIS_ROLE_ALLOW_LIST),
            )
            continue
        av = aas_by_id.get(axis_id)
        if not av:
            skipped_unknown += 1
            log.warning(
                "axis_role_assignments: unknown axis_id %r (role=%s); "
                "writer hallucination — skipping. Valid ids: %s",
                axis_id, role, sorted(aas_by_id.keys()),
            )
            continue
        polyline = av.get("central_axis_polyline") or []
        if not (isinstance(polyline, list) and len(polyline) >= 2):
            skipped_no_geom += 1
            continue
        coords = [list(p) for p in polyline]
        # AUTO-REVERSE based on catalog side. Friendly-side corridors
        # (AA1/AA2/AA3 with side='friendly') are stored in friendly-attack
        # direction (south→north); when the threat re-uses one for a
        # south-bound action the polyline must be reversed so the arrow
        # head lands at the threat's objective (south). Threat-side
        # corridors (AA-THR-*) are already stored in threat-attack
        # direction (rear→objective); no reversal needed.
        av_side = (av.get("side") or "").strip().lower()
        if av_side == "friendly":
            coords = list(reversed(coords))
        direction_used = "reverse" if av_side == "friendly" else "forward"
        role_label = role.upper().replace("_", " ")
        name = f"{role_label} {unit}".strip() or role_label or axis_id
        overlays.append({
            "kind": "axis_of_advance",
            "name": name,
            "geom": {"type": "LineString", "coordinates": coords},
            "attrs": {
                "source": "writer",
                "writer_field": "axis_role_assignments",
                "side": "hostile",  # SITEMP scheme of maneuver = threat
                "role": role,
                "unit_designation": unit,
                "axis_id_source": axis_id,
                "direction": direction_used,
                "echelon": av.get("echelon") or "division",
                "narrative_basis": narrative_basis,
                # Carry the source AA's envelope for the renderer that
                # may want to paint a translucent corridor underlay
                # matching the terrain corridor's natural width.
                "bundle_halfwidth_m": float(av.get("bundle_halfwidth_m") or 5000.0),
            },
        })
    log.info(
        "axis_role_assignments: SITEMP per-COA scheme of maneuver — "
        "input=%d overlays_built=%d skipped_unknown_axis=%d "
        "skipped_unknown_role=%d skipped_no_geom=%d",
        len(rows), len(overlays), skipped_unknown,
        skipped_unknown_role, skipped_no_geom,
    )
    return overlays


# ---------------------------------------------------------------------------
# Threat time-phase line derivation (ATP 2-01.3 Fig 6-3).
#
# A SITEMP carries threat time-phase lines marking the threat's projected
# forward edge at each H+N. Per doctrine, ONE phase line connects the
# leading-edge points across ALL engaged movement axes — i.e. for COA-1
# (three TANK BDE counter-attacks) the H+12 phase line is the LineString
# connecting where 604 TANK BDE has reached on AA-THR-604CTRATK-KOPU-FLANK,
# where 623 TANK BDE has reached on AA-THR-623CTRATK-RONGU-FLANK, and where
# 663 TANK BDE has reached on AA-THR-663CTRATK-OSULA-FLANK at hour 12.
#
# Derivation runs at SITEMP render time from the writer's axis_role_
# assignments + the catalog avenue polylines + a doctrinal rate of march.
# Writer authors NO geometry — Gemma has no spatial awareness and was
# emitting AO-spanning horizontal lines at arbitrary latitudes regardless
# of where the threat was actually projected to be. Replaced 2026-06-01.
#
# Movement roles only: main_effort / supporting / counter_attack. Feint
# and demonstration are NOT real movement (show-of-force without effective
# forward motion), so they don't contribute a forward edge.
#
# Minimum 2 movement axes — a single-axis "phase line" is doctrinally a
# point, not a coordination line, so COAs with 0 or 1 movement axes emit
# zero time-phase lines (positional defense COAs typically fall here and
# correctly carry no threat time-phase lines).

_DOCTRINAL_RATE_KMH = 6.0           # sustained mechanized rate per ATP 2-01.3
_DOCTRINAL_H_INTERVALS = (12, 24, 48, 72)  # ATP 2-01.3 Fig 6-3 set
_MOVEMENT_ROLES = frozenset({"main_effort", "supporting", "counter_attack"})


def _haversine_km(a: list, b: list) -> float:
    """Great-circle distance in km between [lon, lat] points."""
    import math
    lon1, lat1 = math.radians(a[0]), math.radians(a[1])
    lon2, lat2 = math.radians(b[0]), math.radians(b[1])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    h = (math.sin(dlat / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def _point_at_distance_along_polyline(coords: list, target_km: float):
    """Walk a polyline [(lon,lat), ...] accumulating great-circle segment
    distances; return the [lon, lat] point at exactly `target_km` from
    the start. Linear interpolation within the crossing segment.
    Returns None when:
      - polyline is degenerate (<2 points)
      - target_km exceeds polyline length (the unit has completed its
        traversal — it has no "leading edge" to mark at this H+N).

    The earlier clamp-to-endpoint behaviour was wrong: short threat
    counter-attack axes (10-15 km at ~6 km/h take ~2.5 h to complete)
    collapsed every H+12 / H+24 / H+48 / H+72 marker to the same
    polyline endpoint — four phase lines stacked at identical
    coordinates look like zero phase lines on the rendered map. The
    correct doctrinal behaviour is to OMIT the marker for axes whose
    units have already arrived. _derive_threat_time_phase_lines then
    skips H+N when fewer than 2 axes contribute a point.
    """
    if not isinstance(coords, list) or len(coords) < 2:
        return None
    if target_km <= 0:
        return list(coords[0])
    total = 0.0
    for i in range(len(coords) - 1):
        a, b = coords[i], coords[i + 1]
        seg_km = _haversine_km(a, b)
        if total + seg_km >= target_km:
            t = (target_km - total) / seg_km if seg_km > 0 else 0.0
            return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        total += seg_km
    return None


def _derive_threat_time_phase_lines(
    product_type: str,
    structured_data: dict,
    *,
    weather_modulator: float = 1.0,
) -> list[dict]:
    """SITEMP-only. Derive ATP 2-01.3 Fig 6-3 threat time-phase lines from
    the writer's axis_role_assignments + catalog avenue polylines. Each
    H+N output line connects the forward-edge points across ALL engaged
    movement axes at that hour.

    Args:
      product_type: must equal "SITEMP"; returns [] otherwise.
      structured_data: writer's emit; reads axis_role_assignments rows.
      weather_modulator: 0.0-1.0 multiplier on the doctrinal 6 km/h rate.
        Light rain ~0.8, heavy rain ~0.5, dry ~1.0. Conservative default
        1.0 — caller is responsible for resolving weather and passing the
        modulator in (the IPB WEM is the canonical source).

    Returns: overlay dicts ready for the geo-agent push. Each has kind=
    phase_line, attrs.h_hour="H+N" (drives the banner-box rendering per
    drawPhaseLineLabel), and attrs.source="derived" so callers can
    distinguish from scenario_terrain / writer pushes.
    """
    if product_type != "SITEMP":
        return []
    rows = (structured_data or {}).get("axis_role_assignments") or []
    if not isinstance(rows, list) or len(rows) < 2:
        return []

    aas_by_id: dict[str, dict] = {}
    for av in (scenario.TERRAIN.get("avenues_of_approach") or []):
        if isinstance(av, dict) and av.get("id"):
            aas_by_id[av["id"]] = av

    moving_axes: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        role = (row.get("role") or "").strip().lower()
        axis_id = (row.get("axis_id") or "").strip()
        if role not in _MOVEMENT_ROLES or not axis_id:
            continue
        av = aas_by_id.get(axis_id)
        if not av:
            continue
        polyline = av.get("central_axis_polyline") or []
        if not (isinstance(polyline, list) and len(polyline) >= 2):
            continue
        # Threat direction: friendly-side avenues used by the threat in
        # reverse (threat south-bound on a friendly-native polyline)
        # need the polyline reversed so the unit's leading edge tracks
        # the right direction. Threat-side avenues (AA-THR-*) are stored
        # in threat-attack direction already; no reversal.
        av_side = (av.get("side") or "").strip().lower()
        coords = [list(p) for p in polyline]
        if av_side == "friendly":
            coords = list(reversed(coords))
        moving_axes.append({
            "axis_id": axis_id,
            "coords": coords,
            "rate_kmh": _DOCTRINAL_RATE_KMH,
        })

    if len(moving_axes) < 2:
        return []

    # Order axes west-to-east by their start point's longitude so the
    # connected phase line traces left-to-right consistently across the
    # AO and matches the analyst's natural read order.
    moving_axes.sort(key=lambda a: a["coords"][0][0])
    axis_ids = [a["axis_id"] for a in moving_axes]

    overlays: list[dict] = []
    for h in _DOCTRINAL_H_INTERVALS:
        pts: list[list[float]] = []
        for ax in moving_axes:
            target_km = ax["rate_kmh"] * h * weather_modulator
            pt = _point_at_distance_along_polyline(ax["coords"], target_km)
            if pt is None:
                # Axis polyline shorter than target_km — unit has already
                # completed its traversal by H+N. No leading edge to mark
                # for this axis; skip its contribution to the H+N line.
                continue
            pts.append(pt)
        if len(pts) < 2:
            # Fewer than 2 axes still advancing at H+N — no coordination
            # line between them. Doctrinally correct for short tactical
            # counter-attack COAs: the unit arrived hours ago, there's
            # nothing to phase at H+12 / +24 / +48 / +72.
            continue
        overlays.append({
            "kind": "phase_line",
            "name": f"H+{h}",
            "geom": {"type": "LineString", "coordinates": pts},
            "attrs": {
                "source": "derived",
                "h_hour": f"H+{h}",
                "rate_basis": (
                    f"{_DOCTRINAL_RATE_KMH:.0f} km/h mechanized × "
                    f"weather modulator {weather_modulator:.2f}"
                ),
                "axes_engaged": axis_ids,
            },
        })
    log.info(
        "threat_time_phase_lines (derived): product=%s "
        "movement_axes=%d intervals=%d overlays_built=%d",
        product_type, len(moving_axes), len(_DOCTRINAL_H_INTERVALS),
        len(overlays),
    )
    return overlays


def _extract_writer_range_fans(
    product_type: str,
    current_emit: dict,
    intel_data: dict,
) -> list[dict]:
    """SITEMP per-equipment range fan derivation.

    For each row in `current_emit.templated_units`, look up the matching
    cougaar OOB row by id, walk its
    `composition.equipment_list_with_counts`, and emit ONE range_fan
    overlay per UNIQUE nomenclature with an EQ_ENGAGEMENT_RANGE_M entry.
    Fan is anchored at the templated unit's location_mgrs (the OE-
    adjusted SITEMP position, not the baseline OOB MGRS).

    Why this lives on SITEMP and not on the live cougaar push:
      SITEMP is a frozen IPB Step-4 artifact. Once it lands, its
      stand-off envelopes are doctrinal facts about the COA — they
      MUST persist on the master map regardless of whether the live
      cougaar GraphQL is currently reachable. Pushing them under the
      SITEMP:<coa_id> bucket (via the writer-overlays chain in
      tool_render_map) gives them the same atomic-replace lifecycle
      as every other writer overlay — the next SITEMP fanout for
      this COA replaces the prior fan set; cougaar going down does
      not.

    Returns [] for non-SITEMP products, empty templated_units, missing
    OOB cache, or templated units whose composition has no range-fan-
    eligible equipment.

    De-duplication: one fan per UNIQUE nomenclature per unit. A battery
    with 18 BM-21s gets ONE BM-21 fan, not 18 stacked at the same point.
    The renderer paints rings, not symbology counts.
    """
    # Per-product unit-source strategy. Every product with `range_fan`
    # in its map_overlay_kinds gets per-equipment fans here — SITEMP
    # (writer's templated_units), Threat Overlay (kg_units_active
    # row_source → current_emit.rows), Threat Model (writer's
    # threat_template_graphic.templated_units subfield), Enemy COA /
    # GRINTSUM (no writer-side unit emit — fall through to direct
    # cougaar OOB units from intel_data). Adding a new range_fan-
    # capable product means adding an entry below; the per-equipment
    # emit loop is unchanged.
    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
    from utils import equipment_doctrine as _eq

    pt_upper = (product_type or "").upper()
    unit_rows: list = []
    if pt_upper == "SITEMP":
        unit_rows = (current_emit or {}).get("templated_units") or []
    elif pt_upper == "THREAT OVERLAY":
        # row_source=kg_units_active → unit rows live under .rows.
        # Each row carries id (kg_id), name, location_mgrs same as a
        # templated unit. Empty when the writer hasn't merged rows yet
        # (first-render path) — that's fine, the second render after
        # merge fills them.
        unit_rows = (current_emit or {}).get("rows") or []
    elif pt_upper == "THREAT MODEL":
        ttg = (current_emit or {}).get("threat_template_graphic") or {}
        if isinstance(ttg, dict):
            unit_rows = ttg.get("templated_units") or []
    elif pt_upper == "GRINTSUM":
        # No writer indirection — pull units straight from the cougaar
        # OOB dep payload. Threat-side units only (force_type=HOSTILE).
        for resp in ((intel_data or {}).get("get_oob") or []):
            if not isinstance(resp, dict):
                continue
            for u in resp.get("units") or []:
                if not isinstance(u, dict):
                    continue
                if (u.get("force_type") or "").upper() != "HOSTILE":
                    continue
                # Normalize the shape so the loop below sees the same
                # keys it sees for SITEMP templated_units: id +
                # identity + location_mgrs.
                unit_rows.append({
                    "id":             u.get("kg_id") or u.get("id"),
                    "identity":       u.get("name") or u.get("designator"),
                    "location_mgrs":  (
                        (u.get("primary_position") or {}).get("mgrs")
                        if isinstance(u.get("primary_position"), dict)
                        else None
                    ) or u.get("location_mgrs"),
                    # Carry the raw composition through so the per-
                    # equipment loop doesn't need to re-look up the OOB.
                    "_oob_composition_rollup": u.get("composition_rollup") or {},
                })
    else:
        return []

    if not isinstance(unit_rows, list) or not unit_rows:
        return []

    # Reassign the per-unit-row name expected downstream — SITEMP's
    # existing emit code refers to `templated_units` only by variable
    # name; everything else uses `tu`. The original variable name is
    # kept for diff hygiene.
    templated_units = unit_rows

    # OOB row lookup. Match templated_units against the live OOB by id
    # first, falling back to identity/name. The repush_sitemp_artifact
    # path matters here: a SITEMP artifact captured a templated_units
    # snapshot with kbids that are STABLE only within that fanout —
    # the next cougaar OOB snapshot rotates kbids, so id-only lookup
    # finds zero matches when re-deriving range fans for an existing
    # artifact. Unit designators (`name` on OOB, `identity` on
    # templated_units) are stable across snapshots; falling back to
    # them recovers ~70%+ of units.
    oob_by_id: dict[str, dict] = {}
    oob_by_name: dict[str, dict] = {}
    for resp in ((intel_data or {}).get("get_oob") or []):
        if not isinstance(resp, dict):
            continue
        for u in resp.get("units") or []:
            if not isinstance(u, dict):
                continue
            uid = u.get("id") or u.get("kg_id") or ""
            if uid:
                oob_by_id[uid] = u
            nm = (u.get("name") or "").strip()
            if nm:
                oob_by_name[nm] = u

    overlays: list[dict] = []
    units_with_fans = 0
    for tu in templated_units:
        if not isinstance(tu, dict):
            continue
        tu_id = (tu.get("id") or "").strip()
        tu_mgrs = (tu.get("location_mgrs") or "").strip()
        if not tu_mgrs:
            continue
        p = _mgrs_to_latlon(tu_mgrs)
        if not p:
            continue
        oob_row = oob_by_id.get(tu_id) or {}
        if not oob_row:
            tu_identity = (tu.get("identity") or "").strip()
            if tu_identity:
                oob_row = oob_by_name.get(tu_identity) or {}
        # cougaar_adapters.get_red_oob_resolved emits per-unit equipment as a
        # FLAT `composition_rollup` dict ({nomenclature: count}) on the unit
        # row, NOT under a nested `composition.equipment_list_with_counts`.
        # Fall back to the legacy nested shape only if the rollup isn't set —
        # keeps this resilient if the adapter shape rotates back.
        rollup = oob_row.get("composition_rollup") or {}
        if not rollup:
            legacy = oob_row.get("composition") or {}
            for er in (legacy.get("equipment_list_with_counts") or []):
                if isinstance(er, dict):
                    n = (er.get("nomenclature") or "").strip()
                    if n:
                        rollup[n] = rollup.get(n, 0) + int(er.get("count") or 1)
        seen_nomenclatures: set[str] = set()
        unit_label = (tu.get("identity") or tu_id).strip()
        unit_emitted = False
        for nom_raw in rollup.keys():
            nom = (nom_raw or "").strip()
            if not nom or nom in seen_nomenclatures:
                continue
            range_m = _eq.equipment_engagement_range_m(nom)
            if not range_m:
                continue
            seen_nomenclatures.add(nom)
            subkind = _eq.equipment_range_subkind(nom)
            overlays.append({
                "kind": "range_fan",
                "name": f"{unit_label} {nom}",
                "geom": {"type": "Point", "coordinates": [p[1], p[0]]},
                "attrs": {
                    "rf_id": f"sitemp:{tu_id}:{nom}",
                    "bearer_unit_kg_id": tu_id,
                    "bearer_unit_name": unit_label,
                    "equipment_nomenclature": nom,
                    "bands": [
                        {"radius_m": int(range_m),
                         "label": f"{nom} {int(range_m / 1000)}km"},
                    ],
                    "radius_m": int(range_m),
                    "subkind": subkind,
                    "side": "threat",
                    "mgrs": tu_mgrs,
                    # source + coa_id stamped by _push_overlays_to_master_map
                    # at push-time (URL-param bucket overrides body source).
                },
            })
            unit_emitted = True
        if unit_emitted:
            units_with_fans += 1
    log.info(
        "%s range fans derived: units=%d fans=%d (eligible equipment "
        "per EQ_ENGAGEMENT_RANGE_M; one fan per unique nomenclature per "
        "unit)",
        product_type, units_with_fans, len(overlays),
    )
    return overlays


def _extract_writer_overlays(
    product_type: str,
    structured_data: dict,
    *,
    map_id: str | None = None,
) -> list[dict]:
    """Walk merged structured_data for `product_type` and return geo-agent
    overlay dicts derived from writer-emitted geometry. Returns [] when the
    product has no manifest entry or the writer authored no geometry.

    Manifest spec keys:
      - emit_field, kind, id_subfield, name_template
      - geom_subfield + shape: writer emits the geometry directly (Polygon /
        LineString / Point — used by SITEMP for EAs / NAIs / TPLs)
      - routing="terrain_plan" + from_subfield + to_subfield: writer emits
        only two MGRS endpoints and the geo-agent A* terrain planner
        interpolates the centerline. `map_id` must be provided to call the
        planner.
      - routing="per_corridor_terrain_plan" + corridors_subfield +
        corridor_from_subfield + to_subfield: each writer row is an avenue
        of approach with a pipeline-derived nested list of mobility
        corridors (row[corridors_subfield] = [{from_mgrs, ...}, ...]).
        Each corridor produces one overlay sharing the avenue's id (in
        attrs.avenue_of_approach_id for visual grouping) + side + per-row
        attr_subfields. Used by MCOO per ATP 2-01.3 ¶4-22 corridor-
        aggregation semantics.
      - side: hardcoded constant (used by SITEMP — all writer EAs/objectives
        are hostile)
      - side_subfield: row key naming the side enum (used by MCOO —
        avenues_of_approach[].side carries friendly/threat/neutral per-row)
      - attr_subfields: list of row keys whose values pass through to
        attrs (echelon / width_km / decisive / etc.) so the renderer can
        style accordingly.
    """
    from utils.coords import mgrs_to_latlon as _mgrs_to_latlon

    def _route_endpoints_to_geom(
        planner_route_spec: dict, p_from: tuple, p_to: tuple
    ) -> dict | None:
        plan = (
            _plan_terrain_route(map_id, planner_route_spec, p_from, p_to)
            if map_id else None
        )
        coords = plan["coords"] if plan else None
        if not coords:
            coords = [
                [p_from[1], p_from[0]],
                [p_to[1], p_to[0]],
            ]
        return {"type": "LineString", "coordinates": coords}

    manifest = _WRITER_GEOMETRY_MANIFEST.get(product_type or "")
    if not manifest or not isinstance(structured_data, dict):
        return []
    overlays: list[dict] = []
    for spec in manifest:
        rows = structured_data.get(spec["emit_field"]) or []
        if not isinstance(rows, list):
            continue
        # Track origin MGRS already used as another avenue's primary on
        # this side. Two avenues that share an objective should NOT both
        # pick the same primary origin — the doctrinal pattern (per ATP
        # 2-01.3 Fig 4-6) is one primary axis per avenue, with avenues
        # spread across different geographic approaches. Greedy allocation:
        # each avenue in order picks its closest available (unused) same-
        # side origin. Per-side claim sets so friendly + threat origin
        # pools are independent (a friendly Cimza claim doesn't prevent a
        # threat avenue from anchoring there).
        primary_claimed_by_side: dict[str, set[str]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            row_id = row.get(spec["id_subfield"]) or ""
            row_side = None
            side_sub = spec.get("side_subfield")
            if side_sub and row.get(side_sub):
                row_side = row.get(side_sub)
            elif spec.get("side"):
                row_side = spec["side"]

            if spec.get("routing") == "per_corridor_terrain_plan":
                # Fast path A: synthesized avenue (terrain-circuit method).
                # Geom is the central axis polyline (for backward compat
                # with renderers expecting LineString). The hollow
                # multi-tail envelope + per-tail corridor coords ride in
                # attrs so the SVG drawer can paint the doctrinal Fig
                # 4-14 / paper Fig 8 envelope on top.
                envelope = row.get("_envelope_polygon") or []
                if row.get("_synthesized"):
                    central = row.get("_route_coords") or []
                    tails = row.get("_tail_corridor_coords") or []
                    corridors = row.get(
                        spec.get("corridors_subfield") or "mobility_corridors"
                    ) or []
                    if isinstance(central, list) and len(central) >= 2:
                        attrs = {
                            "source": "writer",
                            "writer_field": spec["emit_field"],
                            "writer_row_id": row_id,
                            "avenue_of_approach_id": row_id,
                            "envelope_polygon": [list(p) for p in envelope]
                                                if isinstance(envelope, list) else [],
                            "tail_coords": [[list(p) for p in line] for line in tails],
                            "tail_count": len(tails),
                            "constituent_corridor_count": len(corridors),
                            "bundle_halfwidth_m": float(row.get("_bundle_halfwidth_m") or 0.0),
                            "synthesized": True,
                        }
                        if row_side:
                            attrs["side"] = row_side
                        for attr_key in (spec.get("attr_subfields") or []):
                            val = row.get(attr_key)
                            if val is not None and val != "":
                                attrs[attr_key] = val
                        overlays.append({
                            "kind": spec["kind"],
                            "name": row_id,
                            "geom": {
                                "type": "LineString",
                                "coordinates": [list(p) for p in central],
                            },
                            "attrs": {k: v for k, v in attrs.items() if v is not None},
                        })
                        continue
                # Fast path B: legacy doctrinal-discovery row carrying just
                # `_route_coords` (single planner-routed line from origin
                # KT to objective KT). Paint as a single-tail chevron.
                route_coords = row.get("_route_coords") or []
                if isinstance(route_coords, list) and len(route_coords) >= 2:
                    corridors = row.get(
                        spec.get("corridors_subfield") or "mobility_corridors"
                    ) or []
                    c = corridors[0] if corridors else {}
                    attrs = {
                        "source": "writer",
                        "writer_field": spec["emit_field"],
                        "writer_row_id": row_id,
                        "avenue_of_approach_id": row_id,
                        "primary_corridor_id": c.get("corridor_id"),
                        "primary_from_name": c.get("from_name"),
                        "primary_from_mgrs": c.get("from_mgrs"),
                        "constituent_corridor_count": len(corridors),
                        "discovered": True,
                    }
                    if row_side:
                        attrs["side"] = row_side
                    for attr_key in (spec.get("attr_subfields") or []):
                        val = row.get(attr_key)
                        if val is not None and val != "":
                            attrs[attr_key] = val
                    overlays.append({
                        "kind": spec["kind"],
                        "name": row_id,
                        "geom": {
                            "type": "LineString",
                            "coordinates": [list(p) for p in route_coords],
                        },
                        "attrs": {k: v for k, v in attrs.items() if v is not None},
                    })
                    continue
                # Legacy fallback: row carries only a discovered corridor
                # centerline (no `_route_coords`). Paint that as the
                # chevron — produces the stub-chevron the user has
                # called out; but better than dropping the avenue.
                if row.get("_discovered_corridor_id"):
                    corridors = row.get(spec.get("corridors_subfield") or "mobility_corridors") or []
                    if corridors:
                        c = corridors[0]
                        centerline = (c.get("centerline") or {}).get("coordinates") or []
                        if len(centerline) >= 2:
                            attrs = {
                                "source": "writer",
                                "writer_field": spec["emit_field"],
                                "writer_row_id": row_id,
                                "avenue_of_approach_id": row_id,
                                "primary_corridor_id": c.get("corridor_id"),
                                "primary_from_name": c.get("from_name"),
                                "primary_from_mgrs": c.get("from_mgrs"),
                                "constituent_corridor_count": len(corridors),
                                "discovered": True,
                            }
                            if row_side:
                                attrs["side"] = row_side
                            for attr_key in (spec.get("attr_subfields") or []):
                                val = row.get(attr_key)
                                if val is not None and val != "":
                                    attrs[attr_key] = val
                            overlays.append({
                                "kind": spec["kind"],
                                "name": row_id,
                                "geom": {
                                    "type": "LineString",
                                    "coordinates": [list(p) for p in centerline],
                                },
                                "attrs": {k: v for k, v in attrs.items() if v is not None},
                            })
                            continue  # avenue emitted, next row
                # Per ATP 2-01.3 Figure 4-6 / Figure 4-10 example MCOOs:
                # an avenue of approach is shown as ONE envelope (single
                # chevron) — the formation's general approach toward the
                # objective. The constituent mobility corridors (¶4-22
                # "series of mobility corridors") are the doctrinal detail
                # captured in the avenue's table row, NOT separate map
                # features. Drawing one chevron per corridor produced N
                # overlapping chevrons all converging on each objective,
                # which is the visual mess the doctrinal MCOO explicitly
                # avoids.
                #
                # Pick the PRIMARY corridor (shortest haversine origin →
                # objective among the planned constituents — that's the
                # most direct approach axis the formation would prefer per
                # ¶4-23 "avenues of approach should be prioritized based
                # on how well each supports... timely and efficiently").
                # Render that one chevron for the avenue; keep the full
                # corridor list in the data row for doc-table detail.
                corridors = row.get(spec.get("corridors_subfield") or "mobility_corridors") or []
                if not isinstance(corridors, list) or not corridors:
                    continue
                to_mgrs = row.get(spec.get("to_subfield") or "objective_mgrs")
                if not to_mgrs:
                    continue
                try:
                    p_to = _mgrs_to_latlon(str(to_mgrs).strip())
                except Exception:
                    p_to = None
                if not p_to:
                    continue
                # Score each constituent corridor by haversine origin →
                # objective. Skip corridors with unparseable from MGRS.
                _candidates: list[tuple[float, dict, tuple]] = []
                for corridor in corridors:
                    if not isinstance(corridor, dict):
                        continue
                    from_mgrs = corridor.get(spec.get("corridor_from_subfield") or "from_mgrs")
                    if not from_mgrs:
                        continue
                    try:
                        p_from = _mgrs_to_latlon(str(from_mgrs).strip())
                    except Exception:
                        continue
                    if not p_from:
                        continue
                    # Cheap haversine: great-circle distance in km. Picking
                    # the shortest = most direct = doctrinally the primary
                    # approach axis.
                    _lat1, _lon1 = p_from
                    _lat2, _lon2 = p_to
                    _dlat = math.radians(_lat2 - _lat1)
                    _dlon = math.radians(_lon2 - _lon1)
                    _a = (math.sin(_dlat / 2) ** 2
                          + math.cos(math.radians(_lat1)) * math.cos(math.radians(_lat2))
                            * math.sin(_dlon / 2) ** 2)
                    _dist_km = 2 * 6371.0 * math.asin(math.sqrt(_a))
                    _candidates.append((_dist_km, corridor, p_from))
                if not _candidates:
                    continue
                _candidates.sort(key=lambda c: c[0])
                # Greedy primary-corridor allocation: pick the closest
                # candidate whose from_mgrs hasn't already been claimed by
                # another avenue of approach on this side. Falls back to
                # the closest overall when all candidates are claimed
                # (last avenue in a side-pool where N avenues > N distinct
                # origins).
                _claimed = primary_claimed_by_side.setdefault(row_side or "_unknown", set())
                primary_corridor = None
                p_from = None
                for _dist_km, _corridor, _p_from in _candidates:
                    _o_mgrs = (_corridor.get(
                        spec.get("corridor_from_subfield") or "from_mgrs"
                    ) or "").strip()
                    if _o_mgrs and _o_mgrs not in _claimed:
                        primary_corridor = _corridor
                        p_from = _p_from
                        _claimed.add(_o_mgrs)
                        break
                if primary_corridor is None:
                    _, primary_corridor, p_from = _candidates[0]
                from_mgrs = primary_corridor.get(
                    spec.get("corridor_from_subfield") or "from_mgrs"
                )
                planner_route_spec = {
                    "id": primary_corridor.get("corridor_id") or f"{row_id}-primary",
                    "echelon": row.get("echelon") or primary_corridor.get("class") or "",
                    "name": row_id,
                    "source": "mcoo_avenue_primary",
                    "from": str(from_mgrs).strip(),
                    "to": str(to_mgrs).strip(),
                }
                geom = _route_endpoints_to_geom(planner_route_spec, p_from, p_to)
                if not geom:
                    continue
                attrs = {
                    "source": "writer",
                    "writer_field": spec["emit_field"],
                    "writer_row_id": row_id,
                    "avenue_of_approach_id": row_id,
                    "primary_corridor_id": primary_corridor.get("corridor_id"),
                    "primary_from_name": primary_corridor.get("from_name"),
                    "primary_from_mgrs": from_mgrs,
                    "constituent_corridor_count": len(corridors),
                }
                if row_side:
                    attrs["side"] = row_side
                for attr_key in (spec.get("attr_subfields") or []):
                    val = row.get(attr_key)
                    if val is not None and val != "":
                        attrs[attr_key] = val
                overlays.append({
                    "kind": spec["kind"],
                    "name": row_id,
                    "geom": geom,
                    "attrs": {k: v for k, v in attrs.items() if v is not None},
                })
                continue  # avenue's primary chevron emitted; move to next row

            geom = None
            if spec.get("routing") == "terrain_plan":
                a = row.get(spec.get("from_subfield") or "from_mgrs")
                b = row.get(spec.get("to_subfield") or "to_mgrs")
                if not a or not b:
                    continue
                try:
                    p_from = _mgrs_to_latlon(str(a).strip())
                    p_to = _mgrs_to_latlon(str(b).strip())
                except Exception:
                    p_from = p_to = None
                if not (p_from and p_to):
                    continue
                # Stamp source + endpoints into the dict the planner sees so
                # its cache key distinguishes writer-emit from scenario-emit
                # even when ids collide (writer commonly reuses "AA1"/"AA2"
                # labels from the cougaar terrain feed).
                _planner_route_spec = dict(row)
                _planner_route_spec["source"] = "writer"
                _planner_route_spec["from"] = str(a).strip()
                _planner_route_spec["to"] = str(b).strip()
                geom = _route_endpoints_to_geom(_planner_route_spec, p_from, p_to)
            else:
                raw = row.get(spec["geom_subfield"])
                geom = _writer_geometry_to_geojson(
                    raw, want=spec.get("shape") or "Polygon"
                )
            if not geom:
                continue
            try:
                name = spec["name_template"].format(**row) if row_id else spec["kind"].upper()
            except (KeyError, IndexError):
                name = str(row_id) or spec["kind"].upper()
            attrs = {
                "source": "writer",
                "writer_field": spec["emit_field"],
                "writer_row_id": row_id,
            }
            if row_side:
                attrs["side"] = row_side
            for attr_key in (spec.get("attr_subfields") or []):
                val = row.get(attr_key)
                if val is not None and val != "":
                    attrs[attr_key] = val
            overlays.append({
                "kind": spec["kind"],
                "name": name,
                "geom": geom,
                "attrs": {k: v for k, v in attrs.items() if v is not None},
            })

    # Discovered / synthesized mobility corridors (pipeline-emitted, not
    # writer-emitted). MCOO's doctrinal pipeline stashes one of:
    #   - structured_data["_synthesized_mobility_corridors"]: GVD edges
    #     from the terrain-circuit synthesis (preferred)
    #   - structured_data["_discovered_mobility_corridors"]: legacy
    #     connected-component corridors (fallback)
    # Push each as a `mobility_corridor` overlay so the renderer paints
    # the doctrinal small-bracket symbol per ATP 2-01.3 Fig 4-6 alongside
    # the avenue chevrons.
    synthesized_corridors = structured_data.get("_synthesized_mobility_corridors") or []
    for corridor in synthesized_corridors:
        if not isinstance(corridor, dict):
            continue
        coords = corridor.get("coords") or []
        if len(coords) < 2:
            continue
        overlays.append({
            "kind": "mobility_corridor",
            "name": f"MC-{corridor.get('id')}",
            "geom": {
                "type": "LineString",
                "coordinates": [list(p) for p in coords],
            },
            "attrs": {
                "source": "writer",
                "writer_field": "_synthesized_mobility_corridors",
                "corridor_id": str(corridor.get("id")),
                "length_m": corridor.get("length_m"),
                "width_m": corridor.get("width_m"),
                "class": corridor.get("class"),
                "synthesized": True,
            },
        })
    # Legacy fallback path (connected-component corridors).
    discovered = structured_data.get("_discovered_mobility_corridors") or []
    for corridor in discovered:
        if not isinstance(corridor, dict):
            continue
        centerline = (corridor.get("centerline") or {}).get("coordinates") or []
        if len(centerline) < 2:
            continue
        overlays.append({
            "kind": "mobility_corridor",
            "name": corridor.get("corridor_id") or "MC",
            "geom": {
                "type": "LineString",
                "coordinates": [list(p) for p in centerline],
            },
            "attrs": {
                "source": "writer",
                "writer_field": "_discovered_mobility_corridors",
                "corridor_id": corridor.get("corridor_id"),
                "width_km": corridor.get("width_km"),
                "class": corridor.get("class"),
                "tile_count": corridor.get("tile_count"),
                "edges_touched": corridor.get("edges_touched"),
            },
        })

    # Engagement areas (synthesized by the GVD + circuit pipeline). Each
    # gets a yellow boxed `engagement_area` overlay per ATP 2-01.3 Fig
    # 4-14 / FM 1-02.2 control-measure conventions.
    for ea in (structured_data.get("_synthesized_engagement_areas") or []):
        poly_coords = ea.get("polygon") or []
        if len(poly_coords) < 4:
            continue
        # Rationale text composed HERE from geo-agent's structured facts
        # (corridors_converging, throughput_drop_pct) — plain analytic
        # English, no citations, no method internals.
        ea_conv = ea.get("corridors_converging")
        ea_drop = ea.get("throughput_drop_pct")
        ea_sentences = [
            (f"Engagement area sited where {int(ea_conv)} mobility "
             "corridors converge.")
            if ea_conv else
            "Engagement area sited at a mobility-corridor convergence."
        ]
        if ea_drop:
            ea_sentences.append(
                "Manning this point cuts modeled enemy corridor "
                f"throughput by {float(ea_drop):.0f}%."
            )
        ea_rationale = " ".join(ea_sentences)
        overlays.append({
            "kind": "engagement_area",
            "name": ea.get("id") or "EA",
            "geom": {
                "type": "Polygon",
                "coordinates": [[list(p) for p in poly_coords]],
            },
            "attrs": {
                "source": "writer",
                "writer_field": "_synthesized_engagement_areas",
                "ea_id": ea.get("id"),
                "score": ea.get("score"),
                "centre": ea.get("centre_lonlat"),
                "rationale": ea_rationale,
            },
        })

    # Defensible terrain regions (synthesized). Each gets a green
    # `defensible_terrain` overlay.
    for d in (structured_data.get("_synthesized_defensible_terrain") or []):
        poly_coords = d.get("polygon") or []
        if len(poly_coords) < 4:
            continue
        # Rationale text composed HERE from geo-agent's structured facts
        # (flanks_protected, protection_radius_km) — plain analytic
        # English, no citations, no method internals.
        def_flanks = d.get("flanks_protected")
        def_radius = d.get("protection_radius_km")
        if def_flanks:
            def_rationale = (
                f"Defensible terrain with natural protection on "
                f"{int(def_flanks)} of 4 flanks from NO-GO obstacles"
                + (f" within {float(def_radius):.1f} km" if def_radius else "")
                + ", astride a mobility-corridor convergence."
            )
        else:
            def_rationale = (
                "Defensible terrain: position resists attack across the "
                "maximum number of approach corridors."
            )
        overlays.append({
            "kind": "defensible_terrain",
            "name": d.get("id") or "DEF",
            "geom": {
                "type": "Polygon",
                "coordinates": [[list(p) for p in poly_coords]],
            },
            "attrs": {
                "source": "writer",
                "writer_field": "_synthesized_defensible_terrain",
                "defensible_id": d.get("id"),
                "score": d.get("score"),
                "centre": d.get("centre_lonlat"),
                "rationale": def_rationale,
            },
        })
    return overlays


def _push_writer_overlays_to_geo_agent(map_id: str, overlays: list[dict]) -> dict:
    """Atomic-replace the writer-source overlay layer on the per-product map.

    POSTs to `/api/v1/maps/{id}/sync?source=writer`. Geo-agent deletes every
    overlay where `attrs.source='writer'` for this map then inserts the new
    batch. scenario_terrain / cougaar / opord layers are untouched.

    Empty `overlays` still POSTs (the empty batch deletes any prior writer
    layer from a stale earlier emit). Safe no-op if nothing was ever pushed."""
    base = _GEO_AGENT_API_BASE
    try:
        r = requests.post(
            f"{base}/maps/{map_id}/sync",
            params={"source": "writer"},
            json={"overlays": overlays, "entities": []},
            timeout=30,
        )
        if r.status_code >= 400:
            return {"error": f"HTTP {r.status_code}: {r.text[:400]}",
                     "overlays_sent": len(overlays)}
        return r.json()
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}",
                 "overlays_sent": len(overlays)}


def _push_overlays_to_master_map(*, product_type: str,
                                  overlays: list[dict],
                                  project_id: str = "") -> dict:
    """Mirror a finalized product's overlays onto the PROJECT's master map
    so the operations-map sidebar can toggle visibility per product type.

    The target map is the project/event's map
    (`_project_map_uuid(project_id)` — the event map's uuid IS the
    projectId); callers thread state.project_id.

    Each call POSTs to `/maps/{project map}/sync?source=<product_type>`.
    The geo-agent /sync endpoint transactionally REPLACES every row on the
    target map whose attrs.source equals the query param — so re-mirroring
    the same product type leaves the master map carrying the LATEST
    artifact's overlays for that product. Other products' mirrors and the
    cougaar / scenario_terrain layers are untouched (they sync under their
    own source tokens).

    Each overlay's `attrs.source` is rewritten to `product_type` (overriding
    whatever the per-artifact push tagged) so the geo-agent's per-source
    REPLACE semantics scope cleanly to "everything from this product".
    The original writer attrs (side, kind-specific labels, etc.) are
    preserved — only the source bucket changes.

    Empty `overlays` still POSTs — the empty batch deletes any stale mirror
    from a prior artifact of this product type. This is intentional: when a
    fresh artifact has nothing to paint, the operations map's prior mirror
    for that product is removed rather than left as ghost geometry.

    Returns the endpoint's response. On HTTP error the response includes
    `error`; the render still proceeds — the mirror is best-effort and
    must never roll back the per-artifact push.
    """
    if not product_type:
        return {"skipped": "no product_type"}
    base = _GEO_AGENT_API_BASE
    # When product_type is a compound 'SITEMP:<coa_id>' bucket, split the
    # analytic source name from the COA scope so downstream readers can
    # trivially group by analytic source. Without this split we lose the
    # base product name when the bucket is compound.
    base_product_type = product_type
    coa_id_tag = ""
    if ":" in product_type:
        base_product_type, coa_id_tag = product_type.split(":", 1)
    tagged: list[dict] = []
    for o in overlays:
        attrs = dict(o.get("attrs") or {})
        # Stamp attrs.source on EVERY overlay BEFORE push — closes the
        # untagged-NAI/TAI master-map leak where some overlays slipped
        # through with no attrs.source at all.
        attrs["source"] = base_product_type
        if coa_id_tag:
            attrs["coa_id"] = coa_id_tag
        tagged.append({**o, "attrs": attrs})
    try:
        r = requests.post(
            f"{base}/maps/{_project_map_uuid(project_id)}/sync",
            params={"source": product_type},
            json={"overlays": tagged, "entities": []},
            timeout=30,
        )
        if r.status_code >= 400:
            return {
                "error": f"HTTP {r.status_code}: {r.text[:400]}",
                "overlays_sent": len(tagged),
                "product_type": product_type,
            }
        return r.json()
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "overlays_sent": len(tagged),
            "product_type": product_type,
        }


async def repush_sitemp_artifact(artifact_dir: Path) -> dict:
    """Re-derive writer overlays for one SITEMP artifact + push to master map.

    Loads schema_state.json, runs the full writer-overlay extract chain
    (axes / NAIs / DPs / threat objectives / range fans) against a fresh
    cougaar OOB pull, and atomic-replaces the SITEMP:<coa_id> bucket on
    the master map. Used by the operational repush_latest endpoint so
    analysts get newly-landed overlays (e.g., per-equipment range fans)
    on existing SITEMP artifacts without firing a full Threat COAs chain
    refire.

    Returns a per-artifact result dict with overlays_pushed +
    range_fans_pushed + sync_result keys (or error/skipped).
    """
    artifact_name = artifact_dir.name
    ss_path = artifact_dir / "schema_state.json"
    if not ss_path.is_file():
        return {"skipped": "no schema_state.json", "artifact": artifact_name}
    try:
        ss = json.loads(ss_path.read_text())
    except Exception as e:
        return {"error": f"schema_state read: {type(e).__name__}: {e}",
                 "artifact": artifact_name}
    structured_data = ss.get("structured_data") or {}
    if not isinstance(structured_data, dict) or not structured_data:
        return {"skipped": "no structured_data", "artifact": artifact_name}

    # Resolve coa_id. The SITEMP fanout writes per-COA artifacts with
    # timestamp-only directory names (SITEMP_<TS>_<MS>Z) — coa_id lives
    # inside the schema_state, not in the path. Try the common
    # locations the DAG executor + product_autodraft stamp it onto:
    #   - structured_data.header.coa_id (the SITEMP writer's header row)
    #   - structured_data.coa_id (legacy bare field)
    #   - spec.bound_coa_id (DAG executor's per-COA narrowing tag)
    #   - intel_agent_data.coa_focus.coa_id (resolved state snapshot)
    #   - attrs.coa_id (rare, but cheap to check)
    artifact_id = ss.get("artifact_id") or artifact_name
    header = structured_data.get("header") or {}
    spec = ss.get("spec") or {}
    ia_data = ss.get("intel_agent_data") or {}
    coa_focus = ia_data.get("coa_focus") or {}
    coa_id = (
        (header.get("coa_id") if isinstance(header, dict) else "")
        or structured_data.get("coa_id")
        or (spec.get("bound_coa_id") if isinstance(spec, dict) else "")
        or coa_focus.get("coa_id")
        or (ss.get("attrs") or {}).get("coa_id")
        or ""
    )
    coa_id = (coa_id or "").strip()
    if not coa_id:
        return {"skipped": "no coa_id resolvable", "artifact": artifact_id}

    # Fresh OOB for the range-fan composition lookup. Falls back to
    # last-good cache when cougaar is unreachable — same path live SITEMP
    # generation takes.
    try:
        red = await tool_get_oob({"force_type": "RED"}, None)
    except Exception as e:
        log.warning("repush_sitemp_artifact %s: get_oob failed: %s",
                     artifact_id, e)
        red = None
    intel_data = {"get_oob": [red]} if red else {}

    # Re-run the full writer-overlay chain — mirrors tool_render_map's
    # writer push at intel_agent.py:6909+.
    from utils.project_context import set_active_project
    set_active_project(ia_data.get("project_id") or "")
    map_uuid = ((ss.get("attrs") or {}).get("map_uuid")
                 or _project_map_uuid(ia_data.get("project_id") or ""))
    writer_overlays: list[dict] = []
    try:
        writer_overlays.extend(_extract_writer_overlays(
            "SITEMP", structured_data, map_id=map_uuid))
        writer_overlays.extend(_extract_writer_axis_role_assignments(
            "SITEMP", structured_data))
        writer_overlays.extend(_derive_threat_time_phase_lines(
            "SITEMP", structured_data))
        writer_overlays.extend(_extract_writer_nai_picks(
            "SITEMP", structured_data))
        writer_overlays.extend(_extract_writer_dp_picks(
            "SITEMP", structured_data))
        writer_overlays.extend(_extract_writer_threat_objective_picks(
            "SITEMP", structured_data))
        writer_overlays.extend(_extract_writer_range_fans(
            "SITEMP", structured_data, intel_data))
    except Exception as e:
        return {"error": f"extract chain: {type(e).__name__}: {e}",
                 "artifact": artifact_id}

    # Stamp coa_id on every overlay so the master-map sidebar's
    # compound-source matcher routes correctly.
    for o in writer_overlays:
        attrs = o.setdefault("attrs", {})
        attrs.setdefault("coa_id", coa_id)

    mirror_source = f"SITEMP:{coa_id}"
    sync_result = _push_overlays_to_master_map(
        product_type=mirror_source,
        overlays=writer_overlays,
        project_id=ia_data.get("project_id") or "",
    )
    fan_count = sum(1 for o in writer_overlays
                     if (o.get("kind") or "") == "range_fan")
    return {
        "artifact": artifact_id,
        "coa_id": coa_id,
        "overlays_pushed": len(writer_overlays),
        "range_fans_pushed": fan_count,
        "sync_result": sync_result,
    }


async def repush_latest_sitemp_set(upload_dir: Path) -> dict:
    """Walk `upload_dir/_autodraft` for SITEMP_* artifact directories,
    keep the LATEST per coa_id, and re-push each via
    repush_sitemp_artifact.

    SITEMP fanout writes per-COA artifacts with timestamp-only names
    (SITEMP_<TS>_<MS>Z), so coa_id can't be parsed from the path —
    it must be read from each candidate's schema_state.json. We
    walk newest-first, read just enough to extract the coa_id, and
    dedup so each COA's latest successful SITEMP runs exactly once.

    Returns {artifacts_processed, results: [...], scanned}.
    """
    autodraft_root = upload_dir / "_autodraft"
    if not autodraft_root.is_dir():
        return {"error": "no _autodraft dir", "artifacts_processed": 0}
    sitemp_dirs = sorted(
        [d for d in autodraft_root.iterdir()
         if d.is_dir() and d.name.startswith("SITEMP_")],
        key=lambda d: d.name,
        reverse=True,
    )
    targets: list[Path] = []
    seen_coas: set[str] = set()
    scanned = 0
    for d in sitemp_dirs:
        scanned += 1
        ss_path = d / "schema_state.json"
        if not ss_path.is_file():
            continue
        try:
            ss = json.loads(ss_path.read_text())
        except Exception:
            continue
        sd = ss.get("structured_data") or {}
        header = sd.get("header") or {}
        spec = ss.get("spec") or {}
        ia = ss.get("intel_agent_data") or {}
        cf = ia.get("coa_focus") or {}
        cid = (
            (header.get("coa_id") if isinstance(header, dict) else "")
            or sd.get("coa_id")
            or (spec.get("bound_coa_id") if isinstance(spec, dict) else "")
            or cf.get("coa_id")
            or ""
        )
        cid = (cid or "").strip()
        if not cid or cid in seen_coas:
            continue
        seen_coas.add(cid)
        targets.append(d)
        if len(targets) >= 8:  # doctrinal max 1-4 COAs, slack to 8
            break
    results: list[dict] = []
    for d in targets:
        try:
            r = await repush_sitemp_artifact(d)
        except Exception as e:
            r = {"error": f"{type(e).__name__}: {e}", "artifact": d.name}
        results.append(r)
    return {
        "artifacts_processed": len(results),
        "scanned": scanned,
        "results": results,
    }


def _push_oob_to_geo_agent(
    map_id: str,
    *,
    control_measures: list,
    units: list,
    targets: list,
    unidentified_formations: list | None = None,
) -> dict:
    """Atomic replace of the OOB layer on a project's master map.

    POSTs one snapshot to `POST /api/v1/maps/{id}/sync?source=live_oob`.
    The endpoint transactionally deletes every overlay+entity on the map
    where attrs.source='live_oob', then inserts the snapshot rows.
    Overlays under other source tokens are untouched.

    The OOB→geo-agent shape translation lives in
    `utils.oob.build_geo_agent_sync_payload` (extracted from the dead
    cougaar_adapters module during the 2026-07-09 disconnect).

    Returns the endpoint's response. On HTTP error the response includes
    `error` and the render still proceeds — paint layer just won't have
    fresh dynamic data for this render."""
    base = _GEO_AGENT_API_BASE
    payload = oob.build_geo_agent_sync_payload(
        control_measures=control_measures,
        units=units,
        targets=targets,
        unidentified_formations=unidentified_formations,
    )
    try:
        r = requests.post(
            f"{base}/maps/{map_id}/sync",
            # `live_oob` is the transactional REPLACE key — every
            # overlay+entity with the same attrs.source is wiped before
            # this snapshot lands. Any future live feed pushes under the
            # same bucket so UI consumers, source-token filters, and
            # product writers downstream do NOT need to change.
            params={"source": oob.LIVE_OOB_SOURCE},
            json=payload, timeout=30,
        )
        if r.status_code >= 400:
            return {
                "error": f"HTTP {r.status_code}: {r.text[:400]}",
                "overlays_sent": len(payload["overlays"]),
                "entities_sent": len(payload["entities"]),
            }
        return r.json()
    except Exception as exc:
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "overlays_sent": len(payload["overlays"]),
            "entities_sent": len(payload["entities"]),
        }


async def push_oob_to_project_map(project_id: str) -> dict:
    """ONE-SHOT push of the scenario OOB floor (RED + BLUE units,
    targets, spatial-overlay control measures, unidentified-formation
    pins) onto a PROJECT's master map (map uuid == projectId).

    Fired at PROJECT CREATION (handlers/projects bootstrap) and from the
    operator refire endpoint. There is no periodic re-push: the scenario
    data is static until scenario-update machinery exists, so pushing it
    once per project is the entire sync. The Cougaar 30s poll plane that
    used to re-assert this every tick is dead and disconnected.
    """
    from utils.project_context import set_active_project
    set_active_project(project_id)
    map_uuid = _project_map_uuid(project_id)
    # Ensure the map ROW exists before the first sync — geo-agent
    # auto-creates maps on frontend open, but /sync 404s on an unknown
    # id. PUT /maps/{id} is the documented upsert; seed the viewport
    # from the project's scenario AO bbox so the first open frames the
    # event's area of operations.
    try:
        _bbox = list(scenario.AO_BBOX)
        requests.put(
            f"{_GEO_AGENT_API_BASE}/maps/{map_uuid}",
            json={"name": f"Ops Map {project_id[:8]}",
                   "bbox_west": _bbox[0], "bbox_south": _bbox[1],
                   "bbox_east": _bbox[2], "bbox_north": _bbox[3]},
            timeout=15,
        )
    except Exception as _me:
        log.warning("push_oob_to_project_map: map upsert failed (%s) — "
                    "sync may 404 until the map exists", _me)
    red = await tool_get_oob({"force_type": "RED"}, None)
    blue = await tool_get_oob({"force_type": "BLUE"}, None)
    units = list(red.get("units") or []) + list(blue.get("units") or [])
    tgt = await tool_list_targets({}, None)
    targets = list(tgt.get("targets") or [])
    # Control measures come from the scenario spatial overlay — we own
    # the spatial layer (the dead cougaar one was empty).
    control_measures: list = []
    try:
        from utils import scenario as _scenario
        _fixture = getattr(_scenario, "SPATIAL_OVERLAY", None) or {}
        control_measures = list(_fixture.get("control_measures") or [])
    except Exception as _se:
        log.warning("push_oob_to_project_map: scenario spatial overlay "
                    "read failed: %s", _se)
    # § 12 unidentified-formation pins — only flow in via the RED
    # resolved-OOB payload; merge any from RED + BLUE (BLUE has none).
    unidentified_formations = list(red.get("unidentified_formations") or []) \
                            + list(blue.get("unidentified_formations") or [])
    result = _push_oob_to_geo_agent(
        map_uuid,
        control_measures=control_measures,
        units=units,
        targets=targets,
        unidentified_formations=unidentified_formations,
    )
    if result.get("error"):
        log.warning("push_oob_to_project_map: push error map=%s: %s",
                     map_uuid, result["error"])
    else:
        log.info(
            "push_oob_to_project_map: map=%s red=%d blue=%d targets=%d cms=%d "
            "-> entities_inserted=%s",
            map_uuid,
            len(red.get("units") or []),
            len(blue.get("units") or []),
            len(targets), len(control_measures),
            result.get("entities_inserted") or result.get("entities_replaced"),
        )
    return result


# Map UUID validation pattern.
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# Persistent render-loop worker. A single long-lived background thread
# hosts an asyncio event loop that owns the warm Playwright browser +
# page references the headless renderer stashes between calls
# (see app/app/utils/render/headless.py — `_BROWSER`, `_PLAYWRIGHT`,
# `_PW_CTX_MGR`, `_OWNER_LOOP`, plus the per-(w,h,dpr) warm fast-mode
# context `_WARM_FAST_CTX` / `_WARM_FAST_PAGE` / `_WARM_FAST_KEY`).
# By routing every `_run_async_safe` call through this single loop,
# `_OWNER_LOOP is current_loop` holds across renders and the warm
# fast-mode context survives — dropping second-render-onwards from
# ~25s (cold tile fetch + Playwright launch) to <5s (warm HTTP tile
# cache + already-loaded MapLibre instance).
import asyncio as _asyncio_persistent
import threading as _threading_persistent
import concurrent.futures as _cf_persistent

_RENDER_LOOP: _asyncio_persistent.AbstractEventLoop | None = None
_RENDER_LOOP_LOCK = _threading_persistent.Lock()


def _ensure_render_loop() -> _asyncio_persistent.AbstractEventLoop:
    """Return the persistent render-worker event loop, starting the
    background thread on first call.

    The thread runs `loop.run_forever()` until process exit and is a
    daemon so it does not block interpreter shutdown. We hold a module
    lock around the lazy-init so concurrent first-callers don't race to
    spawn two loops.
    """
    global _RENDER_LOOP
    with _RENDER_LOOP_LOCK:
        if _RENDER_LOOP is not None and not _RENDER_LOOP.is_closed():
            return _RENDER_LOOP
        loop = _asyncio_persistent.new_event_loop()
        ready = _threading_persistent.Event()

        def _runner():
            _asyncio_persistent.set_event_loop(loop)
            ready.set()
            try:
                loop.run_forever()
            finally:
                loop.close()

        t = _threading_persistent.Thread(
            target=_runner,
            name="pytho-render-loop",
            daemon=True,
        )
        t.start()
        ready.wait(timeout=5.0)
        _RENDER_LOOP = loop
        return loop


def _run_async_safe(coro_factory, timeout: float = 30.0):
    """Run an async coroutine to completion on the PERSISTENT render
    worker loop, regardless of caller's loop state.

    A single long-lived background thread hosts an asyncio event loop
    that owns the warm Playwright browser + page references the
    headless renderer stashes between calls. Submitting work to this
    loop means `_WARM_FAST_CTX` survives across renders, dropping the
    second-render-onwards from ~25s (cold tile fetch) to <5s (warm
    HTTP cache + already-loaded MapLibre instance).

    `coro_factory` is a zero-arg callable that returns a fresh coroutine
    on each invocation.
    """
    loop = _ensure_render_loop()
    fut = _asyncio_persistent.run_coroutine_threadsafe(
        coro_factory(), loop,
    )
    try:
        return fut.result(timeout=timeout)
    except _cf_persistent.TimeoutError:
        fut.cancel()
        raise


def _derive_map_id_for_state(state, field_name: str = "map") -> str:
    """Return the per-product map UUID for the active artifact.

    Each product instance owns its own geo-agent map, derived
    deterministically from (active_product_type, pending_artifact_id,
    field_name). Stable across regenerations of the same artifact_id so
    analyst edits in the geo-agent map persist when the product is
    re-rendered; distinct artifact_ids (different sessions / analysts)
    get distinct UUIDs so edits don't collide.

    Falls back to the state's PROJECT map (the projectId itself — legacy
    master map only when no project is threaded) when state is empty or
    carries no artifact binding (rare — legacy harness paths).
    Production callers always have a state with pending_artifact_id set
    up-front.
    """
    if state is None:
        return _project_map_uuid("")
    aid = (getattr(state, "pending_artifact_id", "") or "").strip()
    ptype = (getattr(state, "active_product_type", "") or "").strip()
    if not aid or not ptype:
        return _project_map_uuid(getattr(state, "project_id", ""))
    return map_uuid_for(ptype, aid, field_name)


def compute_map_metadata(field_name: str, state,
                          schema_bbox: Optional[list] = None) -> dict:
    """Compute the deterministic map metadata for a `shape:"map"` paragraph_field
    WITHOUT rendering anything. Returns the same shape `tool_render_map` returns
    on its `format="structured"` short-circuit so callers (notably
    `_build_skeleton` on the DAG path) can populate `skeleton["map_overlays"]`
    + the back-compat header keys without a Playwright render, geo-agent /sync,
    or map-record upsert.

    Three values, all derivable from (active_product_type, pending_artifact_id,
    field_name):
      - `spatial_overlay_url`     = `/files/<uid>/<aid>/<field_name>.jpg`
                                    (matches the path `tool_render_map`
                                    writes to at intel_agent.py:4966-4968)
      - `spatial_overlay_map_id`  = `map_uuid_for(product_type, aid, field_name)`
      - `spatial_overlay_bbox`    = schema-declared per-field bbox, else
                                    `scenario.AO_BBOX`

    The actual PNG is written by the post-merge render in
    `dag_executor.run_dag` (which calls `tool_render_map` after the writer
    emit is available so per-COA geometry can paint on the same map).
    """
    uid = (getattr(state, "user_id", "") or "anonymous").strip() or "anonymous"
    aid = (getattr(state, "pending_artifact_id", "") or "").strip()
    ptype = (getattr(state, "active_product_type", "") or "").strip()
    if aid and ptype:
        map_uuid = map_uuid_for(ptype, aid, field_name)
    else:
        map_uuid = _project_map_uuid(getattr(state, "project_id", ""))
    if isinstance(schema_bbox, (list, tuple)) and len(schema_bbox) == 4:
        bbox = [float(v) for v in schema_bbox]
    else:
        bbox = list(scenario.AO_BBOX)
    png_url = f"/files/{uid}/{aid}/{field_name}.jpg" if aid else ""
    return {
        "map_artifact_id": field_name,
        "format": "structured",
        "spatial_overlay_map_id": map_uuid,
        "spatial_overlay_url": png_url,
        "header": {
            "spatial_overlay_map_id": map_uuid,
            "spatial_overlay_url": png_url,
            "spatial_overlay_bbox": bbox,
        },
    }


def tool_render_map(args, state, intel_data: Optional[dict] = None,
                     current_emit: Optional[dict] = None):
    """Render the active map. PNG bytes land at uploads/<user>/<artifact>/
    <field_name>.jpg and the FastAPI /files static mount serves them back
    to the exporter.

    Data flow:
      1. Caller (usually _build_skeleton via DAG) passes `intel_data` —
         a dict of resolved cougaar tool results keyed by tool name
         ({"get_oob": [...], "list_targets": [...], "get_spatial_overlay":
         [...], ...}). This is what gets pushed to the per-product
         geo-agent map and surfaces on the rendered image.
      2. Falls back to state.intel_agent_data via _extract_ia_data when
         intel_data isn't supplied — preserves the legacy LLM-tool
         invocation path where the IA OE loop populates state before
         the LLM calls render_map. The DAG path always passes
         intel_data explicitly; the fallback exists only for that
         legacy path.

    Map UUID derivation:
      - Each product instance gets its OWN deterministic geo-agent map
        UUID (map_uuid_for(product_type, artifact_id, field_name)).
        Stable across regens of the same artifact (analyst edits in the
        map editor persist); distinct artifacts get distinct UUIDs.
      - Args.map_uuid / args.map_id override is honored for callers that
        need a specific UUID (rare).

    bbox contract:
      - The returned structured["bbox"] is ALWAYS a 4-element list of
        floats in [W, S, E, N] order — never a string label / sentinel.
        Source preference: caller-supplied args.bbox (if valid 4-tuple)
        → the per-product map's stored bbox (geo-agent) → scenario.AO_BBOX
        as the final fallback tied to AO BALTIC RESTORE. An assertion
        enforces the type just before structured is built.
    """
    fmt = args.get("format", "image")
    bbox_arg = args.get("bbox")

    # Schema-driven gating per redesign doc §2.5. The frontend's
    # `loadMapState` uses the URL `layers=` param as the server-side
    # FEATURE-KIND allowlist (passed to /api/v1/map/state?kinds=...). The
    # legitimate source of those kinds is the active product's
    # `map_overlay_kinds` schema field. Schema map paragraphs may also
    # declare a `layers: ["terrain","units","spatial_overlay"]` legacy
    # category enum — that is NOT a feature-kind list and would filter
    # every real feature out if used as the kinds allowlist. So: always
    # prefer the schema's map_overlay_kinds; the legacy args.layers is
    # ignored.
    layers: list[str] = []
    _schema = PRODUCT_SCHEMAS.get(state.active_product_type or "", {}) if state else {}
    _schema_kinds = _schema.get("map_overlay_kinds") if isinstance(_schema, dict) else None
    if isinstance(_schema_kinds, (list, tuple)) and _schema_kinds:
        layers = list(_schema_kinds)
    # Default render dimensions derive from the active product's deliverable
    # at print/projection quality (2x slide pixel dims @ 192 dpi):
    #   landscape ⇒ PPTX slide @ 13.33"×7.5" (matches render_artifact_pptx's
    #     pres.slide_width/slide_height in utils/exporter.py:1886). At 192 dpi
    #     that's 2560×1440 px (16:9) — crisp on a 4K projector, 1080p-and-up
    #     screen, and standard 300-dpi print.
    #   portrait ⇒ DOCX Letter @ 8.5"×11" — 1632×2112 px @ 192 dpi.
    # Caller-supplied width/height override (writer can request a specific
    # zoom or custom thumbnail).
    schema_for_product = PRODUCT_SCHEMAS.get(state.active_product_type or "", {})
    # Orientation derives from output_format. pptx → landscape (16:9 slide),
    # docx → portrait (8.5×11 Letter). One source of truth.
    _is_landscape_product = schema_for_product.get("output_format") == "pptx"
    _default_w, _default_h = (2560, 1440) if _is_landscape_product else (1632, 2112)
    width  = int(args.get("width")  or _default_w)
    height = int(args.get("height") or _default_h)

    # Multi-map support: every map paragraph_field in the schema declares
    # its own filename via the field name. tool_render_map writes the PNG
    # at <artifact_folder>/<field_name>.jpg and stamps per-field metadata
    # at structured_data["map_overlays"][<field_name>]. Caller passes
    # `field_name` to disambiguate; defaults to "map" for single-map products.
    map_artifact_id = (args.get("field_name") or args.get("map_field_name")
                        or "map").strip() or "map"

    # Resolve the per-product map UUID. Explicit override via args.map_uuid
    # / args.map_id takes precedence; otherwise derive deterministically
    # from (active_product_type, pending_artifact_id, field_name). Each
    # product instance + map field gets its own UUID so analyst edits on
    # one product don't bleed onto another. Args.map_artifact_id is the
    # field-name slug, NOT a UUID — kept here for back-compat with legacy
    # callers that overloaded the field.
    requested_uuid = (args.get("map_uuid") or args.get("map_id") or "").strip()
    if requested_uuid and _UUID_RE.match(requested_uuid):
        map_uuid = requested_uuid
    else:
        # Maybe map_artifact_id was UUID-shaped (legacy LLM convention).
        legacy = (args.get("map_artifact_id") or "").strip()
        if legacy and _UUID_RE.match(legacy):
            map_uuid = legacy
        else:
            map_uuid = _derive_map_id_for_state(state, field_name=map_artifact_id)

    bbox_for_render: Optional[list[float]] = None
    if isinstance(bbox_arg, (list, tuple)) and len(bbox_arg) == 4:
        try:
            bbox_for_render = [float(v) for v in bbox_arg]
        except (TypeError, ValueError):
            bbox_for_render = None
    # Always carry a bbox into the render call — the geo-agent's render
    # endpoint defaults to its own hardcoded view (somewhere in Taiwan) when
    # no bbox is supplied, ignoring the map's stored bbox_*/center_*. So
    # whether or not the LLM passed bbox, fall back to the master map's
    # stored bbox — that's the AOR the analyst configured. Hardcoded
    # constant was wider than the AOR and caused basemap halo around the
    # doctrinal paint. Hard-coded IBL fallback only kicks in if the
    # geo-agent fetch fails.
    # GET the per-product map: if it exists, use its stored bbox (analyst
    # may have edited it in the operations map). If 404, the map hasn't
    # been created yet — we PUT-upsert it below before any sync.
    map_exists = False
    if bbox_for_render is None:
        try:
            r = requests.get(f"{_GEO_AGENT_API_BASE}/maps/{map_uuid}", timeout=5)
            if r.ok:
                map_exists = True
                mp = r.json()
                w, s, e, n = (mp.get("bbox_west"), mp.get("bbox_south"),
                              mp.get("bbox_east"), mp.get("bbox_north"))
                if None not in (w, s, e, n):
                    bbox_for_render = [w, s, e, n]
        except Exception as e:
            log.warning("tool_render_map: map bbox fetch failed (%s); using IBL AO fallback", e)
        if bbox_for_render is None:
            bbox_for_render = list(scenario.AO_BBOX)
    else:
        # Caller passed a bbox — still need to know if the map exists so
        # we can create it before the sync pushes try to write to it.
        try:
            r = requests.get(f"{_GEO_AGENT_API_BASE}/maps/{map_uuid}", timeout=5)
            map_exists = r.ok
        except Exception:
            pass

    # Upsert the geo-agent map record (bbox + center + zoom + schema attrs)
    # before any /sync POST tries to write into it (the /sync endpoint 404s
    # on missing maps; without this every per-product map render rendered
    # blank because both scenario_terrain and cougaar pushes failed). Runs
    # on every render — refire / re-render at a different bbox or schema
    # flip overwrites stale viewport / attrs from the previous cycle.
    upsert_geo_agent_map_record(
        map_uuid=map_uuid,
        product_type=(state.active_product_type if state else "") or "",
        field_name=map_artifact_id,
        bbox=bbox_for_render,
        schema=_schema if isinstance(_schema, dict) else {},
    )

    # Header block carries the URL fields the exporter consumes
    # (utils/exporter.py:_render_map_embed reads header.spatial_overlay_url,
    # spatial_overlay_map_id, spatial_overlay_bbox). Nesting these under
    # `header` (rather than at structured-top-level) is what lets the
    # runner-side auto-promote in _build_skeleton wire them into the
    # artifact's header — the writer's _merge_pw_emit only copies
    # header_analysis keys into merged["header"], so a top-level
    # spatial_overlay_url would never reach the exporter.
    header_block: dict[str, Any] = {
        "spatial_overlay_map_id": map_uuid,
        # Always carry the bbox that this render used. The exporter writes
        # this to the figure's data-bbox attr, and DocEditor's "Edit map"
        # hook reads it to build the deep-link URL. Without this fallback
        # the analyst landed on OperationsMap with empty ?bbox= → the
        # provider's hardcoded default (Taiwan). bbox_for_render at this
        # point is either caller-supplied, the master map's stored bbox,
        # or the IBL AO fallback — all of them are correct to embed.
        "spatial_overlay_bbox": list(bbox_for_render),
    }
    # structured["bbox"] MUST be a 4-element list of floats [W, S, E, N],
    # never a string label. Previous "AO_ALPHA-NORTH_DEFAULT" / similar
    # sentinel strings poisoned downstream consumers (exporter wrote them
    # into figure data-bbox attrs, the operations-map deep link surfaced
    # them as bbox=AO_ALPHA-NORTH_DEFAULT and the provider fell through
    # to its hardcoded Taiwan default). bbox_for_render at this point is
    # guaranteed list[float] (caller-supplied, the per-product map's
    # stored bbox, or scenario.AO_BBOX) — assert so a future regression
    # blows up here instead of producing a corrupt artifact.
    assert isinstance(bbox_for_render, list) and len(bbox_for_render) == 4, (
        f"tool_render_map: bbox_for_render must be list[float] of length 4, "
        f"got {type(bbox_for_render).__name__} {bbox_for_render!r}"
    )
    structured: dict[str, Any] = {
        "layers_composed": layers,
        "bbox": list(bbox_for_render),
        "map_uuid": map_uuid,
        # Also carry the map id at top-level for the writer LLM's awareness
        # (the writer references it when filling map_artifact_id in body
        # fields). The exporter reads from header.spatial_overlay_map_id.
        "spatial_overlay_map_id": map_uuid,
        "header": header_block,
    }

    if fmt not in ("image", "both"):
        # Structured-only request — short-circuit; no render needed.
        return {"map_artifact_id": map_artifact_id, "format": fmt, **structured}

    # === Resolved-data → per-product geo-agent map sync ====================
    # Pushes the data needed for the editor to render this product's map.
    # Every render replaces the per-product map's cougaar + scenario_terrain
    # layers atomically — analyst edits on OTHER per-product maps stay
    # untouched (per-product UUIDs isolate them).
    #
    # Data sources — all come from the caller-supplied `intel_data` dict
    # (resolved by the DAG executor from completed cougaar tool runs)
    # OR from state.intel_agent_data as a fallback for the legacy
    # LLM-tool invocation path. NO direct live cougaar fixture reads here.
    try:
        if intel_data is None:
            intel_data = _extract_ia_data(state) if state else {}
        units_pre: list = []
        for resp in (intel_data.get("get_oob") or []):
            if isinstance(resp, dict):
                units_pre.extend(resp.get("units") or [])

        # === Per-COA SITEMP unit templating (ATP 2-01.3 Step-4 doctrinal) ===
        # The SITEMP writer emits `templated_units` — per-COA OE-adjusted
        # positions for the threat units that move under THIS specific
        # enemy COA. Doctrine (ATP 2-01.3 Step 4): the SITEMP shows the
        # threat order of battle TEMPLATED ONTO scenario.TERRAIN for one Threat COA Statement, not
        # the static baseline OOB. Every per-COA SITEMP map MUST visually
        # differ — the whole point of running three SITEMPs is to show
        # three distinct threat dispositions the friendly commander has
        # to plan against.
        #
        # Without this override, every per-COA SITEMP rendered the same
        # 210-unit baseline OOB at fixed scenario positions and the three
        # maps were visually indistinguishable. With it, every templated
        # unit's MGRS is replaced by the writer's per-COA position; units
        # the writer did not template (the broader-force-structure rear
        # echelons that don't reposition) stay at baseline so the analyst
        # still sees the full OOB context. Net per-COA visual delta: the
        # key combat units (typically 15-25 per COA — divisions, maneuver
        # brigades, fires, ADA, recon) sit at COA-specific positions; the
        # remaining 180+ rear-echelon units stay at baseline.
        #
        # Identity match is name-uppercase exact: confirmed against
        # scenario.KG.unit_red — the writer's `identity` field (e.g.
        # "60 MECH IFV DIV", "601 MECH IFV BDE") matches baseline `name`
        # 1:1 (writer prompt is keyed off the OOB designators). Any
        # writer-emitted templated_units row with no baseline match is
        # ignored — that's a writer hallucination (the guidance says
        # every row maps 1:1 to an IDXEnemyOrderOfBattleReport vertex).
        _ap = (state.active_product_type if state else "") or ""
        if _ap == "SITEMP" and isinstance(current_emit, dict):
            _tu = current_emit.get("templated_units") or []
            if isinstance(_tu, list) and _tu:
                from utils.coords import mgrs_to_latlon as _mgrs_to_latlon
                _tu_by_name: dict[str, dict] = {}
                for _t in _tu:
                    if not isinstance(_t, dict):
                        continue
                    _nm = (_t.get("identity") or "").strip().upper()
                    if _nm:
                        _tu_by_name[_nm] = _t
                _overrides = 0
                _missing_mgrs = 0
                _bad_mgrs = 0
                for _u in units_pre:
                    _unm = (_u.get("name") or _u.get("unit_short_name")
                              or "").strip().upper()
                    if not _unm:
                        continue
                    _t = _tu_by_name.get(_unm)
                    if not _t:
                        continue
                    _new_mgrs = (_t.get("location_mgrs") or "").strip()
                    if not _new_mgrs:
                        _missing_mgrs += 1
                        continue
                    _ll = _mgrs_to_latlon(_new_mgrs)
                    if not _ll:
                        _bad_mgrs += 1
                        continue
                    _u["location_mgrs"] = _new_mgrs
                    # mgrs key kept in sync — _t1_unit emits both fields
                    # and downstream consumers (writer prompt, geo-agent
                    # sync payload builder, overlays.json debug dump) read
                    # one or the other.
                    _u["mgrs"] = _new_mgrs
                    # Clear any cached `location` GeoJSON so the geo-agent
                    # sync payload builder recomputes from the new MGRS
                    # rather than re-using the baseline lat/lon.
                    if "location" in _u:
                        del _u["location"]
                    if "loc" in _u:
                        del _u["loc"]
                    _u["_per_coa_templated"] = True
                    _overrides += 1
                _coa = current_emit.get("coa_id") or "(unknown)"
                log.info(
                    "SITEMP per-COA templating: coa=%s overrides=%d/%d "
                    "(writer templated %d units; %d had missing MGRS, "
                    "%d had unparseable MGRS, rest left at baseline)",
                    _coa, _overrides, len(_tu),
                    len(_tu), _missing_mgrs, _bad_mgrs,
                )
        targets_pre: list = []
        for resp in (intel_data.get("list_targets") or []):
            if isinstance(resp, dict):
                targets_pre.extend(resp.get("targets") or [])
        for resp in (intel_data.get("get_target") or []):
            if isinstance(resp, dict):
                t = resp.get("target")
                if t:
                    targets_pre.append(t)
        # Control measures from declared cougaar.get_spatial_overlay dep.
        # Fall back to scenario.OPORD_OVERLAY (via tool_get_spatial_overlay)
        # when no declared dep ran. We own the spatial overlays now —
        # cougaar's spatial layer is empty.
        control_measures_pre: list = []
        spatial_resps = intel_data.get("get_spatial_overlay") or []
        if spatial_resps:
            for resp in spatial_resps:
                if isinstance(resp, dict):
                    control_measures_pre.extend(resp.get("control_measures") or [])
        else:
            try:
                spatial = tool_get_spatial_overlay({}, state)
                control_measures_pre = list(spatial.get("control_measures") or [])
            except Exception as e:
                log.warning("tool_render_map: scenario spatial overlay fallback failed: %s", e)
        sync_result = _push_oob_to_geo_agent(
            map_uuid,
            control_measures=control_measures_pre,
            units=units_pre,
            targets=targets_pre,
        )
        structured["geo_agent_sync"] = sync_result
        # Scenario terrain push (AAs / key terrain / obstacles from
        # scenario.TERRAIN) — landing on the per-product geo-agent map
        # under source=scenario_terrain so the editor + render see the
        # doctrinal paint primitives (chevrons, K-glyphs, obstacle
        # symbols). Reads scenario.TERRAIN directly because get_terrain's
        # current return shape doesn't include the pushable AA/key-terrain
        # structure (those live in scenario.TERRAIN.avenues_of_approach
        # and .key_terrain, not in get_terrain's mobility_classifications/
        # aoi/summary payload).
        try:
            # Schema-driven per-product gates. The active product's
            # schema (templates/products/*.py) carries map_avenue_of_
            # approach_source + map_show_friendly_axis_of_advance
            # primitives that decide which avenues / friendly axes
            # land on this product's map.
            #
            # MCOO / Threat Overlay default to "all" + True (every
            # scenario.TERRAIN avenue + OPERATIONS axis pushed). SITEMP carries
            # "none" + False — per ATP 2-01.3 §6 / fig 6-3 the SITEMP
            # depicts ONE COA's threat scheme of maneuver via the
            # writer's axis_role_assignments hostile arrows; the
            # avenue chevron and the friendly OPORD maneuver plan do
            # not appear at all.
            #
            # The decision lives in the schema, not in this caller —
            # this caller just forwards. Renderer-renders rule still
            # applies: the geo-agent map and the paint layer don't
            # filter.
            _ap_for_terrain = (state.active_product_type or "") if state else ""
            _schema_for_terrain = PRODUCT_SCHEMAS.get(_ap_for_terrain, {}) if _ap_for_terrain else {}
            _aa_source = _schema_for_terrain.get("map_avenue_of_approach_source") or "all"
            _show_friendly_axis = bool(
                _schema_for_terrain.get("map_show_friendly_axis_of_advance", True)
            )
            _ea_perspective = (
                _schema_for_terrain.get("map_engagement_area_perspective") or "all"
            )
            _show_friendly_pls = bool(
                _schema_for_terrain.get("map_show_friendly_phase_lines", True)
            )
            _show_friendly_bdries = bool(
                _schema_for_terrain.get("map_show_friendly_boundaries", True)
            )
            # Per-COA scenario-canonical EA filter — SITEMP (per ATP
            # 2-01.3 ¶6-15 one-per-COA) gates the catalog EAs by the
            # set of friendly avenues the writer's axis_role_assignments
            # actually engage for THIS COA. Other products (MCOO,
            # Threat Overlay) pass None and paint every catalog EA
            # passing the perspective gate. Only meaningful on the
            # second render (when current_emit carries the writer's
            # structured_data); first-render fallback is None →
            # unfiltered, which is fine because the second render
            # overwrites the PNG.
            _ea_engaged_set: Optional[set[str]] = None
            if _ap_for_terrain == "SITEMP" and current_emit is not None:
                try:
                    _ea_engaged_set = _coa_engaged_friendly_avenue_set(
                        current_emit
                    )
                except Exception as _e:
                    log.warning(
                        "scenario_terrain: COA-engaged set derivation "
                        "failed for map=%s: %s — proceeding without "
                        "per-COA EA filter", map_uuid, _e,
                    )
            terrain_sync = _push_scenario_terrain_to_geo_agent(
                map_uuid,
                avenue_of_approach_source=_aa_source,
                show_friendly_axis_of_advance=_show_friendly_axis,
                engagement_area_perspective=_ea_perspective,
                coa_engaged_friendly_avenue_ids=_ea_engaged_set,
                show_friendly_phase_lines=_show_friendly_pls,
                show_friendly_boundaries=_show_friendly_bdries,
            )
            structured["scenario_terrain_sync"] = terrain_sync
            if terrain_sync.get("error"):
                log.warning("scenario_terrain sync error map=%s: %s",
                             map_uuid, terrain_sync["error"])
            else:
                log.info(
                    "scenario_terrain /sync map=%s overlays_replaced=%s",
                    map_uuid,
                    terrain_sync.get("overlays_replaced")
                    or terrain_sync.get("overlays_inserted")
                    or terrain_sync.get("overlays_sent"),
                )
        except Exception as e:
            log.warning("scenario_terrain push wrapper failed map=%s: %s",
                         map_uuid, e, exc_info=True)
            structured["scenario_terrain_sync_error"] = f"{type(e).__name__}: {e}"
        if sync_result.get("error"):
            log.warning("geo-agent /sync error map=%s: %s", map_uuid, sync_result["error"])
        else:
            log.info(
                "geo-agent /sync map=%s units=%d targets=%d cm=%d",
                map_uuid, len(units_pre), len(targets_pre), len(control_measures_pre),
            )
    except Exception as e:
        log.warning("geo-agent sync wrapper failed for map=%s: %s", map_uuid, e, exc_info=True)
        structured["geo_agent_sync_error"] = f"{type(e).__name__}: {e}"

    # Writer-emitted geometry push. When called from dag_executor's
    # post-merge re-render path, `current_emit` carries the merged
    # structured_data. The writer-overlay extractor pulls per-product
    # writer-authored EAs / NAIs / TPLs / threat objectives / threat AAs
    # (per _WRITER_GEOMETRY_MANIFEST) and overlays them on top of the
    # scenario_terrain + cougaar layers via attrs.source='writer'.
    # An empty list still POSTs — that's how a stale prior writer layer
    # gets cleared when the new emit drops a feature.
    #
    # Skipped on the first render (current_emit is None at _build_skeleton
    # time because writer emit hasn't been assembled yet). The dag_executor
    # re-fires this function post-merge with current_emit set, which is
    # when the writer-specific layer lands. Two renders per artifact —
    # the second supersedes the first PNG with the writer overlays baked
    # in. Wasteful but small (the second render reuses every cache except
    # the writer source).
    if current_emit is not None:
        try:
            _ap_for_writer = state.active_product_type if state else ""
            writer_overlays = _extract_writer_overlays(
                _ap_for_writer,
                current_emit,
                map_id=map_uuid,
            )
            # SITEMP per-COA scheme of maneuver — bold role-tagged threat
            # arrows lifted from the writer's axis_role_assignments rows
            # (which select from scenario.TERRAIN.avenues_of_approach by id
            # + optional direction reversal). Pushed as axis_of_advance
            # overlays with attrs.role + side=hostile so drawAxisOfAdvance
            # paints them in role-coded weight/dash on top of the baseline
            # AA chevrons. This is the per-COA visual differentiation the
            # reference Threat COAs exhibits depict — different main effort
            # arrow location per COA, dashed feints, curved counter-attacks.
            writer_overlays.extend(_extract_writer_axis_role_assignments(
                _ap_for_writer,
                current_emit,
            ))
            # SITEMP threat time-phase lines per ATP 2-01.3 Fig 6-3 —
            # DERIVED at render time from axis_role_assignments + catalog
            # avenue polylines + doctrinal mechanized rate (6 km/h ×
            # weather modulator). Writer DOES NOT author phase-line
            # geometry — Gemma has no spatial awareness and was emitting
            # AO-spanning horizontal lines at arbitrary latitudes. The
            # derivation connects the forward-edge points across ALL
            # engaged movement axes per H+N (one phase line = one row of
            # leading-edge points, not one line per axis).
            writer_overlays.extend(_derive_threat_time_phase_lines(
                _ap_for_writer,
                current_emit,
            ))
            # SITEMP per-COA NAIs — writer picks ids from
            # scenario.TERRAIN.nai_catalog (anti-hallucination); backend
            # synthesizes the polygon at the catalog's anchor MGRS.
            writer_overlays.extend(_extract_writer_nai_picks(
                _ap_for_writer,
                current_emit,
            ))
            # SITEMP / Event Template decision points — writer picks
            # ids from scenario.TERRAIN.dp_catalog (DP-1..DP-5); backend
            # reads canonical friendly_decision / owning_commander /
            # ltiov / triggering_indicator / linked_nais / options from
            # the catalog and anchors the DP point overlay at the
            # catalog's MGRS (or fallback to the first linked NAI's
            # anchor when DP entry lacks mgrs). Stops the writer from
            # inventing enemy-decision text in the friendly-DP column.
            writer_overlays.extend(_extract_writer_dp_picks(
                _ap_for_writer,
                current_emit,
            ))
            # SITEMP per-COA threat objectives — writer picks ids from
            # scenario.TERRAIN.threat_objectives + optional intent_override
            # (HOLD/DENY/SEIZE/RETAIN); backend synthesizes the polygon at
            # the catalog's anchor MGRS.
            writer_overlays.extend(_extract_writer_threat_objective_picks(
                _ap_for_writer,
                current_emit,
            ))
            # SITEMP per-equipment range fans. For each templated unit,
            # walk its cougaar OOB composition.equipment_list_with_counts
            # and emit ONE range_fan per UNIQUE nomenclature with a
            # doctrinal range entry (equipment_doctrine.EQ_ENGAGEMENT_RANGE_M).
            # Persisted in the SITEMP:<coa_id> bucket on the master map so
            # the threat's stand-off envelopes survive cougaar GraphQL
            # outages — the SITEMP product itself is frozen, only the
            # underlying OOB feed is live.
            writer_overlays.extend(_extract_writer_range_fans(
                _ap_for_writer,
                current_emit,
                intel_data,
            ))
            # Render z-order: paint NEUTRAL terrain first, then THREAT
            # avenues / overlays, then FRIENDLY last so friendly
            # avenues sit on top of overlapping threat envelopes. The
            # renderer paints features in the order received within an
            # overlay list, so we re-sort by (side-key, kind) before
            # pushing.
            _SIDE_Z = {"neutral": 0, "threat": 1, "hostile": 1,
                       "friendly": 2, "blue": 2, "red": 1}
            def _z(o):
                side = (o.get("attrs") or {}).get("side", "neutral")
                return _SIDE_Z.get(str(side).lower(), 0)
            writer_overlays.sort(key=_z)
            wr_sync = _push_writer_overlays_to_geo_agent(map_uuid, writer_overlays)
            structured["writer_overlay_sync"] = wr_sync
            if wr_sync.get("error"):
                log.warning("writer overlay sync error map=%s: %s",
                             map_uuid, wr_sync["error"])
            else:
                log.info("writer overlay /sync map=%s overlays_sent=%d kinds=%s",
                          map_uuid, len(writer_overlays),
                          sorted({o["kind"] for o in writer_overlays}))

            # Mirror this product's overlays onto the operations master map
            # so the operations-map sidebar's per-product visibility toggle
            # can show/hide just this product's overlay set. The mirror is
            # transactional REPLACE per source token — re-mirroring on each
            # render leaves the master with the LATEST artifact's overlays
            # per product type. Other products and the cougaar /
            # scenario_terrain layers sync under their own source tokens
            # and are untouched. Header_only products that produce no
            # overlays still mirror — the empty batch clears any stale
            # ghost geometry from a prior artifact of the same product.
            try:
                mirror_pt = (state.active_product_type or "").strip() if state else ""
                # Per-COA fanout: when the state carries a coa_focus.coa_id
                # (Threat COAs fanout / per-COA SITEMP fanout / per-COA
                # Threat COA Statement fanout), use a COMPOUND source
                # token "<product>:<coa_id>" so each COA's overlays land
                # in its OWN master-map bucket. Without the compound key,
                # all 4 COAs' fanout pushes hit the same source="SITEMP"
                # bucket, the transactional REPLACE wipes the prior COA's
                # overlays, and only the LAST-fired COA's overlays
                # survive on the master map. The operations-map sidebar's
                # per-COA toggle filter matches on this exact compound
                # token via the SITEMP:<coa_id> tokens in enabledSources.
                _coa_focus_id = ""
                if state is not None:
                    _coa_focus_id = ((getattr(state, "coa_focus", None) or {})
                                       .get("coa_id") or "").strip()
                # ASSERT: when active_product_type is COA-dimensional
                # (SITEMP fanout), coa_focus.coa_id MUST be set. Refuse
                # to push to the bare 'SITEMP' bucket — the fallthrough
                # to product_type-only silently created orphan rows in
                # the bare SITEMP bucket on the master map. Fail loud,
                # not ghost-write.
                _active_pt = getattr(state, "active_product_type", "") if state is not None else ""
                if _active_pt == "SITEMP" and not _coa_focus_id:
                    log.error(
                        "master-map mirror push REFUSED: SITEMP fanout "
                        "without coa_focus.coa_id — would write orphan "
                        "rows to the bare SITEMP bucket. Aborting mirror "
                        "for this artifact (artifact still emits)."
                    )
                    structured["master_map_mirror_error"] = (
                        "SITEMP_FANOUT_MISSING_COA_FOCUS"
                    )
                    raise RuntimeError(
                        "SITEMP fanout without coa_focus.coa_id"
                    )
                mirror_source = mirror_pt
                if mirror_pt and _coa_focus_id:
                    mirror_source = f"{mirror_pt}:{_coa_focus_id}"
                if mirror_pt:
                    # Stamp coa_id on every overlay attr so the UI filter
                    # can also disambiguate even if the consumer reads
                    # source=SITEMP (back-compat with anything that
                    # didn't switch to the compound token).
                    if _coa_focus_id:
                        tagged_writer_overlays = []
                        for o in writer_overlays:
                            attrs = dict(o.get("attrs") or {})
                            attrs.setdefault("coa_id", _coa_focus_id)
                            tagged_writer_overlays.append({**o, "attrs": attrs})
                    else:
                        tagged_writer_overlays = writer_overlays
                    master_sync = _push_overlays_to_master_map(
                        product_type=mirror_source,
                        overlays=tagged_writer_overlays,
                        project_id=getattr(state, "project_id", ""),
                    )
                    structured["master_map_mirror_sync"] = master_sync
                    if master_sync.get("error"):
                        log.warning("master-map mirror error product=%s: %s",
                                     mirror_pt, master_sync["error"])
                    else:
                        log.info("master-map mirror /sync product=%s "
                                 "overlays_sent=%d",
                                 mirror_pt, len(writer_overlays))
                    # Stale-bucket sweep — for COA-dimensional products
                    # (SITEMP fanout), GC compound buckets matching
                    # '<product>:*' whose coa_id is NOT in the current
                    # fanout's coa_id list. Cleans up the COA-ML /
                    # COA-INT / etc. leftovers when the namespace
                    # shifts. Best-effort; failure is logged + ignored.
                    if _coa_focus_id and mirror_pt:
                        try:
                            # Sibling coa_ids — every COA in the active
                            # fanout pass. Primary source is the focus
                            # dict (`state.coa_focus["all_coa_focus_ids"]`,
                            # populated by product_autodraft
                            # _autodraft_sitemp_fanout). Legacy fallback
                            # is `state.all_coa_focus_ids` for older
                            # callers that haven't migrated. EMPTY means
                            # we don't know the fanout scope → SKIP the
                            # sweep rather than nuke other COAs (every
                            # missing-siblings sweep before this fix
                            # wiped 3-of-4 COA buckets, leaving only the
                            # last-fired COA on the master map: the "all
                            # SITEMPs look identical when toggled" bug).
                            _active_coa_ids: set[str] = set()
                            if state is not None:
                                _cf_dict = getattr(state, "coa_focus", None) or {}
                                _cf_siblings = _cf_dict.get("all_coa_focus_ids")
                                if isinstance(_cf_siblings, (list, set, tuple)):
                                    _active_coa_ids = set(
                                        s.strip() for s in _cf_siblings if s
                                    )
                                if not _active_coa_ids:
                                    _legacy = getattr(state, "all_coa_focus_ids", None)
                                    if isinstance(_legacy, (list, set, tuple)):
                                        _active_coa_ids = set(
                                            s.strip() for s in _legacy if s
                                        )
                            if not _active_coa_ids:
                                log.warning(
                                    "master-map stale-bucket sweep SKIPPED "
                                    "for product=%s coa=%s — fanout sibling "
                                    "list empty (state.coa_focus."
                                    "all_coa_focus_ids missing). Doing "
                                    "nothing rather than wipe peer buckets.",
                                    mirror_pt, _coa_focus_id,
                                )
                            else:
                                _active_coa_ids.add(_coa_focus_id)
                                _mirror_map_uuid = _project_map_uuid(
                                    getattr(state, "project_id", ""))
                                _state_url = (
                                    f"{_GEO_AGENT_API_BASE}/maps/"
                                    f"{_mirror_map_uuid}/state"
                                )
                                _state_resp = requests.get(_state_url, timeout=10)
                                if _state_resp.status_code == 200:
                                    _state_body = _state_resp.json() or {}
                                    _seen_sources: set[str] = set()
                                    for _ov in _state_body.get("overlays") or []:
                                        _src = (((_ov or {}).get("attrs") or {})
                                                 .get("source") or "")
                                        if _src.startswith(f"{mirror_pt}:"):
                                            _seen_sources.add(_src)
                                    for _src in _seen_sources:
                                        _stale_coa = _src.split(":", 1)[1]
                                        if _stale_coa in _active_coa_ids:
                                            continue
                                        # Push empty batch to wipe the
                                        # stale compound bucket — the
                                        # geo-agent /sync endpoint
                                        # transactionally REPLACEs by source.
                                        _sweep = requests.post(
                                            f"{_GEO_AGENT_API_BASE}/maps/"
                                            f"{_mirror_map_uuid}/sync",
                                            params={"source": _src},
                                            json={"overlays": [], "entities": []},
                                            timeout=15,
                                        )
                                        log.info(
                                            "master-map stale-bucket sweep "
                                            "source=%s http=%s",
                                            _src, _sweep.status_code,
                                        )
                        except Exception as _sw_e:
                            log.warning(
                                "master-map stale-bucket sweep failed: %s",
                                _sw_e,
                            )
            except Exception as e:
                log.warning("master-map mirror push failed product=%s: %s",
                             (state.active_product_type if state else ""),
                             e, exc_info=True)
                structured["master_map_mirror_error"] = f"{type(e).__name__}: {e}"
        except Exception as e:
            log.warning("writer overlay push failed map=%s: %s",
                         map_uuid, e, exc_info=True)
            structured["writer_overlay_sync_error"] = f"{type(e).__name__}: {e}"

    # PNG lives at uploads/<user_id>/<artifact_id>/map.png so every export
    # for a product sits in one folder. artifact_id is pre-minted (manual-
    # dispatch flow stamps state.pending_artifact_id at turn start) so the
    # path is known BEFORE the writer emits — no rename-at-registration
    # dance, no separate map-UUID-keyed file. The URL the exporter embeds is
    # the same relative path under /files (nginx serves uploads/ at /files/).
    aid = (state.pending_artifact_id or "").strip()
    uid = (state.user_id or "anonymous").strip() or "anonymous"
    if not aid:
        # Fallback when running outside manual-dispatch (legacy harness, tests).
        aid = _next_artifact_id(state.active_product_type or "ARTIFACT")
    artifact_dir = _UPLOAD_DIR / uid / aid
    # Image filename follows the field-name convention so a product with N
    # map paragraphs gets N distinct files (combined_obstacle_overlay_map.jpg
    # + sigact_overlay_map.jpg, etc.). exporter._render_paragraph_value's
    # shape="map" branch reads /files/<uid>/<aid>/<field_name>.jpg. JPEG
    # over PNG because the headless render's zlib PNG encoder hit a 30s
    # screenshot timeout at 2560x1440 — JPEG q=90 encodes the same canvas
    # in 1-3s and is visually indistinguishable at DOCX/PPTX embed dpi.
    _img_filename = f"{map_artifact_id}.jpg"
    png_path = artifact_dir / _img_filename
    png_url  = f"/files/{uid}/{aid}/{_img_filename}"

    try:
        from uuid import UUID as _UUID
        from utils.render import render_map_dispatch, RenderOptions
        artifact_dir.mkdir(parents=True, exist_ok=True)
        # Schema-driven render primitives. Every per-product map behavior
        # (basemap raster, layer visibility presets, contour overlay,
        # cluster mode, label compactness) is read off the active
        # product schema and forwarded as a URL primitive to the
        # OperationsMapHeadless render page. The frontend treats them
        # as raw values --- no product-type switches in any client code.
        # If the schema field is absent the option stays None and the
        # frontend falls through to its analyst-edit default.
        _ap = (state.active_product_type or "") if state else ""
        _schema = PRODUCT_SCHEMAS.get(_ap, {}) if _ap else {}
        opts = RenderOptions(
            width=width,
            height=height,
            bbox=bbox_for_render,
            layers=list(layers) if layers else None,
            product_type=(_ap or None),
            basemap=_schema.get("map_basemap"),
            basemap_layer_visibility=_schema.get("map_basemap_layer_visibility"),
            doctrinal_layer_visibility=_schema.get("map_doctrinal_layer_visibility"),
            show_topographic_contours=_schema.get("map_show_topographic_contours"),
            cluster_mil_units=_schema.get("map_cluster_mil_units"),
            compact_aa_labels=_schema.get("map_compact_aa_labels"),
            compact_legend_labels=_schema.get("map_compact_legend_labels"),
            # Fast mode: caps tile wait at 20s (full = 30s), skips the
            # mandatory 1.5s symbol-flush sleep, AND stashes the warm
            # Playwright context+page across renders so subsequent
            # invocations reuse the HTTP tile cache. Full mode was costing
            # ~36s per post-merge render × 4 COAs = ~144s/fanout. The
            # fast-mode warm-context path drops the second-render-onwards
            # to single-digit seconds (matches the analyst edit-and-render
            # loop the user already tuned to <5s).
            fast=True,
        )
        # render_map_to_png is async (Playwright). Sync caller, possibly
        # nested inside a running event loop — use _run_async_safe so the
        # inner asyncio.run() either fires (no loop) or hops to a fresh
        # thread (loop already running). render_map_to_png_sync was the
        # legacy wrapper that asyncio.run-s unconditionally and breaks
        # when invoked from _build_skeleton's async context.
        # render_map_dispatch picks between geo-agent-hosted render
        # (GEO_AGENT_RENDER_URL set) and in-process Playwright. See
        # utils/render/__init__.py + scratch/docs/map-api/
        # unification-plan.md Task 6. Sync caller, possibly nested
        # inside a running event loop — _run_async_safe hops to a
        # thread if the current loop is already busy.
        png_bytes = _run_async_safe(
            lambda: render_map_dispatch(_UUID(map_uuid), opts),
            timeout=120.0,
        )
        png_path.write_bytes(png_bytes)
        # Cache the render inputs alongside the image so the map can be
        # re-rendered without re-running the LLM / product generation.
        # Consumed by handlers.product_autodraft.refire_product_maps
        # (triggered via /api/diag/product_autodraft/refire?mode=map_only).
        # One file per map field — products with multiple map paragraphs
        # (e.g. GRINTSUM overview_map + zoom_inset_map) get one each.
        try:
            (artifact_dir / f"render_params_{map_artifact_id}.json").write_text(
                json.dumps({
                    "map_uuid": map_uuid,
                    "field_name": map_artifact_id,
                    "bbox": list(bbox_for_render),
                    "layers": list(layers) if layers else None,
                    "width": width,
                    "height": height,
                    "product_type": (state.active_product_type or None) if state else None,
                    "image_relpath": _img_filename,
                }, indent=2)
            )
        except Exception as _cache_err:
            log.warning("tool_render_map: render_params cache write failed: %s", _cache_err)
        # Dump the assembled overlay payload (units / targets / control
        # measures actually pushed to geo-agent) alongside the artifact.
        # Authoritative record of what fed this product's map — used by
        # re-population logic if the per-product geo-agent map is wiped
        # (e.g. container restart loses transient maps), and as part of
        # the product folder's complete data archive per architecture.
        try:
            (artifact_dir / f"overlays_{map_artifact_id}.json").write_text(
                json.dumps({
                    "map_uuid": map_uuid,
                    "field_name": map_artifact_id,
                    "units": units_pre,
                    "targets": targets_pre,
                    "control_measures": control_measures_pre,
                }, default=str, indent=2)
            )
        except Exception as _ov_err:
            log.warning("tool_render_map: overlays.json write failed: %s", _ov_err)
        structured["image_ref"] = png_url
        structured["spatial_overlay_url"] = png_url
        structured["render_bytes"] = len(png_bytes)
        # Mirror into the header block — the exporter (_render_map_embed)
        # reads header.spatial_overlay_url to emit the <figure> tag.
        header_block["spatial_overlay_url"] = png_url
    except Exception as e:
        # Render failure (Playwright crash, ready-signal timeout, etc.) —
        # record the error AND log it loudly so the operator can see
        # which map slot is broken instead of staring at a 404 in the
        # browser. Earlier this was silently swallowed.
        log.warning(
            "tool_render_map: render failed for map=%s field=%s: %s",
            map_uuid, map_artifact_id, e, exc_info=True,
        )
        structured["image_ref"] = png_url
        structured["_render_error"] = f"{type(e).__name__}: {e}"

    return {"map_artifact_id": map_artifact_id, "format": fmt, **structured}


INTEL_AGENT_TOOL_DISPATCH = {
    "get_oob": tool_get_oob,
    "list_targets": tool_list_targets,
    "get_target": tool_get_target,
    "get_recent_intel": tool_get_recent_intel,
    "get_spatial_overlay": tool_get_spatial_overlay,
    "get_environmental": tool_get_environmental,
    "get_terrain": tool_get_terrain,
    "get_civil": tool_get_civil,
    "get_phases": tool_get_phases,
    "list_project_docs": tool_list_project_docs,
    "get_project_doc": tool_get_project_doc,
    "render_map": tool_render_map,
}

INTEL_AGENT_TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "get_oob",
        "description": "Force structure for the AO with composition rollup, from the live Cougaar KB (falls back to fixture only on transport error). Returns RED unit records by default with parent (superior) linkages, SIDC, echelon, location, and equipment composition rollup where Cougaar populates it (RED equipment is currently a known Cougaar gap — composition lists may be empty for RED).",
        "parameters": {"type": "object", "properties": {"force_type": {"type": "string", "enum": ["RED", "BLUE"], "description": "Force side. Defaults to RED."}}}}},
    {"type": "function", "function": {"name": "list_targets",
        "description": "Target collection from Cougaar's IMINT-fused target set (BE-numbered facilities). Optional filter dict; optional since_iso (ISO-8601 instant) to limit by report time. Falls back to scenario.KG fixture only if Cougaar returns zero targets AND no filter is set (empty live set is doctrinally implausible).",
        "parameters": {"type": "object", "properties": {"filter": {"type": "object"}, "since_iso": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_target",
        "description": "Single-vertex target deep-dive. target_id is required. UUID-shaped ids are routed to Cougaar (getVertexByKbID); short prefixed ids (T-DM###, U-####) fall through to the scenario.KG fixture for backward compat.",
        "parameters": {"type": "object", "properties": {"target_id": {"type": "string"}}, "required": ["target_id"]}}},
    {"type": "function", "function": {"name": "get_recent_intel",
        "description": "Multi-INT report stream from Cougaar's fusion layer (IMINT/SIGINT/ELINT/HUMINT/OOB/TACTICAL). Optional since_iso filters by report/observation time. types kept for legacy compat — Cougaar adapter returns all INTs and we bucket into reports/engagements/imagery keys. Falls back to scenario.KG fixture only on adapter failure. Caveat: most TACTICAL passthroughs and HUMINT/ELINT bodies are sparse in the live dev KB — don't over-interpret absent fields.",
        "parameters": {"type": "object", "properties": {"window_hours": {"type": "number"}, "types": {"type": "array", "items": {"type": "string"}}, "since_iso": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_spatial_overlay",
        "description": "Operations spatial overlay for the AO — phase lines, division/brigade boundaries, axes of advance, NAIs / TAIs, NOGO / SLOWGO regions. Returns OPORD-derived overlay when ingested via scenario_ingest; empty otherwise. Never returns Cougaar SpatialControl rows (verified wrong scenario AND null geometry).",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "get_environmental",
        "description": "Returns a full SWO-grade environmental record for the AO: current observed weather + 24-hour meteorological forecast (temp, wind kt+dir, visibility km, ceiling ft, cloud cover, precipitation type) per valid_dtg, astronomical data (BMNT/sunrise/sunset/EENT/moon phase/illumination %), thermal-crossover windows, and SWO attribution. date defaults to scenario DTG.",
        "parameters": {"type": "object", "properties": {"date": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_terrain",
        "description": "Terrain primitives or a named OAKOC layer. Available layers: observation_and_fields_of_fire, avenues_of_approach, key_terrain, obstacles, mobility_classifications, cover_and_concealment. Defaults: AO bbox, all layers.",
        "parameters": {"type": "object", "properties": {"bbox": {"type": "array", "items": {"type": "number"}}, "layer": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_civil",
        "description": "Civil baseline entities for the AO. Default returns all; layer may filter by ASCOPE category (areas|structures|capabilities|organizations|people|events) or PMESII (political|military|economic|social|information|infrastructure). Merges scenario_ingest's OPORD-derived civil entries with the static scenario.KG.civil baseline.",
        "parameters": {"type": "object", "properties": {"layer": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "get_phases",
        "description": "OPORD-derived phased concept of operations — ordered list of phases (id/name/start_dtg/end_dtg/concept/decisive_point/phase_objectives) extracted by scenario_ingest from the OPORD + every FRAGO. Empty until the analyst triggers the green refresh icon on the operations map. Doctrinally consumed by SITREP (Line 9 phase reference), GRINTSUM phase block, IRE phase-keyed running narrative, Intelligence Estimate phase framing, DST decision points anchored to phases, Event Template NAIs tied to phase windows, ICM/ICSM collection windows per phase, INTSUM current-phase framing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_project_docs",
        "description": "Enumerate available project docs (OPORD / OOB / FRAGOs / ANNEX_L_INTEL / RoadToWar / prior published artifacts). Optional filter by type. Returns tags + metadata.",
        "parameters": {"type": "object", "properties": {"filter": {"type": "object"}}}}},
    {"type": "function", "function": {"name": "get_project_doc",
        "description": "Fetch a single project doc body by tag. tag is required.",
        "parameters": {"type": "object", "properties": {"tag": {"type": "string"}}, "required": ["tag"]}}},
    {"type": "function", "function": {"name": "render_map",
        "description": "Compose spatial layers and render a map. layers is required (list of layer names: terrain, units, targets, spatial_overlay, civil, etc.). bbox defaults to AO. format may be 'image' / 'structured' / 'both'.",
        "parameters": {"type": "object", "properties": {"layers": {"type": "array", "items": {"type": "string"}}, "bbox": {"type": "array", "items": {"type": "number"}}, "format": {"type": "string", "enum": ["image", "structured", "both"]}}, "required": ["layers"]}}},
]


# ============================================================================
# PRODUCT-WRITER SKELETON-FIRST DISPATCH
# ============================================================================
class _ProductConfigProxy:
    def get(self, name, default=None):
        if name in PRODUCT_SCHEMAS:
            return get_product_config(name)
        return default if default is not None else {}


PRODUCT_CONFIG = _ProductConfigProxy()


def _stash_intel_tool_result(state, tool_name: str, result: dict):
    """Append a synthetic assistant+tool message pair to
    `state.intel_agent_messages` for `tool_name → result` as if the IA OE
    loop had called the tool. `_extract_ia_data` reads from the message
    log, so this is the canonical way to seed tool results into state.
    Used by the autodraft prewarm path (which bypasses the IA loop)."""
    import uuid as _uuid
    tc_id = f"prewarm-{tool_name}-{_uuid.uuid4().hex[:8]}"
    new_state = state.append_intel_agent({
        "role": "assistant",
        "tool_calls": [{
            "id": tc_id,
            "type": "function",
            "function": {"name": tool_name, "arguments": "{}"},
        }],
    })
    new_state = new_state.append_intel_agent({
        "role": "tool",
        "tool_call_id": tc_id,
        "content": json.dumps(result),
    })
    return new_state


def _extract_ia_data(state) -> dict:
    out = {}
    msgs = list(state.intel_agent_messages)
    tc_index = {}
    for m in msgs:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                tc_index[tc.get("id")] = tc.get("function", {}).get("name", "")
    for m in msgs:
        if m.get("role") == "tool":
            name = tc_index.get(m.get("tool_call_id"), "")
            if not name:
                continue
            try:
                resp = json.loads(m.get("content", "{}"))
            except Exception:
                resp = {"raw": m.get("content", "")}
            out.setdefault(name, []).append(resp)
    return out


def _entity_lookup_from_ia(intel_agent_data: dict) -> dict:
    by_id = {}
    def _name_keys(rec):
        nm = (rec.get("name") or "").strip()
        if not nm: return []
        keys = {nm, nm.upper(), nm.lower()}
        compact = "".join(nm.split())
        keys.update({compact, compact.upper(), compact.lower()})
        return list(keys)
    # Index both "get_oob" (RED+BLUE merged dispatch) and "get_oob_red"
    # (RED-only dispatch from kg_red_oob_active row_source) — same per-unit
    # shape under each tool name; without the second branch HVTL's by_id
    # comes back empty because its DAG dep id is cougaar.get_oob_red.
    for resp in intel_agent_data.get("get_oob", []) + intel_agent_data.get("get_oob_red", []):
        for u in resp.get("units", []):
            if u.get("id"):
                by_id[u["id"]] = u
            for nk in _name_keys(u):
                by_id.setdefault(nk, u)
    for resp in intel_agent_data.get("list_targets", []):
        for t in resp.get("targets", []):
            if t.get("id"):
                by_id[t["id"]] = t
            for nk in _name_keys(t):
                by_id.setdefault(nk, t)
    for resp in intel_agent_data.get("get_target", []):
        t = resp.get("target")
        if t and t.get("id"):
            by_id[t["id"]] = t
            for nk in _name_keys(t):
                by_id.setdefault(nk, t)
    return by_id


_MONTH_ABBR = {1:"JAN",2:"FEB",3:"MAR",4:"APR",5:"MAY",6:"JUN",
                 7:"JUL",8:"AUG",9:"SEP",10:"OCT",11:"NOV",12:"DEC"}
_MONTH_NUM = {v: k for k, v in _MONTH_ABBR.items()}


def canonical_now_dtg() -> str:
    """Authoritative scenario clock — delegates to `utils.scenario_clock`,
    the frozen-now module that computes max(event_data timestamps) + 30s
    and serves it to every product + the scenario-NTP daemon.

    Format: DDHHMMZmonYY (e.g. 291821ZJUN26).
    """
    from utils import scenario_clock
    return scenario_clock.now_dtg()


def dtg_subtract_hours(dtg: str, hours: int) -> str:
    """Subtract `hours` from a DDHHMMZmonYY DTG. Returns the same format with
    rollover handled correctly across day, month, and year boundaries via
    `datetime` arithmetic. If the input doesn't match the expected shape
    (e.g. legacy short-form DDHHMMZ), returns it unchanged."""
    import re
    from datetime import datetime, timedelta
    m = re.match(r"^(\d{2})(\d{2})(\d{2})Z([A-Z]{3})(\d{2})$", (dtg or "").strip())
    if not m:
        return dtg
    dd, hh, mm, mon, yy = m.groups()
    month_num = _MONTH_NUM.get(mon)
    if month_num is None:
        return dtg
    dt = datetime(2000 + int(yy), month_num, int(dd), int(hh), int(mm))
    dt2 = dt - timedelta(hours=hours)
    return (f"{dt2.day:02d}{dt2.hour:02d}{dt2.minute:02d}Z"
            f"{_MONTH_ABBR[dt2.month]}{dt2.year % 100:02d}")


# Map-bearing-ness is declared per-schema by paragraph_fields with shape="map".
# _build_skeleton iterates those and calls tool_render_map per field — no
# central allow-list, no hardcoded single-map slot.


def _build_skeleton(artifact_type: str, intel_agent_data: dict, row_ids: list, state=None,
                     defer_map_render: bool = False) -> dict:
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    cfg = PRODUCT_CONFIG.get(artifact_type, {})
    by_id = _entity_lookup_from_ia(intel_agent_data)
    # Only carry a `rows` list when the schema actually declares row_fields.
    # Otherwise paragraph/structured/lines/header_only/matrix products would
    # ship an empty `rows: []` cruft into the merged artifact.
    skeleton = {"header": {}}
    if schema.get("row_fields") or schema.get("row_factors"):
        skeleton["rows"] = []
    # dtg seeded only when the schema declares it. INTSUM (period-bracketed
    # ID, no `dtg` field) used to get an orphan dtg row stamped here, which
    # rendered as "DTG: 192318Z" even though the schema didn't ask for it.
    _header_specs = schema.get("header_fields") or {}
    if "dtg" in _header_specs:
        skeleton["header"]["dtg"] = canonical_now_dtg()
    # Reporting-window DTGs are programmatic, not LLM-derived. end = scenario
    # canonical-now; start = end - cadence_hours. Writer never fills these —
    # schema marks them source:"computed" and the skeleton-loop below skips
    # them because they are already populated here. Same pair list as
    # exporter._PAIRED_DTG_RANGES so the combined-row renderer + the seeder
    # cover the same field names.
    _WINDOW_DTG_PAIRS = [
        ("period_start_dtg",         "period_end_dtg"),         # INTSUM
        ("valid_window_start_dtg",   "valid_window_end_dtg"),   # WEM
        ("reporting_period_start_z", "reporting_period_end_z"), # GRINTSUM
        ("reporting_period_start",   "reporting_period_end"),   # Combat Assessment
    ]
    _cadence_h = int(schema.get("cadence_hours") or 12)
    for _start_key, _end_key in _WINDOW_DTG_PAIRS:
        _end_spec = _header_specs.get(_end_key) or {}
        _start_spec = _header_specs.get(_start_key) or {}
        if _end_key in _header_specs and _end_spec.get("source") == "computed":
            skeleton["header"][_end_key] = canonical_now_dtg()
        if _start_key in _header_specs and _start_spec.get("source") == "computed":
            _end = skeleton["header"].get(_end_key) or canonical_now_dtg()
            skeleton["header"][_start_key] = dtg_subtract_hours(_end, _cadence_h)
    # Classification: pull from schema default so the IBL canned string
    # ("UNCLASSIFIED // FOR EXERCISE USE ONLY") propagates through every
    # product. Hardcoded "UNCLASSIFIED" here used to clobber the canned
    # default before the skeleton-loop fallback could apply it.
    _class_default = (_header_specs.get("classification") or {}).get("default") or "UNCLASSIFIED"
    skeleton["header"]["classification"] = _class_default
    # Req 3 — drafts vs published namespace. Stamp ownership + draft status on
    # every emitted artifact so _project_docs_index can route visibility:
    #   - status="draft"     → only the owner sees it (privatedocs[user_id])
    #   - status="published" → every analyst on the project sees it (projectdocs)
    # tool_publish_artifact / POST /api/artifacts/<id>/publish flips status.
    skeleton["header"]["owner_user_id"] = state.user_id if state is not None else ""
    skeleton["header"]["status"] = "draft"
    # Per-COA SITEMP binding. When the fan-out fired this artifact, state
    # carries the {coa_id, coa_label, most_dangerous, threat_coa_statement_artifact_id} the
    # writer is targeting. Stamp these mechanically into the header so the
    # writer LLM doesn't have to bind them from prose context — the runner
    # already knows the values. Anything mechanically transformable is the
    # runner's job; the LLM is for natural-language synthesis only.
    _coa_focus = (getattr(state, "coa_focus", None) or {}) if state is not None else {}
    if _coa_focus:
        # coa_type + phase added 2026-06-01 (C6 + C7 audit). coa_type is
        # the doctrinal posture (offense/defense/irregular/hybrid) the
        # upstream Threat COA Statement writer tagged this COA with;
        # phase is the friendly-operation phase the parent Threat COA
        # Statement was emitted under (session-scoped, propagated 1:1
        # so a per-COA SITEMP can never drift onto a different phase
        # than its parent). Both fall through to the LLM-driven header
        # path when coa_focus doesn't carry them (legacy / non-fanout
        # callers).
        _focus_map = {
            "coa_id":               _coa_focus.get("coa_id"),
            "coa_label":            _coa_focus.get("coa_label"),
            "most_dangerous":       _coa_focus.get("most_dangerous"),
            "threat_coa_statement_artifact_id":     _coa_focus.get("threat_coa_statement_artifact_id"),
            "coa_type":             _coa_focus.get("coa_type"),
            "phase":                _coa_focus.get("phase"),
        }
        for _hf, _val in _focus_map.items():
            if _hf in _header_specs and _val is not None and _val != "":
                skeleton["header"][_hf] = _val
    # Per-COA SITEMP coa_type fallback. The upstream Threat COA Statement
    # may have shipped without a coa_type (legacy Threat COA Statements
    # predating the coa_type subfield addition) — derive a doctrinal
    # default from the COA's most_dangerous flag so header.coa_type is
    # never left blank. ATP 2-01.3 §6 treats the most-dangerous COA as
    # the threat-on-offense variant by default; the remaining ranks
    # default to defense. The writer's prose / scheme of maneuver can
    # still override on emit (header_analysis pass in _merge_pw_emit).
    if (artifact_type == "SITEMP"
            and "coa_type" in _header_specs
            and not (skeleton["header"].get("coa_type") or "").strip()):
        _md = bool((_coa_focus or {}).get("most_dangerous"))
        skeleton["header"]["coa_type"] = "offense" if _md else "defense"
    for f, spec in schema.get("header_fields", {}).items():
        if f in skeleton["header"] and skeleton["header"][f]:
            continue
        # session.user — auto-populate from the active analyst's user_id.
        # Used by HEADER_PREPARED_BY so every product attributes to the
        # actual person who hit Generate, not a generic unit label.
        #
        # Special case: autofire-generated artifacts (user_id "_autodraft")
        # are produced by Pytho's event-driven pipeline, not a human analyst.
        # Render those as "PYTHO AI" so the published DOCX reads cleanly
        # ("Prepared By: PYTHO AI") instead of leaking the internal pool name.
        if spec.get("source") == "session.user":
            uid = (state.user_id if state is not None else "") or ""
            skeleton["header"][f] = "PYTHO AI" if uid == "_autodraft" else uid
            continue
        # `source: "session"` (no .user) is the scenario-level header field
        # set — area_of_operations, scale, force_type. AO is constant for
        # the duration of a scenario per ATP 2-01.3 §1 (OPORD-declared AO);
        # asking the LLM to fill it produced "Not reported" every time
        # because the writer has no scenario-context handle. Pull from the
        # OPORD overlay first (scenario_ingest seeds an ao_polygon entry
        # with the OPORD-declared name), then scenario.TERRAIN.aoi as a
        # fallback. Other session-scoped fields (scale, force_type) have no
        # canonical source — leave them blank for the analyst to fill on
        # edit rather than fabricate a default.
        if spec.get("source") == "session" and f == "area_of_operations":
            ao = ""
            try:
                opord_overlay = getattr(scenario, "OPORD_OVERLAY", None) or {}
                # scenario_ingest writes the parsed OPORD entries to
                # OPORD_OVERLAY.control_measures (per scenario_ingest.py
                # contract). The AO is the entry with
                # kind in {"ao_polygon", "ao", "area_of_operations"}.
                cms = opord_overlay.get("control_measures") or []
                if isinstance(cms, list):
                    for cm in cms:
                        if not isinstance(cm, dict):
                            continue
                        if (cm.get("kind") or "").lower() in (
                                "ao_polygon", "ao", "area_of_operations"):
                            ao = (cm.get("name") or cm.get("label") or "").strip()
                            if ao:
                                break
            except Exception:
                ao = ""
            if not ao:
                try:
                    ao = (scenario.TERRAIN.get("aoi") or "").strip()
                except Exception:
                    ao = ""
            if ao:
                skeleton["header"][f] = ao
                continue
        # `source: "session"` + field name `phase` — propagate from the
        # OPORD-derived phased concept of operations. Picks the FIRST phase
        # in scenario.KG.opord_derived_phases that brackets canonical-now
        # by [start_dtg, end_dtg]; falls back to the first phase's name
        # when no phase strictly brackets (cold-start ingest, missing DTG
        # bounds). Every product whose schema declares HEADER_PHASE
        # (SITEMP / SITREP / GRINTSUM / Intelligence Estimate / DST /
        # Event Template / ICM / IRE / INTSUM) inherits this resolver —
        # without it the field silently lands as "" and the writer LLM
        # cannot recover it (source=session is short-circuited at the
        # DAG header_field handler, line ~1648).
        if spec.get("source") == "session" and f == "phase":
            phase_name = ""
            try:
                phases = list(scenario.KG.get("opord_derived_phases") or [])
            except Exception:
                phases = []
            if phases:
                now_dtg = ""
                try:
                    now_dtg = (canonical_now_dtg() or "").strip()
                except Exception:
                    now_dtg = ""
                for p in phases:
                    if not isinstance(p, dict):
                        continue
                    s_dtg = (p.get("start_dtg") or "").strip()
                    e_dtg = (p.get("end_dtg") or "").strip()
                    pname = (p.get("name") or p.get("id") or "").strip()
                    if not pname:
                        continue
                    # Brackets canonical-now (lexical compare is correct
                    # for DDHHMMZMMMYY-shaped DTGs within a single year;
                    # cross-year IBL events are not in the scenario clock
                    # window).
                    if now_dtg and s_dtg and e_dtg and s_dtg <= now_dtg <= e_dtg:
                        phase_name = pname
                        break
                # Fallback — first phase that names anything.
                if not phase_name:
                    for p in phases:
                        if not isinstance(p, dict):
                            continue
                        pname = (p.get("name") or p.get("id") or "").strip()
                        if pname:
                            phase_name = pname
                            break
            if phase_name:
                skeleton["header"][f] = phase_name
                continue
            # Doctrinal fallback when the scenario has no opord_derived_phases
            # populated (cold-start ingest, fixture-only mode, OPORD phases
            # stripped). Per ATP 3-0 the OPORD's first decisive phase is the
            # default analytical baseline; "Phase I (Penetration)" mirrors
            # the BALTIC RESTORE OPORD's named first phase.
            skeleton["header"][f] = "Phase I (Penetration)"
            continue
        # `source: "session"` + field name `as_of_dtg` — TIDAT / SITREP / any
        # ACE-to-FSE push product's observation cutoff. Doctrinally equals
        # header.dtg unless an earlier observation cutoff is explicitly
        # supplied by the runner; we copy header.dtg so the rendered DOCX
        # never shows an empty As-Of DTG (which would read as "stale"
        # regardless of intent).
        if spec.get("source") == "session" and f == "as_of_dtg":
            skeleton["header"][f] = skeleton["header"].get("dtg") or ""
            continue
        # `source: "session"` + field name `routing_destination` — ATP 3-09.32
        # ACE-to-FSE relay. FSE_CCS (FSE Cell Coordination Section) is the
        # doctrinal default route; AFATDS_DIRECT is the immediate-precedence
        # override that is set by the writer when the AGM method_of_engagement
        # names direct fires routing. Empty here would surface as
        # "[ANALYST INPUT REQUIRED]" at render time and is the wrong default —
        # almost every TIDAT routes via the FSE relay.
        if spec.get("source") == "session" and f == "routing_destination":
            skeleton["header"][f] = "FSE_CCS"
            continue
        # `source: "session"` + field name `originating_cell` — FM 2-0
        # ACE-tier intel-fusion desk identifier. Pytho's automated TIDAT
        # push is doctrinally an ACE-targeting-workstation product; the
        # human analyst label below ("G-2 ACE Targeting") matches the
        # ACE-tier convention without claiming a specific analyst.
        if spec.get("source") == "session" and f == "originating_cell":
            skeleton["header"][f] = "G-2 ACE Targeting"
            continue
        # `source: "session"` + field name matching any of {threat_actor,
        # threat, threat_force} — TCOAS uses `threat`, Threat COAs PPTX
        # uses `threat_force`, Threat Model + Threat Capability Statement
        # use `threat_actor`. Prior implementation only matched
        # `threat_actor`, which left TCOAS `Threat Force` and Threat COAs
        # PPTX `Threat Force` header cells rendering as red [ANALYST INPUT
        # REQUIRED] badges. The scenario `default` ("Donovian Armed
        # Forces") is hard-wired to the original Donovian-invasion-of-
        # Estonia scenario and silently leaks into any other scenario
        # (V Corps MITS 26, future SBIRs, etc.) when the writer doesn't
        # override. Resolve from scenario first so the V Corps threat
        # reads as "7th CAA (OCCASUS Alliance)" rather than the default.
        # scenario.KG.threat_actor is a top-level string the scenario module
        # exposes; absent → fall through to the existing schema default.
        if spec.get("source") == "session" and f in ("threat_actor", "threat", "threat_force"):
            ta = ""
            try:
                ta = (scenario.KG.get("threat_actor") or "").strip()
            except Exception:
                ta = ""
            if ta:
                skeleton["header"][f] = ta
                continue
        if "default" in spec:
            tmpl = spec["default"]
            if isinstance(tmpl, str) and "{" in tmpl:
                v = tmpl
                for k, kv in skeleton["header"].items():
                    if isinstance(kv, str):
                        v = v.replace("{" + k + "}", kv)
                if "{" in v and "}" in v:
                    v = ""
                skeleton["header"][f] = v
            else:
                skeleton["header"][f] = tmpl
            continue
        skeleton["header"].setdefault(f, "")

    # Auto-promote render_map() output into the skeleton header so the
    # exporter's _render_map_embed (utils/exporter.py:530) can find the
    # PNG URL the geo-agent returned. The tool returns a `header` block
    # with spatial_overlay_url / spatial_overlay_map_id / spatial_overlay_bbox;
    # the writer's _merge_pw_emit would otherwise drop these fields because
    # they sit inside the tool response, not inside the writer's emitted
    # header_analysis. Promoting them here means the runner is authoritative
    # for these fields — the writer can still override via header_analysis,
    # but it doesn't have to (and shouldn't, since the writer can't know the
    # geo-agent's PNG URL).
    rm_responses = list(intel_agent_data.get("render_map", []) or [])

    # Schema-driven multi-map rendering. Every paragraph_field with
    # shape:"map" declares one map figure; field name IS the PNG filename
    # convention (/files/<uid>/<aid>/<field_name>.png). The per-field
    # map_id + bbox land at structured_data.map_overlays[<field_name>] so
    # the exporter's edit-map button can deep-link the right map per slot.
    _map_paragraph_fields = [
        pf for pf in (schema.get("paragraph_fields") or [])
        if isinstance(pf, dict) and pf.get("shape") == "map"
    ]
    if _map_paragraph_fields and state is not None:
        _existing_map_fields = set()
        for rm in rm_responses:
            if isinstance(rm, dict):
                _existing_map_fields.add(rm.get("map_artifact_id") or "map")
        for _mp in _map_paragraph_fields:
            _field_name = (_mp.get("name") or "").strip()
            if not _field_name:
                continue
            if _field_name in _existing_map_fields:
                continue  # writer/IA already rendered this slot — don't double-fire
            if defer_map_render:
                # DAG path — `dag_executor.run_dag` runs a post-merge render
                # AFTER the writer emit lands, so this first render's PNG
                # would be overwritten anyway. Skip the Playwright + geo-agent
                # roundtrip and compute the metadata deterministically; the
                # post-merge render is the single source of truth for the
                # painted PNG. Saves ~4 s × N map fields per product.
                _resp = compute_map_metadata(_field_name, state,
                                              schema_bbox=_mp.get("bbox"))
            else:
                # Legacy operations-expert → product-writer path — no
                # post-merge render exists; this IS the only render.
                try:
                    _render_args = {
                        "field_name": _field_name,
                        "layers": _mp.get("layers") or
                                  ["terrain", "units", "spatial_overlay"],
                        "format": "image",
                    }
                    if _mp.get("bbox"):
                        _render_args["bbox"] = _mp["bbox"]
                    if _mp.get("map_artifact_id"):
                        _render_args["map_artifact_id"] = _mp["map_artifact_id"]
                    _resp = tool_render_map(_render_args, state, intel_data=intel_agent_data)
                except Exception as e:
                    _resp = {"_render_error": f"{type(e).__name__}: {e}",
                             "map_artifact_id": _field_name}
            if isinstance(_resp, dict):
                rm_responses.append(_resp)

    # Collate per-field map metadata into structured_data.map_overlays so the
    # exporter (shape="map" paragraph_field) can read map_id + bbox for the
    # Edit-map deep link. Each rm_resp carries map_artifact_id == field_name.
    skeleton.setdefault("map_overlays", {})
    for rm_resp in rm_responses:
        if not isinstance(rm_resp, dict):
            continue
        _field_name = rm_resp.get("map_artifact_id") or "map"
        rm_header = rm_resp.get("header") or {}
        _meta = {}
        _map_id_val = (rm_header.get("spatial_overlay_map_id")
                        or rm_resp.get("spatial_overlay_map_id")
                        or rm_resp.get("map_id"))
        if _map_id_val:
            _meta["map_id"] = _map_id_val
        _bbox_val = (rm_header.get("spatial_overlay_bbox")
                      or rm_resp.get("spatial_overlay_bbox"))
        if _bbox_val:
            _meta["bbox"] = _bbox_val
        if _meta:
            skeleton["map_overlays"][_field_name] = _meta
        # Mirror the per-field render output into the header keys so the
        # exporter's _render_map_embed (which reads header.spatial_overlay_url
        # for the figure tag) lights up regardless of the field name.
        # Multi-map products (MCOO names its map "combined_obstacle_overlay_map",
        # GRINTSUM has two map slots, etc.) used to be excluded by an
        # earlier `_field_name == "map"` guard and rendered with a broken
        # figure pointing at /files/.../map.png. Mirror the first map's
        # header keys for single-map back-compat; per-field map_overlays is
        # authoritative for the multi-map case.
        if isinstance(rm_header, dict) and not skeleton["header"].get("spatial_overlay_url"):
            for k in ("spatial_overlay_url",
                      "spatial_overlay_map_id",
                      "spatial_overlay_bbox"):
                v = rm_header.get(k)
                if v in (None, "", []):
                    continue
                skeleton["header"][k] = v

    row_fields = schema.get("row_fields") or {}
    row_factors = schema.get("row_factors") or []
    if row_factors and not row_ids:
        for fname in row_factors:
            row = {}
            for f, spec in row_fields.items():
                src = spec.get("source", "")
                if f == "factor":
                    row[f] = fname
                elif src == "analytical":
                    row[f] = False if f == "hvi_flag" else ""
                elif src.startswith("header_alias:"):
                    row[f] = skeleton["header"].get(src.split(":", 1)[1], "")
                else:
                    row[f] = ""
            for f, spec in row_fields.items():
                if "default" in spec and not row.get(f):
                    row[f] = spec["default"]
            skeleton["rows"].append(row)
    else:
        # expand_to_equipment products (HVTL): row_ids are equipment
        # NOMENCLATURE STRINGS the writer picked, not unit kg_ids. They
        # intentionally won't match by_id (by_id is keyed by unit kg_id /
        # name variants); the per-equipment fanout pass below populates
        # the instance rows from cougaar's composition_rollup. Don't drop
        # the type-row here — preserve it so the expansion has rows to
        # iterate.
        _is_equipment_pass = bool(schema.get("expand_to_equipment"))
        for rid in row_ids:
            # Multi-strategy lookup: exact, then whitespace-stripped (lets
            # writer-typed "626ADA BDE" resolve to cougaar's "626 ADA BDE"),
            # then uppercase-compact. Hallucinated ids that match nothing
            # are skipped so we do not emit half-empty rows — UNLESS the
            # schema expands type-rows into per-equipment instances later,
            # in which case the type-row is the seed the expansion needs.
            rec = by_id.get(rid)
            if not rec:
                _compact = "".join(str(rid).split())
                rec = by_id.get(_compact) or by_id.get(_compact.upper()) or by_id.get(_compact.lower())
            if not rec and not _is_equipment_pass:
                # writer hallucinated this id — drop the row rather than
                # publish an HVT with empty name / category / location.
                continue
            rec = rec or {}
            # `_source_tool` is stamped by `_cougaar_target_to_t1` /
            # `_t1_target` (intel_agent.py:1452, 1496) on every T1 target
            # record, regardless of upstream source. When present it pins
            # the provenance label to that exact tool name; falling back to
            # the legacy `T-`-prefix heuristic only when the rec was built
            # by a producer that did not stamp the field (older OOB
            # extractors, etc.). This corrects Cougaar IMINT rows that have
            # UUID kbID identifiers (no `T-` prefix) being mis-tagged as
            # `kg:get_oob` even though they originate from `list_targets`.
            src_tool = rec.get("_source_tool")
            row = {}
            provenance = {}
            for f, spec in row_fields.items():
                src = spec.get("source", "")
                if src == "kg_id":
                    row[f] = rid
                    provenance[f] = (
                        f"kg:{src_tool}" if src_tool
                        else ("kg:list_targets" if rid.startswith("T-") else "kg:get_oob")
                    )
                elif src.startswith("kg:"):
                    row[f] = rec.get(src[3:], "")
                    if row[f]:
                        provenance[f] = (
                            f"kg:{src_tool}" if src_tool
                            else ("kg:list_targets" if rid.startswith("T-") else "kg:get_oob")
                        )
                elif src.startswith("header_alias:"):
                    row[f] = skeleton["header"].get(src.split(":", 1)[1], "")
                    if row[f]:
                        provenance[f] = f"header.{src.split(':', 1)[1]}"
                elif src == "analytical":
                    row[f] = False if f == "hvi_flag" else ""
                else:
                    row[f] = ""
            for f, spec in row_fields.items():
                if "default" in spec and not row.get(f):
                    row[f] = spec["default"]
            row["_provenance"] = provenance
            skeleton["rows"].append(row)

    # HVT type-row scaffold (per per-coa-product-scope.md §11.1a / §11.4 +
    # FM 3-60 ¶B-31): the writer picks EQUIPMENT NOMENCLATURE TYPES. Each
    # skeleton row is one picked type. The fan-out to (bearer-unit ×
    # instance) rows runs POST-MERGE in _merge_pw_emit so the per-type
    # analytical text the writer just emitted (justification,
    # key_indicator, etc.) is in the type-row BEFORE we clone it across
    # instances. We pre-compute the bearer-unit / composition-rollup
    # universe here and stash it on skeleton["_units_with_comp"] (the
    # underscore-prefixed key is stripped before the artifact serializes)
    # so the post-merge expansion has cougaar's data without re-walking
    # intel_agent_data.
    if schema.get("expand_to_equipment"):
        units_with_comp: list = []
        seen_unit_ids: set = set()
        for u in by_id.values():
            if not isinstance(u, dict):
                continue
            kid = u.get("id") or u.get("name") or ""
            if not kid or kid in seen_unit_ids:
                continue
            seen_unit_ids.add(kid)
            comp = u.get("composition_rollup")
            if not isinstance(comp, dict) or not comp:
                continue
            uname = (u.get("name") or "").strip()
            if not uname:
                continue
            units_with_comp.append({
                "kg_id":         u.get("id") or "",
                "name":          uname,
                "echelon":       u.get("echelon") or "",
                "location_mgrs": u.get("location_mgrs") or "",
                "doi":           u.get("doi") or "",
                "composition":   comp,
            })
        skeleton["_units_with_comp"] = units_with_comp
    return skeleton


def _repair_malformed_dict_keys(value, schema_dict_subfields: dict | None = None):
    """Repair writer-side JSON-emission bugs where the closing brace
    of one value is dropped and the next key gets concatenated into the value
    (yielding an oversized key like `"...prose-text-here,influence_strategies"`).

    Pattern from CCA narrative_assessment:
        {
          "formal_informal_leadership":
          "...,influence_strategies": "actual influence strategies prose",
          ...
        }
    where the analyst-visible field `formal_informal_leadership` value got
    dropped and `influence_strategies` lost its leading quote.

    The repair: when a dict key matches the pattern `<long-prose>,<expected_subfield>`
    AND the schema declares `<expected_subfield>` as a legal sibling subfield,
    split into two entries — the prose becomes the value of the FIRST missing
    declared subfield (or the preceding inserted key), and the second half
    becomes the legal `<expected_subfield>` key. Walks dicts/lists recursively;
    the schema_dict_subfields map passes legal subfield names per parent-field-name.
    """
    if isinstance(value, dict):
        repaired = {}
        legal_subfields = set()
        if schema_dict_subfields:
            for legal_set in schema_dict_subfields.values():
                legal_subfields.update(legal_set)
        for k, v in value.items():
            if not isinstance(k, str) or "," not in k or len(k) < 60:
                repaired[k] = _repair_malformed_dict_keys(v, schema_dict_subfields)
                continue
            head, sep, tail = k.rpartition(",")
            tail = tail.strip()
            if tail in legal_subfields and tail not in value:
                # Decide which existing key the prose belongs to: scan the dict for
                # any declared subfield that is present but currently has an empty
                # / missing value — that's the slot the prose escaped from.
                target_key = None
                for legal in legal_subfields:
                    if legal in value and not value.get(legal):
                        target_key = legal
                        break
                if target_key is None:
                    # No empty slot; the prose was the value of the previous key
                    # in insertion order. Find the key immediately preceding `k`.
                    keys = list(value.keys())
                    try:
                        idx = keys.index(k)
                    except ValueError:
                        idx = 0
                    if idx > 0:
                        target_key = keys[idx - 1]
                if target_key and not repaired.get(target_key):
                    repaired[target_key] = head.strip()
                # The tail key takes the value originally attached to the mangled key.
                repaired[tail] = _repair_malformed_dict_keys(v, schema_dict_subfields)
            else:
                repaired[k] = _repair_malformed_dict_keys(v, schema_dict_subfields)
        return repaired
    if isinstance(value, list):
        return [_repair_malformed_dict_keys(v, schema_dict_subfields) for v in value]
    return value


def _collect_dict_subfields(schema: dict) -> dict:
    """Walk a schema's paragraph_fields, collecting legal subfield-name sets for
    every dict-shaped field. Returns {parent_field_name: [subfield_names]}.
    Used by _repair_malformed_dict_keys to decide which mangled keys to split."""
    out = {}
    for p in (schema.get("paragraph_fields") or []):
        if p.get("shape") == "dict" and p.get("subfields"):
            out[p["name"]] = list(p["subfields"])
        for sub in (p.get("item_subfields") or []):
            if isinstance(sub, dict) and sub.get("shape") == "dict" and sub.get("subfields"):
                out[sub["name"]] = list(sub["subfields"])
    return out


# DTG header field names known to resolve to the scenario clock when the
# schema marks them computed but does not supply a `default` template. Used
# by _apply_computed_header_overrides — extend when new doctrinal products
# add their own report-time DTG field.
_COMPUTED_DTG_FIELDS_TO_NOW: set[str] = {
    "message_dtg",
    "dtg",
}

# Reporting-window DTG pairs known to _build_skeleton. Mirrored here so the
# override pass can re-derive (end = canonical_now_dtg, start = end -
# cadence_hours) at validate time — covers the case where the writer LLM
# overwrote the skeleton's pre-fill (e.g. clamped to a different year) and
# the user-visible artifact would otherwise carry the writer's hallucinated
# DTG instead of the scenario clock.
_COMPUTED_DTG_PAIRS: list[tuple[str, str]] = [
    ("period_start_dtg",         "period_end_dtg"),
    ("valid_window_start_dtg",   "valid_window_end_dtg"),
    ("reporting_period_start_z", "reporting_period_end_z"),
    ("reporting_period_start",   "reporting_period_end"),
]


def _apply_computed_header_overrides(artifact_type: str, merged_header: dict) -> dict:
    """Force every `source: "computed"` header field to the runner-computed
    value, overwriting any writer-emitted text.

    The writer is INSTRUCTED via system prompt not to fill computed fields
    but ignores that 100% of the time on report-DTG headers — sampling a
    plausible DTG from its training corpus (e.g. KLIEGLIGHT walks emit
    "241430Z OCT 24" instead of the scenario clock's "281200ZJUN26").
    Trusting the source token is broken; we enforce post-merge.

    Two passes:
      1. Pure scenario-clock resolution. Fields in
         _COMPUTED_DTG_FIELDS_TO_NOW or unpaired computed-DTG schema fields
         get canonical_now_dtg(). Fields appearing in _COMPUTED_DTG_PAIRS
         get the (end = now, start = now - cadence_hours) treatment.
      2. Template substitution. Fields with a `default` containing
         {placeholders} (e.g. KL-{from_unit}-{message_dtg}) are rebuilt
         from the post-override header — so a kliegl_number computed off
         message_dtg sees the scenario-clock message_dtg, not the writer's
         "241430Z OCT 24". Two passes through the substitution loop catch
         placeholders that resolve to other placeholders (rare; not in any
         current schema but cheap insurance).

    Honors schema state: only acts on fields explicitly marked
    `source: "computed"`. Analytical / session / kg / ref fields are
    untouched.
    """
    if not isinstance(merged_header, dict):
        return merged_header
    schema = PRODUCT_SCHEMAS.get(artifact_type, {}) or {}
    header_specs = schema.get("header_fields") or {}
    if not header_specs:
        return merged_header
    cadence_hours = int(schema.get("cadence_hours") or 12)

    # Pass 1a — paired window DTGs.
    for start_key, end_key in _COMPUTED_DTG_PAIRS:
        end_spec = header_specs.get(end_key) or {}
        start_spec = header_specs.get(start_key) or {}
        if end_key in header_specs and end_spec.get("source") == "computed":
            merged_header[end_key] = canonical_now_dtg()
        if start_key in header_specs and start_spec.get("source") == "computed":
            anchor = merged_header.get(end_key) or canonical_now_dtg()
            merged_header[start_key] = dtg_subtract_hours(anchor, cadence_hours)

    # Pass 1b — standalone computed DTG fields (no pair). Identify by name
    # match against _COMPUTED_DTG_FIELDS_TO_NOW. KLIEGLIGHT's message_dtg
    # lives here.
    for fname, spec in header_specs.items():
        if not isinstance(spec, dict):
            continue
        if spec.get("source") != "computed":
            continue
        if fname in _COMPUTED_DTG_FIELDS_TO_NOW:
            merged_header[fname] = canonical_now_dtg()

    # Pass 2 — template substitution against post-override values. Anything
    # computed with a `default` like "KL-{from_unit}-{message_dtg}" gets
    # rebuilt from the freshly-stamped DTG. Iterate twice so a default that
    # references another computed default resolves on the second pass.
    for _ in range(2):
        for fname, spec in header_specs.items():
            if not isinstance(spec, dict):
                continue
            if spec.get("source") != "computed":
                continue
            tmpl = spec.get("default")
            if not (isinstance(tmpl, str) and "{" in tmpl):
                continue
            value = tmpl
            for k, kv in merged_header.items():
                if isinstance(kv, str):
                    value = value.replace("{" + k + "}", kv)
            # Only commit when every placeholder resolved; leaving "{x}"
            # in the visible artifact is worse than the writer's value.
            if "{" not in value and "}" not in value:
                merged_header[fname] = value
    return merged_header


def _merge_pw_emit(artifact_type: str, skeleton: dict, product_writer_emit: dict) -> tuple:
    cfg = PRODUCT_CONFIG.get(artifact_type, {})
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    merged = json.loads(json.dumps(skeleton))
    ha = product_writer_emit.get("header_analysis", {}) or product_writer_emit.get("header", {})
    if isinstance(ha, dict):
        for k, v in ha.items():
            # Three-way precedence per header field:
            #   1. Skeleton pre-fill wins when present (runner-computed
            #      values like report DTG, scenario clock, classifying-AO
            #      label are authoritative — see _apply_computed_header_
            #      overrides for the strict-overwrite list).
            #   2. Writer emit fills any field the skeleton left blank.
            #   3. Empty writer emit NEVER erases a skeleton pre-fill —
            #      the explicit `not writer-emit` guard below protects
            #      against a writer that omits / blanks an analytical
            #      header (operator-triggered KLIEGLIGHT lookups, partial
            #      regens) from wiping the runner's value.
            skel_v = merged["header"].get(k)
            writer_has_value = v not in (None, "")
            if skel_v and not writer_has_value:
                continue  # keep skeleton pre-fill; writer would erase
            if writer_has_value and not skel_v:
                merged["header"][k] = v
    ra = product_writer_emit.get("rows_analysis", []) or []
    if isinstance(ra, list):
        skeleton_rows = merged.get("rows", [])
        if not skeleton_rows:
            for r in ra:
                if isinstance(r, dict):
                    merged.setdefault("rows", []).append(r)
        else:
            ra_by_id = {r.get("id"): r for r in ra if isinstance(r, dict) and r.get("id")}
            # Writer emits JSON booleans as strings ("true"/"false") when
            # batched alongside text fields — the gemma-4 emit serializes
            # the whole row as a JSON dict and bool values lose their type
            # on JSON parse + downstream re-serialization. Coerce each
            # field whose row_fields spec declares a bool default to a
            # real Python bool so the renderer doesn\'t print "false".
            _rf = (schema.get("row_fields") or {})
            _bool_fields = {fn for fn, spec in _rf.items()
                            if isinstance(spec, dict) and isinstance(spec.get("default"), bool)}
            if _bool_fields:
                for _r in ra_by_id.values():
                    for _bf in _bool_fields:
                        _v = _r.get(_bf)
                        if isinstance(_v, str):
                            _s = _v.strip().lower()
                            if _s in ("true", "1", "yes"):  _r[_bf] = True
                            elif _s in ("false", "0", "no", ""): _r[_bf] = False
            ra_by_factor = {r.get("factor"): r for r in ra if isinstance(r, dict) and r.get("factor") and not r.get("id")}
            for i, row in enumerate(skeleton_rows):
                ra_row = ra_by_id.get(row.get("id")) or ra_by_factor.get(row.get("factor"))
                if ra_row is None and i < len(ra) and isinstance(ra[i], dict):
                    ra_row = ra[i]
                if not ra_row:
                    continue
                product_writer_prov = ra_row.get("_provenance") or {}
                row.setdefault("_provenance", {})
                if isinstance(product_writer_prov, dict):
                    row["_provenance"].update(product_writer_prov)
                for k, v in ra_row.items():
                    if k in ("id", "_provenance"):
                        continue
                    if v not in (None, ""):
                        row[k] = v
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    order_by = schema.get("order_by")
    if order_by and isinstance(order_by, dict):
        sort_field = order_by.get("field")
        direction = order_by.get("direction", "asc")
        rows = merged.get("rows") or []
        if sort_field and rows:
            def _sort_key(r):
                v = r.get(sort_field)
                if isinstance(v, (int, float)):
                    return v
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return float("inf") if direction == "asc" else float("-inf")
            merged["rows"] = sorted(rows, key=_sort_key, reverse=(direction == "desc"))
    for para in cfg.get("body_paragraphs", []):
        if para in product_writer_emit and product_writer_emit[para] not in (None, ""):
            merged[para] = product_writer_emit[para]

    # Propagate any top-level emit keys NOT already covered by header /
    # rows / body_paragraphs. The DAG executor stamps `_provenance` at the
    # emit's top level (one entry per paragraph_field, naming the cougaar
    # tool / upstream artifact that fed the value); without this copy the
    # provenance dict gets dropped on the floor here and the exporter
    # renders zero footnotes / no Sources section. Schema-declared body
    # paragraphs above already handle the prose fields; this catches the
    # metadata siblings.
    for k, v in (product_writer_emit or {}).items():
        if k in ("header", "header_analysis", "rows_analysis", "artifact_id",
                  "dtg", "summary", "gaps"):
            continue
        if k in cfg.get("body_paragraphs", []):
            continue
        if v in (None, "", [], {}):
            continue
        # Don't clobber what's already in merged (skeleton wins for fields
        # the runner pre-populates, e.g. map_overlays).
        if k == "_provenance" and isinstance(v, dict):
            existing = merged.setdefault("_provenance", {})
            if isinstance(existing, dict):
                for fk, fv in v.items():
                    if fk not in existing and fv:
                        existing[fk] = fv
            continue
        if k not in merged:
            merged[k] = v

    missing = []
    for f in cfg.get("header_required", []):
        if not merged.get("header", {}).get(f):
            missing.append(f"header.{f}")
    for i, row in enumerate(merged.get("rows", [])):
        for f in cfg.get("row_required", []):
            if not row.get(f):
                missing.append(f"rows[{i}({row.get('id', '?')})].{f}")
    # Honor `required: False` on paragraph_fields the same way the header/row
    # paths do — only flag empty paragraphs as gaps when the schema marked
    # them required. Two schema agents (TMP targeting, SITREP situation) had
    # to work around the missing check: TMP promoted `parent_etf_eid` to a
    # header field, SITREP added prose telling the model to emit
    # "None this period." for line_17_narrative. The fix is here, in the
    # runner — `body_required` mirrors `header_required` / `row_required`.
    # Map-shape paragraphs (shape:"map") are runner-rendered into
    # merged["map_overlays"][<para>] with a map_id + bbox, NOT into a
    # top-level merged[<para>] string. The body_required check would
    # false-flag every required map field (MCOO's
    # combined_obstacle_overlay_map, DST's decision_support_map, etc.)
    # if it only looked at the top-level slot. Build a per-product set
    # of map-shape paragraph names from the live schema, then consult
    # map_overlays for those.
    _schema_for_map = PRODUCT_SCHEMAS.get(artifact_type, {})
    _map_paragraph_names = {
        p["name"] for p in (_schema_for_map.get("paragraph_fields") or [])
        if isinstance(p, dict) and p.get("shape") == "map" and p.get("name")
    }
    _map_overlays = merged.get("map_overlays") or {}
    for para in cfg.get("body_required", []):
        if para in _map_paragraph_names:
            if not _map_overlays.get(para):
                missing.append(para)
            continue
        if not merged.get(para):
            missing.append(para)
    # Recurse into list_of_dicts paragraph fields with item_subfields and
    # report empty required item subfields. The runner aggregates per
    # subfield-path so the writer sees a concise count rather than 100 lines
    # of `operations[3].parameters[2].current_forecast_value`.
    for p in (schema.get("paragraph_fields") or []):
        if p.get("shape") != "list_of_dicts" or not p.get("item_subfields"):
            continue
        para = p["name"]
        rows = merged.get(para) or []
        if not isinstance(rows, list):
            continue
        # Walk item_subfields. For each subfield, count empty values across rows.
        # Recurse one more level for nested list_of_dicts subfields (e.g.
        # WEM operations[*].parameters[*]).
        def _walk_required(subfield_specs, items, path):
            for sf in subfield_specs:
                if not isinstance(sf, dict):
                    continue
                name = sf.get("name", "")
                # Default required unless explicitly opted out.
                is_required = sf.get("required", True) or sf.get("provenance_required")
                child_specs = sf.get("item_subfields") or []
                child_shape = sf.get("shape")
                empty_count = 0
                total = 0
                child_items_acc = []
                for it in items:
                    if not isinstance(it, dict):
                        continue
                    total += 1
                    val = it.get(name)
                    if val in (None, "", [], {}):
                        empty_count += 1
                    elif child_shape == "list_of_dicts" and child_specs and isinstance(val, list):
                        child_items_acc.extend(val)
                if is_required and empty_count and not child_specs:
                    missing.append(
                        f"{path}.{name}: {empty_count}/{total} empty"
                    )
                if child_specs:
                    _walk_required(child_specs, child_items_acc,
                                    f"{path}.{name}[*]")
        _walk_required(p["item_subfields"], rows, f"{para}[*]")
    # Final pass: enforce `source: "computed"` header semantics. The writer
    # ignores the source token on report-DTG headers (it samples a DTG from
    # its training corpus rather than echo the runner's pre-fill), so any
    # writer-emitted value for a computed field is overwritten here with
    # the runner-computed value (scenario clock + template substitution).
    # Operator-triggered KLIEGLIGHT walks were stamping "241430Z OCT 24"
    # in title + DTG; the override forces the scenario clock instead.
    if isinstance(merged.get("header"), dict):
        merged["header"] = _apply_computed_header_overrides(
            artifact_type, merged["header"],
        )

    # Post-merge equipment fan-out for HVTL (and any other schema with
    # expand_to_equipment: True). The merge above just wrote the writer's
    # per-type analytical text (justification, key_indicator,
    # function_primary, impact_of_loss, critical_elements,
    # detection_signatures, interdependencies) into the N type-rows;
    # this pass clones each enriched type-row across the
    # composition_rollup matches the runner pre-computed in
    # _build_skeleton (stashed on skeleton["_units_with_comp"]). Each
    # resulting instance row carries the type's analytical text +
    # equipment-specific kg-sourced fields (exact nomenclature, organic
    # count, bearer unit, MGRS, FM 3-60 category). Per FM 3-60 ¶B-32:
    # TVA reasons over a CAPABILITY CLASS, and every instance of that
    # class inherits the reasoning — so it's doctrinally correct for
    # the per-type analytical text to repeat across instances.
    if schema.get("expand_to_equipment"):
        units_with_comp = merged.pop("_units_with_comp", None)
        if units_with_comp is None:
            # Pull from skeleton if merge didn't already lift it (defensive).
            units_with_comp = skeleton.get("_units_with_comp") or []
        type_rows = merged.get("rows") or []
        if type_rows and units_with_comp:
            from utils.equipment_doctrine import (
                fm360_category as _fm360_cat,
                is_hvt_worthy as _is_hvt_worthy,
                FM360_TO_TARGET_CATEGORY as _FM360_TO_TARGET,
            )
            collapsed: list = []
            # Track writer-rank so we can renumber compactly after dropping
            # types that matched zero cougaar instances. The published
            # priority must be a contiguous 1..N — gaps confuse the
            # analyst ("did I miss row 3?"). The writer's original rank
            # (1..len(type_rows)) is preserved on _provenance for audit.
            # Nomenclature normalization. Writer LLMs emit doctrinal
            # hyphenated forms ('T-90A', 'MT-55A bridge', 'BMP-2 IFV')
            # while cougaar's composition_rollup uses space-separated
            # forms ('T 90A ATGM', 'MT 55A AVLB', 'IFV BMP 2M TRKD').
            # Naive substring match `'T-90A' in 'T 90A ATGM'.upper()`
            # = False — verified live against the live OOB 179
            # unique nomenclatures vs writer's selected_hvt_kg_ids
            # output. Normalize BOTH sides by:
            #   uppercase + hyphen/underscore/dot → space + collapse
            # so the substring test matches across formatting variants.
            def _norm_nom(s: str) -> str:
                return " ".join(
                    s.upper().replace("-", " ").replace("_", " ").replace(".", " ").split()
                )

            def _collapse_nom(name: str) -> str:
                """Mirror of dag_executor._collapse_nomenclature. Strips
                cougaar's series identifiers (any token containing a digit)
                so 'MTLB 1V13 FDC' / 'MTLB 1V16 FDC' / 'MTLB 1V13 TRKD BTY FDC'
                all collapse to 'MTLB FDC'. Writer's pick is collapsed by
                the same rule; substring match below compares the collapsed
                forms so one writer pick fans out across every cougaar
                series variant of that family+role."""
                kept = [tok for tok in _norm_nom(name).split()
                        if not any(ch.isdigit() for ch in tok)]
                return " ".join(kept) or _norm_nom(name)

            # Prefix-walk fuzzy matching removed 2026-06-11. The writer
            # now receives the EXACT cougaar nomenclature enumeration
            # (inject_oob_nomenclatures: True) and is instructed to
            # pick verbatim. Fuzzy matching previously collapsed
            # 11 different MTLB writer-picks down to 8 identical
            # "MTLB 1V13 FDC" rows because every prefix walked back to
            # "MTLB" and matched the first bearer's MTLB entry.
            # Exact normalized match only: writer's pick must equal a
            # cougaar nomenclature byte-for-byte (after the shared
            # hyphen-vs-space normalization). Picks that don't exact-
            # match are silently dropped at fan-out — writer either
            # learns to pick verbatim from the enumeration, or the
            # row never lands. Belt-and-suspenders with the
            # SAT_THRESHOLD drop in dag_executor's enumeration build.
            #
            # De-dup writer picks BEFORE iteration so multiple
            # rationally-equivalent picks (writer emits 'MTLB 1V13 FDC'
            # AND 'MTLB 1V13 FDC' AND 'MTLB 1V13 FIRE DIRECTION CENTER'
            # — all collapsing to the same cougaar entry) only produce
            # ONE row, not N duplicates.
            _seen_type_norms: set = set()
            for _writer_rank, type_row in enumerate(type_rows, start=1):
                type_str = str(type_row.get("id") or "").strip()
                if not type_str:
                    continue
                type_norm = _collapse_nom(type_str)
                if type_norm in _seen_type_norms:
                    log.info(
                        "expand_to_equipment: dedup-dropping writer pick "
                        "%r (already seen norm %r)", type_str, type_norm,
                    )
                    continue
                _seen_type_norms.add(type_norm)
                type_words = type_norm.split()
                # Aggregate every (bearer_unit, count) match for this type
                # across cougaar. Doctrinally the HVTL row IS the equipment
                # TYPE; bearers + counts are an attribute of the row, not
                # separate rows (one BM-30 row with multiple bearers vs
                # the prior N-rows-per-type that scattered the same HVT
                # across the table).
                bearer_pairs: list[tuple[str, int]] = []
                category_votes: dict[str, int] = {}
                fm360_votes: dict[str, int] = {}
                first_eq_name = ""
                first_mgrs = ""
                first_doi = ""
                total_count = 0
                for u in units_with_comp:
                    unit_subtotal = 0
                    for eq_name, count in (u.get("composition") or {}).items():
                        if not eq_name or not count:
                            continue
                        # Collapsed match: writer's 'MTLB FDC' family pick
                        # equals collapsed cougaar 'MTLB 1V13 FDC' /
                        # 'MTLB 1V16 FDC' / etc — every series variant
                        # aggregates under the one writer pick. Pure
                        # series mismatch ('MTLB FDC' vs 'MTLB CP') still
                        # drops because the role token differs.
                        eq_collapsed = _collapse_nom(eq_name)
                        if type_norm != eq_collapsed:
                            continue
                        if not _is_hvt_worthy(eq_name):
                            continue
                        unit_subtotal += int(count)
                        if not first_eq_name:
                            first_eq_name = eq_name
                            first_mgrs = u.get("location_mgrs") or ""
                            first_doi = u.get("doi") or ""
                        fm360 = _fm360_cat(eq_name) or ""
                        if fm360:
                            fm360_votes[fm360] = fm360_votes.get(fm360, 0) + int(count)
                        tcat = _FM360_TO_TARGET.get(fm360, "")
                        if tcat:
                            category_votes[tcat] = category_votes.get(tcat, 0) + int(count)
                    if unit_subtotal:
                        bearer_pairs.append((u.get("name") or "", unit_subtotal))
                        total_count += unit_subtotal
                if not bearer_pairs:
                    # No instances matched this type — drop the row rather
                    # than publish an empty HVT (writer hallucinated a
                    # nomenclature cougaar doesn't carry).
                    continue
                # Bearer-unit cell: multi-line "Unit (×N)" stack, sorted
                # by count descending so the heaviest holder shows first.
                bearer_pairs.sort(key=lambda kv: (-kv[1], kv[0]))
                bearer_lines = [f"{n} (×{c})" for n, c in bearer_pairs if n]
                # Plurality vote for category — same equipment nomenclature
                # always classifies the same way through fm360_category,
                # but defensive aggregate handles cougaar variants.
                top_cat = max(category_votes.items(), key=lambda kv: kv[1])[0] if category_votes else ""
                top_fm360 = max(fm360_votes.items(), key=lambda kv: kv[1])[0] if fm360_votes else ""
                collapsed_row = json.loads(json.dumps(type_row, default=str))
                row_prov = collapsed_row.get("_provenance") or {}
                if not isinstance(row_prov, dict):
                    row_prov = {}
                collapsed_row["id"] = f"hvt::{type_str}"
                collapsed_row["matched_type"] = type_str
                # Provisional rank; renumbered to a compact 1..N below
                # after empty-bearer types are dropped.
                collapsed_row["_writer_rank"] = _writer_rank
                # The writer-picked nomenclature is the row's "name"
                # (the analyst-facing High-Value Target column).
                collapsed_row["name"] = first_eq_name or type_str
                collapsed_row["bearer_unit"] = "\n".join(bearer_lines)
                collapsed_row["count"] = total_count
                collapsed_row["bearer_count"] = len(bearer_pairs)
                if top_cat:
                    collapsed_row["category"] = top_cat
                if top_fm360:
                    collapsed_row["fm360_category"] = top_fm360
                collapsed_row["location_mgrs"] = first_mgrs
                collapsed_row["doi"] = first_doi
                for _drop_key in ("equipment", "capability_mix"):
                    collapsed_row.pop(_drop_key, None)
                row_prov["name"] = "kg:get_oob_red.composition_rollup"
                row_prov["count"] = "kg:get_oob_red.composition_rollup (sum across bearers)"
                row_prov["category"] = "derived:fm360_category"
                row_prov["bearer_unit"] = "kg:get_oob_red.name (aggregated)"
                row_prov["matched_type"] = "writer.selected_hvt_kg_ids"
                row_prov["priority"] = "writer.selected_hvt_kg_ids order"
                collapsed_row["_provenance"] = row_prov
                collapsed.append(collapsed_row)
            if collapsed:
                # Per FM 3-60 App. C / JP 3-60, the HVTL priority IS the
                # CARVER ordering — sort by descending CARVER total
                # (criticality tiebreak when totals match). Falls back to
                # writer-supplied selected_hvt_kg_ids order when the
                # writer didn't emit a parsable CARVER object for the
                # row (defensive — every published row should carry one
                # per the schema's required: True).
                def _carver_sort_key(r):
                    cs = r.get("carver_score")
                    total = None
                    crit = None
                    if isinstance(cs, dict):
                        try:
                            total = int(cs.get("total")) if cs.get("total") is not None else None
                        except Exception:
                            total = None
                        try:
                            crit = int(cs.get("C")) if cs.get("C") is not None else None
                        except Exception:
                            crit = None
                    # Higher CARVER = earlier (sort descending → negate).
                    # Missing total falls to the end; writer_rank breaks
                    # remaining ties.
                    return (
                        -(total if total is not None else -1),
                        -(crit if crit is not None else -1),
                        r.get("_writer_rank") or 9999,
                    )
                collapsed.sort(key=_carver_sort_key)
                for new_rank, r in enumerate(collapsed, start=1):
                    r["priority"] = new_rank
                    wr = r.pop("_writer_rank", new_rank)
                    prov = r.get("_provenance") or {}
                    if isinstance(prov, dict):
                        cs = r.get("carver_score") or {}
                        ct = cs.get("total") if isinstance(cs, dict) else None
                        prov["priority"] = (
                            f"CARVER total={ct} (writer rank {wr})"
                            if ct is not None
                            else f"writer.selected_hvt_kg_ids order (writer rank {wr})"
                        )
                merged["rows"] = collapsed
        # Strip the internal stash even if expansion didn't run, so the
        # field never leaks into the published artifact.
        merged.pop("_units_with_comp", None)

    return merged, missing


def render_ia_data_for_pw(intel_agent_data: dict) -> str:
    if not intel_agent_data:
        return ""
    lines = ["=== INTEL AGENT RAW RETRIEVALS (full data; cite verbatim where relevant) ==="]
    for tool_name, responses in intel_agent_data.items():
        lines.append(f"\n[{tool_name}] {len(responses)} response(s)")
        for resp in responses:
            try:
                pretty = json.dumps(resp, default=str, indent=2)
                if len(pretty) > 6000:
                    pretty = pretty[:6000] + "\n…(truncated)"
                lines.append(pretty)
            except Exception:
                lines.append(str(resp)[:6000])
    return "\n".join(lines)


def render_skeleton_for_pw(artifact_type: str, skeleton: dict) -> str:
    cfg = PRODUCT_CONFIG.get(artifact_type, {})
    lines = [f"=== SKELETON ({artifact_type}) — programmatic fields pre-filled by runner ==="]
    lines.append("\n[header — pre-filled, do NOT echo back unless changing]")
    for k, v in skeleton.get("header", {}).items():
        lines.append(f"  {k}: {v if v != '' else '(blank — analytical, you fill if applicable)'}")
    if skeleton.get("rows"):
        lines.append(f"\n[rows — {len(skeleton['rows'])} entities pre-filled with T1 fields]")
        for r in skeleton["rows"]:
            lines.append(f"  - id={r.get('id')}")
            for k, v in r.items():
                if k == "id":
                    continue
                show = v if v not in ("", None, False) else "(blank)"
                lines.append(f"      {k}: {show}")
    if cfg.get("body_paragraphs"):
        lines.append("\n[body paragraphs — analytical, all blank]")
        for p in cfg["body_paragraphs"]:
            lines.append(f"  {p}: (blank)")
    # List every blank header field so the writer fills them all in
    # header_analysis on the first pass — both `analytical`-source fields and
    # `session`-source fields that the skeleton couldn't pre-fill (analyst
    # values flow in through the operations expert's prose context, not from
    # a session-state machine).
    blank_header_fields = [
        k for k, v in skeleton.get("header", {}).items() if v in (None, "", False)
    ]
    lines.append("\n=== EMIT INSTRUCTIONS ===")
    lines.append('Emit emit_artifact(artifact_type="' + artifact_type + '", structured_data={...}) where structured_data is the ANALYTICAL layer:')
    if blank_header_fields:
        lines.append("  - header_analysis: {" + ", ".join(blank_header_fields) + "}")
    if skeleton.get("rows"):
        lines.append('  - rows_analysis: list of {"id": <id>, ...analytical fields per row spec}')
    if cfg.get("body_paragraphs"):
        lines.append("  - top-level paragraph fields: " + ", ".join(cfg["body_paragraphs"]))
    lines.append("Do NOT echo skeleton-filled fields. Do NOT write [gap: ...] literals.")
    return "\n".join(lines)


# ============================================================================
# JOB REGISTRY — in-memory inventory of running / completed / failed jobs
# ----------------------------------------------------------------------------
# Every `main()` invocation registers a job at start, updates `phase` and
# `last_event_at_ms` as events flow, and marks `status` to "completed" /
# "failed" / "ws_disconnected" when the turn ends. /api/jobs/{job_id}
# (kept as the chat-resilience refresh hook used by DocEditorPage) reads
# this dict so a mid-generation refresh can resolve the active job.
# Independent of WS connection state — a backgrounded tab does NOT abort
# the job. The unified product_records registry is the source of truth for
# product chains; this dict is now narrow scope (chat → job dispatch only).
# ============================================================================
import collections as _collections
_JOBS: dict = _collections.OrderedDict()
_JOBS_MAX = 200


def _job_register(job_id: str, *, session_id: str = "", user_id: str = "",
                  manual_product_type: str = "", question: str = "",
                  extra: dict | None = None) -> dict:
    """Register a job in the in-memory _JOBS dict. `extra` carries
    DESIGN §11-shaped fields the new generate_product dispatcher writes
    (`request_type`, `request_payload`, `project_id`, `container_id`,
    `status`, `started_at_ms`). Backward compatible: existing callers use
    the kw form for legacy intel_agent.main jobs. Read by /api/jobs/{id}
    (chat-resilience refresh hook)."""
    now_ms = int(time.time() * 1000)
    job = {
        "job_id": job_id,
        "session_id": session_id,
        "user_id": user_id,
        "manual_product_type": manual_product_type or "",
        "question": question or "",
        "status": "running",
        "phase": "starting",
        "started_at_ms": now_ms,
        "ended_at_ms": None,
        "duration_ms": None,
        "events_count": 0,
        "last_event_type": "",
        "last_event_at_ms": now_ms,
        "error": None,
        "artifact_id": None,
    }
    if extra:
        job.update(extra)
    _JOBS[job_id] = job
    while len(_JOBS) > _JOBS_MAX:
        _JOBS.popitem(last=False)
    return job


def _job_update(job_id: str, **fields):
    job = _JOBS.get(job_id)
    if job is None:
        return
    job.update(fields)
    if fields.get("status") in ("completed", "failed", "ws_disconnected",
                                  "interrupted"):
        if job.get("ended_at_ms") is None:
            job["ended_at_ms"] = int(time.time() * 1000)
        if job.get("duration_ms") is None and job.get("started_at_ms"):
            job["duration_ms"] = job["ended_at_ms"] - job["started_at_ms"]


# ============================================================================
# PRODUCT-WRITER TOOLS
# ============================================================================
def _next_artifact_id(product_type: str = "") -> str:
    """Generate a deterministic, human-readable artifact id of the form
    `<PRODUCT>_<YYYYMMDDTHHMMSS_mmm>Z` — e.g.
    `MCOO_20260514T143500_472Z`.

    The id IS the path identifier — the runner uses it as both the artifact
    primary key AND the directory name under uploads/, so every export and
    every map render for one product lives at exactly one folder. No
    A-NNNNN counter, no separate M-NNNN map counter, no UUID mapper.

    Granularity is MILLISECONDS, not seconds. Second-level granularity
    produced collisions when two autodraft fires hit the same second
    (e.g. parallel rail-click + autodraft refresh) — same artifact_id,
    two SessionDetail rows, duplicate dashboard rows whose React keys
    collided across filter changes. Milliseconds plus the per-process
    monotonic counter below make collisions impossible: even if two
    fires call this in the same ms (cross-process race), the counter
    bumps the suffix.
    """
    from datetime import datetime, timezone
    ptype = (product_type or "ARTIFACT").strip().replace(" ", "_") or "ARTIFACT"
    now = datetime.now(timezone.utc)
    ms = now.microsecond // 1000  # 0..999
    base = now.strftime("%Y%m%dT%H%M%S") + f"_{ms:03d}Z"
    aid = f"{ptype}_{base}"
    # Per-process collision guard: if this exact id was minted in the
    # last few ms by an earlier call (e.g. burst-fire across handlers),
    # bump the suffix to keep it unique. The guard is in-memory; cross-
    # process collisions are vanishingly rare at ms precision but the
    # in-process bump catches the common case (same-event-loop burst).
    minted = _next_artifact_id._minted  # type: ignore[attr-defined]
    if aid in minted:
        n = 2
        while f"{aid[:-1]}-{n}Z" in minted:
            n += 1
        aid = f"{aid[:-1]}-{n}Z"
    minted.add(aid)
    # Bound the in-memory set so it doesn't grow forever — keep only the
    # most recent 1024 ids (more than enough to catch same-event-loop
    # bursts; ids from minutes ago can never collide on the timestamp).
    if len(minted) > 1024:
        for stale in list(minted)[:-1024]:
            minted.discard(stale)
    return aid


_next_artifact_id._minted = set()  # type: ignore[attr-defined]


_CHAT_TEMPLATE_TOKEN_RE = re.compile(r"<\|[^|]*\|>")
# Trailing `}}` or `{{` f-string-template leakage from writer prompts that
# pasted partial templates into string outputs. Strip trailing/leading
# clusters AND any unmatched `}}`/`{{` anywhere — but ONLY when there's no
# matching paired brace anywhere else in the string, so we don't damage legal
# JSON-as-text. Aim is the TMP pattern: "SA-15b Tor-M2 #2 (Pikasilla)}}".
_FSTRING_LEAK_TRAILING_RE = re.compile(r"\s*[{}]{2,}\s*$")
_FSTRING_LEAK_LEADING_RE = re.compile(r"^\s*[{}]{2,}\s*")


def _scrub_chat_template_tokens(value):
    """Strip chat-template + f-string-template tokens that leak into string
    fields. Removes:
      - vLLM/Gemma chat-template tokens (e.g. `<|"|>`, `<|tool_call|>`) under
        the gemma4 tool parser.
      - Trailing/leading `}}` or `{{` clusters from writer-side f-string
        template leakage (e.g. TMP `target_name`: `"SA-15b #2 (Pikasilla)}}"`).
    Walks dicts/lists recursively; non-string values pass through."""
    if isinstance(value, str):
        scrubbed = _CHAT_TEMPLATE_TOKEN_RE.sub("", value)
        scrubbed = _FSTRING_LEAK_TRAILING_RE.sub("", scrubbed)
        scrubbed = _FSTRING_LEAK_LEADING_RE.sub("", scrubbed)
        return scrubbed.strip()
    if isinstance(value, dict):
        return {k: _scrub_chat_template_tokens(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_chat_template_tokens(v) for v in value]
    return value


_VALIDATE_EMIT_DISABLED = os.environ.get("PYTHO_DISABLE_VALIDATE_EMIT", "").strip() in ("1", "true", "TRUE", "yes")


def _validate_emit(artifact_type: str, product_writer_emit: dict, state) -> dict:
    """Run the merge + fabrication check for either emit_artifact or
    submit_artifact. Returns either {error: ...} for hard failures or a
    descriptor with the merged structured_data and detected gaps.

    PYTHO_DISABLE_VALIDATE_EMIT=1 short-circuits the whole gate: the
    writer's emit passes straight through as `merged`, no FABRICATED_IDS
    rejection, no INCOMPLETE_ARTIFACT rejection, no gap audit. The
    downstream pipeline still runs (post-emit ranking, derive passes,
    artifact write). Use this flag when the writer's fabrication rate
    is too high to gate on and the analyst-tier review is happening
    downstream instead. Logged at WARNING level on every emit so the
    bypass is visible in the docker-api log stream."""
    if _VALIDATE_EMIT_DISABLED:
        log.warning(
            "_validate_emit bypassed via PYTHO_DISABLE_VALIDATE_EMIT=1 "
            "(artifact_type=%s) — writer emit passes through unchecked.",
            artifact_type,
        )
        # Still run the skeleton merge — bypassing it leaves structured_data
        # in raw writer-emit shape (header_analysis / rows_analysis) and the
        # renderer + row_count consumers see empty header + rows. The bypass
        # is for the FABRICATED_IDS / INCOMPLETE_ARTIFACT rejection gates,
        # NOT for the writer→skeleton merge.
        product_writer_emit = _scrub_chat_template_tokens(product_writer_emit)
        try:
            skeleton = json.loads(state.product_writer_skeleton_json) if state.product_writer_skeleton_json else {}
        except Exception:
            skeleton = {}
        if skeleton:
            schema_for_repair = PRODUCT_SCHEMAS.get(artifact_type, {})
            dict_subfields = _collect_dict_subfields(schema_for_repair)
            if dict_subfields:
                product_writer_emit = _repair_malformed_dict_keys(product_writer_emit, dict_subfields)
            merged, _missing = _merge_pw_emit(artifact_type, skeleton, product_writer_emit)
        else:
            merged = product_writer_emit
        return {"merged": merged, "gaps": [], "dtg": "unknown"}
    product_writer_emit = _scrub_chat_template_tokens(product_writer_emit)
    try:
        skeleton = json.loads(state.product_writer_skeleton_json) if state.product_writer_skeleton_json else {}
    except Exception:
        skeleton = {}
    if not skeleton:
        return {"error": "NO_SKELETON: skeleton not set on state. dispatch_product_writer should have built it."}
    # Repair writer-side mangled-dict-key emission bug (oversized keys carrying
    # both a prose value and the next legal subfield name, e.g. CCA's
    # narrative_assessment `formal_informal_leadership` value running into the
    # `influence_strategies` key). The schema names the legal subfields so we
    # can detect and split on `,<legal-subfield>` tails.
    schema_for_repair = PRODUCT_SCHEMAS.get(artifact_type, {})
    dict_subfields = _collect_dict_subfields(schema_for_repair)
    if dict_subfields:
        product_writer_emit = _repair_malformed_dict_keys(product_writer_emit, dict_subfields)
    merged, missing = _merge_pw_emit(artifact_type, skeleton, product_writer_emit)
    fabricated = detect_fabricated_ids(merged, state.seen_ids)
    if fabricated:
        # Diagnostic — locate WHERE in the emit each fabricated id appears
        # so the writer-prompt fix can target the specific field rather
        # than guessing. Walks merged as a dict tree and matches each
        # fabricated id against string values.
        def _find_paths(node, target, path=""):
            hits = []
            if isinstance(node, str):
                if target in node:
                    hits.append(f"{path or '<root>'}: {node[:120]}")
            elif isinstance(node, dict):
                for k, v in node.items():
                    hits.extend(_find_paths(v, target, f"{path}.{k}" if path else str(k)))
            elif isinstance(node, list):
                for i, item in enumerate(node):
                    hits.extend(_find_paths(item, target, f"{path}[{i}]"))
            return hits
        for fab in fabricated[:5]:
            paths = _find_paths(merged, fab)
            log.warning("FABRICATED_ID path: product=%s id=%r paths=%s",
                        artifact_type, fab, paths[:5])
        return {"error": f"FABRICATED_IDENTIFIERS: {fabricated} not in seen_ids. Use only IDs from the skeleton/data."}
    redactions = detect_redaction_tokens(merged)
    if redactions:
        # Cap to first 5 findings so the writer sees a focused error rather
        # than a wall of paths; the principle is the same regardless of count.
        shown = redactions[:5]
        more = f" (+{len(redactions) - 5} more)" if len(redactions) > 5 else ""
        return {"error": (
            "REDACTION_TOKEN_LEAKED: "
            + "; ".join(shown) + more
            + " — IBL / IDX 26 is UNCLASSIFIED. Substitute the real "
              "scenario value pulled from the skeleton, intel-agent "
              "retrievals, or operations-expert prose. If the data isn't "
              "in those sources, leave the field empty (the runner will "
              "surface the gap) — never emit [REDACTED] / [CLASSIFIED] / "
              "[SECRET] / asterisk-block placeholders."
        )}
    header = merged.get("header", {}) if isinstance(merged.get("header"), dict) else {}
    dtg = (header.get("dtg") or header.get("date_time_group") or merged.get("line_1_dtg") or "unknown")
    return {"merged": merged, "gaps": missing, "dtg": dtg}


def tool_emit_artifact(args, state):
    """Validate the structured_data WITHOUT registering. The writer can call
    this multiple times to self-check before submitting — it returns the
    detected gaps + fabrication errors so the writer can fix them.

    Caches the validated structured_data so the writer can `submit_artifact`
    with no args to register the most recent preview as-is."""
    artifact_type = args.get("artifact_type")
    structured_data = args.get("structured_data", {})
    v = _validate_emit(artifact_type, structured_data, state)
    if v.get("error"):
        return {"error": v["error"]}
    return {
        "ok": True, "preview": True, "type": artifact_type,
        "gaps": v["gaps"],
        "instruction": (
            "Preview only — nothing registered. If the gaps list is empty or "
            "acceptable, call `submit_artifact()` with NO arguments to register "
            "this exact structured_data. Re-pass structured_data only if you "
            "need to fix something the preview surfaced."
        ),
        "_cache_emit": json.dumps({"artifact_type": artifact_type, "structured_data": structured_data}, default=str),
    }


def _deep_merge_dict(base: dict, patch: dict) -> dict:
    """Recursively merge patch into base. Dicts merge by key. Non-dict values
    in patch overwrite base (lists / scalars / null). Used by submit_artifact
    to apply per-field corrections on top of the cached emit_artifact preview
    without forcing the writer to re-emit the entire structured_data."""
    result = dict(base or {})
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = _deep_merge_dict(result[k], v)
        else:
            result[k] = v
    return result


def tool_submit_artifact(args, state):
    """Register the artifact. ONE submit per dispatch — after success, the
    runner terminates the product-writer loop. Submit is rejected when the
    artifact is fundamentally incomplete: body paragraphs missing, all rows
    empty, or list_of_dicts items emitted without any populated content.

    All fields are optional. When `structured_data` is provided, the runner
    deep-merges it into the cached `emit_artifact` preview — pass only the
    fields that need to change for final verification. Pass nothing to
    register the cached preview as-is. Pass a fresh full structured_data to
    overlay everything."""
    cached = {}
    if state.product_writer_last_emit_json:
        try:
            cached = json.loads(state.product_writer_last_emit_json)
        except Exception:
            cached = {}
    artifact_type = args.get("artifact_type") or cached.get("artifact_type")
    patch = args.get("structured_data") or {}
    cached_sd = cached.get("structured_data") or {}
    structured_data = _deep_merge_dict(cached_sd, patch)
    if not structured_data:
        return {"error": "NO_STRUCTURED_DATA: submit_artifact called with nothing to register. Call emit_artifact first, then submit_artifact() to register the previewed structured_data (or pass a structured_data patch to refine it before registering)."}
    if not artifact_type:
        return {"error": "MISSING_ARTIFACT_TYPE: pass artifact_type or call emit_artifact first so the runner knows the product."}
    v = _validate_emit(artifact_type, structured_data, state)
    if v.get("error"):
        return {"error": v["error"]}
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    merged = v["merged"]
    blocking = []
    for p in (schema.get("paragraph_fields") or []):
        if not p.get("required", True):
            continue
        para = p.get("name")
        rows = merged.get(para)
        if rows in (None, "", [], {}):
            blocking.append(f"{para} is empty")
            continue
        if p.get("shape") == "list_of_dicts" and isinstance(rows, list):
            inner_specs = p.get("item_subfields") or []
            if not inner_specs:
                continue
            # If every item is structurally empty (nothing but a name), block.
            content_keys = [sf["name"] for sf in inner_specs
                             if isinstance(sf, dict) and sf.get("name") and sf["name"] not in ("name", "operation", "factor", "id")]
            for i, item in enumerate(rows):
                if not isinstance(item, dict):
                    continue
                if not any(item.get(k) for k in content_keys):
                    blocking.append(f"{para}[{i}] has no populated content fields")
    schema_rows = schema.get("row_fields")
    if schema_rows:
        rows = merged.get("rows") or []
        if rows and all(not any(v for k, v in r.items() if k != "id" and not k.startswith("_"))
                         for r in rows if isinstance(r, dict)):
            blocking.append("rows[*] all empty (no analytical content)")
    if blocking:
        return {
            "error": "INCOMPLETE_ARTIFACT",
            "blocking": blocking,
            "gaps": v["gaps"],
            "instruction": (
                "submit_artifact refuses to register a structurally empty artifact. "
                "Populate the fields above with the analytical content the schema "
                "requires (read forecast values from get_environmental, doctrinal "
                "thresholds from your training, etc.) and re-call submit_artifact. "
                "Calling emit_artifact first to verify is recommended."
            ),
        }
    # Adopt the pre-minted artifact_id from state (manual-dispatch flow stamps
    # it at turn start so tool_render_map can write into the right folder
    # earlier in the turn). Fall back to a fresh mint for legacy/test paths.
    artifact_id = (state.pending_artifact_id or "").strip() or _next_artifact_id(artifact_type)
    return {
        "ok": True, "artifact_id": artifact_id, "type": artifact_type,
        "doctrinal_next": DOCTRINAL_NEXT.get(artifact_type),
        "gaps": v["gaps"],
        "_submit_checkpoint": {
            "artifact_type": artifact_type, "artifact_id": artifact_id,
            "structured_data": v["merged"], "dtg": v["dtg"], "gaps": v["gaps"],
        },
    }


PRODUCT_WRITER_TOOL_DISPATCH = {
    "emit_artifact": tool_emit_artifact,
    "submit_artifact": tool_submit_artifact,
}

PRODUCT_WRITER_TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "emit_artifact",
        "description": "Preview / self-validate the artifact's analytical layer. The runner merges with the pre-built skeleton (programmatic fields) and runs gap detection + fabrication checks, but DOES NOT register. Use this to verify the structured_data is correct before committing. structured_data is the analytical layer ONLY: rows_analysis (list of {id, ...analytical fields per row}) for row-shaped products; header_analysis ({threat_coa, ...}) for header analytical fields; top-level paragraph fields for paragraph-shaped products. Do not echo skeleton fields.",
        "parameters": {"type": "object", "properties": {"artifact_type": {"type": "string"}, "structured_data": {"type": "object"}}, "required": ["artifact_type", "structured_data"]}}},
    {"type": "function", "function": {"name": "submit_artifact",
        "description": "Register the artifact and end the dispatch. The runner records the artifact, assigns an artifact_id, surfaces the gap list to the operations expert, and terminates immediately. ALL ARGUMENTS ARE OPTIONAL — the runner deep-merges any provided structured_data into the most recent emit_artifact preview's structured_data (cached per-dispatch). Common patterns: (a) submit_artifact() with no args registers the cached preview verbatim; (b) submit_artifact(structured_data={\"header_analysis\": {\"phase\": \"2\"}}) patches a single missed field into the cached preview without re-emitting everything; (c) submit_artifact(artifact_type=..., structured_data={...}) registers a fresh payload when no prior emit happened. dict fields merge recursively; lists/scalars in the patch overwrite.",
        "parameters": {"type": "object", "properties": {"artifact_type": {"type": "string"}, "structured_data": {"type": "object"}}}}},
]


# ============================================================================
# OPERATIONS-EXPERT TOOLS
# ============================================================================
def tool_set_product_type(args, state):
    product_type = args.get("product_type", "")
    if product_type not in PRODUCTS:
        return {"error": f"UNKNOWN_PRODUCT_TYPE: {product_type}. Must be one of {PRODUCTS}"}
    return {"_set_product_type": True, "product_type": product_type,
            "schema_brief": schema_brief(product_type)}


def tool_dispatch_intel_agent(args, state):
    instructions = args.get("instructions", "") or args.get("brief", "")
    if not state.active_product_type:
        return {"error": "NO_ACTIVE_PRODUCT_TYPE: Call set_product_type(product_type=<one of the 15 products>) first so I have the doctrinal schema before dispatching the intel agent."}
    product_type = state.active_product_type
    sources = (PRODUCT_SCHEMAS.get(product_type) or {}).get("data_sources") or []
    if sources:
        bullets = "\n".join(f"- {x}" for x in sources)
        template = f"For a {product_type} build, gather:\n\n{bullets}\n\nSkip any source the analyst's instructions explicitly exclude. Tighten window/scope as the analyst directs."
    else:
        template = ""
    return {"_dispatch_intel_agent": True, "instructions": instructions,
            "product_type": product_type, "template": template}


def tool_dispatch_product_writer(args, state):
    if not state.active_product_type:
        return {"error": "NO_ACTIVE_PRODUCT_TYPE: Call set_product_type(product_type=<one of the 15 products>) first."}
    artifact_type = state.active_product_type
    if artifact_type not in PRODUCT_SCHEMAS:
        return {"error": f"NO_PRODUCT_WRITER_PROMPT: templates/products/{artifact_type.lower()}.py SPEC missing. Drafting pending."}
    product_writer_prompt = prompt_for("product_writer", artifact_type)
    return {"_dispatch_product_writer": True, "artifact_type": artifact_type,
            "context": args.get("context", ""),
            "product_writer_prompt": product_writer_prompt}


def _klieglights_for_user(user_id: str, project_id: str) -> list[dict]:
    """Return ALL valid in-window KLIEGLIGHT artifacts visible to `user_id`
    across the analyst's project + the `_autodraft` pool. Ordered newest
    first.

    KLIEGLIGHT is event-driven (one artifact per perishable SIGINT signal),
    so consumers (operator UI showing the perishable queue, downstream
    SITREP Line 11, INTSUM intel block) need the FULL list of fresh
    KLIEGLIGHTs in the current window, not just the latest one.
    klieglight_autofire (poll-delta + startup sweep) is the only generator;
    this function never generates, only enumerates.

    Visibility:
      - autodraft pool always visible (any analyst can claim them)
      - sibling-project drafts only when owned by the requester
      - published artifacts always visible
    """
    db = database.SessionLocal()
    out: list[dict] = []
    try:
        rows = (
            db.query(database.SessionDetail)
            .filter(
                (database.SessionDetail.project_id == project_id) |
                (database.SessionDetail.session_id.like("autodraft-%"))
            )
            .order_by(database.SessionDetail.timestamp.desc())
            .all()
        )
        seen_aids: set[str] = set()
        for row in rows:
            try:
                d = json.loads(row.data or "{}")
            except Exception:
                continue
            trace = d.get("agent_trace") or {}
            for disp in trace.get("product_writer_dispatches") or []:
                atype = (disp.get("artifact_type")
                         or (disp.get("emit") or {}).get("artifact_type"))
                if atype != "KLIEGLIGHT":
                    continue
                emit = disp.get("emit") or {}
                aid = emit.get("artifact_id")
                if not aid or aid in seen_aids:
                    continue
                sd = emit.get("structured_data") or {}
                header = sd.get("header") or {}
                owner = header.get("owner_user_id", "")
                status = header.get("status", "")
                is_autodraft = row.session_id.startswith("autodraft-")
                if not is_autodraft:
                    if status == "draft" and owner and owner != user_id:
                        continue
                seen_aids.add(aid)
                out.append({
                    "artifact_id": aid,
                    "structured_data": sd,
                    "dtg": emit.get("dtg") or "",
                    "summary": emit.get("summary") or "",
                    "gaps": emit.get("gaps") or [],
                    "session_id": row.session_id,
                    "is_autodraft": is_autodraft,
                    "owner_user_id": owner,
                    "status": status,
                })
        return out
    finally:
        db.close()


def _bda_pool_for_user(user_id: str, project_id: str,
                         window_start: str = "",
                         window_end: str = "") -> list[dict]:
    """Return ALL BDA artifacts visible to `user_id` whose
    header.engagement_dtg falls inside [window_start, window_end].

    Combat Assessment consumes the FULL BDA pool in the reporting
    window — every BDA inside [start, end] is one engagement event in
    the CA's selected_bda_artifact_ids set. dag_executor folds this
    pool into the artifact_dep payload when the schema declares
    'BDA' in _ALL_IN_WINDOW.

    Empty window strings ('') disable the time filter — the full visible
    pool is returned. Caller (dag_executor / CA fanout) supplies the
    window when known.
    """
    db = database.SessionLocal()
    out: list[dict] = []
    try:
        rows = (
            db.query(database.SessionDetail)
            .filter(
                (database.SessionDetail.project_id == project_id) |
                (database.SessionDetail.session_id.like("autodraft-%"))
            )
            .order_by(database.SessionDetail.timestamp.desc())
            .all()
        )
        seen_aids: set[str] = set()
        for row in rows:
            try:
                d = json.loads(row.data or "{}")
            except Exception:
                continue
            trace = d.get("agent_trace") or {}
            for disp in trace.get("product_writer_dispatches") or []:
                atype = (disp.get("artifact_type")
                         or (disp.get("emit") or {}).get("artifact_type"))
                if atype != "BDA":
                    continue
                emit = disp.get("emit") or {}
                aid = emit.get("artifact_id")
                if not aid or aid in seen_aids:
                    continue
                sd = emit.get("structured_data") or {}
                header = sd.get("header") or {}
                # Window filter — engagement_dtg lexicographic compare
                # works for ISO-8601 ZULU strings. Empty window bounds
                # disable the check on that side.
                eng_dtg = header.get("engagement_dtg") or header.get("dtg") or ""
                if window_start and eng_dtg and eng_dtg < window_start:
                    continue
                if window_end and eng_dtg and eng_dtg > window_end:
                    continue
                owner = header.get("owner_user_id", "")
                status = header.get("status", "")
                is_autodraft = row.session_id.startswith("autodraft-")
                if not is_autodraft:
                    if status == "draft" and owner and owner != user_id:
                        continue
                seen_aids.add(aid)
                out.append({
                    "artifact_id": aid,
                    "structured_data": sd,
                    "dtg": emit.get("dtg") or "",
                    "summary": emit.get("summary") or "",
                    "session_id": row.session_id,
                    "is_autodraft": is_autodraft,
                    "owner_user_id": owner,
                    "status": status,
                })
        return out
    finally:
        db.close()


def _latest_klieglight_for_user(user_id: str, project_id: str) -> Optional[dict]:
    """Back-compat wrapper — return the newest KLIEGLIGHT (first entry of
    `_klieglights_for_user`) or None. Existing call sites that only need a
    single "show me one" payload still work; new call sites that need the
    full pool should use `_klieglights_for_user` directly."""
    items = _klieglights_for_user(user_id, project_id)
    return items[0] if items else None


def _latest_artifact_for_user_by_type(user_id: str, project_id: str,
                                        artifact_type: str) -> Optional[dict]:
    """Return the newest artifact of `artifact_type` visible to `user_id`
    across the analyst's project + the `_autodraft` pool, or None.

    Visibility mirrors `_klieglights_for_user`:
      - autodraft pool always visible (any analyst inherits these drafts)
      - sibling-project drafts only when owned by the requester
      - published artifacts always visible

    Used by the DAG executor's artifact_dep resolution so consumers (MCOO
    depending on WEM, INTSUM depending on SIGSUM, etc.) read autodrafts
    produced by `product_autodraft.ensure_pool_populated` from sibling
    autodraft sessions. Without this, a fresh MCOO generation in
    walker@pytho.ai's session sees `[MISSING PRECURSOR: WEM]` even though
    a perfectly good WEM autodraft sits in `_autodraft`'s session.
    """
    if not artifact_type:
        return None
    db = database.SessionLocal()
    try:
        rows = (
            db.query(database.SessionDetail)
            .filter(
                (database.SessionDetail.project_id == project_id) |
                (database.SessionDetail.session_id.like("autodraft-%"))
            )
            .order_by(database.SessionDetail.timestamp.desc())
            .all()
        )
        for row in rows:
            try:
                d = json.loads(row.data or "{}")
            except Exception:
                continue
            trace = d.get("agent_trace") or {}
            for disp in trace.get("product_writer_dispatches") or []:
                atype = (disp.get("artifact_type")
                         or (disp.get("emit") or {}).get("artifact_type"))
                if atype != artifact_type:
                    continue
                emit = disp.get("emit") or {}
                aid = emit.get("artifact_id")
                if not aid:
                    continue
                sd = emit.get("structured_data") or {}
                header = sd.get("header") or {}
                owner = header.get("owner_user_id", "")
                status = header.get("status", "")
                is_autodraft = row.session_id.startswith("autodraft-")
                if not is_autodraft:
                    if status == "draft" and owner and owner != user_id:
                        continue
                return {
                    "artifact_id": aid,
                    "structured_data": sd,
                    "dtg": emit.get("dtg") or "",
                    "summary": emit.get("summary") or "",
                    "gaps": emit.get("gaps") or [],
                    "session_id": row.session_id,
                    "is_autodraft": is_autodraft,
                    "owner_user_id": owner,
                    "status": status,
                }
        return None
    finally:
        db.close()


async def tool_generate_product(args, state, on_event=None):
    """Parallel-DAG path for doctrinal product generation.

    Generic across all products in PRODUCT_SCHEMAS — the DAG runner parses
    schema-driven nodes (header_fields, row_fields, paragraph_fields, matrix
    cells) and fans them out in parallel waves. INTSUM-only restriction
    removed once row/matrix emit assembly landed.

    `args` is the request_payload shape — the EXACT websocket-frame body:
        {"product_type": "<INTSUM | WEM | SITREP | …>",
         "session_id": "...",
         "user_id": "...",
         "project_id": "...",
         "context": "Generate INTSUM for Phase II ..."}

    `state` is the existing State dataclass (already loaded by the caller).
    `on_event(event_dict)` is the chokepoint for ALL lifecycle events — the
    runner NEVER calls manager.send_personal_message directly. The dispatcher
    that handed in on_event owns wire delivery + jobs persistence (DESIGN §12
    contract #3).

    KLIEGLIGHT exception
    --------------------
    KLIEGLIGHT generation is EXCLUSIVELY event-driven via
    handlers.klieglight_autofire (on-poll delta + one-shot startup sweep).
    An operator-triggered KLIEGLIGHT request through this entry point does
    NOT generate a fresh artifact — it returns the most recent KLIEGLIGHT
    visible to the requesting analyst (their project + the `_autodraft`
    pool). If no KLIEGLIGHT exists, returns a gap notice. This preserves the
    doctrinal rule that KLIEGLIGHT is time-perishable SIGINT-detection-driven
    (FM 2-0 §1-86; USSID SE 4001; ATP 2-22.6); on-demand re-firing would
    invent emitter data.
    """
    product_type = (args or {}).get("product_type") or ""
    if product_type not in PRODUCT_SCHEMAS:
        return {
            "error": f"unknown_product_type: {product_type!r}",
            "available": sorted(PRODUCT_SCHEMAS.keys()),
        }
    if on_event is None:
        def on_event(e):
            return None

    # KLIEGLIGHT operator-triggered short-circuit: never run the DAG; return
    # the latest existing artifact. The trigger source for new artifacts is
    # the recent-intel poller's on_poll_callback (klieglight_autofire), NOT
    # the operator. This function call IS the operator-triggered path (both
    # the WS `generate_product` frame and the OE `generate_product` tool
    # route here), so the special case lives here — one branch instead of
    # patching both call sites.
    #
    # We INTENTIONALLY skip this short-circuit when the caller is
    # klieglight_autofire itself (autofire calls tool_generate_product with
    # user_id="_autodraft" and a session_id matching `autodraft-*`). Those
    # are the AUTHENTIC generation calls; everyone else gets the lookup.
    caller_user_id = (args or {}).get("user_id") or getattr(state, "user_id", "") or ""
    caller_session = (args or {}).get("session_id") or getattr(state, "session_id", "") or ""
    is_autofire_call = (
        caller_user_id == "_autodraft"
        and caller_session.startswith("autodraft-")
    )
    if product_type == "KLIEGLIGHT" and not is_autofire_call:
        project_id = (args or {}).get("project_id") or ""
        pool = _klieglights_for_user(caller_user_id, project_id)
        if not pool:
            # Gap notice — no perishable SIGINT in the current window. This
            # matches the doctrinal rule: KLIEGLIGHT only exists when an
            # emitter has been detected. Returned in the same shape as a
            # normal DAG error so the WS dispatcher / driver can surface it
            # without special parsing.
            on_event({
                "type": "no_artifact",
                "product_type": "KLIEGLIGHT",
                "reason": "no_perishable_sigint_in_window",
                "message": (
                    "No perishable SIGINT in current window; no KLIEGLIGHT "
                    "available."
                ),
            })
            return {
                "error": "no_klieglight_available",
                "detail": (
                    "No perishable SIGINT in current window; no KLIEGLIGHT "
                    "available. KLIEGLIGHTs are generated automatically on "
                    "SIGINT detection."
                ),
            }
        # KLIEGLIGHT is event-driven, one artifact per perishable SIGINT
        # signal — when the operator (or the OE) asks for "KLIEGLIGHT" we
        # return the FULL pool of in-window KLIEGLIGHTs, not just the
        # newest. Downstream consumers (SITREP, INTSUM) need every one of
        # them; the operator UI shows the latest as default but can swap
        # to any sibling in the pool. We do NOT re-render any artifact —
        # render_artifact_files is idempotent but expensive and every
        # file is already on disk from the original autofire.
        def _entry_with_links(art: dict) -> dict:
            owner = art.get("owner_user_id") or "_autodraft"
            if art.get("is_autodraft"):
                owner = "_autodraft"
            rel = f"{owner}/{art['artifact_id']}"
            return {
                "artifact_id": art["artifact_id"],
                "artifact_type": "KLIEGLIGHT",
                "structured_data": art["structured_data"],
                "htmlLink": f"/files/{rel}/artifact.html",
                "docxLink": f"/files/{rel}/artifact.docx",
                "pptxLink": None,
                "source_session_id": art["session_id"],
                "is_autodraft": art["is_autodraft"],
                "dtg": art.get("dtg"),
                "summary": art.get("summary", ""),
            }

        pool_entries = [_entry_with_links(a) for a in pool]
        latest_entry = pool_entries[0]

        # Pool event — UI consumes this to render the full list. New event
        # type so existing job-tracking code that branches on artifact_emitted
        # is undisturbed. The pool entries themselves carry per-artifact
        # links so the UI can open any of them.
        on_event({
            "type": "klieglight_pool",
            "product_type": "KLIEGLIGHT",
            "count": len(pool_entries),
            "items": pool_entries,
            "operator_triggered_lookup": True,
        })
        # artifact_emitted for the LATEST so the job registry's
        # `artifact_id` field gets stamped (main.py:1601). The job's
        # "result artifact" is the newest in the pool; the rest are
        # surfaced via the klieglight_pool event above.
        on_event({
            "type": "artifact_emitted",
            "artifact_id": latest_entry["artifact_id"],
            "type_": "KLIEGLIGHT",
            "htmlLink": latest_entry["htmlLink"],
            "docxLink": latest_entry["docxLink"],
            "pptxLink": None,
            "operator_triggered_lookup": True,
            "pool_count": len(pool_entries),
        })
        return {
            "status": "ok",
            # Single-artifact fields mirror the latest entry — keeps callers
            # that haven't been pool-aware-ified working (job-finalize path,
            # legacy OE response parsers).
            "artifact_id": latest_entry["artifact_id"],
            "artifact_type": "KLIEGLIGHT",
            "structured_data": latest_entry["structured_data"],
            # `rendered` sub-dict is the DAG-path contract that _run_and_finalize
            # reads to populate the `artifact_complete` WS frame (main.py:1665).
            # Without this the frontend gets htmlLink=None and DocEditorPage
            # surfaces "exporter render may have failed". The files already
            # exist on disk — the autofire that produced this artifact ran
            # render_artifact_files at emit time.
            "rendered": {
                "htmlLink": latest_entry["htmlLink"],
                "docxLink": latest_entry["docxLink"],
                "pptxLink": None,
            },
            "htmlLink": latest_entry["htmlLink"],
            "docxLink": latest_entry["docxLink"],
            "pptxLink": None,
            "source_session_id": latest_entry["source_session_id"],
            "is_autodraft": latest_entry["is_autodraft"],
            # Pool fields — the full in-window set, newest first.
            "pool": pool_entries,
            "pool_count": len(pool_entries),
            "operator_triggered_lookup": True,
            # Analyst-facing summary — keep doctrinally neutral. The
            # `operator_triggered_lookup` / `pool_count` fields above
            # carry the implementation-mechanism detail for the runner
            # and the docker logs (see log.info at the autofire entry
            # point and the pool-resolve log line earlier in this
            # function — both verbose for engineers). The chat / WS
            # frame surfaces this string verbatim, so it must not leak
            # internals like "operator-triggered" / "not a fresh
            # generation" / "lookup".
            "summary": (
                f"Retrieving updated KLIEGLIGHT on this signal — "
                f"{len(pool_entries)} in-window KLIEGLIGHT"
                f"{'s' if len(pool_entries) != 1 else ''} returned."
            ),
        }

    from runtime.dag_executor import run_dag
    # Global per-product serialization — every product fire path
    # (dashboard Generate, autodraft sweep, KLIEGLIGHT autofire, OSIR
    # autofire, BDA autofire, map-driven right-click) funnels through
    # this function, so the lock acquired here serializes the entire
    # server. Multiple users + multiple in-flight chains queue at this
    # lock; only one chain holds it at a time. The session_id is
    # registered for progress tracking so the dashboard can read
    # "Generating... XX%" off the wave-end record_transition broadcasts
    # during the chain's run.
    from utils import llm as _llm
    _sess = (args or {}).get("session_id") or getattr(state, "session_id", "") or ""
    log.info(
        "tool_generate_product GATE_WAIT: product=%s session=%s "
        "queue_holders_present=%s",
        product_type, _sess,
        _llm.PRODUCT_GATE.locked(),
    )
    async with _llm.PRODUCT_GATE:
        log.info(
            "tool_generate_product GATE_ACQUIRED: product=%s session=%s",
            product_type, _sess,
        )
        # Bind the session_id as the active chain for the vllm gate's
        # progress hook + seed a token budget so chain_progress_pct
        # returns a meaningful number on the first wave (vllm budget
        # 131k × ~3 waves ≈ 400k tokens typical; we use the gate's
        # in-process budget × 4 as a rough chain-total estimate so the
        # tokens-consumed counter normalizes to a sensible percentage).
        _cv_tok = _llm.set_current_chain_session(_sess)
        if _sess:
            try:
                _budget = getattr(_llm._GATE, "_budget", 131072)
                _llm.set_chain_total(_sess, _budget * 4)
            except Exception:
                pass
        try:
            return await run_dag(product_type, args, state, on_event)
        finally:
            _llm.reset_current_chain_session(_cv_tok)
            _llm.clear_chain(_sess)
            log.info(
                "tool_generate_product GATE_RELEASE: product=%s session=%s",
                product_type, _sess,
            )


def tool_publish_artifact(args, state):
    """Flip a draft artifact's status to 'published' so every analyst on the
    project sees it via _project_docs_index. The runner-side loop performs
    the actual state mutation; this tool returns a marker (mirrors the
    `_set_product_type` / `_dispatch_intel_agent` pattern)."""
    artifact_id = (args.get("artifact_id") or "").strip()
    if not artifact_id:
        return {"error": "artifact_id required"}
    # Verify the artifact exists in the current state's registry. The actual
    # status flip happens in run_operations_expert_loop where it can rewrite
    # state. We pre-check here so we can emit a clean error to the agent.
    for atype, entries in state.artifact_registry:
        for entry in entries:
            if entry.get("artifact_id") == artifact_id:
                return {"_publish_artifact": True, "artifact_id": artifact_id,
                        "artifact_type": atype}
    return {"error": f"UNKNOWN_ARTIFACT_ID: {artifact_id} is not in the registry. "
                     "Only artifacts emitted in this session can be published from this tool."}


_TOP_N_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}


def _parse_top_n(context_text: str) -> Optional[int]:
    """Parse an analyst top-N directive from the operations-expert context.

    CARVER and similar row products are inherently top-N priority exercises
    (FM 3-60 App. G). When the analyst says 'top three Phase 2 HVTs' the
    skeleton MUST be 3 rows, not the full active set — otherwise the writer
    honors the directive (3 rows) but the gap detector flags the unfilled
    skeleton tail. Returns None when no directive is present (default-all
    behavior unchanged).
    """
    if not context_text:
        return None
    text = context_text.lower()
    # Numeric forms: "top 3", "top-3", "first 5", "best 4".
    m = re.search(r"\b(?:top|first|best|leading)[\s\-]+(\d{1,2})\b", text)
    if m:
        try:
            n = int(m.group(1))
            if 1 <= n <= 50:
                return n
        except ValueError:
            log.debug("top-N parse: regex matched %r but int() failed", m.group(1))
    # Word forms: "top three", "first five".
    m = re.search(r"\b(?:top|first|best|leading)[\s\-]+(" +
                   "|".join(_TOP_N_WORDS.keys()) + r")\b", text)
    if m:
        return _TOP_N_WORDS[m.group(1)]
    return None


def _derive_row_ids(artifact_type: str, intel_agent_data: dict, context_text: str) -> list:
    schema = PRODUCT_SCHEMAS.get(artifact_type, {})
    if schema.get("shape") != "row":
        return []
    row_source = schema.get("row_source") or ""
    if row_source == "factor_list":
        return []
    targets = [t for resp in intel_agent_data.get("list_targets", [])
                for t in resp.get("targets", []) if t.get("id")]
    units = [u for resp in intel_agent_data.get("get_oob", [])
              for u in resp.get("units", []) if u.get("id")]
    if row_source == "kg_targets_active":
        # Writer-controlled row-id selection — when the schema declares
        # row_id_selection_field this fallback path MUST NOT seed all
        # rows. dag_executor.py:3603 (harvest path) overrides row_ids
        # with the writer-emitted selection AFTER the selection
        # header_field lands; here we short-circuit so the kg_targets_
        # active full-pool seed doesn't preempt that override. HVTL
        # already proves this works (see selected_hvt_kg_ids singleton
        # wave). Without this short-circuit, HPTL / CARVER / TSS would
        # emit a row per active target instead of the writer's selection.
        if (schema.get("row_id_selection_field") or "").strip():
            log.info(
                "_derive_row_ids[%s]: row_id_selection_field=%s set — "
                "deferring to dag_executor harvest path (NOT seeding "
                "from kg_targets_active full pool)",
                artifact_type, schema.get("row_id_selection_field"),
            )
            return []
        active = [t["id"] for t in targets
                   if (t.get("size_posture_activity") or "").lower() != "defeated"]
        # Honor explicit target IDs in the analyst's prose context first —
        # the operations expert often resolves a top-N directive into named
        # IDs ('Proceed with: T-DA0173, T-DA0601, T-DA0201'). Intersect with
        # the active set so we don't pick up defeated targets or fabricated
        # IDs from the analyst's copy-paste.
        explicit = re.findall(r"T-[A-Z]{2}\d{3,5}", context_text or "")
        if explicit:
            active_set = set(active)
            seen = set()
            picked = []
            for tid in explicit:
                if tid in active_set and tid not in seen:
                    seen.add(tid)
                    picked.append(tid)
            if picked:
                return picked
        # Fallback: bare top-N directive without resolved IDs ('top three
        # Phase 2 HVTs'). Slice the active set; the writer then fills every
        # skeleton row.
        top_n = _parse_top_n(context_text or "")
        if top_n is not None:
            return active[:top_n]
        return active
    if row_source == "kg_units_active":
        active = [u["id"] for u in units
                   if "DESTROYED" not in (u.get("size_posture_activity") or "").upper()
                   and "DEFEATED" not in (u.get("size_posture_activity") or "").upper()]
        explicit = re.findall(r"U-[A-Z0-9\-]{3,}", context_text or "")
        if explicit:
            active_set = set(active)
            seen = set()
            picked = []
            for uid in explicit:
                if uid in active_set and uid not in seen:
                    seen.add(uid)
                    picked.append(uid)
            if picked:
                return picked
        top_n = _parse_top_n(context_text or "")
        if top_n is not None:
            return active[:top_n]
        return active
    if row_source == "kg_target_single":
        explicit = re.findall(r"T-[A-Z]{2}\d{3,5}", context_text or "")
        if explicit:
            return [explicit[0]]
        return [t["id"] for t in targets[:1]]
    if row_source == "dst_dps":
        # PIR rows are 1-per-DST-decision-point. Look up the latest DST
        # autodraft from the pool, extract decision_points[].id, name each
        # PIR row "PIR-<DP-ID>" so the writer per-column LLM call can
        # mechanically map row_id → upstream DP context.
        #
        # Reading from the autodraft pool (not state.artifact_registry) so
        # the autodraft path works without a prior session-scoped DST gen.
        # The dep-aware sweep guarantees DST is in the pool before PIR
        # fires (PIR consumes_artifacts lists DST).
        try:
            from handlers.product_autodraft import _autodraft_pool_index
            pool = _autodraft_pool_index(getattr(state, "project_id", ""))
            dst_entries = pool.get("Decision Support Template") or []
            if not dst_entries:
                return []
            # Latest entry first (pool is ordered). Pull its structured_data
            # and extract decision_points[].id.
            dst_entry = dst_entries[0]
            dispatch = dst_entry.get("dispatch") or {}
            sd = (dispatch.get("emit") or {}).get("structured_data") or {}
            dps = sd.get("decision_points") or []
            ids = []
            for dp in dps:
                if not isinstance(dp, dict):
                    continue
                dp_id = (dp.get("id") or "").strip()
                if dp_id:
                    ids.append(f"PIR-{dp_id}")
            return ids
        except Exception:
            log.exception("_derive_row_ids dst_dps: failed to read DST pool")
            return []
    return []


OPERATIONS_EXPERT_TOOL_DISPATCH = {
    "set_product_type": tool_set_product_type,
    "dispatch_intel_agent": tool_dispatch_intel_agent,
    "dispatch_product_writer": tool_dispatch_product_writer,
    "publish_artifact": tool_publish_artifact,
    # Parallel-DAG path. Currently INTSUM-only; non-INTSUM calls return a
    # fallback hint and the OE falls back to the legacy dispatch_intel_agent +
    # dispatch_product_writer flow. Tool handler is async; OE loop today is
    # sync, so OE wiring runs through a sync shim that awaits tool_generate_product
    # in the worker thread's event loop.
    "generate_product": tool_generate_product,
}

OPERATIONS_EXPERT_TOOLS_SCHEMA = [
    {"type": "function", "function": {"name": "set_product_type",
        "description": "Load the doctrinal schema for a product. Returns the schema brief: doctrine reference, ordering rule, header fields with source tags, fixed structure (rows / paragraphs / lines / matrix axes / enums), doctrinal subtleties, hard rules, and the intel-agent data sources. Call this BEFORE dispatch_intel_agent or dispatch_product_writer so I know exactly what the schema fixes (and so I avoid proposing factor lists, COA frameworks, line numbers, or other structural elements the schema already covers). The session records the active_product_type from this call, and subsequent dispatches use it.",
        "parameters": {"type": "object", "properties": {"product_type": {"type": "string", "description": "one of: " + ", ".join(PRODUCTS)}}, "required": ["product_type"]}}},
    {"type": "function", "function": {"name": "dispatch_intel_agent",
        "description": "Dispatch the junior intel agent with my analyst-derived instructions. Requires set_product_type to have been called first; the runner uses the active_product_type to load the schema's data-sources template into the intel agent's context as the doctrinal baseline. The intel agent executes the template plus my instructions (which take precedence — e.g., 'skip weather this cycle' suppresses the template's get_environmental). I do not pick tools myself; the schema owns the doctrinal feeders, the analyst owns the directives.",
        "parameters": {"type": "object", "properties": {"instructions": {"type": "string", "description": "analyst directives / scope / focus / exclusions / time windows. Required."}}, "required": ["instructions"]}}},
    {"type": "function", "function": {"name": "dispatch_product_writer",
        "description": "Invoke the per-product writer with the analyst-aligned plan. Requires set_product_type to have been called first. The runner builds a skeleton (programmatic fields pre-filled from the intel agent's T1 entity records, plus a canonical-now DTG default) and shows it to the writer alongside the intel agent's full raw retrievals. The writer emits the analytical layer including any header fields it can derive from the prose context or intel-agent retrievals (phase from OPORD, force_type from analyst directive, etc.). The runner merges and registers the artifact. Row entities for row-shaped products are derived automatically. Returns artifact_id, doctrinal_next, and a one-line summary. Only call after the analyst has explicitly approved.",
        "parameters": {"type": "object", "properties": {
            "context": {"type": "string", "description": "plain-prose context for the writer: analyst intent + analyst directives (per-row priorities, exclusions, header values like 'force type: HBCT', 'phase: 2 (advance)'), specific target ids when relevant (e.g., 'BDA for T-DA0172'), constraints. The writer reads this prose plus the intel-agent retrievals (full OPORD/FRAGO/etc.) and writes whatever header_analysis fields it can derive."}
        }, "required": ["context"]}}},
    {"type": "function", "function": {"name": "publish_artifact",
        "description": "Publish a draft artifact from privatedocs to projectdocs. After publishing, every analyst on the project sees it in list_project_docs / get_project_doc; before publishing, only the analyst who created the draft can see it. Call this once the analyst has reviewed and approved the artifact for project-wide visibility.",
        "parameters": {"type": "object", "properties": {
            "artifact_id": {"type": "string", "description": "The artifact_id returned by a prior dispatch_product_writer emit (e.g., 'WEM-0001', 'T-DA0173')."}
        }, "required": ["artifact_id"]}}},
    {"type": "function", "function": {"name": "generate_product",
        "description": "Canonical single-call path for generating a doctrinal product end-to-end via the parallel-DAG runner. Currently DAG-enabled for INTSUM only — for other product_type values the call returns a fallback hint and you must use the dispatch_intel_agent + dispatch_product_writer two-step path. When DAG-enabled, this fans out cougaar retrievals, header fields, and paragraph fields in parallel, merges into structured_data, validates, registers, and renders — all in one tool call. Returns {artifact_id, gaps, turn_total_ms, rendered}.",
        "parameters": {"type": "object", "properties": {
            "product_type": {"type": "string", "description": "Doctrinal product type. DAG-enabled: INTSUM. Anything else returns the fallback hint."},
            "context": {"type": "string", "description": "Analyst intent, scope, directives, header values — same prose shape you'd pass to dispatch_product_writer's `context`. The DAG's per-field writers see this verbatim."}
        }, "required": ["product_type"]}}},
]

# Manual-dispatch variant: same tools as OPERATIONS_EXPERT_TOOLS_SCHEMA minus
# `set_product_type` (the manual path pre-sets active_product_type from the
# Generate-button payload, so there is no schema-selection step). Loaded into
# `_chat` only when the loop is running with `operations_expert_manual.md` as
# the system prompt — see run_operations_expert_loop's tools_schema param.
OPERATIONS_EXPERT_MANUAL_TOOLS_SCHEMA = [
    tool for tool in OPERATIONS_EXPERT_TOOLS_SCHEMA
    if tool["function"]["name"] != "set_product_type"
]


# ============================================================================
# CHAT WRAPPERS — vLLM via utils.llm
# ============================================================================
def _chat(messages, tools, thinking, timeout=600, max_tokens=8000, temperature=0.2):
    # Default timeout bumped 300→600s after OED + Threat Overlay v2 runs both
    # hit ReadTimeout on product_writer iteration 5/6. With real cougaar data
    # flowing, the writer has substantially more substance to compose per call
    # — 300s is too tight for landscape map-bearing products on 4-wide contention.
    return llm_module.invoke_vllm_chat(
        messages, tools=tools, thinking=thinking,
        timeout=timeout, max_tokens=max_tokens, temperature=temperature,
    )


# ============================================================================
# Malformed tool-call recovery
# ============================================================================
# vLLM's gemma4 tool parser intermittently drops tool calls when the model
# emits them with chat-template-token quotes (`<|"|>` instead of `"`). The
# parser falls through to content; we recognize the pattern and reconstruct
# the call so the writer's intended emit doesn't get lost.
_MALFORMED_TC_PREFIX = "<|tool_call"


def _recover_malformed_tool_call(content: str) -> Optional[dict]:
    if not content or _MALFORMED_TC_PREFIX not in content[:80]:
        return None
    s = content
    # Strip leading template-token wrapper variants.
    for prefix in ("<|tool_call|>", "<|tool_call>", "call:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
    # Trim trailing template wrapper.
    for suffix in ("<|/tool_call|>", "<|end_tool_call|>"):
        if s.rstrip().endswith(suffix):
            s = s.rstrip()[:-len(suffix)]
    # Strip leading "call:" when nested inside the wrapper.
    s = s.lstrip()
    if s.startswith("call:"):
        s = s[len("call:"):]
    brace = s.find("{")
    if brace < 1:
        return None
    name = s[:brace].strip().rstrip("(")
    body = s[brace:]
    # Quote-token → real quote.
    body = body.replace('<|"|>', '"')
    # Bare keys → quoted keys (very rough; matches `,key:` and `{key:`).
    body = re.sub(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', body)
    # Strip any remaining template tokens defensively.
    body = re.sub(r"<\|[^|]*\|>", "", body)
    try:
        args = json.loads(body)
    except Exception:
        log.debug("mangled tool-call JSON salvage failed for name=%s body=%r", name, body[:200], exc_info=True)
        return None
    return {"name": name, "arguments": json.dumps(args, default=str)}


def summarize_build(messages, artifact_id, artifact_type):
    excerpt = "\n".join(json.dumps(m, default=str)[:500] for m in messages)[:7000]
    user_msg = f"artifact: {artifact_id} (type {artifact_type})\nbuild log:\n{excerpt}"
    try:
        resp = llm_module.invoke_vllm_chat(
            [{"role": "system", "content": SUMMARIZER_PROMPT},
             {"role": "user", "content": user_msg}],
            tools=None, thinking=False, timeout=120, temperature=0.1,
        )
        return (resp["choices"][0]["message"].get("content") or "").strip() or f"{artifact_id} ({artifact_type}) emitted"
    except Exception:
        return f"{artifact_id} ({artifact_type}) emitted"


# ============================================================================
# INTEL-AGENT LOOP
# ============================================================================
_IA_FETCH_TOOLS = {"get_oob", "list_targets", "get_target", "get_recent_intel",
                   "get_environmental", "get_terrain", "get_civil",
                   "list_project_docs", "get_project_doc", "render_map"}


def _parse_ia_content(content: str):
    status = "completed"
    gaps = []
    lines = content.splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        low = s.lower()
        if low.startswith("status:"):
            v = s.split(":", 1)[1].strip().lower()
            if v in ("completed", "blocked", "clarification_needed"):
                status = v
        elif low.startswith("gaps:"):
            tail = s.split(":", 1)[1].strip()
            if tail and tail.lower() != "none":
                gaps.append(tail)
            for j in range(i + 1, len(lines)):
                gl = lines[j].strip()
                if not gl:
                    break
                if gl.lower().startswith("status:"):
                    break
                if gl.startswith("- "):
                    gaps.append(gl[2:].strip())
                elif gl.startswith("* "):
                    gaps.append(gl[2:].strip())
                else:
                    gaps.append(gl)
            break
    return status, gaps


def run_intel_agent_loop(state: State, brief: str, max_iter=15, on_event=None):
    _ia_t0 = time.monotonic()
    state = replace(state, intel_agent_messages=({"role": "system", "content": prompt_for("intel_agent")},
                                                 {"role": "user", "content": brief}))
    raw_data = {}
    sources_consulted = []
    iter_timings = []
    # Count how many fetch-tool calls have actually fired in this dispatch.
    # Used to reject "final content without any retrievals" — observed under
    # load when intel_agent skips Message 1 (tool calls) and jumps straight to
    # Message 2 (final report), starving the writer of data. The prompt floor
    # alone is not enforced by the model reliably; the runner enforces it.
    tools_fired_count = 0
    # Track whether we've already pushed a "you MUST fire tools" re-prompt so
    # we only do it once (don't infinite-loop if the model still refuses).
    no_tools_reprompted = False
    for it in range(max_iter):
        state = replace(state, iteration=state.iteration + 1)
        _t0 = time.monotonic()
        try:
            with stage("llm_intel_agent",
                       iteration=state.iteration,
                       msg_count=len(state.intel_agent_messages)):
                resp = _chat(list(state.intel_agent_messages), INTEL_AGENT_TOOLS_SCHEMA, thinking=False)
        except Exception as e:
            return state, {"status": "http_error", "summary": str(e), "data": raw_data,
                            "sources_consulted": sources_consulted, "gaps": [],
                            "exit_reason": "http_error",
                            "timing_ms": int((time.monotonic() - _ia_t0) * 1000),
                            "iter_timings": iter_timings}
        _llm_ms = int((time.monotonic() - _t0) * 1000)
        choice = resp["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        tool_calls = msg.get("tool_calls") or []
        usage = resp.get("usage", {})
        iter_timings.append({"iter": it + 1, "llm_ms": _llm_ms,
                             "completion_tokens": usage.get("completion_tokens"),
                             "prompt_tokens": usage.get("prompt_tokens")})
        asst = {"role": "assistant", "content": content}
        if tool_calls:
            asst["tool_calls"] = tool_calls
        state = state.append_intel_agent(asst)
        if not tool_calls:
            # Hard guard: if intel_agent tries to terminate with final content
            # before ANY tool has fired, force one more round demanding tool
            # calls. The prompt's per-product retrieval floor is unenforced
            # otherwise (CCA/Threat Overlay v3 both skipped retrievals).
            if content.strip() and tools_fired_count == 0 and not no_tools_reprompted:
                no_tools_reprompted = True
                state = state.append_intel_agent({"role": "user", "content":
                    "[RUNNER REJECT] You emitted a final report with zero tool "
                    "calls in this dispatch. The downstream product writer has "
                    "NO data. Re-read your brief and the per-product retrieval "
                    "floor in your system prompt — emit a tool_calls array "
                    "containing every retrieval the brief implies, in one "
                    "message, content empty. Do not emit a Status: line until "
                    "after the tools have run."})
                continue
            if content.strip():
                status, gaps = _parse_ia_content(content)
                return state, {"status": status, "summary": content, "data": raw_data,
                                "sources_consulted": sources_consulted, "gaps": gaps,
                                "exit_reason": "content",
                                "timing_ms": int((time.monotonic() - _ia_t0) * 1000),
                                "iter_timings": iter_timings}
            state = state.append_intel_agent({"role": "user", "content": "[RUNNER] Empty output. Either call one of the data tools or emit a final content message starting with 'Status: completed|blocked|clarification_needed' to terminate."})
            continue
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except Exception:
                tool_args = {}
            _emit(on_event, {"type": "intel_agent_tool_call", "name": tool_name})
            handler = INTEL_AGENT_TOOL_DISPATCH.get(tool_name)
            if not handler:
                state = state.append_intel_agent({"role": "tool", "tool_call_id": tc["id"],
                                                   "content": json.dumps({"error": f"unknown tool {tool_name}"})})
                continue
            response = handler(tool_args, state)
            tools_fired_count += 1
            if tool_name in _IA_FETCH_TOOLS:
                ids = harvest_seen_ids(response)
                if ids:
                    state = state.add_seen(ids)
                bucket = raw_data.setdefault(tool_name, [])
                bucket.append({"args": tool_args, "response": response})
                if tool_name not in sources_consulted:
                    sources_consulted.append(tool_name)
            visible = {k: v for k, v in response.items() if not k.startswith("_")} if isinstance(response, dict) else response
            state = state.append_intel_agent({"role": "tool", "tool_call_id": tc["id"],
                                               "content": json.dumps(visible, default=str)})
    return state, {"status": "max_iterations_exceeded", "summary": "intel agent hit max iterations",
                    "data": raw_data, "sources_consulted": sources_consulted, "gaps": [],
                    "exit_reason": "max_iter",
                    "timing_ms": int((time.monotonic() - _ia_t0) * 1000),
                    "iter_timings": iter_timings}


# ============================================================================
# PRODUCT-WRITER LOOP
# ============================================================================
def run_product_writer_loop(state: State, artifact_type: str, context: str,
                             product_writer_prompt: str, max_iter=8, on_event=None):
    _pw_t0 = time.monotonic()
    state = replace(state, product_writer_messages=({"role": "system", "content": product_writer_prompt},
                                                     {"role": "user", "content": context}))
    iter_timings = []
    for it in range(max_iter):
        state = replace(state, iteration=state.iteration + 1)
        _t0 = time.monotonic()
        # Thinking on EVERY iter. The "disable thinking on iter>=1" optimization
        # broke OED v9 — writer entered emit→fail→emit loops with thinking=False,
        # unable to reason about WHY validation failed and just repeating the
        # same mistake. The 286s → 30s saving for Threat Overlay's iter-2
        # re-emit was offset by far worse failures on complex schemas. Keep
        # thinking on for all iters; reasoning is the writer's recovery
        # mechanism on FABRICATED_IDENTIFIERS / INCOMPLETE_ARTIFACT errors.
        try:
            with stage("llm_product_writer",
                       artifact_type=artifact_type,
                       iteration=state.iteration,
                       msg_count=len(state.product_writer_messages)):
                resp = _chat(list(state.product_writer_messages), PRODUCT_WRITER_TOOLS_SCHEMA, thinking=True)
        except Exception as e:
            return state, {"status": "http_error", "artifact_id": "", "summary": str(e),
                            "exit_reason": "http_error",
                            "timing_ms": int((time.monotonic() - _pw_t0) * 1000),
                            "iter_timings": iter_timings}
        _llm_ms = int((time.monotonic() - _t0) * 1000)
        choice = resp["choices"][0]
        msg = choice["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []
        usage = resp.get("usage", {})
        iter_timings.append({"iter": it + 1, "llm_ms": _llm_ms,
                             "completion_tokens": usage.get("completion_tokens"),
                             "prompt_tokens": usage.get("prompt_tokens"),
                             "reasoning_len": len(reasoning)})
        # Recover gemma4 tool-parser failures: the model intends emit_artifact /
        # submit_artifact but vLLM drops the call into content with template
        # tokens. Reconstruct and treat as a real tool_call.
        if not tool_calls and content:
            recovered = _recover_malformed_tool_call(content)
            if recovered and recovered["name"] in PRODUCT_WRITER_TOOL_DISPATCH:
                tool_calls = [{"id": f"recovered-{it}", "type": "function", "function": recovered}]
                content = ""
        asst = {"role": "assistant", "content": content}
        if reasoning:
            asst["reasoning"] = reasoning
        if tool_calls:
            asst["tool_calls"] = tool_calls
        state = state.append_product_writer(asst)
        if not tool_calls and content.strip():
            # Writer emitted prose but no tool call. This means it described
            # the artifact in narrative form instead of registering it. Refuse
            # to exit — push a hard re-prompt so the dispatch ends with a real
            # submit_artifact call. (Pre-fix: returned "completed" with empty
            # artifact_id, which the OE-layer surfaced as a no_artifact
            # failure; Threat Overlay + OED both hit this in v3/v4.)
            state = state.append_product_writer({"role": "user",
                                                  "content": f"[RUNNER REJECT] You emitted prose with no tool call. The product writer's ONLY exit is `submit_artifact()`. Call `emit_artifact(artifact_type='{artifact_type}', structured_data={{...your analytical layer...}})` now, then `submit_artifact()` once the preview is clean. Do not produce additional narrative."})
            continue
        if not tool_calls:
            state = state.append_product_writer({"role": "user",
                                                  "content": f"[RUNNER] Empty output. You must call emit_artifact(artifact_type='{artifact_type}', structured_data={{...analytical layer...}}). The runner has already pre-filled the skeleton; emit only your analytical content (rows_analysis / header_analysis / paragraph fields). Do not return without calling emit_artifact."})
            continue
        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except Exception:
                tool_args = {}
            handler = PRODUCT_WRITER_TOOL_DISPATCH.get(tool_name)
            if not handler:
                state = state.append_product_writer({"role": "tool", "tool_call_id": tc["id"],
                                                      "content": json.dumps({"error": f"unknown tool {tool_name}"})})
                continue
            response = handler(tool_args, state)
            # Cache the validated emit_artifact preview's structured_data so
            # the writer can submit_artifact() with no args to register it.
            if isinstance(response, dict) and response.get("_cache_emit"):
                state = replace(state, product_writer_last_emit_json=response["_cache_emit"])
            if isinstance(response, dict) and response.get("_submit_checkpoint"):
                checkpoint = response["_submit_checkpoint"]
                visible = {k: v for k, v in response.items() if not k.startswith("_")}
                state = state.append_product_writer({"role": "tool", "tool_call_id": tc["id"],
                                                      "content": json.dumps(visible, default=str)})
                build_log = state.product_writer_messages
                summary = summarize_build(list(build_log), checkpoint["artifact_id"], checkpoint["artifact_type"])
                cp_gaps = checkpoint.get("gaps", []) if isinstance(checkpoint, dict) else []
                entry = {"artifact_id": checkpoint["artifact_id"], "source": "pytho",
                         "dtg": checkpoint["dtg"], "structured_data": checkpoint["structured_data"],
                         "summary": summary, "gaps": cp_gaps}
                state = state.with_artifact(checkpoint["artifact_type"], entry)
                state = state.add_seen({checkpoint["artifact_id"]})
                # submit_artifact is the commit step — terminate the dispatch
                # immediately. Any subsequent submit/emit calls would have
                # nowhere to land.
                return state, {"status": "completed",
                                "artifact_id": checkpoint["artifact_id"],
                                "summary": summary,
                                "exit_reason": "submit_artifact",
                                "timing_ms": int((time.monotonic() - _pw_t0) * 1000),
                                "iter_timings": iter_timings}
            visible = {k: v for k, v in response.items() if not k.startswith("_")} if isinstance(response, dict) else response
            state = state.append_product_writer({"role": "tool", "tool_call_id": tc["id"],
                                                  "content": json.dumps(visible, default=str)})
    return state, {"status": "max_iterations_exceeded", "artifact_id": "",
                    "summary": "product writer hit max iterations",
                    "exit_reason": "max_iter",
                    "timing_ms": int((time.monotonic() - _pw_t0) * 1000),
                    "iter_timings": iter_timings}


# ============================================================================
# OPERATIONS-EXPERT OUTER LOOP
# ============================================================================
def _emit(on_event, event):
    """Best-effort callback invocation. The callback is supplied by the WS
    handler in main.py and pushes a JSON frame down the websocket. We never
    let a callback failure abort the turn — the analyst still gets the
    terminal reply even if streaming breaks mid-flight."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:
        log.debug("_emit on_event callback raised", exc_info=True)


def _emit_seg(on_segment, segment_type: str, payload: dict):
    """Best-effort segment-write invocation. Mirrors `_emit` but writes a
    persistent chat-history row via the closure built in `run_turn`. Every
    OE-loop emit point — thinking, tool_call, tool_result — also pings
    this so the segment lands in SessionDetail and is recoverable via
    /api/sessions/{id}/messages even if the WS drops mid-turn.

    `on_segment(segment_type, payload)` is the closure. None means the
    caller didn't wire persistence (e.g. test harness); we no-op silently.
    """
    if on_segment is None:
        return
    try:
        on_segment(segment_type, payload)
    except Exception:
        log.debug("_emit_seg on_segment callback raised", exc_info=True)


def _truncate(text, n=200):
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def run_operations_expert_loop(state: State, max_iter=20, trace=None,
                                 tools_schema=None, on_event=None,
                                 on_segment=None):
    """Run one turn of the operations-expert loop. `trace` is an in-out dict that
    accumulates this turn's intel-agent and product-writer dispatches for the
    caller to persist. `tools_schema` defaults to `OPERATIONS_EXPERT_TOOLS_SCHEMA`;
    pass `OPERATIONS_EXPERT_MANUAL_TOOLS_SCHEMA` for the Generate-button manual
    dispatch path (which has no `set_product_type` tool).

    `on_event(dict)` is an optional callback for incremental streaming. The
    runner invokes it at: (a) each OE iteration when `reasoning` is set,
    (b) before each OE tool dispatch, (c) after each tool returns, plus
    intel-agent / product-writer milestones forwarded from their inner
    loops. Callback errors are swallowed — the terminal reply path is the
    authoritative source of truth."""
    if tools_schema is None:
        tools_schema = OPERATIONS_EXPERT_TOOLS_SCHEMA
    if trace is None:
        trace = {"intel_agent_dispatches": [], "product_writer_dispatches": []}
    turn_start_ts = time.time()
    for _ in range(max_iter):
        state = replace(state, iteration=state.iteration + 1)
        llm_t0 = time.time()
        try:
            with stage("llm_operations_expert",
                       iteration=state.iteration,
                       msg_count=len(state.operations_expert_messages)):
                resp = _chat(list(state.operations_expert_messages), tools_schema, thinking=True)
        except Exception as e:
            return state, f"[operations expert HTTP ERR] {e}"
        llm_dur_ms = int((time.time() - llm_t0) * 1000)
        msg = resp["choices"][0]["message"]
        content = msg.get("content") or ""
        reasoning = msg.get("reasoning") or ""
        tool_calls = msg.get("tool_calls") or []
        asst = {"role": "assistant", "content": content}
        if reasoning:
            asst["reasoning"] = reasoning
        if tool_calls:
            asst["tool_calls"] = tool_calls
        state = state.append_operations_expert(asst)

        if reasoning:
            _emit(on_event, {"type": "operations_expert_thinking", "text": reasoning, "dur_ms": llm_dur_ms})
            _emit_seg(on_segment, "oe_thinking",
                       {"content": reasoning, "dur_ms": llm_dur_ms})
        else:
            # Even without reasoning, surface the LLM round-trip cost as an
            # `operations_expert_llm` event so the analyst sees how long OE's decision step
            # took. Without this the UI would only see costs at tool-result
            # time and the "thinking…" gap before a dispatch is invisible.
            _emit(on_event, {"type": "operations_expert_llm", "dur_ms": llm_dur_ms})

        if not tool_calls:
            if content.strip():
                return state, content
            state = state.append_operations_expert({"role": "user",
                                                     "content": "[RUNNER] Empty output. Either emit content as your final analyst-facing message or call one of: dispatch_intel_agent, dispatch_product_writer."})
            continue

        for tc in tool_calls:
            tool_name = tc["function"]["name"]
            try:
                tool_args = json.loads(tc["function"]["arguments"])
            except Exception:
                tool_args = {}
            _emit(on_event, {"type": "operations_expert_tool_call", "name": tool_name, "args": tool_args})
            _emit_seg(on_segment, "tool_call",
                       {"tool_name": tool_name, "tool_args": tool_args,
                        "tool_call_id": tc.get("id", "")})
            handler = OPERATIONS_EXPERT_TOOL_DISPATCH.get(tool_name)
            if not handler:
                state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                         "content": json.dumps({"error": f"unknown tool {tool_name}"})})
                _emit(on_event, {"type": "operations_expert_tool_result", "name": tool_name,
                                 "summary": f"unknown tool {tool_name}", "dur_ms": 0})
                _emit_seg(on_segment, "tool_result",
                           {"tool_name": tool_name, "tool_call_id": tc.get("id", ""),
                            "content": f"unknown tool {tool_name}", "dur_ms": 0,
                            "ok": False})
                continue
            tool_t0 = time.time()
            # The OE loop runs in a worker thread with no current event
            # loop, so it's safe to `asyncio.run` any coroutine the
            # handler returns — this is NOT the sync-over-async
            # anti-pattern: we ARE the sync caller and there's no parent
            # loop being blocked. Detect coroutine handlers (cougaar
            # tools are now async-native — get_oob / list_targets /
            # get_target / get_recent_intel — and generate_product is
            # async; the rest are sync) and asyncio.run uniformly.
            if tool_name == "generate_product":
                # Thread the OE's on_event through so DAG node events stream
                # back through the same chokepoint the WS dispatcher set up.
                response = asyncio.run(handler(tool_args, state, on_event=on_event))
            elif asyncio.iscoroutinefunction(handler):
                response = asyncio.run(handler(tool_args, state))
            else:
                response = handler(tool_args, state)
            tool_dur_ms = int((time.time() - tool_t0) * 1000)

            if isinstance(response, dict) and response.get("_set_product_type"):
                product_type = response["product_type"]
                schema_brief = response["schema_brief"]
                state = replace(state, active_product_type=product_type)
                state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                         "content": json.dumps({
                                                             "product_type": product_type,
                                                             "schema_brief": schema_brief,
                                                             "active_product_type_set": True,
                                                         }, default=str)})
                _emit(on_event, {"type": "operations_expert_tool_result", "name": "set_product_type",
                                 "summary": f"active_product_type = {product_type}",
                                 "dur_ms": tool_dur_ms})
                _emit_seg(on_segment, "tool_result",
                           {"tool_name": "set_product_type", "tool_call_id": tc.get("id", ""),
                            "content": f"active_product_type = {product_type}",
                            "product_type": product_type, "dur_ms": tool_dur_ms,
                            "ok": True})
                continue

            if isinstance(response, dict) and response.get("_dispatch_intel_agent"):
                instructions = response.get("instructions", "") or response.get("brief", "")
                product_type = response.get("product_type")
                template = response.get("template", "") or ""
                if template and instructions:
                    brief = (f"=== DOCTRINAL DATA SOURCES TEMPLATE ({product_type}) ===\n{template}\n\n"
                             f"=== ANALYST INSTRUCTIONS (override/supplement template) ===\n{instructions}")
                elif template:
                    brief = f"=== DOCTRINAL DATA SOURCES TEMPLATE ({product_type}) ===\n{template}"
                else:
                    brief = instructions
                _emit(on_event, {"type": "intel_agent_dispatch_start", "brief": _truncate(instructions or brief, 400)})
                intel_agent_t0 = time.time()
                with stage("dispatch_intel_agent", product_type=product_type):
                    state, intel_agent_result = run_intel_agent_loop(state, brief, on_event=on_event)
                intel_agent_dur_ms = int((time.time() - intel_agent_t0) * 1000)
                trace["intel_agent_dispatches"].append({
                    "brief": brief, "messages": list(state.intel_agent_messages),
                    "summary": intel_agent_result.get("summary", ""),
                    "sources_consulted": intel_agent_result.get("sources_consulted", []),
                    "gaps": intel_agent_result.get("gaps", []),
                    "status": intel_agent_result.get("status", ""),
                    "timing_ms": intel_agent_result.get("timing_ms"),
                    "iter_timings": intel_agent_result.get("iter_timings", []),
                })
                intel_agent_tool_result = {"intel_agent_status": intel_agent_result["status"],
                                   "summary": intel_agent_result.get("summary", ""),
                                   "sources_consulted": intel_agent_result.get("sources_consulted", []),
                                   "gaps": intel_agent_result.get("gaps", [])}
                state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                         "content": json.dumps(intel_agent_tool_result, default=str)})
                _emit(on_event, {"type": "operations_expert_tool_result", "name": "dispatch_intel_agent",
                                 "summary": f"{intel_agent_result.get('status', '?')} — sources: "
                                            f"{', '.join(intel_agent_result.get('sources_consulted', []) or []) or 'none'}",
                                 "dur_ms": intel_agent_dur_ms})
                _emit_seg(on_segment, "tool_result",
                           {"tool_name": "dispatch_intel_agent", "tool_call_id": tc.get("id", ""),
                            "content": intel_agent_result.get("summary", "") or "",
                            "status": intel_agent_result.get("status", ""),
                            "sources_consulted": intel_agent_result.get("sources_consulted", []) or [],
                            "gaps": intel_agent_result.get("gaps", []) or [],
                            "dur_ms": intel_agent_dur_ms,
                            "ok": intel_agent_result.get("status") not in ("error", "failed")})
                continue

            if isinstance(response, dict) and response.get("_dispatch_product_writer"):
                artifact_type = response["artifact_type"]
                context = response["context"]
                product_writer_prompt = response["product_writer_prompt"]
                intel_agent_data = _extract_ia_data(state)
                row_ids = _derive_row_ids(artifact_type, intel_agent_data, context)
                skeleton = _build_skeleton(artifact_type, intel_agent_data, row_ids, state=state)
                # Reset both per-dispatch caches before starting the PW loop.
                state = replace(state, product_writer_skeleton_json=json.dumps(skeleton, default=str), product_writer_last_emit_json="")
                skeleton_rendered = render_skeleton_for_pw(artifact_type, skeleton)
                intel_agent_data_rendered = render_ia_data_for_pw(intel_agent_data)
                product_writer_user_message = (skeleton_rendered
                                    + "\n\n=== OPERATIONS-EXPERT CONTEXT ===\n" + context
                                    + ("\n\n" + intel_agent_data_rendered if intel_agent_data_rendered else ""))
                _emit(on_event, {"type": "product_writer_dispatch_start", "artifact_type": artifact_type})
                with stage("dispatch_product_writer", artifact_type=artifact_type):
                    state, product_writer_result = run_product_writer_loop(state, artifact_type, product_writer_user_message, product_writer_prompt, on_event=on_event)
                state = replace(state, product_writer_skeleton_json="", product_writer_last_emit_json="")
                actual_artifact_id = ""
                reg = state.registry_dict()
                if artifact_type in reg and reg[artifact_type]:
                    actual_artifact_id = reg[artifact_type][-1].get("artifact_id", "")
                emit_record = None
                if actual_artifact_id:
                    last = reg[artifact_type][-1]
                    emit_record = {
                        "artifact_id": actual_artifact_id,
                        "structured_data": last.get("structured_data"),
                        "dtg": last.get("dtg"),
                        "summary": last.get("summary", ""),
                        "gaps": last.get("gaps", []),
                    }
                    # Emit a structural artifact_emitted event; htmlLink/docxLink/
                    # pptxLink are added by the WS handler in main.py once the
                    # exporter has rendered the files. The frontend already
                    # handles the rendered links via the terminal-reply path
                    # (intel_agent.artifacts) — this event is a "writer
                    # finished" signal so the chat panel can show progress
                    # before the render step completes.
                    _emit(on_event, {"type": "artifact_emitted",
                                     "artifact_id": actual_artifact_id,
                                     "type_": artifact_type,
                                     "htmlLink": None, "docxLink": None, "pptxLink": None})
                trace["product_writer_dispatches"].append({
                    "artifact_type": artifact_type, "context": context,
                    "skeleton": skeleton, "row_ids": row_ids,
                    "messages": list(state.product_writer_messages),
                    "emit": emit_record,
                    "status": product_writer_result.get("status", ""),
                    "exit_reason": product_writer_result.get("exit_reason"),
                    "timing_ms": product_writer_result.get("timing_ms"),
                    "iter_timings": product_writer_result.get("iter_timings", []),
                })
                if emit_record:
                    product_writer_tool_result = {"product_writer_status": "emitted",
                                       "artifact_id": actual_artifact_id,
                                       "summary": product_writer_result.get("summary", "") or f"Artifact {actual_artifact_id} registered.",
                                       "gaps": emit_record["gaps"]}
                    _emit(on_event, {"type": "operations_expert_tool_result", "name": "dispatch_product_writer",
                                     "summary": f"emitted {actual_artifact_id} ({artifact_type})"})
                    _emit_seg(on_segment, "tool_result",
                               {"tool_name": "dispatch_product_writer", "tool_call_id": tc.get("id", ""),
                                "content": f"emitted {actual_artifact_id} ({artifact_type})",
                                "artifact_id": actual_artifact_id, "artifact_type": artifact_type,
                                "gaps": emit_record.get("gaps") or [],
                                "ok": True})
                else:
                    product_writer_tool_result = {"product_writer_status": "emit_failed",
                                       "artifact_id": "",
                                       "summary": product_writer_result.get("summary", ""),
                                       "exit_reason": product_writer_result.get("exit_reason", ""),
                                       "error": "NO_ARTIFACT_REGISTERED. The product writer did not call emit_artifact. The artifact does NOT exist in the registry. Do NOT report success to the analyst. Either re-dispatch with corrections or tell the analyst the generation failed and why."}
                    _emit(on_event, {"type": "operations_expert_tool_result", "name": "dispatch_product_writer",
                                     "summary": f"emit_failed ({product_writer_result.get('exit_reason', '?')})"})
                    _emit_seg(on_segment, "tool_result",
                               {"tool_name": "dispatch_product_writer", "tool_call_id": tc.get("id", ""),
                                "content": f"emit_failed ({product_writer_result.get('exit_reason', '?')})",
                                "artifact_type": artifact_type,
                                "exit_reason": product_writer_result.get("exit_reason", ""),
                                "ok": False})
                state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                         "content": json.dumps(product_writer_tool_result, default=str)})
                continue

            if isinstance(response, dict) and response.get("_publish_artifact"):
                # Flip header.status from "draft" to "published" in two places:
                #   1. state.artifact_registry — current in-memory state so
                #      subsequent _project_docs_index calls in THIS turn see the
                #      flip.
                #   2. trace["product_writer_dispatches"][...].emit — the trace
                #      dict is what save_turn persists into the SessionDetail
                #      row, and load_state rebuilds artifact_registry from
                #      these emits next turn. Patching it here keeps the flip
                #      durable across session reloads.
                # For artifacts emitted in PRIOR turns (whose dispatch entries
                # live in EARLIER SessionDetail rows, not in this turn's
                # trace), the publish endpoint in main.py handles the DB
                # rewrite via publish_artifact_in_db. The in-turn case below
                # covers the common path where an analyst publishes the
                # artifact they just generated.
                artifact_id = response["artifact_id"]
                state, found_atype = state.publish_artifact(artifact_id)
                for disp in trace.get("product_writer_dispatches") or []:
                    emit = disp.get("emit") or {}
                    if str(emit.get("artifact_id")) != artifact_id:
                        continue
                    sd = emit.get("structured_data") or {}
                    header = sd.get("header") or {}
                    header["status"] = "published"
                    sd["header"] = header
                    emit["structured_data"] = sd
                    disp["emit"] = emit
                publish_tool_result = {
                    "publish_status": "ok",
                    "artifact_id": artifact_id,
                    "artifact_type": found_atype or response.get("artifact_type", ""),
                    "status": "published",
                    "note": "Artifact is now visible to every analyst on the project.",
                }
                state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                         "content": json.dumps(publish_tool_result, default=str)})
                _emit(on_event, {"type": "operations_expert_tool_result", "name": "publish_artifact",
                                 "summary": f"published {artifact_id}"})
                _emit_seg(on_segment, "tool_result",
                           {"tool_name": "publish_artifact", "tool_call_id": tc.get("id", ""),
                            "content": f"published {artifact_id}",
                            "artifact_id": artifact_id,
                            "artifact_type": found_atype or response.get("artifact_type", ""),
                            "ok": True})
                continue

            visible = {k: v for k, v in response.items() if not k.startswith("_")} if isinstance(response, dict) else response
            state = state.append_operations_expert({"role": "tool", "tool_call_id": tc["id"],
                                                     "content": json.dumps(visible, default=str)})
            _emit(on_event, {"type": "operations_expert_tool_result", "name": tool_name,
                             "summary": _truncate(json.dumps(visible, default=str), 160)})
            _emit_seg(on_segment, "tool_result",
                       {"tool_name": tool_name, "tool_call_id": tc.get("id", ""),
                        "content": _truncate(json.dumps(visible, default=str), 800),
                        "ok": True})

    return state, ""


# ============================================================================
# IN-PROCESS DRIVER (for the testing harness)
# ============================================================================
def run_turn(question: str, *, session_id: str, project_id: str, user_id: str,
              db, title: Optional[str] = None,
              manual_product_type: str = "",
              on_event=None) -> dict:
    """One analyst message → one turn. Loads state from DB, runs operations-expert
    loop, persists the new SessionDetail row, returns the analyst-facing answer.

    `manual_product_type` switches the operations expert into manual-dispatch
    mode (no `set_product_type` tool, no scope-confirmation step, product type
    pre-set in state). Used by the Generate-button path
    (handlers/product.py:generate). Empty string = normal chat mode.

    `on_event(dict)` is an optional callback for incremental event streaming;
    forwarded into the OE loop. The harness path leaves it None — only the
    WebSocket entry point in `main` wires it to send frames over the
    socket."""
    timestamp = math.floor(time.time() * 1000)
    # Zero out the per-thread instrumentation registers BEFORE any code touches
    # them — the executor reuses worker threads across turns, so we must not
    # inherit the prior turn's stage_events or cougaar_call_summary. Also
    # binds on_event so the `stage(...)` context manager can push frames over
    # the WebSocket without being threaded through every function signature.
    _reset_turn_local(session_id or "", on_event)

    from utils.project_context import set_active_project
    set_active_project(project_id)
    state = load_state(session_id, db, user_id=user_id, project_id=project_id,
                       manual_product_type=manual_product_type)
    state = state.append_operations_expert({"role": "user", "content": question})
    baseline = len(state.operations_expert_messages) - 1  # index of the user msg we just added

    # Persist the analyst message IMMEDIATELY so chat history survives even
    # if the WS drops mid-turn. The full turn audit row is still written
    # by save_turn below; this is the per-segment chat-history record.
    user_segment_id: Optional[int] = None
    try:
        user_segment_id = append_segment(
            db, session_id=session_id, project_id=project_id,
            user_id=user_id, segment_type="user_msg",
            payload={"content": question, "title": (title or question)[:120]},
        )
    except Exception as e:
        log.warning("append_segment(user_msg) failed: %s", e, exc_info=True)

    trace = {"intel_agent_dispatches": [], "product_writer_dispatches": []}

    # Closure that writes per-segment chat-history rows. Captures the db/
    # session/project/user identity so the OE loop doesn't need to thread
    # those through every emit site. Mirrors how `on_event` is captured —
    # one closure per turn, called many times. Each call is a single
    # SessionDetail row with `type: "message_segment"` + `_segment_type`
    # discriminator, surfaced through /api/sessions/{id}/messages.
    def _segment_writer(segment_type: str, payload: dict) -> None:
        try:
            append_segment(
                db, session_id=session_id, project_id=project_id,
                user_id=user_id, segment_type=segment_type, payload=payload,
            )
        except Exception as e:
            log.debug("on_segment(%s) failed: %s", segment_type, e, exc_info=True)

    tools_schema = (OPERATIONS_EXPERT_MANUAL_TOOLS_SCHEMA
                    if manual_product_type
                    else OPERATIONS_EXPERT_TOOLS_SCHEMA)
    _t0 = time.monotonic()
    with stage("operations_expert_loop", manual_product_type=manual_product_type or None):
        state, answer = run_operations_expert_loop(state, trace=trace,
                                                     tools_schema=tools_schema,
                                                     on_event=on_event,
                                                     on_segment=_segment_writer)
    turn_total_ms = int((time.monotonic() - _t0) * 1000)

    # Persist the assistant's final answer + the per-tool artifact references
    # this turn produced, so the chat list shows the OE's reply and any
    # generated artifacts even if the WS dropped before the terminal frame
    # made it to the client.
    artifacts_this_turn_for_segment = [
        {"artifact_id": d["emit"]["artifact_id"], "artifact_type": d["artifact_type"]}
        for d in trace.get("product_writer_dispatches") or [] if d.get("emit")
    ]
    assistant_segment_id: Optional[int] = None
    try:
        seg_payload = {"content": answer or ""}
        if artifacts_this_turn_for_segment:
            seg_payload["artifacts"] = artifacts_this_turn_for_segment
        assistant_segment_id = append_segment(
            db, session_id=session_id, project_id=project_id,
            user_id=user_id, segment_type="assistant_msg",
            payload=seg_payload,
        )
    except Exception as e:
        log.warning("append_segment(assistant_msg) failed: %s", e, exc_info=True)

    with stage("save_turn", project_id=project_id):
        save_turn(
            db, session_id=session_id, project_id=project_id, user_id=user_id,
            timestamp=timestamp, question=question, answer=answer or "",
            title=(title or question)[:120],
            state_after=state, state_before_oe_msg_count=baseline,
            intel_agent_dispatches=trace["intel_agent_dispatches"],
            product_writer_dispatches=trace["product_writer_dispatches"],
            iter_timings=[], turn_total_ms=turn_total_ms,
        )

    artifacts_this_turn = [
        {"artifact_id": d["emit"]["artifact_id"], "type": d["artifact_type"],
         "gaps": d["emit"].get("gaps", [])}
        for d in trace["product_writer_dispatches"] if d.get("emit")
    ]
    # Capture stage_events + cougaar_call_summary from the worker thread's
    # thread-local before run_turn returns. The WS handler in main() runs on
    # the asyncio event-loop thread; pulling the snapshot here keeps it
    # bound to the executor thread that produced it.
    captured_stage_events = stage_events_snapshot()
    captured_cougaar_summary = cougaar_call_summary_snapshot()
    return {
        "session_id": session_id,
        "answer": answer or "",
        "active_product_type": state.active_product_type,
        "artifacts": artifacts_this_turn,
        "turn_total_ms": turn_total_ms,
        "stage_events": captured_stage_events,
        "cougaar_call_summary": captured_cougaar_summary,
        # Chat-history segment ids (ms-precision SessionDetail PKs) written
        # by append_segment for the user message and the assistant answer.
        # Carried out so the WS handler can stamp them into turn_summary —
        # the frontend uses them to advance `lastMsgIdRef` during normal
        # session operation, so the next reconnect's gap-fill resumes from
        # the correct point.
        "user_segment_id": user_segment_id,
        "assistant_segment_id": assistant_segment_id,
    }


def _augment_schema_states(rendered_artifacts: list, stage_events: list,
                            cougaar_call_summary: dict) -> None:
    """Re-write schema_state.json beside each rendered artifact to include
    the FULL turn-wide stage_events list (including render/export stages
    that happened AFTER the initial schema_state dump in
    render_artifact_files) and the final cougaar_call_summary.

    render_artifact_files writes the first version of schema_state.json with
    the data it could see at write-time. By the time we're back in the WS
    handler we have the full set of stages — augment the file in place. If
    the file is missing or unparseable, log and move on; this is a debug
    aid and must never block the artifact reply.
    """
    from handlers.product import UPLOAD_DIR
    for entry in rendered_artifacts or []:
        artifact_id = entry.get("artifact_id")
        html_link = entry.get("htmlLink") or ""
        if not artifact_id:
            continue
        # htmlLink shape: ${FILE_HOST_API}<owner>/<artifact_id>/artifact.html
        # Extract owner from the link path; if absent (render failed), scan
        # uploads/ for the matching artifact_id.
        owner = ""
        if html_link:
            try:
                tail = html_link.split("/files/", 1)[-1]
                owner = tail.split("/")[0] if "/" in tail else ""
            except Exception:
                owner = ""
        candidate_dirs = []
        if owner:
            candidate_dirs.append(UPLOAD_DIR / owner / artifact_id)
        else:
            try:
                for owner_dir in UPLOAD_DIR.iterdir():
                    candidate = owner_dir / artifact_id
                    if candidate.exists():
                        candidate_dirs.append(candidate)
                        break
            except Exception:
                log.debug("upload-dir probe for artifact_id=%s failed", artifact_id, exc_info=True)
        for artifact_dir in candidate_dirs:
            schema_state_path = artifact_dir / "schema_state.json"
            if not schema_state_path.exists():
                continue
            try:
                existing = json.loads(schema_state_path.read_text(encoding="utf-8"))
            except Exception:
                existing = {}
            existing["stage_events"] = stage_events
            existing["cougaar_call_summary"] = cougaar_call_summary
            try:
                schema_state_path.write_text(
                    json.dumps(existing, default=str, indent=2),
                    encoding="utf-8",
                )
            except Exception as e:
                log.warning("schema_state augment write failed for %s: %s", artifact_id, e)


# ============================================================================
# WEBSOCKET ENTRY POINT — mirrors handlers/agents.py:main shape
# ============================================================================
async def main(data, manager, websocket):
    """One analyst message arrives over the WebSocket. Dispatch a turn against
    the same `run_turn` function the testing harness uses, and stream the
    operations expert's reply back.

    Generate-button UX (Req: ProductLibraryModal navigates straight to
    DocEditor and opens this socket): if `run_turn` reports artifacts, we
    render each artifact to HTML + DOCX/PPTX on disk and attach the public
    /files/ links to the payload before sending. The DocChatPanel front-end
    forwards the first artifact's links to DocEditor's `onChatArtifact`
    handler, which fetches the htmlLink and flips out of the pending
    placeholder. Render failure does NOT block the answer — we still send
    OE's text, with `htmlLink` left null and a `render_error` field set.

    Incremental event streaming: the OE/IA/PW loops invoke an `on_event`
    callback at thinking/tool-call/dispatch milestones. We marshal those
    events from the worker thread (run_turn runs in an executor) back into
    the event loop with `run_coroutine_threadsafe` so they hit the websocket
    in real time, well before the terminal reply. The terminal reply still
    fires at the end with the full `answer` + `intel_agent.artifacts` —
    legacy clients that only read that frame keep working unchanged.
    """
    import asyncio as _asyncio
    session_id = data.get("sessionId")
    project_id = ("default-" + data.get("userId", "")
                  if data.get("projectId") == "default" else data.get("projectId", ""))
    user_id = data.get("userId", "")
    question = (data.get("question") or {}).get("prompt", "") or ""
    # Manual-dispatch signal from the Generate-button flow (DocChatPanel sends
    # this on its FIRST frame when the analyst clicked Generate from
    # ProductLibraryModal). Switches the OE prompt to operations_expert_manual.md
    # and uses the OPERATIONS_EXPERT_MANUAL_TOOLS_SCHEMA (no set_product_type,
    # no scope-confirmation step). Empty string = normal chat mode.
    manual_product_type = (data.get("manual_product_type")
                            or (data.get("question") or {}).get("manual_product_type")
                            or "")

    loop = _asyncio.get_event_loop()

    # Register the job so /api/jobs/{id} (chat-resilience refresh hook)
    # can surface running/finished/failed state regardless of whether the
    # WS stays connected. The job_id is deterministic off (session_id,
    # start_ms) so two simultaneous turns on the same session don't collide.
    job_id = f"{session_id}:{int(time.time() * 1000)}"
    job = _job_register(
        job_id,
        session_id=session_id or "",
        user_id=user_id or "",
        manual_product_type=manual_product_type or "",
        question=question or "",
    )

    def _push_event(event):
        # Update the job registry whether or not the WS is alive.
        try:
            etype = event.get("type", "") if isinstance(event, dict) else ""
            _job_update(
                job_id,
                phase=etype or job.get("phase", ""),
                events_count=(job.get("events_count", 0) or 0) + 1,
                last_event_type=etype,
                last_event_at_ms=int(time.time() * 1000),
                **({"artifact_id": event.get("artifact_id")}
                   if isinstance(event, dict) and event.get("type") == "artifact_emitted" else {}),
            )
        except Exception:
            log.debug("_job_update failed during ws_event_callback", exc_info=True)
        try:
            payload = json.dumps(event, default=str)
        except Exception:
            log.warning("ws_event_callback: event serialize failed type=%s",
                        type(event).__name__, exc_info=True)
            return
        # Schedule the send on the event loop from this worker thread.
        # WS may be closed (analyst backgrounded the tab / navigated away) —
        # swallow send failures so the turn keeps running. The terminal-reply
        # path and the job registry remain authoritative sources of truth.
        try:
            _asyncio.run_coroutine_threadsafe(
                manager.send_personal_message(payload, websocket), loop
            )
        except Exception:
            log.debug("ws_event_callback: send_personal_message dispatch failed", exc_info=True)

    # Guard: empty question with no manual_product_type means the analyst
    # sent an empty frame (or the frontend's reconnect logic re-fired the
    # last frame with no body). Firing run_turn anyway burns a 15-30s LLM
    # call that produces a hallucinated "Standing by" response, and the
    # frontend's WS reconnect then re-sends the empty frame, forming a
    # thinking loop. Reject server-side without invoking the LLM.
    if not (question or "").strip() and not (manual_product_type or "").strip():
        try:
            await manager.send_personal_message(json.dumps({
                "type": "intel_agent_terminal",
                "answer": ("Send a request, question, or product to generate. "
                            "I act on your input — I do not orchestrate on an empty turn."),
                "artifacts": [],
                "session_id": session_id or "",
                "skipped_empty_input": True,
            }), websocket)
        except Exception:
            log.debug("skipped_empty_input notice send failed", exc_info=True)
        _job_update(job_id, phase="skipped_empty_input")
        return

    db = database.SessionLocal()
    result = None
    rendered_artifacts: list = []

    def _run_turn_and_render():
        """Run the turn + render every artifact on the SAME executor thread.

        Two reasons this lives in one closure on one thread:
          1. The `stage(...)` instrumentation uses thread-local state, so
             render-side stage_events have to record on the same worker
             thread that recorded the LLM/tool events. Splitting `run_turn`
             from the render loop across two threads would orphan the
             render stages.
          2. Each WS turn already gets its own executor worker thread, so
             concurrent turns stay isolated — locking is unnecessary, and
             each turn's stage_events/cougaar_call_summary are private to
             its thread.

        Returns the run_turn result with `stage_events` updated to include
        the render+export stages this closure adds.
        """
        from handlers import product as product_handler
        turn_result = run_turn(
            question, session_id=session_id, project_id=project_id, user_id=user_id, db=db,
            title=data.get("title") or question,
            manual_product_type=manual_product_type,
            on_event=_push_event,
        )
        # run_turn returned, but the thread-local stage_events list is STILL
        # accumulating in this same thread — extend it with the render-side
        # stages so the turn_summary covers trigger → artifact-persist.
        local_rendered: list = []
        for a in turn_result.get("artifacts", []) or []:
            entry = {
                "artifact_id": a.get("artifact_id"),
                "type": a.get("type"),
                "gaps": a.get("gaps", []),
                "htmlLink": None,
                "docxLink": None,
                "pptxLink": None,
            }
            try:
                with stage("render_artifact_files",
                           artifact_id=a.get("artifact_id"),
                           artifact_type=a.get("type")):
                    rendered = product_handler.render_artifact_files(
                        session_id=session_id,
                        artifact_id=a.get("artifact_id"),
                        artifact_type=a.get("type"),
                        gaps=a.get("gaps") or [],
                        db=db,
                    )
                if rendered.get("status") == "ok":
                    entry["htmlLink"] = rendered.get("htmlLink")
                    entry["docxLink"] = rendered.get("docxLink")
                    entry["pptxLink"] = rendered.get("pptxLink")
                else:
                    entry["render_error"] = rendered.get("error", "render failed")
            except Exception as e:
                entry["render_error"] = f"render exception: {e}"
            local_rendered.append(entry)
        # Refresh the snapshots to include the render stages we just added,
        # then RE-WRITE schema_state.json beside each artifact so the
        # post-render stages (export, persist) make it into the dump.
        final_stage_events = stage_events_snapshot()
        final_cougaar_summary = cougaar_call_summary_snapshot()
        try:
            _augment_schema_states(local_rendered, final_stage_events,
                                    final_cougaar_summary)
        except Exception as aug_err:
            log.warning("schema_state augment failed: %s", aug_err)
        turn_result["stage_events"] = final_stage_events
        turn_result["cougaar_call_summary"] = final_cougaar_summary
        turn_result["_rendered_artifacts"] = local_rendered
        return turn_result

    # Surface a "data integration in progress" status to the chat sidebar
    # before the LLM dispatch kicks off — buys visible feedback during the
    # 1-3 s before the first operations-expert event arrives, and explains
    # to the analyst that tool_render_map is going to pull live state from
    # Cougaar before paint. The frontend (ChatAgent) renders this with a
    # pulsing-dot status row; subsequent events scroll it up naturally.
    _push_event({
        "type": "data_integration_start",
        "message": "Integrating live data from Cougaar…",
    })

    try:
        result = await loop.run_in_executor(None, _run_turn_and_render)
        rendered_artifacts = result.get("_rendered_artifacts") or []
    except Exception as run_err:
        # The turn raised before producing any result. Record the error in
        # the job registry so /api/jobs/{id} surfaces it; the WS send below
        # is best-effort.
        _job_update(job_id, status="failed", error=f"{type(run_err).__name__}: {run_err}")
        try:
            await manager.send_personal_message(
                json.dumps({"error": f"{type(run_err).__name__}: {run_err}",
                            "job_id": job_id}, default=str),
                websocket,
            )
        except Exception:
            log.warning("could not notify client of run failure for job=%s", job_id, exc_info=True)
        raise
    finally:
        db.close()

    # Job finished cleanly (result was produced). Stamp registry — artifact_id
    # may already be set via the artifact_emitted event but this is the
    # canonical "done" signal.
    last_aid = None
    if rendered_artifacts:
        last_aid = rendered_artifacts[-1].get("artifact_id")
    _job_update(job_id, status="completed",
                artifact_id=last_aid or _JOBS.get(job_id, {}).get("artifact_id"))

    # Emit a final turn_summary frame BEFORE the terminal reply so the
    # frontend latency UI has the complete breakdown to render. Carries the
    # ordered stage_events list, per-stage durations, and the artifact_ids
    # the turn produced. Idempotent + cheap; fire-and-forget.
    final_stage_events = result.get("stage_events") or []
    per_stage_total_ms = 0
    stage_breakdown: list[dict] = []
    for ev in final_stage_events:
        dur = ev.get("duration_ms") or 0
        per_stage_total_ms += dur
        stage_breakdown.append({"stage": ev.get("stage"), "duration_ms": dur})
    turn_summary = {
        "type": "turn_summary",
        "session_id": session_id or "",
        "job_id": job_id,
        "completed_at": _now_iso(),
        "turn_total_ms": result.get("turn_total_ms"),
        "per_stage_total_ms": per_stage_total_ms,
        "stage_breakdown": stage_breakdown,
        "stage_events": final_stage_events,
        "cougaar_call_summary": result.get("cougaar_call_summary") or {},
        "artifact_ids": [a.get("artifact_id") for a in rendered_artifacts if a.get("artifact_id")],
        # Chat-history segment ids written by run_turn's append_segment
        # calls. Frontend bumps `lastMsgIdRef` to max(these) so the next
        # WS reconnect's gap-fill resumes from after them.
        "user_segment_id": result.get("user_segment_id"),
        "assistant_segment_id": result.get("assistant_segment_id"),
    }
    try:
        await manager.send_personal_message(json.dumps(turn_summary, default=str), websocket)
    except Exception:
        log.debug("turn_summary send failed for session=%s job=%s", session_id, job_id, exc_info=True)

    data["timestamp"] = math.floor(time.time() * 1000)
    data["answer"] = result["answer"]
    data["job_id"] = job_id
    data["intel_agent"] = {
        "active_product_type": result["active_product_type"],
        "artifacts": rendered_artifacts,
        "turn_total_ms": result["turn_total_ms"],
        "job_id": job_id,
    }
    # WS may have been closed by the analyst backgrounding the tab — swallow
    # the send failure so the turn doesn't traceback. The result is still
    # persisted (artifact on disk + DB session-detail row + job registry).
    try:
        await manager.send_personal_message(json.dumps(data, default=str), websocket)
    except Exception as send_err:
        _job_update(job_id, status="ws_disconnected",
                    error=f"WS closed before terminal reply: {send_err}")
