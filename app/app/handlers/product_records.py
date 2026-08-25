"""Unified product-records registry — the source of truth for every
generation's live state across pytho-app.

Supersedes the three legacy in-memory fire-history / inflight
registries (product_autodraft._FIRE_HISTORY + _INFLIGHT,
klieglight_autofire._FIRE_HISTORY, osir_autofire._FIRE_HISTORY).
intel_agent._JOBS is preserved as a narrow chat-resilience refresh
hook (/api/jobs/{id}) and is no longer involved in product chains.
Every entry point (WS generate_product, REST generate-async,
autodraft sweep, SITEMP fanout, KLIEGLIGHT autofire, OSIR autofire,
refire) registers through this ProductRecord-shaped registry. See
scratch/tmp/jobs-system-design.md for the full design; see
scratch/tmp/jobs-legacy-deletion-checklist.md for the deletion sites
this module obsoletes.

Key shapes:

  - ProductRecord (in-memory dict, dataclass-style): the per-attempt
    row. Stable record_id, chain_head_id grouping retries, three states
    (running, complete, failed).
  - JobEvent (DB-persisted): every state transition appended forever.
    Lifespan replay reads this to rehydrate _RECORDS after restart.

Retry chain: the chain's `chain_head_id` is the record_id of the FIRST
attempt. Subsequent retries mint NEW record_ids that share the same
chain_head_id and carry parent_record_id pointing at the previous
attempt. The frontend collapses every record with the same
chain_head_id into ONE displayed row showing the LATEST attempt's
state.

Broadcast semantics: every record_transition acquires _BROADCAST_LOCK,
appends a JobEvent, increments _SEQ, and broadcasts a
product_record_upsert WS frame to every connected analyst's socket.
Frames arrive in monotonic seq order — frontend reducer dedups by seq.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
import weakref
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Literal, Optional

from utils import database

log = logging.getLogger(__name__)


# ─── states + source enums ──────────────────────────────────────────────

State = Literal["running", "complete", "failed"]
Source = Literal[
    "ws_generate_product",
    "rest_generate_async",
    "autodraft_sweep",
    "sitemp_fanout",
    "klieglight_autofire",
    "osir_autofire",
    "refire",
    "pre_registry",  # synthesized on lifespan merge for legacy artifacts
]
ErrorKind = Literal[
    "llm_oom",
    "writer_invalid",
    "map_render_failed",
    "cancelled",
    "gate_timeout",
    "server_restart",
    "internal",
]


# ─── ProductRecord dataclass ────────────────────────────────────────────

@dataclass
class ProductRecord:
    record_id: str
    chain_head_id: str
    parent_record_id: Optional[str]               # previous attempt of THIS chain (retries)
    product_parent_record_id: Optional[str]       # SITEMP-fanout child → parent record
    product_type: str
    coa_id: Optional[str]
    coa_label: Optional[str]
    source: Source
    source_metadata: dict
    state: State
    phase: str
    attempt_index: int
    started_at_ms: int
    gate_acquired_at_ms: Optional[int]
    writer_started_at_ms: Optional[int]
    ended_at_ms: Optional[int]
    duration_ms: Optional[int]
    artifact_id: Optional[str]
    autodraft_session_id: Optional[str]
    error: Optional[str]
    error_kind: Optional[ErrorKind]
    request_payload: dict
    # Soft-delete flag — set by delete_chain. Records are NEVER popped
    # from _RECORDS; the UI projection (list_chains / _chain_view /
    # has_active_chain_for_product) skips deleted rows. The audit log
    # (JobEvent) and replay path retain everything forever for data
    # analysis / forensic replay.
    is_deleted: bool = False
    # Wave progress for in-flight chains. Updated by the DAG executor
    # at each wave end via record_transition. 0-99 while running,
    # cleared on terminal transition. Dashboard reads this off the
    # product_record_upsert broadcast and renders 'Generating... XX%'.
    progress_pct: int = 0
    progress_wave: str = ""
    # Runtime-only — NOT serialized in REST/WS frames.
    gate_serial: Optional[int] = None
    cancel_event: Any = None                      # asyncio.Event
    # weakref to the running task so cancel_record can call task.cancel().
    # Cleared on terminal transition.
    _task_ref: Any = None


# ─── in-process state ───────────────────────────────────────────────────

# Records are kept FOREVER in memory (and forever in JobEvent on disk).
# delete_chain marks records is_deleted=True so the UI hides them but
# replay + data-analysis paths still see them. No eviction window, no
# count cap — per user requirement that nothing is ever discarded.
_RECORDS: "OrderedDict[str, ProductRecord]" = OrderedDict()
# Keyed by record_id; entries are removed on terminal+deleted transitions
# (see _maybe_release_record_lock) so the dict doesn't grow unbounded
# across long-running processes.
_RECORD_LOCKS: weakref.WeakValueDictionary[str, asyncio.Lock] = (
    weakref.WeakValueDictionary()
)
# Strong refs to locks for records that are still in flight. Released
# (and thus eligible for GC out of _RECORD_LOCKS) once a record reaches
# a terminal state AND has been soft-deleted, or simply on terminal
# transition for the simple case. Keep an entry here as long as any
# coroutine MIGHT still acquire the lock.
_RECORD_LOCK_STRONG: dict[str, asyncio.Lock] = {}
# Maps chain_head_id → latest attempt's record_id. The frontend
# collapses by chain head, so we keep this index for O(1) "latest of
# chain" lookups.
_CHAIN_LATEST: dict[str, str] = {}
_RUNNING_TASKS: dict[str, asyncio.Task] = {}

# Monotonic broadcast sequence. Set on every emission; frontend reducer
# dedups by this value.
_SEQ: int = 0

# Single lock to make seq-assignment + event-log append + WS broadcast
# atomic so frames arrive in strict monotonic order.
_BROADCAST_LOCK = asyncio.Lock()

# Sweep-cancel signal — set by cancel_record when the cancelled
# record's source is "autodraft_sweep". The sweep loop checks it at
# the top of each iteration and breaks early, synthesizing
# cancelled-by-cascade rows for not-yet-started products.
_SWEEP_CANCELLED = asyncio.Event()

# Fanout-cancel signal — set by cancel_record when cancelling a
# SITEMP-fanout parent (Threat COA Statement) OR when one of its
# children is cancelled. The fanout loop checks it before dispatching
# the next COA child and breaks early.
_FANOUT_CANCELLED = asyncio.Event()

# Shutdown-in-progress signal. Flipped by main.py's lifespan
# shutdown handler BEFORE asyncio cancels in-flight tasks. The
# register_and_run wrapper's CancelledError arm checks this — if
# set, the record is stamped with error_kind="server_restart" so
# replay_from_db_and_auto_retry can re-fire it on next boot. Without
# this flag, every in-flight job at restart time looks identical to
# a user-initiated cancel and would NOT auto-retry.
_SHUTDOWN_IN_PROGRESS = False


def signal_shutdown() -> None:
    """Mark the module as in shutdown. Subsequent CancelledError
    transitions stamp error_kind='server_restart' instead of
    'cancelled' so the next boot's replay can distinguish."""
    global _SHUTDOWN_IN_PROGRESS
    _SHUTDOWN_IN_PROGRESS = True

