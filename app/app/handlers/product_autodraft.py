"""Per-product autodraft sweep — generate a draft of every product at startup.

For every product schema in PRODUCT_SCHEMAS, fire one autodraft into the
shared autodraft session if no artifact of that product type exists yet.

Why: a fresh analyst opening the doctrinal product flow finds every panel
populated — they edit drafts, never blank scaffolds. Downstream products
(MCOO, INTSUM, SITEMP) no longer render "[MISSING PRECURSOR: WEM]" gap
notices on their first generation because WEM/TEM/CCM/LOS/etc. drafts
already exist in the autodraft pool by the time the analyst lands.

Order: producers first (single-source, no consumes_artifacts), then
consumers in dependency order. Without this, MCOO would fire before WEM
exists and inherit a gap notice the autodraft is meant to prevent.

Concurrency: sequential. The KLIEGLIGHT autofire and this sweep both share
the writer LLM (vLLM Gemma 4) — parallel autodrafts would saturate it. One
at a time keeps the writer responsive for analyst-driven Generate clicks
that arrive mid-sweep.

Visibility: drafts land under user_id=_AUTODRAFT_USER_ID,
project_id=_AUTODRAFT_PROJECT_ID — the same pool KLIEGLIGHT autodrafts use.
list_project_docs reads from this pool when the analyst hasn't generated
their own version of a product yet (so MCOO's WEM artifact dep finds the
autodraft).

Trigger: FastAPI startup, AFTER the KLIEGLIGHT startup sweep completes
(SIGSUM / GRINTSUM / INTSUM consume KLIEGLIGHT autodrafts, so KLIEGLIGHTs
must exist first).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


# Autodraft attribution user — artifacts fire into the PROJECT's pool
# (project_id threaded from the caller) under this user namespace.
_AUTODRAFT_USER_ID = "_autodraft"


# Products the sweep should NOT autodraft. Event-driven single-INT products
# own their own per-record fire paths (klieglight_autofire fires SIGINT
# KLIEGLIGHTs per-emitter; osir_autofire fires OSIRs per-OSINT-row); any
# premature autodraft here would fire without a triggering kbID and produce
# an empty / hallucinated artifact.
#
# Per-target products (TMP, CDE, BDA, CA) are also skipped here — they
# require a target binding to author a meaningful header, and the sweep
# has no mechanism to enumerate targets or fan out per-target. The
# architectural entry point is the operations map right-click on a red
# mil_unit, which threads target_entity_id through /api/products/generate
# (see scratch/docs/ibl/map-only-target-products.md). Leaving these in the
# sweep produces empty-header artifacts that pollute the autodraft pool
# (spark had 129 empty CDE folders accumulated this way) and risks
# downstream cross-product corruption when a consumer matches the "latest"
# artifact and finds one with no target binding.
#
# TIDAT is intentionally NOT in this set even though it is per-target:
# the right-click handler addresses TIDAT explicitly via target_focus,
# and listing TIDAT here would block that operator-initiated path. The
# right-click flow goes through refresh_drafts → _autodraft_one_registered
# with target_focus set; the sweep itself only ever fires TIDAT through
# the unattended autodraft_sweep source, which is what register_and_run's
# sweep-dedup already declines for an active chain. Empty-header TIDATs
# fired by an unbound sweep are prevented by the writer-prompt gap
# notice (target_entity_id absent → no artifact written).
_SKIP_PRODUCTS: set[str] = {
    "KLIEGLIGHT",
    "OSIR",
    "TMP",
    "CDE",
    "BDA",
    "CA",
    # TIDAT skipped from the unattended sweep — generated EXCLUSIVELY
    # by the operations-map right-click flow which uses refresh_drafts()
    # with target_focus and bypasses this skip set. Sweep-fired TIDATs
    # without target_entity_id were polluting the chain table and the
    # tidat-pushed-badges client query.
    "TIDAT",
}


# Status of the most recent ensure_pool_populated invocation — exposed so
# operators can see whether the startup sweep landed, was deferred behind a
# cold vllm, or errored. Updated by ensure_pool_populated + the health gate.
_LAST_SWEEP_STATUS: dict = {
    "state": "idle",          # idle | running | deferred | complete | failed
    "started_at_ms": None,
    "completed_at_ms": None,
    "next_retry_at_ms": None,
    "retry_attempt": 0,
    "last_result": None,
    "last_error": None,
}


def sweep_status_snapshot() -> dict:
    """Public snapshot of the most recent sweep invocation state.
    Surfaced via /api/diag/product_autodraft so an operator can see whether
    the startup sweep is deferred behind a cold vllm and when the next
    retry will fire."""
    return dict(_LAST_SWEEP_STATUS)


async def _emit_completion(product_type: str, ok: bool, artifact_id: str,
                           session_id: str, by: str, error: str) -> None:
    """Broadcast a `product_generated` notification on fire completion so every
    analyst's notification bell learns it finished/failed (mirrors the
    KLIEGLIGHT autofire path). Best-effort — a broken notification pipe must
    never fail or roll back the fire itself."""
    try:
        from handlers import notifications
        from utils.exporter import PRODUCT_LONG_NAMES
        await notifications.emit_notification({
            "type": "product_generated",
            "ok": bool(ok),
            "product_type": product_type,
            "long_name": PRODUCT_LONG_NAMES.get(product_type, product_type),
            "artifact_id": artifact_id or None,
            "autodraft_session_id": session_id or None,
            "error": error or None,
            "by": by or "",
            "fired_at_ms": int(time.time() * 1000),
        })
    except Exception:
        log.exception("product_generated notification emit failed for %s", product_type)


def _vllm_healthy(timeout_sec: float = 5.0) -> bool:
    """Return True if vllm's /v1/models endpoint responds with a model list.

    Used as an upstream gate before the sweep: if vllm is cold (warming the
    model into LPDDR5X takes 60-90s on GB10), firing N writer calls at it
    will throw N times in a row, every fire enters fire_history with an
    error, and the sweep exits with fired=0 — leaving the pool empty until
    the next process restart. Health-check first; defer the sweep if vllm
    isn't ready.
    """
    import requests
    from utils import config
    api = config.Config.VLLM_API()
    if not api:
        log.warning("product_autodraft health gate: VLLM_API env var is not set")
        return False
    base = api.split("/v1/")[0] if "/v1/" in api else api.rstrip("/")
    try:
        resp = requests.get(f"{base}/v1/models", timeout=timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        models = (data or {}).get("data") or []
        if not models:
            log.warning(
                "product_autodraft health gate: %s/v1/models returned empty data",
                base,
            )
            return False
        return True
    except Exception as e:
        log.warning(
            "product_autodraft health gate: %s/v1/models probe failed: %s: %s",
            base, type(e).__name__, e,
        )
        return False


# Exponential backoff schedule (seconds) used when the startup sweep is
# deferred because vllm is cold. Caps at 30min; after the last entry the
# operator-driven /resume endpoint is the recovery path.
_SWEEP_RETRY_BACKOFF_SEC: tuple[int, ...] = (60, 300, 1800)


def _topological_product_order(schemas: dict[str, dict]) -> list[str]:
    """Kahn-style topological sort over `consumes_artifacts`.

    Producers (no consumes) fire first, consumers fire after every artifact
    they consume has been autodrafted. Cycles (shouldn't exist doctrinally
    but the schema set is human-edited) fall through to alphabetical tail
    rather than deadlocking the sweep.
    """
    pending: dict[str, set[str]] = {}
    for name, spec in schemas.items():
        consumes = set((spec or {}).get("consumes_artifacts") or [])
        # Only keep dependencies that exist as products (so a typo or a
        # stale name doesn't block the sort).
        pending[name] = {c for c in consumes if c in schemas}
    ordered: list[str] = []
    remaining = dict(pending)
    while remaining:
        ready = sorted(n for n, deps in remaining.items() if not deps)
        if not ready:
            # Cycle — break it by taking the alphabetically-first remaining
            # node and pretending its deps are satisfied. Logs at warning so
            # the next schema edit can fix the cycle.
            ready = [sorted(remaining.keys())[0]]
            log.warning(
                "product_autodraft topo sort hit a cycle, breaking with %s; "
                "remaining=%s",
                ready[0], sorted(remaining.keys()),
            )
        for name in ready:
            ordered.append(name)
            del remaining[name]
        for deps in remaining.values():
            deps.difference_update(ready)
    return ordered


def _autodraft_pool_index(project_id: str = "") -> dict[str, list[dict]]:
    """Build `{product_type: [latest-first {session_id, artifact_id, dispatch}, ...]}`
    for ONE project's pool (pools are per project/event; blank matches
    only pre-unification rows and exists for the migration window).

    Sourced from the DB (SessionDetail rows with session_id like 'autodraft-%').
    The dep-aware sweep calls `_autodraft_has_missing_precursors` /
    `_invalidate_draft` without re-querying per product.
    """
    import json
    pool: dict[str, list[dict]] = {}
    try:
        from utils import database
        db = database.SessionLocal()
        try:
            rows = (
                db.query(database.SessionDetail)
                .filter(database.SessionDetail.session_id.like("autodraft-%"))
                .filter(database.SessionDetail.project_id == project_id)
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
                    pt = (disp.get("artifact_type")
                          or (disp.get("emit") or {}).get("artifact_type"))
                    if not pt:
                        continue
                    aid = (disp.get("emit") or {}).get("artifact_id") or ""
                    pool.setdefault(pt, []).append({
                        "session_id": row.session_id,
                        "artifact_id": aid,
                        "from": "db",
                        "dispatch": disp,
                    })
        finally:
            db.close()
    except Exception:
        log.exception("product_autodraft: DB scan for existing autodrafts failed")
    return pool


def _autodraft_has_missing_precursors(product_type: str,
                                      pool: dict[str, list[dict]] | None = None) -> bool:
    """True if the latest autodraft of `product_type` contains a
    `[MISSING PRECURSOR:` gap notice anywhere in its structured_data.

    Set when a field's `consumes_artifacts` dep was empty at autodraft time —
    poisoned downstream consumers inherit those placeholder strings verbatim.
    Detecting + re-firing the upstream-then-self chain is the fix.
    """
    import json as _json
    pool = pool if pool is not None else _autodraft_pool_index()
    entries = pool.get(product_type) or []
    for entry in entries:
        disp = entry.get("dispatch") or {}
        emit = disp.get("emit") or {}
        sd = emit.get("structured_data") or {}
        try:
            blob = _json.dumps(sd, default=str)
        except Exception:
            blob = str(sd)
        if "[MISSING PRECURSOR" in blob:
            return True
        # First poisoned-or-clean entry is authoritative (entries are
        # latest-first per DB ordering); short-circuit after the most recent.
        return False
    return False


def _invalidate_draft(product_type: str,
                       pool: dict[str, list[dict]] | None = None) -> int:
    """Cache-invalidate: remove every autodraft entry for `product_type`
    from BOTH the DB (SessionDetail rows + Session rows whose session_id
    starts `autodraft-` AND whose only dispatch is for this product type)
    AND from the on-disk uploads/_autodraft/<aid> folder. Returns the
    count of DB rows deleted.

    Conservative: only deletes Session rows whose dispatch list is entirely
    this product_type (multi-dispatch sessions are left alone — they belong
    to the legacy OE loop and may carry analyst conversation context).
    """
    import json as _json
    import shutil as _shutil
    from pathlib import Path as _Path
    deleted_rows = 0
    pool = pool if pool is not None else _autodraft_pool_index()
    entries = pool.get(product_type) or []
    if not entries:
        return 0
    try:
        from utils import database
        db = database.SessionLocal()
        try:
            session_ids = {e.get("session_id") for e in entries if e.get("session_id")}
            artifact_ids = {e.get("artifact_id") for e in entries if e.get("artifact_id")}
            for sid in session_ids:
                if not sid or not sid.startswith("autodraft-"):
                    continue
                row = (db.query(database.SessionDetail)
                          .filter(database.SessionDetail.session_id == sid)
                          .one_or_none())
                if row is None:
                    continue
                try:
                    d = _json.loads(row.data or "{}")
                except Exception:
                    d = {}
                trace = d.get("agent_trace") or {}
                disps = trace.get("product_writer_dispatches") or []
                disp_types = {(disp.get("artifact_type")
                                or (disp.get("emit") or {}).get("artifact_type"))
                              for disp in disps}
                disp_types.discard(None)
                if disp_types == {product_type}:
                    db.delete(row)
                    sess = (db.query(database.Session)
                              .filter(database.Session.session_id == sid)
                              .one_or_none())
                    if sess is not None:
                        db.delete(sess)
                    deleted_rows += 1
            db.commit()
        finally:
            db.close()
    except Exception:
        log.exception("product_autodraft: DB delete failed for %s", product_type)
    # On-disk pool — uploads/_autodraft/<artifact_id>/
    try:
        from handlers.product import UPLOAD_DIR
        for aid in artifact_ids:
            if not aid:
                continue
            adir = _Path(UPLOAD_DIR) / "_autodraft" / aid
            if adir.is_dir():
                _shutil.rmtree(adir, ignore_errors=True)
    except Exception:
        log.exception("product_autodraft: disk cleanup failed for %s", product_type)
    log.info("product_autodraft: purged %d poisoned autodraft(s) for %s",
             deleted_rows, product_type)
    return deleted_rows


def _resolve_kg_record_inline(target_kg_id: str) -> dict:
    """Resolve a bound target's full named_unit record from the resolved
    OOB snapshot on disk, keyed by kg_id.

    THE canonical target-resolution path for per-target products
    (TIDAT / TMP / CDE / BDA / CA): the /api/v1/entities/{id} HTTP
    fallback 500s for every id tested (verified 2026-06-10), so without
    this record inline the writer emits 'no bound target KG record' gap
    notices and picks arbitrary targets from the dep payloads. The
    kg_id namespace is shared end-to-end: the resolved snapshot keys
    named_units by it, the live_oob map sync stamps it as each
    mil-unit feature's `source_kbid`, the operations-map right-click
    sends it as target_entity_id, and the TIDAT-pushed badge joins
    chains back to map features by it.

    Extracted from bda_autofire's inline block (2026-07-07) so every
    dispatch path shares ONE loader. Returns {} when the snapshot is
    missing or the id doesn't resolve — callers treat that as
    'unknown' and the writer gap-notices loudly.
    """
    try:
        snap = Path("/app/data/cougaar_raw_snapshots/oob_resolved_latest.json")
        if snap.exists():
            doc = json.loads(snap.read_text())
            for nu in (doc.get("resolved") or {}).get("named_units") or []:
                if (nu.get("kg_id") or nu.get("id")) == target_kg_id:
                    return nu
    except Exception as e:
        log.debug("kg_record inline lookup failed for %r: %s", target_kg_id, e)
    return {}


async def _autodraft_one(product_type: str,
                         *,
                         project_id: str = "",
                         coa_focus: dict | None = None,
                         target_focus: dict | None = None,
                         _registered_record: "Any" = None,
                         _session_id: str | None = None) -> dict:
    """Generate a single autodraft for one product_type via the DAG runner.

    Mirrors klieglight_autofire._fire_one but parametric on product_type.
    Returns a result dict; errors are captured (don't propagate to the
    sweep loop).

    `coa_focus` (optional) — per-COA override used by the SITEMP fan-out
    path. Carries {coa_id, coa_label, rank, most_dangerous, threat_coa_statement_artifact_id}
    so the writer authors COA-specific templated_units / engagement_areas
    / NAIs / TPLs rather than generic ones. The autodraft context prose
    references the focus so the writer prompt picks up the doctrinal
    binding (per ATP 2-01.3 ¶6-15 the SITEMP is one-per-COA).

    `target_focus` (optional) — single-target binding override used by
    the operations-map right-click path on per-target products. Carries
    {entity_id, side, ...} pointing at one HPTL/AGM target. When set
    AND product_type == "TIDAT", the writer context prose names the
    bound target and tells the writer to populate every header field
    FROM THAT kg entity and echo method_of_engagement +
    method_of_fire_and_control verbatim from the matching AGM row.
    One artifact per click — no fanout, no enumeration. Mechanically
    symmetric with coa_focus, but bind-one not fan-out-N.

    `_registered_record` (optional, threaded by _autodraft_one_registered)
    — the ProductRecord this fire is registered under in the unified
    registry. When set, the function calls record_transition on terminal
    states so the unified UI sees state/phase/artifact_id flips.
    """
    from handlers.intel_agent import (
        State, tool_generate_product,
    )

    # The session id is now minted in _autodraft_one_registered BEFORE
    # register_and_run, so the ProductRecord carries it from creation.
    # Resolution order:
    #   1. explicit _session_id kwarg (registered-flow call sites pass this)
    #   2. _registered_record.autodraft_session_id (retry path — register_and_run
    #      stamps the session at creation, the dispatcher reads it off the record)
    #   3. fresh mint (direct / harness callers without a registered record)
    autodraft_session_id = (
        _session_id
        or (getattr(_registered_record, "autodraft_session_id", None) if _registered_record else None)
        or f"autodraft-{uuid.uuid4()}"
    )
    fired_at_ms = int(time.time() * 1000)

    # Hard-guard: per-target products MUST have a bound target_focus.
    # Per-target schemas (shape=header_only — TIDAT / TMP / CDE / BDA /
    # CA) author per-kg-entity; without target_focus the writer has no
    # binding and emits a malformed artifact. The /api/products/generate
    # endpoint enforces this at HTTP 400, and the autodraft sweep skips
    # them via _SKIP_PRODUCTS — but admin refire paths
    # (refire_product_maps / refire_product_exports / direct
    # _autodraft_one calls) could still slip through, so backstop here.
    _PER_TARGET = {"TIDAT", "TMP", "CDE", "BDA", "CA"}
    if product_type in _PER_TARGET and not (target_focus and target_focus.get("entity_id")):
        msg = (
            f"per-target product {product_type!r} requires target_focus "
            f"with entity_id; refusing to autodraft without a target binding"
        )
        log.error("product_autodraft: %s (session=%s)", msg, autodraft_session_id)
        return {
            "ok": False,
            "error": msg,
            "product_type": product_type,
            "autodraft_session_id": autodraft_session_id,
        }

    log.info(
        "product_autodraft START: product=%s session=%s coa=%s target=%s",
        product_type, autodraft_session_id,
        (coa_focus or {}).get("coa_id"),
        (target_focus or {}).get("entity_id"),
    )

    from utils.project_context import set_active_project
    set_active_project(project_id)
    state = State.fresh(
        autodraft_session_id,
        user_id=_AUTODRAFT_USER_ID,
        manual_product_type=product_type,
        coa_focus=coa_focus or None,
        project_id=project_id,
    )
    # Register session_id → record_id so the DAG executor can emit
    # progress_pct via record_transition at each wave end. State is
    # @dataclass(frozen=True) so we can't stamp the record_id directly;
    # the llm module holds a transient side-channel map cleared on
    # chain complete.
    if _registered_record is not None:
        try:
            from utils import llm as _llm
            _llm.register_session_record(
                autodraft_session_id, _registered_record.record_id,
            )
        except Exception:
            pass

    # No state prewarm — DAG resolves cougaar deps per schema declarations
    # and passes the resolved data to tool_render_map directly via the
    # intel_data parameter (see handlers/intel_agent.py:tool_render_map
    # and runtime/dag_executor.py's intel_agent_data assembly). The
    # prior autodraft_prewarm_state shortcut was deleted 2026-05-26.

    # Generic context — the DAG already pulls every data source it needs
    # via schema-declared consumes / depends_on; the autodraft doesn't have
    # an event-driven seed like KLIEGLIGHT does. A short prose preamble
    # naming the AO + classification is enough to let the writer anchor.
    context = (
        f"Autodraft pass for {product_type}. UNCLASSIFIED // FOR EXERCISE USE ONLY. "
        f"Generate a complete draft from the available cougaar tools + scenario "
        f"fixtures. The writer should populate every required field; missing "
        f"upstream artifacts surface as gap notices the analyst can fix on edit."
    )
    if coa_focus:
        coa_id = coa_focus.get("coa_id") or ""
        coa_label = coa_focus.get("coa_label") or ""
        rank = coa_focus.get("rank")
        most_dangerous = bool(coa_focus.get("most_dangerous"))
        threat_coa_statement_aid = coa_focus.get("threat_coa_statement_artifact_id") or ""
        flag = "MOST-DANGEROUS" if most_dangerous else f"rank={rank}"
        context += (
            f"\n\nThis is a per-COA SITEMP. Author every field SPECIFIC TO "
            f"COA {coa_id} ('{coa_label}', {flag}) from Threat COA Statement artifact "
            f"{threat_coa_statement_aid}. The header MUST carry coa_id='{coa_id}', "
            f"coa_label='{coa_label}', most_dangerous={most_dangerous}, "
            f"threat_coa_statement_artifact_id='{threat_coa_statement_aid}'. templated_units, "
            f"engagement_areas, named_areas_of_interest, "
            f"objectives, main_effort, shaping_operations, fires, "
            f"reconnaissance, enemy_commanders_intent — every one of these "
            f"reflects HOW THE THREAT EXECUTES THIS SPECIFIC COA, not a "
            f"generic doctrinal template. Author one situation template "
            f"per threat course of action; each per-COA SITEMP must be "
            f"distinguishable from the others (drives a different "
            f"friendly decision)."
        )

    # Single-target binding for per-target products initiated from the
    # operations map right-click. Mechanically symmetric with the
    # coa_focus block above, but the semantics differ:
    #
    #   coa_focus → fanout (N per-COA SITEMPs, each parameterized)
    #   target_focus → bind-one (ONE per-target product per click,
    #                  zero-fanout, MUST NOT emit additional targets)
    #
    # The operations map handler captures the analyst's right-click on
    # a single red mil_unit and threads its kg entity_id through
    # /api/products/generate. The writer prompt below tells the writer
    # to populate target_id / name / category / location_mgrs / size /
    # activity / doi / detection_source FROM THAT entity, and to echo
    # method_of_engagement + method_of_fire_and_control verbatim from
    # the AGM.structured_data.agm_rows[target_id] entry (the AGM is the
    # canonical source for the engagement method; the writer must NOT
    # re-derive it). Only TIDAT is wired in this pass — TMP / CDE / BDA
    # right-click menus land in phase-2, at which point this block
    # extends to a small dispatch keyed on product_type.
    # Per-target Deliver-phase products. The operations-map right-click
    # gesture binds ONE kg entity to ONE artifact for each of these.
    # TIDAT (push to FSE), TMP (mensurated aimpoint data sheet), CDE
    # (collateral damage estimate), BDA (battle damage assessment), CA
    # (combat assessment). All five share the same kg-inline contract:
    # writer receives the bound entity's identity inline and uses it
    # verbatim for target_id / target_name / target_location_mgrs /
    # target_size / target_activity / detection_source, with empty
    # strings on any kg-field that isn't present (no placeholders).
    _PER_TARGET_PRODUCTS = {"TIDAT", "TMP", "CDE", "BDA", "CA"}
    # TIDAT-specific hard_rule enforcement at dispatch-time. The schema
    # declares TARGET_FOCUS_BINDING_HARD_RULE (target_entity_id required
    # via right-click) + the active HPTL membership rule + the not-in-
    # terminal-posture rule. Enforce them BEFORE LLM emission so a
    # malformed artifact is never written. The schema text alone is
    # advisory; without these gates the per-target dispatch path can
    # accept an off-HPTL right-click and emit a TIDAT artifact for a
    # target the FSE has no authority over.
    if product_type == "TIDAT":
        _tidat_entity_id = (target_focus or {}).get("entity_id") or ""
        if not _tidat_entity_id:
            msg = (
                "TIDAT TARGET_FOCUS_BINDING_HARD_RULE: target_entity_id "
                "required via the operations-map right-click trigger. "
                "Refusing emission without a bound target."
            )
            log.error(
                "TIDAT dispatch gate FAIL: %s (session=%s)",
                msg, autodraft_session_id,
            )
            return {
                "ok": False,
                "error": msg,
                "product_type": product_type,
                "autodraft_session_id": autodraft_session_id,
            }
        # Resolve the bound unit's KG record ONCE, up front: the HPTL
        # gate below needs hvt_worthy_count and the per-target writer
        # block needs the whole record inline (the caller may have
        # supplied it — bda_autofire does; the right-click
        # /api/products/generate path does not). Stash it on
        # target_focus so the downstream block reuses this resolution
        # instead of re-reading the snapshot.
        if not (target_focus.get("_kg_record") or {}):
            _resolved_rec = _resolve_kg_record_inline(_tidat_entity_id)
            if _resolved_rec:
                target_focus["_kg_record"] = _resolved_rec
        _bound_kg_record = target_focus.get("_kg_record") or {}

        # Gate (b): bound target must be on the active HPTL. Two match
        # forms, because HPTL rows carry CATEGORY-form ids
        # (`hvt::<equipment>` — the HPTL is a rollup of which HVT
        # categories matter), not per-unit kg ids:
        #   1. direct row-id match (kept for any HPTL emission that
        #      does carry unit ids), else
        #   2. the bound unit fields HPTL-worthy equipment
        #      (hvt_worthy_count > 0 on its resolved KG record — the
        #      same equipment_doctrine.is_hvt_worthy classification the
        #      HPTL rows are built from). A unit with zero HVT-worthy
        #      equipment is not on the HPTL by any reading — refuse.
        # Gate (c): bound target must NOT be in {DEFEATED, DESTROYED,
        # EXPIRED, MOOTED} per the most recent BDA / CA. These checks
        # tolerate registry-resolution failures (treat as 'unknown,
        # proceed') so a transient upstream outage doesn't block a
        # legitimate right-click — but they DO fail loudly when the
        # registry explicitly returns a terminal posture or no-HPTL-row
        # signal.
        try:
            _reg = getattr(state, "registry_dict", None)
            _registry = _reg() if callable(_reg) else {}
            _hptl_items = (_registry or {}).get("HPTL") or ()
            if _hptl_items:
                _latest_hptl = _hptl_items[0] if isinstance(_hptl_items, (list, tuple)) else None
                _hptl_sd = (_latest_hptl or {}).get("structured_data") or {}
                _rows = (
                    _hptl_sd.get("hptl_rows")
                    or _hptl_sd.get("rows")
                    or []
                )
                if isinstance(_rows, list) and _rows:
                    _ids = {
                        r.get("id") for r in _rows
                        if isinstance(r, dict) and r.get("id")
                    }
                    _hvt_worthy = 0
                    try:
                        _hvt_worthy = int(_bound_kg_record.get("hvt_worthy_count") or 0)
                    except (TypeError, ValueError):
                        _hvt_worthy = 0
                    if _tidat_entity_id not in _ids and _hvt_worthy <= 0:
                        msg = (
                            f"TIDAT hard_rule: bound target {_tidat_entity_id!r} "
                            f"is not on the active HPTL (no row-id match and "
                            f"no HPTL-worthy equipment on the unit). "
                            f"Refusing emission."
                        )
                        log.error("TIDAT dispatch gate FAIL: %s", msg)
                        return {
                            "ok": False,
                            "error": msg,
                            "product_type": product_type,
                            "autodraft_session_id": autodraft_session_id,
                        }
                    if _tidat_entity_id not in _ids:
                        log.info(
                            "TIDAT dispatch gate: target %r passes HPTL "
                            "membership via hvt_worthy_count=%d (rows are "
                            "category-form)", _tidat_entity_id, _hvt_worthy,
                        )
            # Terminal-posture check via the most recent BDA/CA pool.
            _TERMINAL_POSTURES = {"DEFEATED", "DESTROYED", "EXPIRED", "MOOTED"}
            for _aname in ("BDA", "CA"):
                _items = (_registry or {}).get(_aname) or ()
                for _it in (_items if isinstance(_items, (list, tuple)) else []):
                    _isd = (_it or {}).get("structured_data") or {}
                    _ihdr = _isd.get("header") or {}
                    if _ihdr.get("target_id") != _tidat_entity_id:
                        continue
                    _posture = (
                        _ihdr.get("target_posture")
                        or _isd.get("overall_status")
                        or ""
                    )
                    if isinstance(_posture, str) and _posture.upper() in _TERMINAL_POSTURES:
                        msg = (
                            f"TIDAT hard_rule: bound target "
                            f"{_tidat_entity_id!r} is in terminal posture "
                            f"{_posture!r} per latest {_aname}. Refusing "
                            f"emission."
                        )
                        log.error("TIDAT dispatch gate FAIL: %s", msg)
                        return {
                            "ok": False,
                            "error": msg,
                            "product_type": product_type,
                            "autodraft_session_id": autodraft_session_id,
                        }
        except Exception as _e:
            log.warning(
                "TIDAT dispatch gate registry-resolution failed "
                "(treating as 'unknown, proceed'): %s", _e,
            )

    if target_focus and product_type in _PER_TARGET_PRODUCTS:
        entity_id = target_focus.get("entity_id") or ""
        side = target_focus.get("side") or ""

        # Resolve the bound entity's kg record INLINE so the per-field LLM
        # calls have the actual target identity in their dep payload — they
        # don't run with kg-lookup tools available (header_only products
        # author each field in an independent llm call against the
        # assembled spec + dep payload only). Without inlining, the writer
        # sees `target_entity_id='<uuid>'` but no data backing it, and
        # picks an arbitrary target from the HPTL/AGM dep payloads — which
        # is the bug we're fixing.
        kg_data_block = ""
        # Inline kg record short-circuits the broken
        # /api/v1/entities/{entity_id} HTTP path (verified 2026-06-10:
        # that endpoint 500s for EVERY kbid/entity_id tested, so
        # without the record inline every per-target product
        # (BDA / TIDAT / TMP / CDE / CA) ends up with 'no bound target
        # KG record' gap notices in every paragraph). Callers may
        # supply it (bda_autofire does); otherwise resolve it HERE from
        # the OOB snapshot so every dispatch path — the operations-map
        # right-click /api/products/generate, the dashboard, manual
        # harnesses — gets the same binding without each caller
        # duplicating the loader.
        _inline_kg = (target_focus or {}).get("_kg_record") or {}
        if not _inline_kg and entity_id:
            _inline_kg = _resolve_kg_record_inline(entity_id)
        if isinstance(_inline_kg, dict) and (_inline_kg.get("name")
                                                or _inline_kg.get("kg_id")):
            _pp = _inline_kg.get("primary_position") or {}
            kg_data_block = (
                f"\n\n=== BOUND TARGET KG RECORD (use this verbatim) ===\n"
                f"kg_id: {_inline_kg.get('kg_id') or entity_id}\n"
                f"name: {_inline_kg.get('name') or ''}\n"
                f"affiliation: HOSTILE\n"
                f"force_type: RED\n"
                f"echelon: {_inline_kg.get('echelon') or ''}\n"
                f"organization_type: {_inline_kg.get('organization_type') or ''}\n"
                f"composition: {_inline_kg.get('composition_rollup') or {}}\n"
                f"strength: {_inline_kg.get('strength_pct_manned') or ''}\n"
                f"posture: {_inline_kg.get('disposition') or _inline_kg.get('current_activity') or ''}\n"
                f"current_activity: {_inline_kg.get('current_activity') or _inline_kg.get('disposition') or ''}\n"
                f"doi (date of information): {_pp.get('last_seen') or ''}\n"
                f"sidc: {_inline_kg.get('sidc') or ''}\n"
                f"parent_unit: {_inline_kg.get('parent_unit') or ''}\n"
                f"location_lat_lon: ({_pp.get('lat')}, {_pp.get('lon')})\n"
                f"position_source: {_inline_kg.get('position_source') or 'raw'}\n"
                f"hvt_worthy_count: {_inline_kg.get('hvt_worthy_count', 0)}\n"
                f"=== END BOUND TARGET KG RECORD ===\n"
            )
        # When inline kg_data_block was built above, skip the HTTP
        # path entirely (the /api/v1/entities/{id} endpoint 500s).
        try:
            if kg_data_block:
                _body = None
            else:
                # The right-clicked entity_id is the geo-agent kg row id,
                # not a cougaar kbID — and cougaar is often unreachable
                # in-exercise anyway. Fetch from the geo-agent's
                # authoritative entity endpoint, which carries the merged
                # cougaar/scenario_terrain record with all the attrs the
                # TIDAT header needs (name, sidc, echelon, affiliation,
                # location_mgrs, parent, etc.).
                import os, json as _json, urllib.request, urllib.error
                _geo_base = (os.environ.get("GEO_API_URL",
                                            "http://host.docker.internal:9845").rstrip("/")
                             + "/api/v1")
                _url = f"{_geo_base}/entities/{entity_id}"
                try:
                    with urllib.request.urlopen(_url, timeout=4) as _resp:
                        _body = _json.loads(_resp.read())
                except urllib.error.HTTPError as _he:
                    log.warning("TIDAT target_focus entity lookup HTTP %s entity_id=%s url=%s",
                                 _he.code, entity_id, _url)
                    _body = None
                except Exception as _e:
                    log.warning("TIDAT target_focus entity lookup failed entity_id=%s: %s",
                                 entity_id, _e)
                    _body = None
            if kg_data_block:
                # Inline kg_data_block already populated — fall through
                # to the per-product-type echo block below without
                # touching kg_data_block.
                pass
            elif _body and isinstance(_body, dict):
                _attrs = _body.get("attrs") or {}
                _geom = _body.get("geom") or {}
                _coords = _geom.get("coordinates") if isinstance(_geom, dict) else None
                # NOTE: source_kbid + parent_unit_designator intentionally
                # OMITTED. The DAG validator (validation_failed:
                # FABRICATED_IDENTIFIERS) rejects any string in an
                # analyst-output field that wasn't in seen_ids — the
                # writer was quoting our inlined parent kbID into output
                # fields and tripping the check. The downstream writer
                # only needs the kg_id (which the validator's _PER_TARGET
                # binding registers) plus identity attrs; the internal
                # cougaar kbID linkage stays in the kg layer.
                kg_data_block = (
                    f"\n\n=== BOUND TARGET KG RECORD (use this verbatim) ===\n"
                    f"kg_id: {_body.get('id') or entity_id}\n"
                    f"name: {_attrs.get('name') or _attrs.get('unit_short_name') or ''}\n"
                    f"affiliation: {_attrs.get('affiliation') or ''}\n"
                    f"force_type: {_attrs.get('force_type') or ''}\n"
                    f"echelon: {_attrs.get('echelon') or ''}\n"
                    f"category: {_attrs.get('category') or ''}\n"
                    f"organization_type: {_attrs.get('target_category') or _attrs.get('organization_type') or ''}\n"
                    f"composition: {_attrs.get('composition') or ''}\n"
                    f"strength: {_attrs.get('personnel_status') or _attrs.get('strength_pct_manned') or ''}\n"
                    f"posture: {_attrs.get('posture') or _attrs.get('unit_status') or ''}\n"
                    f"current_activity: {_attrs.get('current_activity') or _attrs.get('disposition') or ''}\n"
                    f"doi (date of information): {_attrs.get('doi') or _attrs.get('last_updated') or _attrs.get('locationLastUpdate') or ''}\n"
                    f"sidc: {_attrs.get('sidc') or _attrs.get('symbolic_identifier') or ''}\n"
                    f"location_mgrs: {_attrs.get('location_mgrs') or ''}\n"
                    f"location_lon_lat: {_coords}\n"
                    f"source: {_attrs.get('source') or ''}\n"
                    f"=== END BOUND TARGET KG RECORD ===\n"
                )
            else:
                kg_data_block = (
                    f"\n\n=== BOUND TARGET KG RECORD ===\n"
                    f"kg_id: {entity_id}\n"
                    f"(geo-agent /entities/{entity_id} returned no record — "
                    f"the writer must emit a gap notice in every kg-sourced "
                    f"field rather than substituting another target.)\n"
                    f"=== END BOUND TARGET KG RECORD ===\n"
                )
        except Exception as e:
            log.warning(
                "TIDAT target_focus kg lookup failed entity_id=%s: %s",
                entity_id, e,
            )

        # Per-product-type specific echo instructions. TIDAT echoes
        # method_of_engagement from AGM; TMP fields are stub markers for
        # certified mensuration; CDE pulls from NSL/RTL/CCA-Matrix proximity;
        # BDA/CA reference the engagement record and prior TIDAT push.
        _echo_block_by_pt = {
            "TIDAT": (
                "Echo method_of_engagement and method_of_fire_and_control "
                f"VERBATIM from the matching row in "
                f"AGM.structured_data.agm_rows where target_id == "
                f"'{entity_id}'. If no such agm_row exists (the bound target "
                f"is not on the active HPTL), emit a gap notice in those two "
                f"fields rather than substituting another target's method."
            ),
            "TMP": (
                "JDPI_ID and parent_etf_eid are placeholder fields awaiting "
                "certified TMP work-center mensuration. Mark STUB=true and "
                "emit a Stub Reason explaining the analyst-required input. "
                "Do NOT invent a JDPI ID."
            ),
            "CDE": (
                "Cite the NSL / RTL / CCA-Matrix entry that establishes the "
                "collateral concern when the bound target lies within a "
                "protected-entity buffer. Quantify civilian-proximity radii "
                "from the kg location."
            ),
            "BDA": (
                "Echo engagement record from the matching AGM row for the "
                "bound target. Phase-1 (physical) / Phase-2 (functional) / "
                "Phase-3 (target-system) damage all reference the same "
                f"target_id='{entity_id}'. Reattack recommendation cites "
                "the active TSS row's accuracy threshold."
            ),
            "CA": (
                "Combat assessment frames are BDA + Munition Effectiveness "
                "Assessment + Reattack Recommendation, all per the matching "
                f"engagement record for target_id='{entity_id}'."
            ),
        }
        _echo_text = _echo_block_by_pt.get(product_type, "")

        context += (
            f"\n\nThis is a per-target {product_type} authored from an "
            f"analyst right-click on a single target. Bind to ONE target "
            f"only — kg entity_id='{entity_id}' (side='{side}'). The "
            f"header MUST carry target_id='{entity_id}'. Populate target_id, "
            f"target_name, target_category, target_location_mgrs, "
            f"target_size, target_activity, doi, and detection_source FROM "
            f"the BOUND TARGET KG RECORD block below — that block is the "
            f"authoritative source for this target's identity. Do NOT "
            f"enumerate other targets in the HPTL/AGM/TSS dep payloads to "
            f"author this artifact; those dep payloads are for "
            f"cross-reference, NOT for picking a different target than the "
            f"one bound here. "
            f"{_echo_text} "
            f"One artifact per click. Field rules: target_id is the kg_id "
            f"value verbatim; never empty. push_status (if present) starts "
            f"at PENDING. "
            f""
            f"DOCTRINAL DEFAULTS — these session-context fields are NEVER "
            f"blank and NEVER [ANALYST INPUT REQUIRED]; derive from the "
            f"doctrinal defaults below: "
            f" - routing_destination: 'FSE_CCS' (the doctrinal "
            f"   ACE-to-FSE Cell Coordination Section relay; "
            f"   'AFATDS_DIRECT' is the immediate-precedence override for "
            f"   time-critical engagements where the FSE relay would add "
            f"   latency — use only when the AGM method_of_engagement "
            f"   explicitly calls for direct fires routing). "
            f" - originating_cell: 'G-2 ACE Targeting' (the Analysis "
            f"   Control Element targeting workstation, doctrinal ACE-tier "
            f"   intel-fusion desk per FM 2-0). "
            f" - phase: use the scenario's current phase if upstream "
            f"   OPORD/FRAGO payload supplies one; otherwise 'Phase I "
            f"   (Penetration)' as the OPORD-declared first phase. "
            f" - as_of_dtg: SAME value as the header.dtg field (this "
            f"   push's cutoff time equals the artifact's emission DTG "
            f"   unless an earlier observation cutoff is explicitly named "
            f"   in the bound target context). "
            f" - target_number: if upstream HPTL row provides one (kg "
            f"   block target_number, or HPTL dep payload row keyed by "
            f"   target_id), use that verbatim; otherwise auto-assign "
            f"   'T-D' + last 4 hex chars of target_id uppercased "
            f"   (deterministic from kg_id, no fabrication). "
            f""
            f"BANNED OUTPUT STRINGS: the literal phrases 'Not reported', "
            f"'TBD', 'unknown', 'N/A', 'n/a', 'nil', '-', '—' are "
            f"BANNED in every field of this artifact. [ANALYST INPUT "
            f"REQUIRED] is RESERVED for fields that genuinely require "
            f"analyst inspection of source material the writer cannot "
            f"see (sensor product imagery interpretation, originator "
            f"verification, classification override) — NEVER use it for "
            f"the session-context fields named above, which have "
            f"doctrinal defaults. If a field has no source data in "
            f"either the BOUND TARGET KG RECORD or upstream dep payloads "
            f"AND no doctrinal default exists for it, emit an EMPTY "
            f"string and the runner surfaces the gap downstream."
            + kg_data_block
        )

    args = {
        "product_type": product_type,
        "session_id": autodraft_session_id,
        "user_id": _AUTODRAFT_USER_ID,
        "project_id": project_id,
        "context": context,
    }

    # Mark the writer-started phase on the unified record so the UI
    # flips from "awaiting_gate" to "writer".
    if _registered_record is not None:
        try:
            from handlers import product_records as _pr
            await _pr.record_transition(
                _registered_record.record_id,
                event_type="writer_started",
                phase="writer",
                writer_started_at_ms=int(time.time() * 1000),
                autodraft_session_id=autodraft_session_id,
            )
        except Exception:
            log.exception("unified record_transition (writer_started) failed for "
                          "%s/%s", product_type, autodraft_session_id)

    try:
        result = await tool_generate_product(args, state, on_event=None)
    except Exception as e:
        log.exception("product_autodraft FAILED: product=%s", product_type)
        entry = {
            "product_type": product_type,
            "artifact_id": None,
            "autodraft_session_id": autodraft_session_id,
            "fired_at_ms": fired_at_ms,
            "error": f"{type(e).__name__}: {e}",
        }
        # Re-raise so register_and_run's except-arm transitions the
        # unified record to failed. Legacy callers (not wrapped through
        # register_and_run) still get the entry returned via the
        # exception escape — they should call _autodraft_one_registered
        # instead.
        if _registered_record is not None:
            raise
        return entry

    if isinstance(result, dict) and result.get("error"):
        log.warning(
            "product_autodraft DAG returned error: product=%s err=%s detail=%s",
            product_type, result.get("error"), result.get("detail"),
        )
        entry = {
            "product_type": product_type,
            "artifact_id": None,
            "autodraft_session_id": autodraft_session_id,
            "fired_at_ms": fired_at_ms,
            "error": f"{result.get('error')}: {result.get('detail') or ''}",
        }
        # Surface the DAG error as an exception so the unified wrapper
        # transitions to failed.
        if _registered_record is not None:
            raise RuntimeError(entry["error"])
        return entry

    artifact_id = (result or {}).get("artifact_id") or ""
    log.info(
        "product_autodraft DONE: product=%s artifact_id=%s",
        product_type, artifact_id,
    )

    # Per-target post-write header force-overwrite. For TMP specifically,
    # the writer leaves target_id + weaponeering_objective blank under
    # the autodraft path because the session middleware doesn't thread
    # the bound target's identifiers into header session fields. Force-
    # write them here from the bound target_focus + the matching HPTL
    # row's weaponeering recommendation. Mirrors the coa_focus header-
    # force-write for SITEMP fanout.
    if (target_focus and product_type == "TMP"
            and getattr(state, "products", None)
            and "TMP" in (state.products or {})):
        try:
            entity_id = target_focus.get("entity_id") or ""
            tmp_prod = state.products["TMP"]
            sd = tmp_prod.structured_data if hasattr(tmp_prod, "structured_data") else None
            if isinstance(sd, dict):
                hdr = sd.setdefault("header", {})
                hdr["target_id"] = entity_id
                # Pull weaponeering_objective from the matching HPTL row.
                hptl_prod = (state.products or {}).get("HPTL")
                hptl_sd = getattr(hptl_prod, "structured_data", None) if hptl_prod else None
                if isinstance(hptl_sd, dict):
                    rows = (
                        hptl_sd.get("hptl_rows")
                        or hptl_sd.get("rows")
                        or []
                    )
                    if isinstance(rows, list):
                        for row in rows:
                            if not isinstance(row, dict):
                                continue
                            if (row.get("id") or row.get("target_id")) == entity_id:
                                wo = (
                                    row.get("weaponeering_objective")
                                    or row.get("desired_effect")
                                    or ""
                                )
                                if wo:
                                    hdr["weaponeering_objective"] = wo
                                break
        except Exception as _e:
            log.warning(
                "TMP post-write header force-write failed entity_id=%s: %s",
                target_focus.get("entity_id"), _e,
            )

    entry = {
        "product_type": product_type,
        "artifact_id": artifact_id,
        "autodraft_session_id": autodraft_session_id,
        "fired_at_ms": fired_at_ms,
        "error": None,
    }
    # Stamp artifact_id on the unified record so the chain row shows it.
    # The session_id is stamped at registration (register_and_run accepts
    # autodraft_session_id and writes it onto the ProductRecord at creation),
    # so this transition no longer needs to re-stamp it as a safety net.
    if _registered_record is not None:
        try:
            from handlers import product_records as _pr
            await _pr.record_transition(
                _registered_record.record_id,
                event_type="phase",
                phase="completed",
                artifact_id=artifact_id,
            )
        except Exception:
            log.exception("unified record_transition (artifact stamp) failed for "
                          "%s/%s", product_type, autodraft_session_id)
    return entry


async def _autodraft_one_registered(product_type: str,
                                       *,
                                       project_id: str = "",
                                       coa_focus: dict | None = None,
                                       target_focus: dict | None = None,
                                       product_parent_record_id: str | None = None,
                                       source: str = "autodraft_sweep",
                                       source_metadata: dict | None = None,
                                       force: bool = False) -> dict:
    """Run _autodraft_one through the unified product_records registry.

    Every entry point that fires a product (sweep, fanout, refire,
    /api/products/generate-async) should go through HERE instead of
    calling _autodraft_one directly. register_and_run handles the
    queued→running→complete/failed transitions + broadcast +
    JobEvent log.

    Returns the legacy entry dict so existing callers keep working.
    """
    from handlers import product_records as _pr

    # jobs-unification-005 wire-up: sweep dedup. Skip the fire if the
    # registry already has an active (non-cancelled) chain for this
    # product type AND the source is the unattended autodraft sweep.
    # The dedup is intentionally NOT applied to operator-initiated
    # paths (refire, rest_generate_async, ws_generate_product) — those
    # are explicit re-runs and should NEVER be skipped on dedup grounds.
    # SITEMP fanout children carry source="sitemp_fanout" with a
    # per-COA discriminator (coa_id) — those also bypass dedup because
    # the per-COA chain head is distinct from the product-type lookup.
    SWEEP_DEDUP_SOURCES = ("autodraft_sweep",)
    if (
        source in SWEEP_DEDUP_SOURCES
        and not force
        and _pr.has_active_chain_for_product(product_type, project_id)
    ):
        log.info(
            "_autodraft_one_registered: skipping %s — has_active_chain_for_product "
            "True (sweep dedup; operator paths bypass this check; force=%s)",
            product_type, force,
        )
        return {
            "product_type": product_type,
            "artifact_id": "",
            "autodraft_session_id": "",
            "fired_at_ms": int(time.time() * 1000),
            "error": "",
            "_skipped": "active_chain_exists",
        }

    captured: dict[str, Any] = {}

    # Mint the session_id HERE (one level above register_and_run) so it can
    # be stamped onto the ProductRecord at creation. Previously this UUID
    # was minted inside _autodraft_one — after the record already existed
    # with autodraft_session_id=None — which is the field-gap PR #63
    # worked around. Lifting the mint up removes the gap entirely.
    autodraft_session_id = f"autodraft-{uuid.uuid4()}"

    async def _coro(record):
        entry = await _autodraft_one(
            product_type,
            project_id=project_id,
            coa_focus=coa_focus,
            target_focus=target_focus,
            _registered_record=record,
            _session_id=autodraft_session_id,
        )
        captured["entry"] = entry
        return entry

    # Stamp the target binding onto source_metadata so the dashboard's
    # per-target row renderer can read target_entity_id off the chain
    # without touching the writer artifact (see WhiteCellHome.tsx
    # Name-cell phase-1 wiring in map-only-target-products.md). The
    # caller's source_metadata wins on conflict — defensive merge so a
    # caller that already set target_entity_id (e.g. a manual harness)
    # is not overwritten.
    merged_source_metadata = dict(source_metadata or {})
    # PROJECT threading — retries and dashboards resolve the fire's
    # project from here (same contract as the signal autofires).
    if project_id:
        merged_source_metadata.setdefault("project_id", project_id)
    if target_focus and target_focus.get("entity_id"):
        merged_source_metadata.setdefault(
            "target_entity_id", target_focus.get("entity_id")
        )

    record_id = await _pr.register_and_run(
        source=source,
        product_type=product_type,
        request_payload={
            "product_type": product_type,
            "project_id": project_id,
            "coa_focus": coa_focus or {},
            "target_focus": target_focus or {},
        },
        coro_factory=_coro,
        coa_id=(coa_focus or {}).get("coa_id"),
        coa_label=(coa_focus or {}).get("coa_label"),
        product_parent_record_id=product_parent_record_id,
        source_metadata=merged_source_metadata,
        autodraft_session_id=autodraft_session_id,
    )

    # Wait for the registered task to finish so the caller sees the
    # same blocking semantics _autodraft_one had. register_and_run
    # returns the record_id immediately AFTER scheduling the task —
    # await it here to preserve the legacy "fire returns when complete"
    # behavior.
    task = _pr._RUNNING_TASKS.get(record_id)  # type: ignore[attr-defined]
    if task is not None:
        try:
            await task
        except (Exception, asyncio.CancelledError):
            # register_and_run's except-arm already transitioned the
            # record to failed/cancelled and appended a JobEvent.
            # Don't re-raise — let captured["entry"] carry the legacy
            # error shape if present, else synthesize one.
            pass

    # Fall back to a synthesized failure entry if _autodraft_one
    # raised before populating entry (cancel propagated through).
    if "entry" not in captured:
        captured["entry"] = {
            "product_type": product_type,
            "artifact_id": None,
            "autodraft_session_id": None,
            "fired_at_ms": int(time.time() * 1000),
            "error": "cancelled or interrupted before completion",
        }
    return captured["entry"]


async def _autodraft_sitemp_fanout(force: bool = False,
                                     project_id: str = "") -> list[dict]:
    """Fan out one SITEMP autodraft per COA in the latest Threat COA Statement artifact.

    Per ATP 2-01.3 ¶6-15 the analyst constructs as many situation
    templates as necessary to depict the full range of threat courses of
    action. Our Threat COA Statement schema emits 3 COAs (rank=1 most-likely, rank=2
    intermediate, rank=3 most-dangerous), so 3 SITEMPs land — each
    parameterized to author per-COA templated_units, EAs, NAIs, TPLs.

    If no Threat COA Statement is in the pool yet, fires nothing (returns empty list);
    the dep-aware sweep loop reschedules SITEMP for the next pass once
    Threat COA Statement lands. If the latest Threat COA Statement carries 0 COAs, fires one generic
    SITEMP (legacy back-compat) so the artifact still exists.

    `force` (optional, default False) is threaded through to each per-COA
    `_autodraft_one_registered` call — bypasses the autodraft-pool dedup
    so an operator refire can re-fan the fanout even when a prior fanout's
    children are still in-flight.
    """
    import json as _json
    from pathlib import Path as _Path
    from handlers.product import UPLOAD_DIR

    pool = _autodraft_pool_index(project_id)
    threat_coa_statement_entries = pool.get("Threat COA Statement") or []
    if not threat_coa_statement_entries:
        log.info("sitemp fanout: no Threat COA Statement in pool yet — deferring SITEMP")
        return []

    latest_threat_coa_statement = threat_coa_statement_entries[0] or {}
    threat_coa_statement_aid = latest_threat_coa_statement.get("artifact_id") or ""
    threat_coa_statement_dir = _Path(UPLOAD_DIR) / "_autodraft" / threat_coa_statement_aid
    schema_state = threat_coa_statement_dir / "schema_state.json"
    coas: list[dict] = []
    # Header carried by the upstream Threat COA Statement artifact —
    # session-scoped fields (phase, area_of_operations) propagate to each
    # SITEMP fan-out child so the per-COA SITEMP renders at the same
    # operational phase as its parent. Without this propagation the SITEMP
    # _build_skeleton fallback leaves header.phase = "" (C7 audit finding).
    threat_coa_statement_header: dict = {}
    try:
        if schema_state.is_file():
            data = _json.loads(schema_state.read_text())
            coas = list((data.get("structured_data") or {}).get("coas") or [])
            threat_coa_statement_header = dict(
                (data.get("structured_data") or {}).get("header") or {}
            )
    except Exception as e:
        log.warning("sitemp fanout: Threat COA Statement schema_state unreadable %s: %s",
                    schema_state, e)

    # Session-scoped phase from the upstream Threat COA Statement. The
    # Threat COA Statement carries header.phase as a session field (the
    # phase of the friendly operation the analyst was on when the
    # parent product fired); each per-COA SITEMP inherits the same
    # value so the published artifacts read consistently. Empty string
    # when the parent header doesn't carry it (cold-start ingest race).
    threat_coa_statement_phase = (threat_coa_statement_header.get("phase") or "").strip()

    # Prefer Threat COAs.coa_slides[].coa_id as the source-of-truth for
    # fanout coa_ids when the Threat COAs artifact exists and shares the
    # same upstream Threat COA Statement. This makes the SITEMP source-
    # bucket coa_ids match what the sidebar reads from the OperationsMap
    # /current endpoint. Falls back to Threat COA Statement coas[] when
    # Threat COAs hasn't run yet.
    try:
        threat_coas_entries = pool.get("Threat COAs") or []
        if threat_coas_entries:
            _tc = threat_coas_entries[0] or {}
            _tc_aid = _tc.get("artifact_id") or ""
            _tc_dir = _Path(UPLOAD_DIR) / "_autodraft" / _tc_aid
            _tc_schema = _tc_dir / "schema_state.json"
            if _tc_schema.is_file():
                _tc_data = _json.loads(_tc_schema.read_text())
                _tc_sd = _tc_data.get("structured_data") or {}
                _tc_header = _tc_sd.get("header") or {}
                _linked_aid = (
                    _tc_header.get("threat_coa_statement_artifact_id") or ""
                )
                # Only adopt Threat COAs coa_ids when it links to OUR
                # latest Threat COA Statement — otherwise the older
                # Threat COAs would override a fresh Threat COA Statement.
                if _linked_aid == threat_coa_statement_aid:
                    _slides = list(_tc_sd.get("coa_slides") or [])
                    if _slides:
                        # Replace coa_ids verbatim from coa_slides[] but
                        # preserve the per-COA payload from coas (label /
                        # rank / coa_type) where the keys match.
                        _coas_by_id = {
                            c.get("coa_id"): c for c in coas
                            if isinstance(c, dict)
                        }
                        _rebuilt: list[dict] = []
                        for _slide in _slides:
                            if not isinstance(_slide, dict):
                                continue
                            _cid = _slide.get("coa_id")
                            if not _cid:
                                continue
                            _base = dict(_coas_by_id.get(_cid) or {})
                            _base["coa_id"] = _cid
                            _rebuilt.append(_base)
                        if _rebuilt:
                            log.info(
                                "sitemp fanout: adopted %d coa_ids from "
                                "Threat COAs %s (linked to Threat COA "
                                "Statement %s)",
                                len(_rebuilt), _tc_aid,
                                threat_coa_statement_aid,
                            )
                            coas = _rebuilt
    except Exception as _e:
        log.warning(
            "sitemp fanout: Threat COAs coa_slides resolution failed "
            "(falling back to Threat COA Statement coas[]): %s", _e,
        )

    # Resolve the unified parent record so children carry
    # product_parent_record_id back to their Threat COA Statement chain
    # head. Looks up the most-recent Threat-COA-Statement record_id from
    # the registry; None means the parent fired pre-registry (lifespan
    # synth) and children won't carry a registry-level parent link.
    parent_record_id: str | None = None
    try:
        from handlers import product_records as _pr
        for r in reversed(list(_pr._RECORDS.values())):  # type: ignore[attr-defined]
            if r.product_type == "Threat COA Statement" and r.state == "complete":
                parent_record_id = r.record_id
                break
    except Exception:
        log.debug("sitemp fanout: unified parent lookup skipped", exc_info=True)

    if not coas:
        log.info("sitemp fanout: Threat COA Statement %s has no coas — firing single SITEMP",
                  threat_coa_statement_aid)
        return [await _autodraft_one_registered(
            "SITEMP",
            project_id=project_id,
            product_parent_record_id=parent_record_id,
            source="sitemp_fanout",
            force=force,
        )]

    log.info("sitemp fanout: firing %d SITEMP(s) for Threat COA Statement %s phase=%r",
              len(coas), threat_coa_statement_aid, threat_coa_statement_phase)

    # Sibling coa_ids — every COA in THIS fanout pass, stamped on each
    # focus dict so the master-map stale-bucket sweep (intel_agent
    # tool_render_map) knows which compound source buckets are part of
    # the active fanout and MUST NOT be wiped. Without this list, the
    # sweep treats every other COA's bucket as stale → only the
    # last-fired COA survives on the master map (the "all SITEMPs look
    # identical when toggled" bug).
    all_coa_focus_ids = [
        (c.get("coa_id") or "").strip()
        for c in coas
        if isinstance(c, dict) and (c.get("coa_id") or "").strip()
    ]

    # Clear any stale fanout-cancel signal from a prior parent. The
    # signal is set by product_records.cancel_record when a Threat-COA-
    # Statement parent gets cancelled OR when one of these children
    # gets cancelled — either case invalidates the rest of the fanout.
    try:
        from handlers import product_records as _pr
        _pr.clear_fanout_cancelled()
    except Exception:
        pass

    entries: list[dict] = []
    for coa in coas:
        if not isinstance(coa, dict):
            continue
        # Cascade cancel — if a prior child of this fanout was
        # cancelled (or the parent was cancelled), abort the rest.
        # Synthesize cancelled entries so the catalog rows aren't
        # silently missing.
        try:
            from handlers import product_records as _pr
            if _pr.fanout_cancelled().is_set():
                log.info("sitemp fanout: cancelled mid-pass; skipping remaining "
                          "COAs (%d remaining)", len(coas) - len(entries))
                entries.append({
                    "product_type": "SITEMP",
                    "artifact_id": None,
                    "autodraft_session_id": None,
                    "fired_at_ms": int(time.time() * 1000),
                    "error": "Cancelled — SITEMP fanout aborted by upstream cancel",
                    "coa_id": coa.get("coa_id") or "",
                })
                continue
        except Exception:
            pass
        # coa_type carries through from upstream so the SITEMP header's
        # coa_type enum cell ([offense, defense, irregular, hybrid]) is
        # mechanically stamped from the doctrinal posture the Threat COA
        # Statement writer assigned — no second LLM call to re-derive it.
        focus = {
            "coa_id": coa.get("coa_id") or "",
            "coa_label": coa.get("label") or "",
            "rank": coa.get("rank"),
            "most_dangerous": bool(coa.get("most_dangerous")),
            "threat_coa_statement_artifact_id": threat_coa_statement_aid,
            "coa_type": (coa.get("coa_type") or "").strip(),
            "phase": threat_coa_statement_phase,
            "all_coa_focus_ids": list(all_coa_focus_ids),
        }
        entry = await _autodraft_one_registered(
            "SITEMP",
            project_id=project_id,
            coa_focus=focus,
            product_parent_record_id=parent_record_id,
            source="sitemp_fanout",
            force=force,
        )
        entry["coa_id"] = focus["coa_id"]
        entries.append(entry)
    return entries


async def ensure_pool_populated(*, project_id: str = "",
                                  throttle_sec: float = 0.5,
                                  max_passes: int = 3,
                                  skip_health_gate: bool = False) -> dict:
    """Cache warmup: fire every rollup product missing from the autodraft
    pool. Existing entries (clean OR poisoned) are left alone — operator
    handles refresh via `refresh_drafts`.

    Dep-aware, multi-pass: a product fires only when every dep it
    consumes is in the pool. Loop up to `max_passes` times so consumers
    deferred behind a producer get a shot after the producer lands.
    Three passes covers the current DAG depth (producers → SITEMP/MCOO/
    Threat COA Statement → Event Template / Threat Overlay → DST / ICM / SITREP).

    Sequential within a pass so parallel writer LLM calls don't saturate
    vLLM and block analyst Generate clicks. `throttle_sec` is a short
    sleep between fires; 0 disables.

    `skip_health_gate` — operator-driven invocations (the /resume endpoint)
    set this True so the sweep proceeds even if the probe is flaky; the
    sweep's per-fire error capture will still surface a cold vllm as
    errors=N in the response.

    Returns roll-up of fire / defer / error counts across passes.
    """
    from templates import PRODUCT_SCHEMAS

    _LAST_SWEEP_STATUS["state"] = "running"
    _LAST_SWEEP_STATUS["started_at_ms"] = int(time.time() * 1000)
    _LAST_SWEEP_STATUS["completed_at_ms"] = None
    _LAST_SWEEP_STATUS["last_error"] = None

    schemas = dict(PRODUCT_SCHEMAS)
    ordered = _topological_product_order(schemas)
    eligible = [p for p in ordered if p not in _SKIP_PRODUCTS]

    fired_total = 0
    errors_total = 0
    deferred: list[str] = []
    per_pass: list[dict] = []

    for pass_idx in range(max_passes):
        pool = _autodraft_pool_index(project_id)
        fired_this_pass = 0
        errors_this_pass = 0
        attempted_this_pass = 0
        deferred = []
        for product_type in eligible:
            if product_type in pool:
                continue  # cache hit — nothing to do
            schema = schemas.get(product_type) or {}
            deps = [d for d in (schema.get("consumes_artifacts") or [])
                    if d in schemas and d not in _SKIP_PRODUCTS]
            missing_deps = [d for d in deps if d not in pool]
            if missing_deps:
                deferred.append(product_type)
                log.info(
                    "ensure_pool_populated pass %d: defer %s (deps not ready: %s)",
                    pass_idx + 1, product_type, missing_deps,
                )
                continue
            attempted_this_pass += 1
            try:
                if product_type == "SITEMP":
                    # Fan out: one SITEMP per Threat COA Statement COA per ATP 2-01.3
                    # ¶6-15 (one situation template per threat course of
                    # action). The COAs come from the latest Threat COA Statement artifact
                    # — guaranteed to be in the pool by topo sort since
                    # SITEMP consumes_artifacts includes Threat COA Statement.
                    entries = await _autodraft_sitemp_fanout(project_id=project_id)
                    fired_this_pass += sum(1 for e in entries if e and not e.get("error"))
                    errors_this_pass += sum(1 for e in entries if e and e.get("error"))
                    if throttle_sec > 0:
                        await asyncio.sleep(throttle_sec)
                    continue
                # Cancel-cascade gate (V3 lock-in): if the analyst
                # cancelled a sweep-source record, every remaining
                # unstarted product in this pass is invalid by
                # construction. Break early — _SWEEP_CANCELLED is
                # cleared at the top of the next sweep call.
                try:
                    from handlers import product_records as _pr
                    if _pr.sweep_cancelled().is_set():
                        log.info("ensure_pool_populated: sweep cancelled — "
                                  "aborting pass at %s", product_type)
                        break
                except Exception:
                    pass
                entry = await _autodraft_one_registered(product_type,
                                                          project_id=project_id)
                if entry and entry.get("error"):
                    errors_this_pass += 1
                elif entry and entry.get("artifact_id"):
                    fired_this_pass += 1
                    # Re-add to pool so consumers later in the same pass can
                    # see this artifact without waiting for the next pass.
                    pool.setdefault(product_type, []).insert(0, {
                        "session_id": entry.get("autodraft_session_id"),
                        "artifact_id": entry.get("artifact_id"),
                        "from": "_fire_history",
                        "dispatch": None,
                    })
            except Exception:
                log.exception(
                    "ensure_pool_populated pass %d: _autodraft_one raised for %s",
                    pass_idx + 1, product_type,
                )
                errors_this_pass += 1
            if throttle_sec > 0:
                await asyncio.sleep(throttle_sec)

        per_pass.append({
            "pass": pass_idx + 1,
            "fired": fired_this_pass,
            "errors": errors_this_pass,
            "attempted": attempted_this_pass,
            "deferred": list(deferred),
        })
        fired_total += fired_this_pass
        errors_total += errors_this_pass
        log.info(
            "ensure_pool_populated pass %d complete: attempted=%d fired=%d "
            "errors=%d deferred=%d (%s)",
            pass_idx + 1, attempted_this_pass, fired_this_pass,
            errors_this_pass, len(deferred), deferred,
        )
        # Loop-exit semantics — three cases, only ONE of them means "stop":
        #   1. attempted_this_pass == 0  → nothing left to do (everything's
        #      cached or remaining deferred products have unmet deps that
        #      will never resolve). Stop.
        #   2. fired_this_pass == 0 AND errors_this_pass > 0  → every fire
        #      this pass errored. The most common cause is cold vllm at
        #      startup. We CONTINUE — the next pass retries the same set;
        #      vllm may have warmed by then. Burns through max_passes
        #      rather than exiting silently with an empty pool.
        #   3. fired_this_pass > 0  → real progress; keep looping so
        #      consumers deferred behind producers get their shot.
        if attempted_this_pass == 0:
            log.info(
                "ensure_pool_populated pass %d: nothing attempted "
                "(everything cached or impossible deps); stopping",
                pass_idx + 1,
            )
            break
        if fired_this_pass == 0 and errors_this_pass > 0:
            log.warning(
                "ensure_pool_populated pass %d: 0 fires landed, %d errors — "
                "continuing to next pass (vllm may still be warming)",
                pass_idx + 1, errors_this_pass,
            )
            # Throttle between fully-failed passes so we don't slam vllm
            # while it's mid-load. 10s is enough breathing room without
            # blowing the startup budget across all max_passes.
            await asyncio.sleep(10.0)

    log.info(
        "ensure_pool_populated complete: total=%d eligible=%d passes=%d "
        "fired=%d errors=%d deferred=%d",
        len(schemas), len(eligible), len(per_pass),
        fired_total, errors_total, len(deferred),
    )
    result = {
        "products_total": len(schemas),
        "products_eligible": len(eligible),
        "passes_run": len(per_pass),
        "fired": fired_total,
        "errors": errors_total,
        "deferred_final": deferred,
        "per_pass": per_pass,
    }
    _LAST_SWEEP_STATUS["state"] = "complete" if fired_total > 0 or errors_total == 0 else "failed"
    _LAST_SWEEP_STATUS["completed_at_ms"] = int(time.time() * 1000)
    _LAST_SWEEP_STATUS["last_result"] = result
    _LAST_SWEEP_STATUS["next_retry_at_ms"] = None
    _LAST_SWEEP_STATUS["retry_attempt"] = 0
    return result


async def refire_one_product_type(pt: str, mode: str = "full",
                                    project_id: str = "") -> dict:
    """Generate ONE fresh autodraft for a single `pt` product type.

    Canonical single-product entry point used by the
    `/api/artifacts/{aid}/refire-immediate-downstream` finalize cascade —
    the analyst finalizes upstream A, the SPA POSTs the cascade modal's
    selected downstream B, and this function fires exactly one new B
    artifact against the now-updated upstream A's schema_state.

    For single-product types: wraps `_autodraft_one(pt)`. For SITEMP:
    wraps `_autodraft_sitemp_fanout()` (fans out one SITEMP per Threat COA
    Statement COA per ATP 2-01.3 ¶6-15).

    Returns the same shape `/api/diag/product_autodraft/refire` returns:
        {requested: [pt], unknown: [...], fired: [entry,...], errors: [...]}
    so the cascade response and the diag refire response have identical
    semantics (one downstream caller can consume either).

    `mode` is accepted for signature parity with the diag endpoint but is
    not yet exercised here — single-product cascade refires are always
    full DAG (LLM) regen. Map-only / export-only refires go through the
    diag endpoint which takes a multi-product list.
    """
    from templates import PRODUCT_SCHEMAS

    fired: list[dict] = []
    unknown: list[str] = []
    errors: list[dict] = []
    if pt not in PRODUCT_SCHEMAS:
        unknown.append(pt)
        return {
            "requested": [pt],
            "unknown": unknown,
            "fired": fired,
            "errors": errors,
        }
    try:
        if pt == "SITEMP":
            # SITEMP is per-COA — fan out across the latest Threat COA
            # Statement's COAs so the cascade emits N per-COA SITEMPs.
            # _autodraft_sitemp_fanout also back-fills Threat COA
            # Statement's coas[i].sitemp_artifact_id and re-renders the
            # Threat COA Statement so the per-COA SITEMP maps embed in
            # the published Threat COA Statement artifact.
            entries = await _autodraft_sitemp_fanout(project_id=project_id)
            fired.extend(entries)
        else:
            entry = await _autodraft_one_registered(pt, source="refire",
                                                     project_id=project_id)
            fired.append(entry)
    except Exception as exc:
        log.exception("refire_one_product_type: _autodraft_one raised for %s", pt)
        errors.append({"product_type": pt, "error": f"{type(exc).__name__}: {exc}"})
    return {
        "requested": [pt],
        "unknown": unknown,
        "fired": fired,
        "errors": errors,
    }


async def refresh_drafts(product_types: list[str], by: str = "",
                         target_focus: dict | None = None,
                         force: bool = False,
                         project_id: str = "") -> dict:
    """Generate a FRESH autodraft for each requested `product_types`
    WITHOUT touching the existing one. Each call produces an
    additional artifact_id; prior autodrafts remain on disk + in the
    DB pool for comparison across iterations.

    Use after deploying code (writer prompts, renderer, fixtures) that
    changes what a fresh draft would look like. Exposed via
    POST /api/diag/product_autodraft/refire?products=A,B,C.

    For each product_type calls _autodraft_one directly — fires a
    new session, new artifact_id, new on-disk dir. ensure_pool_populated
    is NOT called (it would no-op since the pool is already populated).

    `by` is the analyst email (attribution for the completion
    notification). Per-product state transitions (queued / running /
    complete / failed) flow through product_records register_and_run
    inside _autodraft_one_registered; a `product_generated` notification
    is broadcast on success or failure either way.

    `target_focus` (optional) — single-target binding from the operations
    map right-click. Carries {entity_id, side, ...} for the bound target.
    Threaded only into per-target product fires (currently TIDAT only —
    TMP/CDE/BDA wired in phase-2). Ignored for SITEMP (which uses
    coa_focus fanout) and for non-per-target products.

    `force` (optional, default False) — bypasses the autodraft-pool
    dedup gate (`_autodraft_one_registered` + `_autodraft_sitemp_fanout`
    would otherwise skip when an active chain for the same product_type
    already exists). Passed through unchanged to both fan-outs. Use
    when the caller has already made a UX-level "yes, refire even if
    one's running" decision (verify/refire endpoints).

    Returns per-product fire results so the caller can chase the new
    artifact_ids.
    """
    from templates import PRODUCT_SCHEMAS

    fired: list[dict] = []
    unknown: list[str] = []
    errors: list[dict] = []
    for pt in product_types:
        if pt not in PRODUCT_SCHEMAS:
            unknown.append(pt)
            continue
        try:
            if pt == "SITEMP":
                # SITEMP is per-COA — fan out across the latest Threat COA Statement's COAs
                # so the published artifact is N per-COA SITEMPs (each
                # parameterized with its COA focus), not one generic one.
                # _autodraft_sitemp_fanout also back-fills Threat COA Statement's
                # coas[i].sitemp_artifact_id and re-renders Threat COA Statement so the
                # per-COA SITEMP maps embed in the published Threat COA Statement artifact.
                entries = await _autodraft_sitemp_fanout(force=force,
                                                           project_id=project_id)
                fired.extend(entries)
                emitted = next((e for e in entries if e.get("artifact_id")), None)
                ok = emitted is not None
                artifact_id = (emitted or {}).get("artifact_id") or ""
                session_id = (emitted or {}).get("autodraft_session_id") or ""
                err = "" if ok else (
                    "; ".join(str(e.get("error")) for e in entries if e.get("error"))
                    or "SITEMP fan-out produced no artifact")
            else:
                entry = await _autodraft_one_registered(
                    pt, source="rest_generate_async",
                    project_id=project_id,
                    target_focus=target_focus,
                    source_metadata={"by": by},
                    force=force,
                )
                fired.append(entry)
                ok = bool(entry.get("artifact_id")) and not entry.get("error")
                artifact_id = entry.get("artifact_id") or ""
                session_id = entry.get("autodraft_session_id") or ""
                err = str(entry.get("error") or "")
            await _emit_completion(pt, ok, artifact_id, session_id, by, err)
        except Exception as exc:
            log.exception("refresh_drafts: _autodraft_one raised for %s", pt)
            msg = f"{type(exc).__name__}: {exc}"
            errors.append({"product_type": pt, "error": msg})
            await _emit_completion(pt, False, "", "", by, msg)
    return {
        "requested": list(product_types),
        "unknown": unknown,
        "fired": fired,
        "errors": errors,
    }


async def refire_product_maps(product_types: list[str],
                                project_id: str = "") -> dict:
    """Re-render the map image(s) of existing autodrafts without re-running
    product generation. Reads `render_params_<field>.json` cached by
    `tool_render_map` in the latest autodraft dir per product, then calls
    `render_map_to_png` directly with those exact params and overwrites the
    image in place. The PPTX / HTML / schema_state.json are untouched.

    Iteration helper for renderer work — edit JS, push, rebuild nginx,
    `mode=map_only` refire, look at the new image. No LLM calls.

    Caller: POST /api/diag/product_autodraft/refire?products=...&mode=map_only.
    """
    import json as _json
    from pathlib import Path as _Path
    from uuid import UUID as _UUID
    from handlers.product import UPLOAD_DIR
    from templates import PRODUCT_SCHEMAS
    from utils.render import render_map_dispatch, RenderOptions

    pool = _autodraft_pool_index(project_id)
    out: dict[str, dict] = {}
    unknown: list[str] = []

    for pt in product_types:
        if pt not in PRODUCT_SCHEMAS:
            unknown.append(pt)
            continue
        entries = pool.get(pt) or []
        if not entries:
            out[pt] = {"status": "no_autodraft", "fields": []}
            continue
        latest_aid = (entries[0] or {}).get("artifact_id")
        if not latest_aid:
            out[pt] = {"status": "no_artifact_id", "fields": []}
            continue
        adir = _Path(UPLOAD_DIR) / "_autodraft" / latest_aid
        if not adir.is_dir():
            out[pt] = {"status": "no_autodraft_dir",
                       "expected": str(adir), "fields": []}
            continue
        param_files = sorted(adir.glob("render_params_*.json"))
        if not param_files:
            out[pt] = {"status": "no_cached_params",
                       "hint": "regenerate once with mode=full to populate cache",
                       "fields": []}
            continue
        per_field: list[dict] = []
        for pf in param_files:
            try:
                params = _json.loads(pf.read_text())
            except Exception as e:
                per_field.append({"field": pf.stem, "status": "params_unreadable",
                                  "error": f"{type(e).__name__}: {e}"})
                continue
            field = params.get("field_name") or pf.stem.replace("render_params_", "")
            img_relpath = params.get("image_relpath") or f"{field}.jpg"
            img_path = adir / img_relpath
            # Reconstruct RenderOptions from the schema, NOT from cached
            # params. Cached params only carry the per-instance bbox /
            # dimensions + map_uuid; the schema-driven primitives
            # (basemap, layer visibility, kind allowlist, etc.) come
            # from the active product schema so this stays in lockstep
            # with tool_render_map's build.
            from templates import PRODUCT_SCHEMAS
            _pt = params.get("product_type") or pt
            _schema = PRODUCT_SCHEMAS.get(_pt, {}) or {}
            _kinds = _schema.get("map_overlay_kinds")
            opts = RenderOptions(
                width=int(params.get("width") or 2560),
                height=int(params.get("height") or 1440),
                bbox=params.get("bbox"),
                layers=list(_kinds) if isinstance(_kinds, (list, tuple)) and _kinds else None,
                product_type=_pt,
                basemap=_schema.get("map_basemap"),
                basemap_layer_visibility=_schema.get("map_basemap_layer_visibility"),
                doctrinal_layer_visibility=_schema.get("map_doctrinal_layer_visibility"),
                show_topographic_contours=_schema.get("map_show_topographic_contours"),
                cluster_mil_units=_schema.get("map_cluster_mil_units"),
                compact_aa_labels=_schema.get("map_compact_aa_labels"),
                compact_legend_labels=_schema.get("map_compact_legend_labels"),
            )
            try:
                # Refresh the geo-agent map record's schema-derived attrs
                # (basemap / allowed_kinds / doctrinal layer visibility)
                # so the embedded EditMap overlay opens on the correct
                # state after a renderer-iteration refire. Cheap PUT;
                # idempotent. Mirrors what tool_render_map does on the
                # full path.
                from handlers.intel_agent import upsert_geo_agent_map_record
                upsert_geo_agent_map_record(
                    map_uuid=str(params["map_uuid"]),
                    product_type=_pt,
                    field_name=field,
                    bbox=params.get("bbox") or [],
                    schema=_schema,
                )
                png_bytes = await render_map_dispatch(
                    _UUID(str(params["map_uuid"])), opts
                )
                img_path.write_bytes(png_bytes)
                per_field.append({"field": field, "status": "rerendered",
                                  "bytes": len(png_bytes),
                                  "image_path": str(img_path)})
            except Exception as e:
                per_field.append({"field": field, "status": "render_failed",
                                  "error": f"{type(e).__name__}: {e}"})
        out[pt] = {"status": "ok", "artifact_id": latest_aid,
                    "fields": per_field}

    return {"mode": "map_only", "requested": list(product_types),
            "unknown": unknown, "results": out}


async def refire_product_exports(product_types: list[str],
                                   project_id: str = "") -> dict:
    """Re-export the deliverable (HTML + DOCX/PPTX) for every existing
    autodraft of the requested products. Reads the structured_data out of
    the per-session State registry (same source the original render used)
    and pipes it through `render_artifact_files`. No LLM. No map regen.

    Use when the exporter itself changed (table-style fixes, XML diet,
    new chrome) and you want every existing artifact to pick up the new
    output without re-running the writer. Cheaper than mode=full
    (no LLM tokens, no new artifact_id, no DB churn) but heavier than
    mode=map_only (the full PPTX zip is rewritten).

    Caller: POST /api/diag/product_autodraft/refire?products=...&mode=export_only.
    """
    from handlers.product import render_artifact_files
    from templates import PRODUCT_SCHEMAS

    pool = _autodraft_pool_index(project_id)
    out: dict[str, dict] = {}
    unknown: list[str] = []

    for pt in product_types:
        entries = pool.get(pt) or []
        if not entries:
            out[pt] = {"status": "no_autodraft"}
            continue
        per_artifact: list[dict] = []
        for e in entries:
            sid = e.get("session_id")
            aid = e.get("artifact_id")
            if not sid or not aid:
                per_artifact.append({"artifact_id": aid, "status": "missing_ids"})
                continue
            try:
                result = render_artifact_files(
                    session_id=sid,
                    artifact_id=aid,
                    artifact_type=pt,
                )
                per_artifact.append({
                    "artifact_id": aid,
                    "status": result.get("status", "unknown"),
                    "htmlLink": result.get("htmlLink"),
                    "docxLink": result.get("docxLink"),
                    "pptxLink": result.get("pptxLink"),
                    "error": result.get("error"),
                })
            except Exception as exc:
                log.exception("refire_product_exports: %s/%s failed", pt, aid)
                per_artifact.append({
                    "artifact_id": aid,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                })
        out[pt] = {"status": "ok", "artifacts": per_artifact}
        if pt not in PRODUCT_SCHEMAS:
            unknown.append(pt)

    return {"mode": "export_only", "requested": list(product_types),
            "unknown": unknown, "results": out}


async def sweep_for_project(project_id: str) -> dict:
    """Per-project product sweep — fire every rollup product missing
    from `project_id`'s pool. Invoked by the PROJECT CREATION bootstrap
    (handlers/projects) after the project's signal autofires
    (KLIEGLIGHT / OSIR sweeps) have run, so the rollup consumers
    (SIGSUM / INTSUM / GRINTSUM / IRE / CCA / OED) find their single-INT
    deps already in the pool. There is no startup-time global sweep
    anymore — pools are per project and fill when the project is
    created. Health-gates on vllm with the same deferred-backoff
    behavior the old startup sweep had.
    """
    return await _gated_sweep_or_defer(project_id)


async def _gated_sweep_or_defer(project_id: str) -> dict:
    """Health-gate the sweep. If vllm responds, run ensure_pool_populated
    synchronously and return its result. If not, schedule an exponential-
    backoff retry on a background task and return a "deferred" descriptor
    so the caller (startup hook) doesn't block on the wait.
    """
    if _vllm_healthy():
        log.info("product_autodraft: vllm healthy, running sweep project=%s",
                 project_id)
        return await ensure_pool_populated(project_id=project_id)

    # Cold vllm — defer behind backoff.
    delay_sec = _SWEEP_RETRY_BACKOFF_SEC[0]
    next_retry_ms = int(time.time() * 1000) + int(delay_sec * 1000)
    log.warning(
        "product_autodraft: vllm cold; deferring sweep by %ds "
        "(retry schedule %s, operator can force-run via "
        "POST /api/diag/product_autodraft/resume)",
        delay_sec, _SWEEP_RETRY_BACKOFF_SEC,
    )
    _LAST_SWEEP_STATUS["state"] = "deferred"
    _LAST_SWEEP_STATUS["next_retry_at_ms"] = next_retry_ms
    _LAST_SWEEP_STATUS["retry_attempt"] = 0
    _LAST_SWEEP_STATUS["last_error"] = "vllm health probe failed"
    asyncio.create_task(
        _deferred_sweep_retry_loop(project_id),
        name="product_autodraft_deferred_sweep",
    )
    return {
        "state": "deferred",
        "reason": "vllm health probe failed",
        "next_retry_in_sec": delay_sec,
        "retry_schedule_sec": list(_SWEEP_RETRY_BACKOFF_SEC),
        "operator_resume_endpoint": "POST /api/diag/product_autodraft/resume",
    }


async def _deferred_sweep_retry_loop(project_id: str) -> None:
    """Background task spawned when the startup sweep is deferred behind
    a cold vllm. Walks _SWEEP_RETRY_BACKOFF_SEC, probing vllm before each
    attempt. Stops as soon as a sweep completes (state == complete) OR
    the backoff schedule is exhausted (operator must hit /resume).

    Idempotent across operator resumes: if /resume runs the sweep
    successfully while this task is sleeping, the next wake checks
    _LAST_SWEEP_STATUS and exits.
    """
    for attempt, delay_sec in enumerate(_SWEEP_RETRY_BACKOFF_SEC, start=1):
        _LAST_SWEEP_STATUS["retry_attempt"] = attempt
        _LAST_SWEEP_STATUS["next_retry_at_ms"] = (
            int(time.time() * 1000) + int(delay_sec * 1000)
        )
        log.info(
            "product_autodraft deferred-retry: attempt %d/%d sleeping %ds",
            attempt, len(_SWEEP_RETRY_BACKOFF_SEC), delay_sec,
        )
        await asyncio.sleep(delay_sec)
        # If an operator-driven /resume completed in the meantime, bail.
        if _LAST_SWEEP_STATUS.get("state") == "complete":
            log.info(
                "product_autodraft deferred-retry: sweep already completed "
                "(operator /resume or prior attempt); exiting backoff loop",
            )
            return
        if not _vllm_healthy():
            log.warning(
                "product_autodraft deferred-retry: attempt %d/%d — vllm "
                "still cold, scheduling next backoff",
                attempt, len(_SWEEP_RETRY_BACKOFF_SEC),
            )
            continue
        log.info(
            "product_autodraft deferred-retry: attempt %d/%d — vllm healthy, "
            "running sweep",
            attempt, len(_SWEEP_RETRY_BACKOFF_SEC),
        )
        try:
            await ensure_pool_populated(project_id=project_id)
            return
        except Exception:
            log.exception(
                "product_autodraft deferred-retry: ensure_pool_populated raised "
                "on attempt %d/%d",
                attempt, len(_SWEEP_RETRY_BACKOFF_SEC),
            )
            _LAST_SWEEP_STATUS["state"] = "failed"
            _LAST_SWEEP_STATUS["last_error"] = "ensure_pool_populated raised"
            # Continue to next backoff step rather than abort — vllm may
            # have flapped between the probe and the sweep.
    log.error(
        "product_autodraft deferred-retry: backoff schedule exhausted; "
        "pool may still be empty. Operator must POST "
        "/api/diag/product_autodraft/resume to retry.",
    )
    _LAST_SWEEP_STATUS["state"] = "failed"
    _LAST_SWEEP_STATUS["next_retry_at_ms"] = None
    _LAST_SWEEP_STATUS["last_error"] = (
        "backoff schedule exhausted; operator must hit /resume"
    )


async def resume_sweep(*, force: bool = False) -> dict:
    """Operator-driven: re-run the autodraft sweep on demand.

    Same function the startup hook calls, but bypasses the vllm health
    gate by default (the operator hit this BECAUSE they know vllm is up
    now). Use this when:
      - The startup sweep deferred behind a cold vllm and the backoff
        schedule exhausted before vllm finished warming.
      - You restarted vllm mid-exercise and want the pool refreshed.
      - You want to force a sweep without waiting for the next backoff.

    `force=True` is reserved for "I know what I'm doing, run it even if
    health probe fails" — currently same as default since we always skip
    the gate on operator-driven resumes. Kept as an explicit param so a
    future tightening (e.g. probe-required by default, force to override)
    doesn't break the signature.
    """
    log.info("product_autodraft resume_sweep: operator-driven sweep (force=%s)", force)
    from handlers.intel_agent import active_project_ids
    results = {}
    for pid in active_project_ids():
        results[pid] = await ensure_pool_populated(
            project_id=pid, skip_health_gate=True)
    return {"projects": results}