# WS broadcast callback — main.py wires this at startup to
# _ws_broadcast or equivalent. Kept as a module-level hook so this
# module doesn't import from main (avoids the circular import).
_BROADCAST_CALLBACK: Optional[Callable[[dict], "asyncio.Future"]] = None


def set_broadcast_callback(cb: Callable[[dict], "asyncio.Future"]) -> None:
    """Wire the WS broadcast function. main.py calls this on startup
    after _ws_broadcast is defined."""
    global _BROADCAST_CALLBACK
    _BROADCAST_CALLBACK = cb


# ─── serialization for the wire ─────────────────────────────────────────

def _product_static_metadata(product_type: str) -> dict:
    """Static per-product metadata used by the dashboard row render:
    long_name, format badge, is_xlsx routing flag, category grouping.
    Pulled from PRODUCT_SCHEMAS + PRODUCT_CATEGORIES (in-memory, fast).
    Cached on the first call; the catalog never changes at runtime."""
    cache = _product_static_metadata._cache  # type: ignore[attr-defined]
    if product_type in cache:
        return cache[product_type]
    try:
        from templates import PRODUCT_SCHEMAS
        from utils.exporter import (
            PRODUCT_CATEGORIES,
            PRODUCT_LONG_NAMES,
        )
    except Exception:
        # If PRODUCT_SCHEMAS isn't importable here, fall back to bare
        # product_type as the display name.
        meta = {"long_name": product_type, "format": "docx", "is_xlsx": False,
                "category": None, "category_order": 0, "step_label": None}
        cache[product_type] = meta
        return meta
    schema = PRODUCT_SCHEMAS.get(product_type, {}) or {}
    ofmts = list(
        schema.get("output_formats")
        or [schema.get("output_format") or "docx"]
    )
    is_xlsx = ofmts == ["xlsx"]
    fmt = "xlsx" if is_xlsx else (schema.get("output_format") or "docx")
    cat_entry = PRODUCT_CATEGORIES.get(product_type)
    category, cat_order, step = cat_entry if cat_entry else (None, 0, None)
    meta = {
        "long_name": PRODUCT_LONG_NAMES.get(product_type, product_type),
        "format": fmt,
        "is_xlsx": is_xlsx,
        "category": category,
        "category_order": cat_order,
        "step_label": step,
    }
    cache[product_type] = meta
    return meta


_product_static_metadata._cache = {}  # type: ignore[attr-defined]


def _artifact_header_lookup(artifact_id: Optional[str],
                              autodraft_session_id: Optional[str]) -> dict:
    """For a completed chain, read the artifact's header from the
    SessionDetail row so the row can render the right approval state.
    Returns {status, approved_by, approved_at} or empty dict on miss.

    Cheap-ish: one indexed SessionDetail row per call. Cached at the
    artifact level so successive list_chains() / record_upsert frames
    don't repeatedly hit SQLite for the same artifact.
    """
    if not artifact_id:
        return {}
    cache = _artifact_header_lookup._cache  # type: ignore[attr-defined]
    if artifact_id in cache:
        return cache[artifact_id]
    if not autodraft_session_id:
        return {}
    try:
        db = database.SessionLocal()
        try:
            row = (db.query(database.SessionDetail)
                   .filter(database.SessionDetail.session_id == autodraft_session_id)
                   .order_by(database.SessionDetail.timestamp.desc())
                   .first())
        finally:
            db.close()
    except Exception:
        return {}
    if not row:
        return {}
    try:
        payload = json.loads(row.data or "{}")
    except Exception:
        return {}
    trace = payload.get("agent_trace") or {}
    for disp in trace.get("product_writer_dispatches") or []:
        emit = disp.get("emit") or {}
        if str(emit.get("artifact_id") or "") != str(artifact_id):
            continue
        sd = emit.get("structured_data") or {}
        header = sd.get("header") or {}
        out = {
            "status": header.get("status") or "draft",
            "approved_by": header.get("approved_by", "") or "",
            "approved_at": header.get("approved_at", 0) or 0,
        }
        cache[artifact_id] = out
        return out
    return {}


_artifact_header_lookup._cache = {}  # type: ignore[attr-defined]


def invalidate_artifact_header_cache(artifact_id: Optional[str]) -> None:
    """Drop the header cache for an artifact. Called when the artifact
    is published / reverted / approval state changes so subsequent
    list_chains() reads see fresh values."""
    if not artifact_id:
        return
    _artifact_header_lookup._cache.pop(artifact_id, None)  # type: ignore[attr-defined]


def _serialize_record(r: ProductRecord) -> dict:
    """Wire shape: explicit field list, no recursive descent into
    runtime-only objects (asyncio.Task / asyncio.Event would otherwise
    blow up under dataclasses.asdict's recursive serialization).
    Gate position is derived at read time.

    Includes the dashboard rendering metadata (long_name, format,
    is_xlsx, category, step_label) so the frontend doesn't need a
    second fetch to render rows. For complete records the artifact's
    header (status / approved_by / approved_at) is also folded in."""
    meta = _product_static_metadata(r.product_type)
    header = (_artifact_header_lookup(r.artifact_id, r.autodraft_session_id)
              if r.state == "complete" else {})
    return {
        "record_id": r.record_id,
        "chain_head_id": r.chain_head_id,
        "parent_record_id": r.parent_record_id,
        "product_parent_record_id": r.product_parent_record_id,
        "product_type": r.product_type,
        "coa_id": r.coa_id,
        "coa_label": r.coa_label,
        "source": r.source,
        "source_metadata": dict(r.source_metadata or {}),
        "state": r.state,
        "phase": r.phase,
        "attempt_index": r.attempt_index,
        "started_at_ms": r.started_at_ms,
        "gate_acquired_at_ms": r.gate_acquired_at_ms,
        "writer_started_at_ms": r.writer_started_at_ms,
        "ended_at_ms": r.ended_at_ms,
        "duration_ms": r.duration_ms,
        "artifact_id": r.artifact_id,
        "autodraft_session_id": r.autodraft_session_id,
        "error": r.error,
        "error_kind": r.error_kind,
        "request_payload": dict(r.request_payload or {}),
        "gate_position": _gate_position(r) if r.state == "running" else None,
        # Wave progress for 'Generating... XX%' on in-flight rows.
        # 0 for queued/just-started, 1-99 while running, cleared
        # (no field render) on terminal transition.
        "progress_pct": getattr(r, "progress_pct", 0) if r.state == "running" else 0,
        "progress_wave": getattr(r, "progress_wave", "") if r.state == "running" else "",
        # Dashboard render metadata (static schema + dynamic header).
        "long_name": meta["long_name"],
        "format": meta["format"],
        "is_xlsx": meta["is_xlsx"],
        "category": meta["category"],
        "category_order": meta["category_order"],
        "step_label": meta["step_label"],
        "approval_status": header.get("status") or None,
        "approved_by": header.get("approved_by") or "",
        "approved_at": header.get("approved_at") or 0,
    }


def _gate_position(r: ProductRecord) -> Optional[int]:
    """Walk the vllm gate's queue to compute this record's position.
    Returns 1-based position; None if not currently waiting."""
    if r.gate_serial is None:
        return None
    try:
        from utils.llm import _GATE  # lazy import to avoid load-time cycle
        # Walk _GATE's internal queue. The queue is a list of (serial,
        # event) tuples; position = index + 1 of the matching serial.
        queue = list(getattr(_GATE, "_queue", []))
        for i, entry in enumerate(queue):
            serial = entry[0] if isinstance(entry, tuple) else getattr(entry, "serial", None)
            if serial == r.gate_serial:
                return i + 1
    except Exception:
        return None
    return None


# ─── chain projection (the UI's row shape) ──────────────────────────────

def _chain_view(chain_head_id: str) -> Optional[dict]:
    """Collapse every record with this chain_head_id into one row.
    The row carries the LATEST attempt's state, phase, artifact, etc.,
    plus the earliest attempt's started_at_ms for chain-level sort.

    Enriched with the dashboard rendering metadata (long_name,
    format, is_xlsx, category, step_label) and — for completed
    chains — the artifact header (status / approved_by / approved_at)
    so the frontend doesn't need a separate catalog endpoint."""
    members = [r for r in _RECORDS.values()
               if r.chain_head_id == chain_head_id and not r.is_deleted]
    if not members:
        return None
    # Sort by attempt_index ascending; latest is the highest index.
    members.sort(key=lambda r: r.attempt_index)
    latest = members[-1]
    earliest = members[0]
    meta = _product_static_metadata(latest.product_type)
    header = (_artifact_header_lookup(latest.artifact_id,
                                       latest.autodraft_session_id)
              if latest.state == "complete" else {})
    return {
        "chain_head_id": chain_head_id,
        "latest_record_id": latest.record_id,
        "product_type": latest.product_type,
        "coa_id": latest.coa_id,
        "coa_label": latest.coa_label,
        "source": latest.source,
        "source_metadata": latest.source_metadata,
        "product_parent_record_id": latest.product_parent_record_id,
        "state": latest.state,
        "phase": latest.phase,
        "attempts": len(members),
        "started_at_ms": earliest.started_at_ms,
        "latest_started_at_ms": latest.started_at_ms,
        "ended_at_ms": latest.ended_at_ms,
        "duration_ms": latest.duration_ms,
        "artifact_id": latest.artifact_id,
        "autodraft_session_id": latest.autodraft_session_id,
        "error": latest.error,
        "error_kind": latest.error_kind,
        "gate_position": _gate_position(latest) if latest.state == "running" else None,
        # Wave progress for the in-flight chain. 0 when not running so
        # the dashboard's "Generating… XX%" branch falls back to bare
        # "Generating…" on queued / complete rows.
        "progress_pct": getattr(latest, "progress_pct", 0) if latest.state == "running" else 0,
        "progress_wave": getattr(latest, "progress_wave", "") if latest.state == "running" else "",
        # Dashboard render metadata.
        "long_name": meta["long_name"],
        "format": meta["format"],
        "is_xlsx": meta["is_xlsx"],
        "category": meta["category"],
        "category_order": meta["category_order"],
        "step_label": meta["step_label"],
        "approval_status": header.get("status") or None,
        "approved_by": header.get("approved_by") or "",
        "approved_at": header.get("approved_at") or 0,
    }


def list_chains() -> list[dict]:
    """Return one ChainView per chain_head_id present in _RECORDS,
    sorted started_at_ms DESC. Caller can re-sort client-side per
    column. Soft-deleted records / chains are hidden from this view but
    remain in _RECORDS and JobEvent for replay + data analysis."""
    seen: set[str] = set()
    chains: list[dict] = []
    for r in _RECORDS.values():
        if r.is_deleted:
            continue
        if r.chain_head_id in seen:
            continue
        seen.add(r.chain_head_id)
        cv = _chain_view(r.chain_head_id)
        if cv:
            chains.append(cv)
    chains.sort(key=lambda c: c["started_at_ms"], reverse=True)
    return chains


def has_active_chain_for_product(product_type: str,
                                  project_id: str = "") -> bool:
    """Sweep dedup check, scoped to ONE project's chains: a chain whose
    LATEST record is in {running, complete, failed} for the product
    type AND carries the same source_metadata.project_id blocks a new
    autodraft fire for that project. Chains without a project stamp
    (pre-unification history) block nothing — their pool entries
    already gate the sweep via _autodraft_pool_index. Only `cancelled`
    or chain-absent allows dispatch. Soft-deleted chains do NOT block —
    the analyst has explicitly chosen to start over."""
    seen: set[str] = set()
    for r in _RECORDS.values():
        if r.is_deleted:
            continue
        if r.chain_head_id in seen:
            continue
        seen.add(r.chain_head_id)
        latest_id = _CHAIN_LATEST.get(r.chain_head_id, r.record_id)
        latest = _RECORDS.get(latest_id)
        if not latest or latest.is_deleted or latest.product_type != product_type:
            continue
        latest_project = ((latest.source_metadata or {}).get("project_id")
                           or "") if isinstance(latest.source_metadata, dict) else ""
        if latest_project != (project_id or ""):
            continue
        if latest.state in ("running", "complete", "failed"):
            return True
    return False


# ─── core: record_transition (the broadcast point) ──────────────────────

def _now_ms() -> int:
    return int(time.time() * 1000)


def _record_lock(record_id: str) -> asyncio.Lock:
    """Return (creating if needed) the per-record lock. Held by both the
    strong-ref dict (so it survives across awaits while a job is in
    flight) and the weak-value dict (so terminated+deleted records'
    locks can be reclaimed by GC). See _maybe_release_record_lock."""
    lock = _RECORD_LOCKS.get(record_id)
    if lock is None:
        lock = asyncio.Lock()
        _RECORD_LOCKS[record_id] = lock
    _RECORD_LOCK_STRONG[record_id] = lock
    return lock


def _maybe_release_record_lock(record_id: str) -> None:
    """Drop the strong ref to a record's lock once no further mutation
    is possible (terminal state AND soft-deleted). The WeakValueDictionary
    entry will follow once nothing else references the lock.

    Records still in flight (state="running") keep their strong ref so a
    concurrent record_transition can re-acquire the same lock instance."""
    r = _RECORDS.get(record_id)
    if r is None:
        # Defensive — replay or backfill never registered this id, just
        # let the strong ref go.
        _RECORD_LOCK_STRONG.pop(record_id, None)
        return
    if r.state in ("complete", "failed") and r.is_deleted:
        _RECORD_LOCK_STRONG.pop(record_id, None)


async def record_transition(record_id: str, **changes) -> Optional[ProductRecord]:
    """Apply field updates to a record, append a JobEvent, broadcast
    the upsert. The single mutation entry point — every other helper in
    this module calls through here.

    `event_type` (in **changes) drives the JobEvent.event_type column;
    if omitted defaults to 'phase' (a soft update). Pop it before
    applying changes to the record itself.

    Lock discipline (jobs-unification-007): _BROADCAST_LOCK is held only
    for the millisecond it takes to mutate the record, assign seq, and
    snapshot the event payload. The synchronous SQLite write runs OFF
    the lock via asyncio.to_thread so a slow disk doesn't stall every
    other in-flight transition. The WS broadcast runs AFTER the DB
    write so wire-order matches DB-order (frontend reducer relies on
    strict monotonic seq).
    """
    event_type = changes.pop("event_type", "phase")
    event_snapshot: Optional[dict] = None
    record_snapshot: Optional[dict] = None
    seq: Optional[int] = None
    r: Optional[ProductRecord] = None
    async with _BROADCAST_LOCK:
        async with _record_lock(record_id):
            r = _RECORDS.get(record_id)
            if r is None:
                log.warning("record_transition: unknown record_id=%s", record_id)
                return None
            for k, v in changes.items():
                if hasattr(r, k):
                    setattr(r, k, v)
            # Derive duration_ms on terminal transitions.
            if r.state in ("complete", "failed") and r.ended_at_ms is None:
                r.ended_at_ms = _now_ms()
            if (r.ended_at_ms is not None and r.writer_started_at_ms is not None
                    and r.duration_ms is None):
                r.duration_ms = r.ended_at_ms - r.writer_started_at_ms
        global _SEQ
        _SEQ += 1
        seq = _SEQ
        # Snapshot event + wire payloads while holding the broadcast
        # lock so concurrent transitions can't reorder them, but do the
        # actual SQLite write below outside the in-memory critical
        # section.
        event_snapshot = _job_event_snapshot(r, event_type, seq)
        record_snapshot = _serialize_record(r)
    # DB write off-lock so disk I/O doesn't stall the next caller.
    try:
        await asyncio.to_thread(_persist_job_event, event_snapshot)
    except Exception:
        log.exception("JobEvent persist failed for record_id=%s seq=%s",
                      record_id, seq)
    # Broadcast AFTER DB write so wire-order matches DB-order.
    await _broadcast({
        "type": "product_record_upsert",
        "seq": seq,
        "record": record_snapshot,
    })
    # If this transition put the record into a terminal+deleted state,
    # release the strong ref to its lock so the entry can be reclaimed.
    _maybe_release_record_lock(record_id)
    return r


def _job_event_snapshot(r: ProductRecord, event_type: str, seq: int) -> dict:
    """Build the dict that will become a JobEvent row. Captured under
    _BROADCAST_LOCK so the snapshot reflects the record at the exact
    point in time the seq was assigned. The actual SQLite write is then
    handed to a thread (see _persist_job_event) so disk latency doesn't
    serialize every other transition behind it."""
    return {
        "seq": seq,
        "record_id": r.record_id,
        "chain_head_id": r.chain_head_id,
        "event_type": event_type,
        "state_after": r.state,
        "phase": r.phase,
        "attempt_index": r.attempt_index,
        "artifact_id": r.artifact_id,
        "error": r.error,
        "error_kind": r.error_kind,
        "payload": json.dumps({
            "source": r.source,
            "source_metadata": r.source_metadata,
            "coa_id": r.coa_id,
            "coa_label": r.coa_label,
            "product_type": r.product_type,
            "product_parent_record_id": r.product_parent_record_id,
            "parent_record_id": r.parent_record_id,
            "autodraft_session_id": r.autodraft_session_id,
            "is_deleted": r.is_deleted,
        }, default=str),
        "emitted_at": _now_ms(),
    }


def _analyst_event_from_job_snapshot(snapshot: dict):
    """Build an AnalystEvent row mirroring a JobEvent snapshot
    (surface='generation') so the product fix-list sees the
    generate/fail/retry/cancel lifecycle without a second source.
    user_id comes from source_metadata.by when present, else NULL
    (server-emitted)."""
    import uuid as _uuid
    try:
        payload = json.loads(snapshot.get("payload") or "{}")
    except Exception:
        payload = {}
    meta = payload.get("source_metadata") if isinstance(payload, dict) else None
    user_id = meta.get("by") if isinstance(meta, dict) else None
    return database.AnalystEvent(
        event_id=str(_uuid.uuid4()),
        user_id=user_id,
        event_type="generation." + str(snapshot.get("event_type")),
        surface="generation",
        product_type=payload.get("product_type"),
        session_id=payload.get("autodraft_session_id"),
        artifact_id=snapshot.get("artifact_id"),
        value_num=snapshot.get("attempt_index"),
        detail=json.dumps({
            "record_id": snapshot.get("record_id"),
            "chain_head_id": snapshot.get("chain_head_id"),
            "state_after": snapshot.get("state_after"),
            "phase": snapshot.get("phase"),
            "error_kind": snapshot.get("error_kind"),
            "source": payload.get("source"),
            "coa_id": payload.get("coa_id"),
        }, default=str),
    )


def _persist_job_event(snapshot: dict) -> None:
    """Synchronous SQLite write of a pre-captured JobEvent snapshot.
    Intended to be invoked from asyncio.to_thread so the event loop
    isn't blocked on disk I/O. Best-effort — exceptions propagate to
    the caller which already wraps the call in try/log."""
    db = database.SessionLocal()
    try:
        evt = database.JobEvent(**snapshot)
        db.add(evt)
        db.commit()
    finally:
        db.close()
    # Mirror the lifecycle into analyst_event AFTER the job_event is safely
    # committed — fully isolated so a telemetry failure can never jeopardize
    # the job_event write (which lifespan replay depends on).
    try:
        db2 = database.SessionLocal()
        try:
            db2.add(_analyst_event_from_job_snapshot(snapshot))
            db2.commit()
        finally:
            db2.close()
    except Exception:
        log.exception("analyst_event mirror failed for record_id=%s",
                      snapshot.get("record_id"))


def _append_job_event(r: ProductRecord, event_type: str, seq: int) -> None:
    """Synchronous append used by code paths that aren't already in
    an awaitable context (lifespan replay, register_and_run's started
    event, retry_chain's retried event). For the hot path
    (record_transition) we use _job_event_snapshot +
    asyncio.to_thread(_persist_job_event) so the broadcast lock is held
    only briefly. Best-effort; failures are logged and swallowed."""
    try:
        _persist_job_event(_job_event_snapshot(r, event_type, seq))
    except Exception:
        log.exception("JobEvent insert failed for record_id=%s seq=%s",
                      r.record_id, seq)


async def _broadcast(frame: dict) -> None:
    """Dispatch a WS frame to every connected analyst's socket."""
    if _BROADCAST_CALLBACK is None:
        return
    try:
        res = _BROADCAST_CALLBACK(frame)
        if asyncio.iscoroutine(res) or isinstance(res, asyncio.Future):
            await res
    except Exception:
        log.exception("broadcast failed for frame type=%s seq=%s",
                      frame.get("type"), frame.get("seq"))


# ─── register_and_run: the universal wrapper ────────────────────────────

async def register_and_run(
    *,
    source: Source,
    product_type: str,
    request_payload: dict,
    coro_factory: Callable[[ProductRecord], "asyncio.Future"],
    chain_head_id: Optional[str] = None,
    parent_record_id: Optional[str] = None,
    attempt_index: int = 1,
    product_parent_record_id: Optional[str] = None,
    coa_id: Optional[str] = None,
    coa_label: Optional[str] = None,
    source_metadata: Optional[dict] = None,
    autodraft_session_id: Optional[str] = None,
) -> str:
    """Create a record, dispatch coro_factory(record), capture its
    success/failure into the registry + audit log. Returns record_id.

    coro_factory receives the freshly-minted ProductRecord and should
    return a coroutine that does the actual work (writer, DAG, etc.).
    The coroutine is responsible for transitioning phases via
    record_transition; this wrapper handles the final
    `complete`/`failed`/`cancelled` transition based on the coroutine's
    return / raise.

    On success the coroutine MUST set record.artifact_id (via a
    record_transition call) before returning.
    """
    record_id = uuid.uuid4().hex
    if chain_head_id is None:
        chain_head_id = record_id
    cancel_event = asyncio.Event()
    r = ProductRecord(
        record_id=record_id,
        chain_head_id=chain_head_id,
        parent_record_id=parent_record_id,
        product_parent_record_id=product_parent_record_id,
        product_type=product_type,
        coa_id=coa_id,
        coa_label=coa_label,
        source=source,
        source_metadata=dict(source_metadata or {}),
        state="running",
        phase="awaiting_gate",
        attempt_index=attempt_index,
        started_at_ms=_now_ms(),
        gate_acquired_at_ms=None,
        writer_started_at_ms=None,
        ended_at_ms=None,
        duration_ms=None,
        artifact_id=None,
        # Stamped at registration when the caller already has the session id
        # (autodraft path mints it before register_and_run so it's known
        # here). Closes the field-gap that PR #63 worked around: a record
        # could otherwise reach `complete` with an empty session_id if the
        # writer_started transition failed/was skipped, which made it
        # invisible to the backfill's (session_id, artifact_id) dedup and
        # produced a clickable duplicate row. Stamping at creation removes
        # the gap entirely.
        autodraft_session_id=autodraft_session_id,
        error=None,
        error_kind=None,
        request_payload=dict(request_payload or {}),
        gate_serial=None,
        cancel_event=cancel_event,
    )
    _RECORDS[record_id] = r
    _CHAIN_LATEST[chain_head_id] = record_id

    # Broadcast the "started" event before dispatching so the UI sees
    # the row appear immediately.
    async with _BROADCAST_LOCK:
        global _SEQ
        _SEQ += 1
        seq = _SEQ
        _append_job_event(r, "started", seq)
        await _broadcast({
            "type": "product_record_upsert",
            "seq": seq,
            "record": _serialize_record(r),
        })

    async def _run():
        try:
            await coro_factory(r)
            # coroutine succeeded — transition to complete if it didn't
            # already.
            current = _RECORDS.get(record_id)
            if current and current.state == "running":
                await record_transition(
                    record_id,
                    event_type="completed",
                    state="complete",
                    phase="completed",
                )
        except asyncio.CancelledError:
            # Distinguish task-cancelled-by-shutdown from
            # task-cancelled-by-user. If the lifespan shutdown handler
            # has flipped _SHUTDOWN_IN_PROGRESS, this CancelledError is
            # uvicorn killing tasks during shutdown — stamp it
            # server_restart so replay auto-retries on next boot. User
            # cancels (cancel_record / cancel REST endpoint) come
            # through with _SHUTDOWN_IN_PROGRESS=False and stay
            # error_kind="cancelled".
            if _SHUTDOWN_IN_PROGRESS:
                await record_transition(
                    record_id,
                    event_type="failed",
                    state="failed",
                    phase="server_restart",
                    error="Job in flight at server restart.",
                    error_kind="server_restart",
                )
            else:
                await record_transition(
                    record_id,
                    event_type="cancelled",
                    state="failed",
                    phase="cancelled",
                    error="Cancelled",
                    error_kind="cancelled",
                )
            raise
        except Exception as exc:
            await record_transition(
                record_id,
                event_type="failed",
                state="failed",
                phase="errored",
                error=f"{type(exc).__name__}: {exc}",
                error_kind=_classify_error(exc),
            )
        finally:
            r._task_ref = None
            _RUNNING_TASKS.pop(record_id, None)

    task = asyncio.create_task(_run())
    r._task_ref = task
    _RUNNING_TASKS[record_id] = task
    return record_id


def _classify_error(exc: BaseException) -> ErrorKind:
    """Map an exception to a coarse error_kind for analyst triage.

    The writer-validation branch checks for BOTH 'json' and 'decode'
    (matching the json.JSONDecodeError exception name) to avoid false
    positives — writer error messages frequently contain the word
    'JSON' standalone (e.g. "expected JSON object", "JSON schema
    validation failed against ...") which is NOT a decode failure and
    classifying it as writer_invalid would route every schema error
    into the wrong retry bucket. Coerced to lowercase for case
    insensitivity.
    """
    msg = f"{type(exc).__name__}: {exc}".lower()
    if "oom" in msg or "out of memory" in msg or "kv cache" in msg:
        return "llm_oom"
    if "timeout" in msg and "gate" in msg:
        return "gate_timeout"
    if "validat" in msg or ("json" in msg and "decode" in msg):
        return "writer_invalid"
    if "map" in msg and ("render" in msg or "tile" in msg):
        return "map_render_failed"
    return "internal"


# ─── cancel ─────────────────────────────────────────────────────────────

async def cancel_record(record_id: str, *, reason: str = "Cancelled") -> Optional[ProductRecord]:
    """Cancel a running record. Idempotent — cancel of a terminal row
    is a no-op (returns the record's current state).

    Side effects:
      1. record.cancel_event.set() — for cooperative writers that
         check the event between phases.
      2. _GATE.cancel_waiter(record_id) — removes the record from the
         vllm gate's wait queue if it's pre-gate.
      3. task.cancel() — propagates CancelledError to the writer.
         httpx aborts the open vllm POST; vllm's engine sees the TCP
         disconnect and releases the KV cache slot.
      4. If source=autodraft_sweep, sets _SWEEP_CANCELLED — the sweep
         loop breaks early and cancels remaining unstarted products.
      5. If this is a Threat-COA-Statement parent OR a sitemp_fanout
         child, cancels every sibling SITEMP child (parent dead =
         children invalid; you can't render N SITEMPs without the
         parent's COA list).
    """
    r = _RECORDS.get(record_id)
    if r is None or r.state != "running":
        return r
    if r.cancel_event is not None:
        r.cancel_event.set()
    # Pre-gate cancel — pull the waiter off the queue.
    if r.gate_serial is not None:
        try:
            from utils.llm import _GATE
            cancel_waiter = getattr(_GATE, "cancel_waiter", None)
            if callable(cancel_waiter):
                await cancel_waiter(record_id)
        except Exception:
            log.exception("gate cancel_waiter failed for record_id=%s", record_id)
    # Cascade signals.
    if r.source == "autodraft_sweep":
        _SWEEP_CANCELLED.set()
    if r.product_type == "Threat COA Statement":
        _FANOUT_CANCELLED.set()
    # Now cancel the task. The wrapper's except CancelledError arm
    # writes the failed/cancelled transition.
    task = _RUNNING_TASKS.get(record_id)
    if task is not None and not task.done():
        task.cancel()
    return r


def sweep_cancelled() -> asyncio.Event:
    """Expose the sweep-cancel signal so product_autodraft can poll
    it in the sweep loop. Cleared at the top of each NEW sweep pass
    by clear_sweep_cancelled()."""
    return _SWEEP_CANCELLED


def clear_sweep_cancelled() -> None:
    _SWEEP_CANCELLED.clear()


def fanout_cancelled() -> asyncio.Event:
    return _FANOUT_CANCELLED


def clear_fanout_cancelled() -> None:
    _FANOUT_CANCELLED.clear()


# ─── retry ──────────────────────────────────────────────────────────────

async def retry_chain(chain_head_id: str,
                      *,
                      dispatcher: Callable[[ProductRecord], "asyncio.Future"],
                      by: str = "",
                      ) -> Optional[str]:
    """Mint a new record extending the chain. Valid only when the
    chain's latest record is failed. Returns the new record_id.

    The caller (retry endpoint) provides a dispatcher that knows how
    to re-fire the original source (autodraft_sweep, sitemp_fanout,
    etc.) given the request_payload from the parent. The dispatcher
    receives the new ProductRecord and returns the same coroutine
    register_and_run normally awaits.

    `by` is the user who triggered the retry. When provided it overrides
    the parent's stored `source_metadata.by` so the new attempt attributes
    the regeneration to whoever clicked Retry, not the original creator.
    (Without this the retry inherited the parent's `by` verbatim — which is
    how stale test values like "regen-test" kept reappearing on retries.)
    """
    latest_id = _CHAIN_LATEST.get(chain_head_id)
    if not latest_id:
        return None
    parent = _RECORDS.get(latest_id)
    if parent is None or parent.state != "failed":
        return None
    retry_metadata = dict(parent.source_metadata or {})
    if by:
        retry_metadata["by"] = by
    # Mint a fresh session_id for the retry attempt — the retry is a new
    # generation pass, not a reuse of the parent's failed session. Stamped
    # onto the new ProductRecord at registration so the retry record never
    # exists with autodraft_session_id=None (same field-gap closure as the
    # primary autodraft path).
    retry_session_id = f"autodraft-{uuid.uuid4()}"
    new_id = await register_and_run(
        source=parent.source,
        product_type=parent.product_type,
        request_payload=parent.request_payload,
        coro_factory=dispatcher,
        chain_head_id=chain_head_id,
        parent_record_id=parent.record_id,
        attempt_index=parent.attempt_index + 1,
        product_parent_record_id=parent.product_parent_record_id,
        coa_id=parent.coa_id,
        coa_label=parent.coa_label,
        autodraft_session_id=retry_session_id,
        source_metadata=retry_metadata,
    )
    # Append a retried event on the PARENT's audit trail so the log
    # carries the linkage even after parent eviction.
    async with _BROADCAST_LOCK:
        global _SEQ
        _SEQ += 1
        seq = _SEQ
        _append_job_event(parent, "retried", seq)
    return new_id


# ─── delete ─────────────────────────────────────────────────────────────

async def delete_chain(chain_head_id: str, *, db) -> bool:
    """Soft-delete every record of the chain: mark is_deleted=True so
    the UI hides the row, but keep the records and JobEvent log
    forever for replay + data analysis (per user requirement: nothing
    is ever physically removed). The on-disk artifact file IS removed
    via intel_agent.delete_artifact_in_db — the audit row stays.

    Valid when the latest record is in a terminal state — `complete`
    (has an artifact to remove) or `failed` (no artifact). A `running`
    chain must be cancelled first.

    Caller must pass a DB session for the artifact deletion side
    effect.
    """
    latest_id = _CHAIN_LATEST.get(chain_head_id)
    if not latest_id:
        return False
    latest = _RECORDS.get(latest_id)
    if (latest is None
            or latest.state not in ("complete", "failed")
            or latest.is_deleted):
        return False
    # Artifact deletion via the existing handler. The artifact file is
    # removed; the ProductRecord (and its JobEvent history) survive.
    artifact_id = latest.artifact_id
    if artifact_id:
        try:
            from handlers import intel_agent
            intel_agent.delete_artifact_in_db(db, artifact_id)
        except Exception:
            log.exception("delete_artifact_in_db failed for artifact_id=%s",
                          artifact_id)
    # Soft-mark every record in the chain. _CHAIN_LATEST is left intact
    # so replay's "latest of chain" lookup keeps pointing at the right
    # record (with is_deleted=True the UI projection skips it).
    targets = [(rid, r) for rid, r in _RECORDS.items()
               if r.chain_head_id == chain_head_id and not r.is_deleted]
    if not targets:
        return False
    async with _BROADCAST_LOCK:
        global _SEQ
        for rid, r in targets:
            r.is_deleted = True
            _SEQ += 1
            _append_job_event(r, "deleted", _SEQ)
        _SEQ += 1
        seq = _SEQ
        await _broadcast({
            "type": "product_record_delete",
            "seq": seq,
            "chain_head_id": chain_head_id,
            "reason": "deleted",
        })
    # Now that the chain's records are terminal+deleted, drop the strong
    # refs to their locks so they can be garbage-collected. Records left
    # running (shouldn't happen — guarded above on latest.state) keep
    # their lock as a safety net.
    for rid, _ in targets:
        _maybe_release_record_lock(rid)
    return True


# ─── snapshot for WS resume + initial fetch ─────────────────────────────

def snapshot() -> dict:
    """Full chain list + monotonic seq. Sent on WS accept and on
    resume_jobs replies."""
    return {
        "chains": list_chains(),
        "as_of_seq": _SEQ,
    }


async def emit_snapshot() -> None:
    """Broadcast a product_records_snapshot WS frame on demand. Used
    by the WS handler in main.py to satisfy an inbound
    {type: 'request_snapshot'} frame from a reconnecting subscriber:
    the client may have missed seq numbers during a network drop, so
    rather than try to splice events_since onto a stale local cache
    the client asks for a full snapshot and resets.

    The frame carries the wire-shape per-record list (filtered to
    non-deleted) so the frontend can hydrate its reducer in one pass.
    """
    payload = {
        "type": "product_records_snapshot",
        "seq": _SEQ,
        "records": [_serialize_record(r) for r in _RECORDS.values()
                    if not r.is_deleted],
    }
    await _broadcast(payload)


async def events_since(seq: int) -> list[dict]:
    """Pull JobEvent rows newer than `seq` for client-side replay
    after a WS reconnect. Returns the wire-shape upsert frames in
    order."""
    out: list[dict] = []
    try:
        db = database.SessionLocal()
        try:
            rows = (db.query(database.JobEvent)
                    .filter(database.JobEvent.seq > seq)
                    .order_by(database.JobEvent.seq.asc())
                    .all())
            for row in rows:
                r = _RECORDS.get(row.record_id)
                if r is None:
                    continue
                out.append({
                    "type": "product_record_upsert",
                    "seq": row.seq,
                    "record": _serialize_record(r),
                })
        finally:
            db.close()
    except Exception:
        log.exception("events_since query failed")
    return out


# ─── lifespan replay + auto-retry on restart ────────────────────────────

async def replay_from_db_and_auto_retry(
    *,
    dispatcher_for_source: Callable[[Source, ProductRecord],
                                     "asyncio.Future"],
    horizon_ms: int = 30 * 24 * 60 * 60 * 1000,  # 30 days
) -> int:
    """On docker-api startup, walk JobEvent log and rebuild _RECORDS
    to the last-known state. Any record left in `running` after
    replay is treated as orphaned by the restart (the asyncio task
    is dead) and AUTO-RETRIED per the V4 design lock-in.

    Returns the number of records rehydrated.
    """
    cutoff = _now_ms() - horizon_ms
    try:
        db = database.SessionLocal()
        try:
            rows = (db.query(database.JobEvent)
                    .filter(database.JobEvent.emitted_at >= cutoff)
                    .order_by(database.JobEvent.seq.asc())
                    .all())
        finally:
            db.close()
    except Exception:
        log.exception("replay: JobEvent query failed")
        return 0

    # Replay events into _RECORDS. Each event_type either creates a
    # record (started) or mutates an existing one (everything else).
    # Also advance _SEQ to the max seq seen so post-replay broadcasts
    # don't collide with already-persisted JobEvent rows.
    global _SEQ
    for row in rows:
        if row.seq > _SEQ:
            _SEQ = row.seq
        payload = json.loads(row.payload or "{}")
        if row.event_type == "started" and row.record_id not in _RECORDS:
            r = ProductRecord(
                record_id=row.record_id,
                chain_head_id=row.chain_head_id,
                parent_record_id=payload.get("parent_record_id"),
                product_parent_record_id=payload.get("product_parent_record_id"),
                product_type=payload.get("product_type") or "UNKNOWN",
                coa_id=payload.get("coa_id"),
                coa_label=payload.get("coa_label"),
                source=payload.get("source") or "pre_registry",
                source_metadata=payload.get("source_metadata") or {},
                state="running",
                phase=row.phase or "awaiting_gate",
                attempt_index=row.attempt_index or 1,
                started_at_ms=row.emitted_at,
                gate_acquired_at_ms=None,
                writer_started_at_ms=None,
                ended_at_ms=None,
                duration_ms=None,
                artifact_id=row.artifact_id,
                autodraft_session_id=payload.get("autodraft_session_id"),
                error=row.error,
                error_kind=row.error_kind,
                request_payload={},  # not stored in event; re-dispatched via dispatcher_for_source
                gate_serial=None,
                cancel_event=None,
            )
            _RECORDS[row.record_id] = r
            _CHAIN_LATEST[row.chain_head_id] = row.record_id
        else:
            r = _RECORDS.get(row.record_id)
            if not r:
                continue
            if row.state_after in ("running", "complete", "failed"):
                r.state = row.state_after
            else:
                log.warning(
                    "replay: unexpected state_after=%r on record_id=%s "
                    "seq=%s — keeping current state=%r",
                    row.state_after, row.record_id, row.seq, r.state,
                )
            if row.phase:
                r.phase = row.phase
            if row.artifact_id:
                r.artifact_id = row.artifact_id
            if row.error is not None:
                r.error = row.error
            if row.error_kind:
                r.error_kind = row.error_kind
            # is_deleted is preserved on replay so a chain that was
            # soft-deleted before restart stays hidden in the UI
            # projection but visible to data-analysis paths.
            if row.event_type == "deleted":
                r.is_deleted = True
            else:
                # Earlier events may carry the deletion flag in their
                # payload (post-fix snapshots include it); honor it
                # without overwriting a deletion seen on a later event.
                if payload.get("is_deleted") is True:
                    r.is_deleted = True
            r.ended_at_ms = row.emitted_at if r.state in ("complete", "failed") else r.ended_at_ms
            _CHAIN_LATEST[r.chain_head_id] = row.record_id

    # Emergency override: when the operator needs a quiet boot (no
    # chains auto-fired from a backlog of mid-flight / server-restart
    # records), set PYTHO_DISABLE_RESTART_AUTO_RETRY=1 in the docker-api
    # env. Synthesizes the terminal `failed` transition for any
    # state="running" record so the audit log stays linked, but does NOT
    # re-fire. Any state="failed" + error_kind="server_restart" record
    # is left in its terminal state without a retry — the operator can
    # manually re-fire via /api/products/records/{id}/retry once they're
    # ready. This is the "clear out all the pending chains" escape hatch
    # used during demo-prep / debugging when a backlog of failed chains
    # would otherwise stack into a thundering herd on restart.
    _disable_auto_retry = bool(os.environ.get("PYTHO_DISABLE_RESTART_AUTO_RETRY"))
    # Auto-retry every record stranded by the previous run. Two cases
    # to catch:
    #   1. state="running" — replay never saw a terminal event. The
    #      previous process crashed (SIGKILL, OOM) before the
    #      register_and_run wrapper could stamp anything. Synthesize
    #      a failed/server_restart transition for the audit log so the
    #      JobEvent timeline has the linkage.
    #   2. state="failed" with error_kind="server_restart" — the
    #      previous process's lifespan shutdown ran cleanly, flipped
    #      _SHUTDOWN_IN_PROGRESS, and the wrapper's CancelledError arm
    #      stamped the terminal transition. No synthesis needed; just
    #      retry. CHECK ONLY THE LATEST RECORD per chain so retries
    #      that previously succeeded (or were user-cancelled later)
    #      don't get re-fired.
    auto_retried = 0
    chain_latest_records = {
        rid: r for rid, r in _RECORDS.items()
        if _CHAIN_LATEST.get(r.chain_head_id) == rid
    }
    for rid, r in list(chain_latest_records.items()):
        retry = False
        if r.state == "running":
            # Crash without lifespan cleanup — synthesize a terminal
            # failed transition first.
            r.state = "failed"
            r.phase = "server_restart"
            r.error = "Job in flight at server restart; auto-retrying."
            r.error_kind = "server_restart"
            r.ended_at_ms = _now_ms()
            _SEQ += 1
            _append_job_event(r, "failed", _SEQ)
            retry = True
        elif r.state == "failed" and r.error_kind == "server_restart":
            # Clean shutdown left this terminal transition. Retry it.
            retry = True
        if retry and _disable_auto_retry:
            # Operator opted out. Audit log already carries the terminal
            # transition; just don't re-fire. Counter stays at 0 so the
            # log line below reads "auto_retried=0" — visible signal that
            # the env override is in effect.
            log.info(
                "product_records replay: auto-retry SKIPPED for "
                "chain=%s record=%s (PYTHO_DISABLE_RESTART_AUTO_RETRY set)",
                r.chain_head_id, r.record_id,
            )
            continue
        if retry:
            try:
                source: Source = r.source
                # request_payload was lost in the event log — caller's
                # dispatcher_for_source must reconstruct from chain
                # state + source-specific lookups (e.g. sweep dispatcher
                # knows how to re-fire by product_type).
                await register_and_run(
                    source=source,
                    product_type=r.product_type,
                    request_payload={},
                    coro_factory=lambda new_r, s=source: dispatcher_for_source(s, new_r),
                    chain_head_id=r.chain_head_id,
                    parent_record_id=r.record_id,
                    attempt_index=r.attempt_index + 1,
                    product_parent_record_id=r.product_parent_record_id,
                    coa_id=r.coa_id,
                    coa_label=r.coa_label,
                    source_metadata=r.source_metadata,
                    # Auto-retry-on-restart: same rule as retry_chain —
                    # mint a fresh session_id at registration so the new
                    # record can't exist with autodraft_session_id=None.
                    autodraft_session_id=f"autodraft-{uuid.uuid4()}",
                )
                auto_retried += 1
            except Exception:
                log.exception("auto-retry on restart failed for chain=%s",
                              r.chain_head_id)
    log.info("product_records replay: rehydrated=%d auto_retried=%d",
             len(_RECORDS), auto_retried)
    # Backfill on-disk artifacts that never registered through the
    # unified path (autodraft sweep before the registry shipped,
    # KLIEGLIGHT pre-registry, manual exports, etc.). Without this
    # step the dashboard would be empty after a fresh boot until the
    # next sweep ran. Failed-row backfill from in-process fire history
    # is the same merge intel_agent.list_autodraft_catalog used to
    # perform — pulled in here so /api/products/records sees it too.
    backfilled = backfill_pre_registry_artifacts()
    if backfilled:
        log.info("product_records replay: backfilled %d pre-registry artifacts",
                 backfilled)
    return len(_RECORDS)


def backfill_pre_registry_artifacts() -> int:
    """Walk autodraft SessionDetail rows, synthesizing chains for any
    pre-registry artifact that has no corresponding ProductRecord.
    Returns the count of new chains minted.

    Idempotent: dedup is by (autodraft_session_id, artifact_id).
    Failure rows are no longer backfilled here — the unified registry
    captures them natively via register_and_run's except-arm.
    """
    minted = 0
    # Build a set of (autodraft_session_id, artifact_id) already
    # represented in the registry so we don't double-register.
    represented_artifacts: set[tuple[str, str]] = set()
    # Sessions already OWNED by a live registry chain (running or complete,
    # not itself a backfill). The artifact-level dedup above only fires once
    # the live chain is complete AND has both fields stamped — so if backfill
    # runs mid-generation (chain running, artifact_id not yet stamped) it
    # would mint a duplicate clickable chain for the same artifact. Skipping
    # any session a live chain already owns closes that race.
    represented_sessions: set[str] = set()
    for r in _RECORDS.values():
        if r.source == "pre_registry":
            continue
        if r.autodraft_session_id and r.state in ("running", "complete"):
            represented_sessions.add(r.autodraft_session_id)
        if r.state == "complete" and r.artifact_id and r.autodraft_session_id:
            represented_artifacts.add((r.autodraft_session_id, r.artifact_id))

    try:
        from templates import PRODUCT_SCHEMAS
        from utils.exporter import PRODUCT_CATEGORIES
    except Exception:
        PRODUCT_SCHEMAS = {}
        PRODUCT_CATEGORIES = {}

    try:
        db = database.SessionLocal()
        try:
            rows = (db.query(database.SessionDetail)
                    .filter(database.SessionDetail.session_id.like("autodraft-%"))
                    .order_by(database.SessionDetail.timestamp.desc())
                    .all())
        finally:
            db.close()
    except Exception:
        log.exception("backfill: SessionDetail walk failed")
        rows = []

    seen_aids: set[str] = set()  # per-walk dedup (same artifact, multiple SessionDetail rows)
    for row in rows:
        try:
            payload = json.loads(row.data or "{}")
        except Exception:
            continue
        trace = payload.get("agent_trace") or {}
        for disp in trace.get("product_writer_dispatches") or []:
            emit = disp.get("emit") or {}
            artifact_id = emit.get("artifact_id")
            if not artifact_id:
                continue
            aid = str(artifact_id)
            if aid in seen_aids:
                continue
            seen_aids.add(aid)
            product_type = (disp.get("artifact_type")
                            or emit.get("artifact_type") or "")
            if not product_type:
                continue
            # Skip uncategorized stray internal types (mirrors the
            # legacy catalog filter — uncategorized rows would render
            # but have no rail home).
            cat_entry = PRODUCT_CATEGORIES.get(product_type) if PRODUCT_CATEGORIES else None
            if cat_entry is None or cat_entry[0] is None:
                continue
            if row.session_id in represented_sessions:
                continue
            if (row.session_id, aid) in represented_artifacts:
                continue
            # Synthesize a one-attempt complete chain.
            head = uuid.uuid4().hex
            rid = head  # head and only record share id for pre-registry chains
            r = ProductRecord(
                record_id=rid,
                chain_head_id=head,
                parent_record_id=None,
                product_parent_record_id=None,
                product_type=product_type,
                coa_id=None,
                coa_label=None,
                source="pre_registry",
                source_metadata={},
                state="complete",
                phase="completed",
                attempt_index=1,
                started_at_ms=row.timestamp or _now_ms(),
                gate_acquired_at_ms=None,
                writer_started_at_ms=None,
                ended_at_ms=row.timestamp or _now_ms(),
                duration_ms=None,
                artifact_id=aid,
                autodraft_session_id=row.session_id,
                error=None,
                error_kind=None,
                request_payload={},
                gate_serial=None,
                cancel_event=None,
            )
            _RECORDS[rid] = r
            _CHAIN_LATEST[head] = rid
            represented_artifacts.add((row.session_id, aid))
            minted += 1

    return minted


# ─── ticket form (support_ticket table) ─────────────────────────────────

def submit_ticket(*, user_email: Optional[str], title: str,
                   description: str, category: Optional[str] = None,
                   db=None) -> int:
    """Insert a SupportTicket row. Returns ticket id."""
    owns_db = db is None
    if owns_db:
        db = database.SessionLocal()
    try:
        t = database.SupportTicket(
            user_email=user_email,
            category=category,
            title=title,
            description=description,
            status="open",
            created_at=_now_ms(),
        )
        db.add(t)
        db.commit()
        return t.id
    finally:
        if owns_db:
            db.close()
