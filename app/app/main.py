import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import logging
import tempfile
import uvicorn

# Module-level logger. Code audit caught `from venv import logger` here
# as a typo from a long-gone scaffold — venv ships a private logger
# object that Just Happened to satisfy logger.info() calls but is wrong
# in every other respect (wrong name, not registered with our config).
logger = logging.getLogger(__name__)
log = logger
from fastapi import FastAPI, HTTPException, Request, status, WebSocket, WebSocketDisconnect, Depends, Body, File, UploadFile, Query, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from handlers import chat, aar, agents, product, intel_agent
from handlers import (
    klieglight_autofire,
    notifications as notifications_handler,
    osir_autofire,
    product_autodraft,
    wopi as wopi_handler,
    cougaar_graph,
)
from routers.api.ext import ext_app
from routers.api import product_types as product_types_router
from utils import database
from utils import oob
from typing import List, Literal, Optional
import hashlib
import json
import logging
from os import path
import re
import shutil
import os
import random
import time
from utils import search, config, events
from datetime import datetime
from faster_whisper import WhisperModel
from threading import Thread


log_file_path = path.join(path.dirname(path.abspath(__file__)), 'logging.conf')
FILE_HOST_API = '/files/'

# IBL ships with a single hardcoded project per user (`default-{userId}`,
# see handlers/agents.py + handlers/intel_agent.py). The /api/projects/{id}
# DELETE route shouldn't exist in IBL at all, but the frontend still wires
# it; treat any delete targeted at the IBL hardcoded project as a no-op
# + warning so a stray click doesn't wipe the analyst's only project.
# Env var lets non-IBL deploys override.
IBL_HARDCODED_PROJECT_PREFIX = os.environ.get(
    "IBL_HARDCODED_PROJECT_PREFIX", "default-"
)


def _is_ibl_hardcoded_project(project_id: str) -> bool:
    """True if `project_id` is the IBL single-project sentinel.
    Matches the prefix used by handlers/agents.py + intel_agent.py:
    `default-{userId}` (where empty userId → bare 'default')."""
    if not project_id:
        return False
    return (
        project_id == "default"
        or project_id.startswith(IBL_HARDCODED_PROJECT_PREFIX)
    )

# setup loggers
logging.config.fileConfig(log_file_path, disable_existing_loggers=False)
  
class ConnectionManager:
    # def __init__(self):
    #     self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        # self.active_connections.append(websocket)

    # def disconnect(self, websocket: WebSocket):
    #     self.active_connections.remove(websocket)

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_text(message)

    # async def broadcast(self, message: str):
    #     for connection in self.active_connections:
    #         await connection.send_text(message)


# Shared set of currently-open /ws/intel_agent sockets. notifications_handler
# iterates this set to broadcast {"type":"notification"} frames to every
# connected analyst. Mutated by the websocket_intel_agent route on accept +
# disconnect. NOT used by the per-session multiplexing layer — that is
# handled by frame sessionId routing on the frontend.
_ACTIVE_INTEL_SOCKETS: set = set()


async def _ws_broadcast(frame: dict) -> None:
    """Fan out a frame to every open /ws/intel_agent socket. Used by
    the unified jobs system (handlers/product_records.py) to broadcast
    product_record_upsert / product_record_delete / gate_stats /
    product_records_snapshot frames on every state transition.

    Best-effort: a stale socket that .send_json raises on is removed
    from the set; never raises out of this helper.
    """
    import json as _json
    payload = _json.dumps(frame, default=str)
    dead = []
    for ws in list(_ACTIVE_INTEL_SOCKETS):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _ACTIVE_INTEL_SOCKETS.discard(ws)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Unified startup + shutdown via the FastAPI lifespan handler.

    Replaces the deprecated @app.on_event("startup") / @app.on_event("shutdown")
    pair (removed in Starlette's roadmap). Lifespan gives us a single try/yield/
    finally so a startup-half failure still runs shutdown cleanup.

    Body references module-level globals (manager, _ACTIVE_INTEL_SOCKETS,
    etc.) that are defined later in this module — that's
    fine because the function is only invoked by uvicorn AFTER module
    import finishes, at which point every global exists.
    """
    # ── Startup ──────────────────────────────────────────────────────
    database.Base.metadata.create_all(bind=database.engine)

    # Launch Whisper model loading in a separate thread — non-blocking.
    # Model files MUST be pre-staged at ${PYTHO_DATA:-./data}/models/
    # (bind-mounted to /app/models in container; faster_whisper's
    # download_root). load_whisper_model() will skip the HF download and
    # use the local files. For the airgapped IBL deployment the model is
    # Systran/faster-whisper-medium.en (~750 MB). If the dir is empty and
    # the deployment is online, faster_whisper will download on first
    # call; if airgapped, it will crash — pre-stage the files BEFORE
    # going offline.
    thread = Thread(target=load_whisper_model, daemon=True)
    thread.start()
    print("Startup event triggered — Whisper model loading in background thread.")

    # ── Cloud-surface startup (edge+cloud unification) ───────────────
    # rfi column migrations + the thread-pool executor the ported
    # routers/ws handlers use. start_whisper=False: THIS lifespan owns
    # whisper loading (load_whisper_model publishes into core.startup's
    # global so the ported transcription routes see the same model).
    from core import startup as _cloud_core_startup
    await _cloud_core_startup.startup_event(start_whisper=False)

    # ── Unified jobs registry wiring ─────────────────────────────────
    # Wire the broadcast callback so handlers/product_records.py can
    # fan out state transitions to every connected analyst's WS. Then
    # replay the JobEvent audit log to rehydrate _RECORDS, auto-retrying
    # any record left in-flight by a prior restart (V4 lock-in).
    from handlers import product_records as _pr
    _pr.set_broadcast_callback(_ws_broadcast)

    async def _restart_dispatcher(source, new_record):
        """Replay-time dispatcher — re-fires by source.

        klieglight_autofire / osir_autofire records persist their raw
        record into request_payload (commit a6817db), so on restart we
        can pull `raw_record` straight back out and call the source's
        _fire_one with the registered record threaded through — same
        contract as the *_registered wrappers in
        handlers/{klieglight,osir}_autofire.py:_fire_one_registered.

        Never call a *_registered wrapper from here — those would mint
        a SEPARATE chain whose artifact never stamps onto this
        auto-retry record. Dispatch to the raw worker with the new
        ProductRecord threaded in via _registered_record / direct
        record_transition calls so the worker's transitions land on
        the right record.
        """
        from handlers import product_autodraft as _pa
        src = (source or "").strip()
        payload = getattr(new_record, "request_payload", None) or {}
        raw_record = payload.get("raw_record") if isinstance(payload, dict) else None
        # PROJECT threading — autofires are per project/event; the fire
        # wrappers stamp project_id into source_metadata so a retry
        # lands in the SAME project's pool and map.
        _meta = getattr(new_record, "source_metadata", None) or {}
        retry_project_id = (_meta.get("project_id") or "").strip() \
            if isinstance(_meta, dict) else ""

        if src == "klieglight_autofire":
            from handlers import klieglight_autofire
            from handlers import product_records as _pr2
            if not raw_record:
                return {"product_type": new_record.product_type,
                        "error": "klieglight raw_record missing from request_payload; "
                                 "KLIEGLIGHT sweep will re-discover the signal."}
            try:
                entry = await klieglight_autofire._fire_one(raw_record, project_id=retry_project_id)
            except Exception as exc:
                return {"product_type": new_record.product_type,
                        "error": f"klieglight _fire_one raised: {type(exc).__name__}: {exc}"}
            # Mirror _fire_one_registered's terminal stamping so the
            # auto-retry record reflects success/failure correctly.
            if entry.get("artifact_id") and not entry.get("error"):
                try:
                    await _pr2.record_transition(
                        new_record.record_id,
                        event_type="phase",
                        phase="completed",
                        artifact_id=entry["artifact_id"],
                        autodraft_session_id=entry.get("autodraft_session_id"),
                    )
                except Exception:
                    log.exception("klieglight restart: record_transition failed")
            elif entry.get("error"):
                # Let register_and_run's except-arm flip to failed.
                raise RuntimeError(str(entry["error"]))
            return entry

        if src == "osir_autofire":
            from handlers import osir_autofire
            from handlers import product_records as _pr2
            if not raw_record:
                return {"product_type": new_record.product_type,
                        "error": "osir raw_record missing from request_payload; "
                                 "OSIR sweep will re-discover the OSINT."}
            try:
                entry = await osir_autofire._fire_one(raw_record, project_id=retry_project_id)
            except Exception as exc:
                return {"product_type": new_record.product_type,
                        "error": f"osir _fire_one raised: {type(exc).__name__}: {exc}"}
            if entry.get("artifact_id") and not entry.get("error"):
                try:
                    await _pr2.record_transition(
                        new_record.record_id,
                        event_type="phase",
                        phase="completed",
                        artifact_id=entry["artifact_id"],
                        autodraft_session_id=entry.get("autodraft_session_id"),
                    )
                except Exception:
                    log.exception("osir restart: record_transition failed")
            elif entry.get("error"):
                raise RuntimeError(str(entry["error"]))
            return entry

        if src == "bda_autofire":
            from handlers import bda_autofire as _bda
            from handlers import product_records as _pr3
            if not raw_record:
                return {"product_type": new_record.product_type,
                        "error": "bda raw_record missing from request_payload; "
                                 "BDA sweep will re-discover the RECCEXREP."}
            try:
                entry = await _bda._fire_one(raw_record, project_id=retry_project_id)
            except Exception as exc:
                return {"product_type": new_record.product_type,
                        "error": f"bda _fire_one raised: {type(exc).__name__}: {exc}"}
            if entry.get("artifact_id") and not entry.get("error"):
                try:
                    await _pr3.record_transition(
                        new_record.record_id,
                        event_type="phase",
                        phase="completed",
                        artifact_id=entry["artifact_id"],
                        autodraft_session_id=entry.get("autodraft_session_id"),
                    )
                except Exception:
                    log.exception("bda restart: record_transition failed")
            elif entry.get("error"):
                raise RuntimeError(str(entry["error"]))
            return entry

        # Default: autodraft path — covers autodraft_sweep / refire /
        # rest_generate_async / ws_generate_product / sitemp_fanout.
        # coa_focus is unavailable post-restart for sitemp_fanout
        # children; if needed it will be picked up by the SITEMP
        # fanout re-run on next sweep.
        return await _pa._autodraft_one(
            new_record.product_type,
            _registered_record=new_record,
        )

    try:
        rehydrated = await _pr.replay_from_db_and_auto_retry(
            dispatcher_for_source=_restart_dispatcher,
        )
        print(f"Unified jobs registry: rehydrated {rehydrated} records "
              f"(server_restart in-flight rows auto-retried).")
    except Exception as e:
        print(f"Unified jobs registry replay failed (non-fatal): {e}")

    # ------------------------------------------------------------------
    # Notifications wiring — the autofire notification emitters stay
    # connected (fires now happen at PROJECT CREATION via the
    # handlers/projects bootstrap, not from a poller).
    # ------------------------------------------------------------------
    # The Cougaar recent-intel poll plane is DEAD AND DISCONNECTED
    # (2026-07-09): no 30s poller, no poll callbacks, no startup sweeps.
    # Scenario data is static until scenario-update machinery exists, so
    # every per-project producer (OOB push, scenario terrain push,
    # signal-report autofires, the product autodraft sweep) runs ONCE
    # per project when the project is created.
    notifications_handler.register_connection_manager(manager, _ACTIVE_INTEL_SOCKETS)
    notifications_handler.wire_klieglight_autofire()
    notifications_handler.wire_osir_autofire()

    # ------------------------------------------------------------------
    # Manual injects folder — analyst-swappable PROJECT_DOCS source
    # ------------------------------------------------------------------
    # Overlays the inline scenario.PROJECT_DOCS catalog with analyst-dropped
    # docx files at event time. Default mount point is /app/manual_injects
    # (bind from /home/pytho/pytho-app/manual_injects/
    # on spark-dev; symlink to an SMB share for event-day swap). Initial
    # synchronous load so PROJECT_DOCS reflects on-disk docx before the
    # first request lands. Background watcher polls every 30s for mtime
    # changes and re-merges.
    from utils import manual_injects as _manual_injects
    await _manual_injects.initial_load()
    asyncio.create_task(
        _manual_injects.watcher_loop(),
        name="manual_injects_watcher",
    )
    print("Manual injects watcher started — PROJECT_DOCS now reflects /app/manual_injects/*.docx (poll 30s).")

    # Scenario clocks are PER PROJECT now — each event's frozen-now
    # anchor computes lazily on its first canonical-now read inside that
    # project's own context (and recomputes when the manual-injects
    # overlay rev changes). Nothing to freeze at startup: there is no
    # global clock and no default event.

    # ── Yield to the running application ─────────────────────────────
    try:
        yield
    finally:
        # ── Shutdown ─────────────────────────────────────────────────
        # Cancel the recent-intel poller cleanly on shutdown so uvicorn's
        # reload doesn't leave an orphaned task printing stack traces.
        # Flip the unified jobs registry's shutdown flag BEFORE the asyncio
        # cancellation cascade so the register_and_run wrapper stamps
        # in-flight records as error_kind='server_restart' (auto-retry on
        # next boot) rather than 'cancelled' (which looks like a user
        # cancel and would NOT auto-retry).
        try:
            from handlers import product_records as _pr_shutdown
            _pr_shutdown.signal_shutdown()
        except Exception:
            log.exception("signal_shutdown failed during lifespan teardown")
        # Cloud-surface teardown: drain the thread-pool executor the
        # ported routers/ws handlers use.
        try:
            from core import startup as _cloud_core_shutdown
            await _cloud_core_shutdown.shutdown_event()
        except Exception:
            log.exception("cloud shutdown_event failed during lifespan teardown")


app = FastAPI(title="Anvil-IPOE", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    # NOTE: allow_credentials=True is DELIBERATE — keep it.
    # The pytho-app stack is a permissive internal mesh (sign-in, Collabora
    # WOPI, tileserver, IRC) and previous attempts to flip this to False
    # broke sign-in (2026-06-03). Yes, allow_origins="*" + credentials=true
    # is a spec violation that some browsers will reject when an actual
    # Cookie is present on the request — but the analyst flow's cookies are
    # set with the right SameSite/path attributes so the affected paths
    # never combine the two. Do NOT "fix" this without an explicit
    # "audit for hardening" instruction from the lead; the no-unsolicited-
    # security-hardening rule in CLAUDE.md MEMORY tracks the recurrence.
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TODO: put it in env
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
app.mount("/files", StaticFiles(directory=UPLOAD_DIR), name=UPLOAD_DIR)


database.init_db()

app.include_router(product_types_router.router, prefix="/api")
# WOPI host for Collabora XLSX editing. Routes are prefix-less because
# WOPI requires both /api/wopi/* (token mint, browser-facing) and
# /wopi/files/* (Collabora-facing, hit from inside the docker network).
app.include_router(wopi_handler.router)
# Canonical-on-disk finalize endpoints — POST /api/artifacts/{aid}/finalize
# + POST /api/artifacts/{aid}/refire-immediate-downstream. The router
# exposes both endpoints at module scope inside handlers/product.py.
app.include_router(product.router)
# External partner API sub-app: own OpenAPI schema + Swagger UI at
# /api/ext/docs. Mounted apps bypass the main app's CORS middleware — fine
# here, the ext API is server-to-server (X-API-Key), not browser-facing.
app.mount("/api/ext", ext_app)

manager = ConnectionManager()


class User(BaseModel):
    userId: str
    email: str
    firstName: Optional[str] = None
    lastName: Optional[str] = None

class UserIn(BaseModel):
    email: str
    # Optional for the CLOUD sign-in contract: the cloud SPA verifies the
    # password against Cognito first and POSTs /api/auth with email only
    # (this service just resolves the user record). The edge SPA has no
    # Cognito and always sends the password for local verification.
    password: str | None = None

class SignUpIn(BaseModel):
    email: str
    password: str
    firstName: Optional[str] = None
    lastName: Optional[str] = None



class DocumentIn(BaseModel):
    user_id: str
    file_id: str
    title: str
    link: str

class DocumentQueryIn(BaseModel):
    query: Optional[str] = None

class DoctrineChatIn(BaseModel):
    question: str
    selected_doc_ids: List[str] = []








class FeedbackCreate(BaseModel):
    sentiment: Literal['liked', 'disliked']
    question: str
    answer: str
    comment: Optional[str] = None
    session_id: Optional[str] = None
    project_id: Optional[str] = None

class FeedbackUpdate(BaseModel):
    comment: str

class FeedbackResponse(BaseModel):
    id: int
    user_email: str
    sentiment: str
    question: str
    answer: str
    comment: Optional[str]
    session_id: Optional[str]
    project_id: Optional[str]
    created_at: datetime

    class Config:
        from_attributes = True

_client_logger = logging.getLogger("pytho.client")


class ClientLogPayload(BaseModel):
    """Forwarded frontend error/warning. Browsers never display console output
    to analysts in the field — we surface these through the same logging
    pipeline backend errors flow through, so a failed WS reconnect or a
    swallowed render exception shows up in container logs instead of dying
    silently in DevTools."""
    level: Literal["debug", "info", "warn", "error"] = "warn"
    label: str = Field(..., max_length=200)
    message: str = Field("", max_length=4000)
    url: Optional[str] = Field(None, max_length=2000)
    user_agent: Optional[str] = Field(None, max_length=500)
    stack: Optional[str] = Field(None, max_length=8000)
    context: Optional[dict] = None


@app.post("/api/client_log")
async def client_log(payload: ClientLogPayload):
    """Single-shot endpoint for the UI to forward errors/warnings. Fire-and-
    forget on the client (sendBeacon / fetch keepalive). Logged to the
    `pytho.client` logger so logging.conf can route it separately from app
    logs if desired."""
    level = {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }[payload.level]
    _client_logger.log(
        level,
        "[client] %s | %s | url=%s ua=%s ctx=%s%s",
        payload.label,
        payload.message,
        payload.url or "-",
        (payload.user_agent or "-")[:120],
        json.dumps(payload.context or {}, default=str)[:1000],
        f"\n{payload.stack}" if payload.stack else "",
    )
    return {"ok": True}


@app.get("/api/")
def read_root():
    return {"Hello": "World"}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    from handlers.intel_agent import _JOBS
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"job {job_id} not in registry")
    return job


# ─────────────────────────────────────────────────────────────────
# Unified jobs system — handlers/product_records.py is the registry.
# Design: scratch/tmp/jobs-system-design.md (v2-final).
# /api/jobs/{job_id} above is kept as the chat-resilience refresh hook
# used by DocEditorPage on mid-generation refresh (different code path).
# ─────────────────────────────────────────────────────────────────

@app.get("/api/products/records")
def products_records_endpoint(state: Optional[str] = None):
    """Unified chain list — one row per logical job (chain_head_id).
    Each chain shows the LATEST attempt's state, phase, artifact,
    error. Default sort: started_at_ms DESC. Frontend re-sorts client-
    side per column.

    Optional `state` filter ("running" | "complete" | "failed") so the
    /api/products/catalog alias can hand-off here.
    """
    from handlers import product_records
    chains = product_records.list_chains()
    if state:
        chains = [c for c in chains if c.get("state") == state]
    # vllm gate stats for the live "#N in queue / N running" badges.
    gate_stats = {}
    try:
        from utils import llm
        gate_stats = llm.vllm_gate_stats()
    except Exception:
        pass
    return JSONResponse(content={
        "chains": chains,
        "gate": gate_stats,
        "as_of_seq": product_records.snapshot()["as_of_seq"],
    }, status_code=200)


@app.post("/api/products/records/{record_id}/cancel")
async def products_record_cancel_endpoint(record_id: str):
    """Cancel a running record. Idempotent — cancel on a terminal row
    returns the current state with 200. Side effects: sets the
    record's cancel_event, pulls from vllm gate waiter queue if
    pre-gate, propagates task.cancel(). Cascade signals fire for
    autodraft_sweep records (cancels remainder of sweep) and Threat-
    COA-Statement parents (cancels child SITEMP fanout)."""
    from handlers import product_records
    r = await product_records.cancel_record(record_id)
    if r is None:
        raise HTTPException(status_code=404, detail=f"record {record_id} not found")
    return JSONResponse(content={"ok": True, "record_id": record_id,
                                  "state": r.state}, status_code=200)


@app.post("/api/products/chains/{chain_head_id}/retry")
async def products_chain_retry_endpoint(chain_head_id: str, by: str = ""):
    """Retry the latest record of a chain. Valid only when the latest
    is `failed`. Mints a new record sharing the same chain_head_id;
    parent_record_id points at the failed parent; attempt_index += 1.
    The frontend reducer shows the SAME displayed row whose state
    flips back to `running` — no new row in the UI.

    `by` is the user who triggered the retry; it overrides the parent's
    stored generator so the "generating by" attribution reflects whoever
    kicked off the regen rather than the original creator."""
    from handlers import product_records
    from handlers import product_autodraft

    # Dispatcher chooses how to re-fire based on the original source.
    # IMPORTANT: never call any *_registered wrapper here — those wrap
    # with register_and_run, which would spawn a SEPARATE chain (new
    # chain_head_id) whose artifact never lands on the retry record.
    # Dispatch to the raw worker with the new ProductRecord threaded
    # in, so the artifact_id / autodraft_session_id / writer_started
    # transitions stamp onto the retry's own record.
    async def _dispatcher(new_record):
        src = (new_record.source or "").strip()
        # PROJECT threading — same contract as the restart dispatcher:
        # source_metadata.project_id routes the retry into the original
        # fire's project pool and map.
        _meta = getattr(new_record, "source_metadata", None) or {}
        retry_project_id = (_meta.get("project_id") or "").strip() \
            if isinstance(_meta, dict) else ""
        # Product autodraft path covers sweep, refire, REST and WS
        # generate, and sitemp fanout children (they too come from
        # _autodraft_one). coa_focus comes off the parent payload if
        # the chain was a per-COA SITEMP child.
        coa_focus = None
        try:
            payload = new_record.request_payload or {}
            cf = payload.get("coa_focus") or None
            if isinstance(cf, dict) and cf:
                coa_focus = cf
        except Exception:
            coa_focus = None
        if src in (
            "autodraft_sweep", "refire", "rest_generate_async",
            "ws_generate_product", "sitemp_fanout",
        ):
            return await product_autodraft._autodraft_one(
                new_record.product_type,
                coa_focus=coa_focus,
                _registered_record=new_record,
            )
        if src == "klieglight_autofire":
            from handlers import klieglight_autofire
            raw = (new_record.request_payload or {}).get("raw_record") or {}
            entry = await klieglight_autofire._fire_one(raw, project_id=retry_project_id)
            # Post-hoc stamp — _fire_one doesn't accept _registered_record
            # yet, so transition the retry record manually after the
            # worker returns. register_and_run completes the state
            # transition once the coroutine returns.
            from handlers import product_records as _pr
            if isinstance(entry, dict) and entry.get("artifact_id"):
                await _pr.record_transition(
                    new_record.record_id,
                    artifact_id=entry.get("artifact_id"),
                    autodraft_session_id=entry.get("autodraft_session_id"),
                )
            return entry
        if src == "osir_autofire":
            from handlers import osir_autofire
            raw = (new_record.request_payload or {}).get("raw_record") or {}
            entry = await osir_autofire._fire_one(raw, project_id=retry_project_id)
            from handlers import product_records as _pr
            if isinstance(entry, dict) and entry.get("artifact_id"):
                await _pr.record_transition(
                    new_record.record_id,
                    artifact_id=entry.get("artifact_id"),
                    autodraft_session_id=entry.get("autodraft_session_id"),
                )
            return entry
        if src == "bda_autofire":
            from handlers import bda_autofire
            raw = (new_record.request_payload or {}).get("raw_record") or {}
            entry = await bda_autofire._fire_one(raw, project_id=retry_project_id)
            from handlers import product_records as _pr
            if isinstance(entry, dict) and entry.get("artifact_id"):
                await _pr.record_transition(
                    new_record.record_id,
                    artifact_id=entry.get("artifact_id"),
                    autodraft_session_id=entry.get("autodraft_session_id"),
                )
            return entry
        # Unknown / pre-registry sources fall through to autodraft.
        return await product_autodraft._autodraft_one(
            new_record.product_type,
            coa_focus=coa_focus,
            _registered_record=new_record,
        )

    new_id = await product_records.retry_chain(chain_head_id,
                                                 dispatcher=_dispatcher,
                                                 by=by)
    if new_id is None:
        raise HTTPException(status_code=409, detail={
            "error": "illegal_transition",
            "message": "chain not retriable — latest must be `failed`",
        })
    return JSONResponse(content={
        "ok": True,
        "chain_head_id": chain_head_id,
        "new_record_id": new_id,
    }, status_code=200)


@app.delete("/api/products/records/{record_id}")
async def products_record_delete_endpoint(record_id: str,
                                            db: Session = Depends(database.get_db)):
    """Delete a terminal chain — removes the artifact (if any) AND every
    record of the chain from _RECORDS. JobEvent audit log stays
    forever. Valid when the latest record of the chain is `complete` or
    `failed`; a `running` chain must be cancelled first."""
    from handlers import product_records
    r = product_records._RECORDS.get(record_id)  # type: ignore[attr-defined]
    if r is None:
        raise HTTPException(status_code=404, detail=f"record {record_id} not found")
    ok = await product_records.delete_chain(r.chain_head_id, db=db)
    if not ok:
        raise HTTPException(status_code=409, detail={
            "error": "illegal_transition",
            "message": "chain not deletable — latest must be `complete` or `failed`",
        })
    return JSONResponse(content={"ok": True, "chain_head_id": r.chain_head_id},
                         status_code=200)


@app.get("/api/products/chains/{chain_head_id}/events")
def products_chain_events_endpoint(chain_head_id: str,
                                     db: Session = Depends(database.get_db)):
    """Full JobEvent audit history for one chain — every attempt's
    transition, every error, every cancel. Drives any future "see
    history" UI. Per Q5 the log is kept forever."""
    rows = (db.query(database.JobEvent)
            .filter(database.JobEvent.chain_head_id == chain_head_id)
            .order_by(database.JobEvent.seq.asc())
            .all())
    return JSONResponse(content={
        "chain_head_id": chain_head_id,
        "events": [{
            "seq": r.seq, "record_id": r.record_id,
            "event_type": r.event_type, "state_after": r.state_after,
            "phase": r.phase, "attempt_index": r.attempt_index,
            "artifact_id": r.artifact_id, "error": r.error,
            "error_kind": r.error_kind, "emitted_at": r.emitted_at,
            "payload": r.payload,
        } for r in rows],
    }, status_code=200)


# ─── tickets (V5b: left-nav "Submit Feedback") ────────────────────

class TicketIn(BaseModel):
    title: str
    description: str
    category: Optional[str] = None
    user_email: Optional[str] = None


@app.post("/api/tickets")
def tickets_create_endpoint(body: TicketIn,
                              db: Session = Depends(database.get_db)):
    """Submit a support ticket from the left-nav feedback form.
    Saves to support_ticket table. Replaces the prior "Email Us"
    mailto modal."""
    from handlers import product_records
    if not (body.title or "").strip():
        raise HTTPException(status_code=400, detail="title is required")
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail="description is required")
    tid = product_records.submit_ticket(
        user_email=body.user_email,
        title=body.title.strip(),
        description=body.description.strip(),
        category=(body.category or "").strip() or None,
        db=db,
    )
    return JSONResponse(content={"ok": True, "ticket_id": tid},
                         status_code=200)


@app.get("/api/tickets")
def tickets_list_endpoint(db: Session = Depends(database.get_db)):
    """List submitted tickets newest-first. Engineering pokes this
    for visibility."""
    rows = (db.query(database.SupportTicket)
            .order_by(database.SupportTicket.created_at.desc())
            .all())
    return JSONResponse(content={
        "tickets": [{
            "id": r.id, "user_email": r.user_email,
            "category": r.category, "title": r.title,
            "description": r.description, "status": r.status,
            "created_at": r.created_at,
        } for r in rows],
    }, status_code=200)




@app.post("/api/signup", response_model=User)
def signup_user(req: SignUpIn, db: Session = Depends(database.get_db)):
    email = (req.email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required.")
    if not req.password:
        raise HTTPException(status_code=400, detail="A password is required.")
    if db.query(database.User).filter(database.User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with that email already exists.")
    row = database.User(
        email=email,
        password=req.password,
        first_name=req.firstName,
        last_name=req.lastName,
        created_at=int(time.time() * 1000),
    )
    db.add(row)
    db.commit()
    return {"userId": email, "email": email,
            "firstName": req.firstName, "lastName": req.lastName}


@app.post("/api/reset_password", response_model=User)
def reset_password(userIn: UserIn, db: Session = Depends(database.get_db)):
    # Trust-based reset (no email/token — on-prem closed network): anyone
    # who knows the email sets a new password. Returns the user so the
    # frontend can log straight in.
    email = (userIn.email or "").strip().lower()
    if not userIn.password:
        raise HTTPException(status_code=400, detail="A new password is required.")
    row = db.query(database.User).filter(database.User.email == email).first()
    if row is None:
        raise HTTPException(status_code=404, detail="No account with that email.")
    row.password = userIn.password
    db.commit()
    return {"userId": row.email, "email": row.email,
            "firstName": row.first_name, "lastName": row.last_name}



@app.get("/api/sessions/{session_id}/messages")
def list_session_messages_endpoint(
    session_id: str, since: int = 0, limit: int = 1000,
    db: Session = Depends(database.get_db),
):
    """Return chat-history segments for `session_id`, ordered by timestamp asc.

    `since` (optional, ms-precision timestamp) returns rows strictly after
    that timestamp — used by the frontend on WS reconnect to backfill
    segments that landed during the disconnect. `?since=0` (the default)
    returns the full history.

    The frontend ChatAgent calls this on mount to render persisted chat
    history (survives F5 / sidebar navigation / multi-tab), then appends
    live WS segments as they arrive. The WS payload for a segment is the
    same shape this endpoint returns for that segment, so the merge is a
    straight append keyed on `id`.

    Returns `{messages: [...], session_id, since}`. Each message:
      {id, created_at_ms, role, segment_type, content,
       artifact_id?, artifact_type?, job_id?, ...}
    """
    messages = intel_agent.list_session_messages(db, session_id=session_id,
                                                    since_ts=since, limit=limit)
    return JSONResponse(content={
        "session_id": session_id,
        "since": since,
        "messages": messages,
    }, status_code=200)


@app.get("/api/sessions/{session_id}/artifacts")
def list_session_artifacts_endpoint(
    session_id: str, db: Session = Depends(database.get_db)
):
    """Return every artifact already emitted into this session.

    Powers the Generate-button refresh-safety fix in DocEditorPage: on mount
    the page queries this endpoint, and if any artifact already exists for
    `session_id` it skips firing the synthesized "Generate {TYPE}" WS message
    and loads the existing artifact instead. Without this guard a page
    refresh during a 100-150s generation kicks off a SECOND generation in
    parallel and the analyst sees the placeholder again.

    Thin route — walks `SessionDetail.agent_trace.product_writer_dispatches`
    via `intel_agent.list_session_artifacts`.
    """
    artifacts = intel_agent.list_session_artifacts(session_id, db)
    return JSONResponse(content={"session_id": session_id,
                                  "artifacts": artifacts}, status_code=200)


@app.post("/api/products/threat_coas/refire_chain")
async def threat_coas_refire_chain_endpoint(by: str = "", force: bool = False, project_id: str = ""):
    """Refire the entire in-combat threat-analysis chain end-to-end:

        Threat Overlay -> Threat COA Statement -> SITEMP (per-COA fanout)
        -> Threat COAs

    This is the operations-map "Refresh" entry point on the Threat COAs
    parent row. It regenerates every step so the downstream Threat COAs
    PPTX consumes fresh upstream artifacts. Threat Overlay completes
    before Threat COA Statement starts; SITEMP runs as a per-COA fanout
    against the freshly produced Threat COA Statement (and back-fills
    coa_slides[*].sitemp_artifact_id into the Threat COA Statement); then
    Threat COAs (consumes_artifacts=['Threat COA Statement','SITEMP'])
    runs last against the new upstream artifacts.

    Fire-and-forget per CLAUDE.md "Never wait synchronously on an
    async-by-nature server call". The handler spawns the chain via
    asyncio.create_task and returns 202 immediately; per-product state,
    attribution, JobEvent rows, the "generating" pill, and the
    `product_generated` notification all flow through the existing
    register_and_run path inside _autodraft_one_registered — no
    duplicate state machine, no new chain id concept, no new broadcast
    frame, no new fanout helper.

    `refresh_drafts` (handlers/product_autodraft.py:1293) already
    iterates the requested list in order and awaits each product before
    starting the next, and dispatches SITEMP through
    _autodraft_sitemp_fanout. The chain ordering is satisfied by
    passing the list in dependency order. See refresh_drafts for the
    canonical fire path — do not invent a parallel implementation here.

    `by` (optional) — analyst email, threaded into source_metadata for
    attribution. Matches /api/products/generate's `by` param.

    `force` (optional, default False) — bypass every autodraft-pool
    dedup / cache-hit gate. Normal operator refires already skip the
    sweep-dedup (source=rest_generate_async), but any deeper cache
    check (has_active_chain_for_product, "recent artifact exists",
    dep-cache reuse in the DAG) is what `force=True` bulldozes
    through. Use when you know an artifact for this product is
    already in the pool AND you want a genuinely new one — e.g.,
    after deploying render-path code and needing to prove the change
    took effect end-to-end.
    """
    from templates import PRODUCT_SCHEMAS

    chain = ["Threat Overlay", "Threat COA Statement", "SITEMP", "Threat COAs"]
    # Paranoia: every name in the chain is stable and registered in
    # PRODUCT_SCHEMAS, but guard against a future rename leaving this
    # endpoint silently no-op'ing.
    for pt in chain:
        if pt not in PRODUCT_SCHEMAS:
            raise HTTPException(
                status_code=404,
                detail=f"unknown product_type {pt!r} in threat-analysis chain")

    asyncio.create_task(product_autodraft.refresh_drafts(chain, by=by, force=force, project_id=project_id))
    return JSONResponse(content={
        "ok": True,
        "status": "firing",
        "products": chain,
        "by": by or None,
        "force": force,
    }, status_code=202)


@app.post("/api/products/threat_model/refire_chain")
async def threat_model_refire_endpoint(by: str = "", force: bool = False, project_id: str = ""):
    """Refire Threat Model (Threat Template). Single-product chain —
    product_autodraft resolves the consumes_artifacts deps (SIGSUM /
    HUMINTSUM / OSIR / DonovianDoctrine) from existing artifacts. Same
    fire-and-forget contract as /api/products/threat_coas/refire_chain.
    `force` (default False) bypasses autodraft-pool dedup — see the
    threat_coas endpoint for the full semantics.
    """
    from templates import PRODUCT_SCHEMAS
    if "Threat Model" not in PRODUCT_SCHEMAS:
        raise HTTPException(status_code=404,
                              detail="Threat Model not registered")
    asyncio.create_task(product_autodraft.refresh_drafts(
        ["Threat Model"], by=by, force=force, project_id=project_id))
    return JSONResponse(content={
        "ok": True, "status": "firing",
        "products": ["Threat Model"], "by": by or None, "force": force,
    }, status_code=202)


@app.post("/api/products/event_template/refire_chain")
async def event_template_refire_endpoint(by: str = "", force: bool = False, project_id: str = ""):
    """Refire Event Template. Single-product chain — product_autodraft
    resolves consumes_artifacts deps (SITEMP / Threat COA Statement /
    DonovianDoctrine) from existing artifacts. Fire-and-forget.
    `force` (default False) bypasses autodraft-pool dedup — see the
    threat_coas endpoint for the full semantics.
    """
    from templates import PRODUCT_SCHEMAS
    if "Event Template" not in PRODUCT_SCHEMAS:
        raise HTTPException(status_code=404,
                              detail="Event Template not registered")
    asyncio.create_task(product_autodraft.refresh_drafts(
        ["Event Template"], by=by, force=force, project_id=project_id))
    return JSONResponse(content={
        "ok": True, "status": "firing",
        "products": ["Event Template"], "by": by or None, "force": force,
    }, status_code=202)


@app.post("/api/products/sitemp/repush_latest")
async def sitemp_repush_latest_endpoint():
    """Re-derive writer overlays (axes / NAIs / DPs / threat objectives /
    range fans) for the latest per-COA SITEMP artifact set and push
    each to its SITEMP:<coa_id> bucket on the master map. Used when
    new derivation logic lands (e.g., per-equipment range fans) and
    analysts need the overlays on the live ops map without waiting
    for the next full Threat COAs chain refire.

    Walks uploads/_autodraft/SITEMP_*, dedups by coa_id (latest per
    COA), and re-runs the writer-overlay chain against each artifact's
    schema_state.json + a fresh cougaar OOB pull. Returns per-artifact
    results.
    """
    from pathlib import Path as _Path
    upload_dir = _Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))
    result = await intel_agent.repush_latest_sitemp_set(upload_dir)
    return JSONResponse(content={"ok": True, **result}, status_code=200)


@app.post("/api/products/scenario_terrain/refire")
async def scenario_terrain_refire_endpoint():
    """Operator-driven refresh of the master-map scenario_terrain layer.

    Mirrors /api/products/threat_coas/refire_chain but for the static
    scenario range_fans + terrain primitives that today are gated behind
    on_poll_sync_oob — which hard-fails when Cougaar GraphQL is
    unreachable, leaving the master map's range_fans stale (RF-60/62/66-
    FA naming etc.) with no operator-driven path to refresh short of
    restarting docker-api or fixing Cougaar.

    Re-invokes intel_agent._push_scenario_terrain_to_geo_agent against
    EVERY active project's master map (map uuid == projectId; the
    legacy master map when no event records exist yet). Fire-and-forget;
    returns 202 immediately. No Cougaar dependency.
    """
    async def _do_refire():
        try:
            # Lazy import — _push_scenario_terrain_to_geo_agent lives
            # next to the scenario_terrain push pipeline in intel_agent.
            push_fn = getattr(intel_agent, "_push_scenario_terrain_to_geo_agent", None)
            if not callable(push_fn):
                logger.warning(
                    "scenario_terrain refire: "
                    "_push_scenario_terrain_to_geo_agent not available "
                    "— skipping"
                )
                return
            from utils.project_context import set_active_project
            for pid in intel_agent.active_project_ids():
                set_active_project(pid)
                push_fn(intel_agent._project_map_uuid(pid))
        except Exception as e:
            logger.exception("scenario_terrain refire failed: %s", e)

    asyncio.create_task(_do_refire())
    return JSONResponse(content={
        "ok": True,
        "status": "firing",
    }, status_code=202)


@app.get("/api/products/autodraft_latest")
def autodraft_latest_endpoint(
    product_type: str, db: Session = Depends(database.get_db)
):
    """Return the latest autodraft artifact for the requested product type.

    Powers DocEditorPage's cache-read on doc-open: when the analyst hits
    Generate for a product whose autodraft pool already has a warm draft,
    the page loads the autodraft directly instead of firing a full DAG
    run. ~instant load vs minutes of regen.

    Returns the same shape as /api/sessions/<id>/artifacts so the existing
    loadArtifact() path on the frontend can consume it unchanged.
    """
    artifacts = intel_agent.list_autodraft_artifacts_by_type(product_type, db)
    return JSONResponse(content={"product_type": product_type,
                                  "artifacts": artifacts}, status_code=200)


@app.get("/api/health")
def health_endpoint():
    """Liveness probe. Returns 200 OK with a small JSON body so external
    monitors (Docker HEALTHCHECK, load balancers, uptime probes) get a
    cheap go/no-go signal without touching the DB or the scenario clock.

    Some probe currently polls this path ~2/sec from the docker bridge
    gateway AND through nginx; before this handler it 404'd, filling
    docker-api's access log with noise and making the dashboard feel
    slow because every interaction competed with the log/network churn.
    Returning 200 here silences the noise without changing the probe
    cadence.
    """
    return JSONResponse(content={"status": "ok"}, status_code=200)


@app.get("/api/config")
def app_config_endpoint():
    """Public SPA config — one fetch on app boot. Surfaces feature flags
    the frontend needs to branch on without coupling them to product_types
    or any other domain endpoint. Add new top-level booleans here as
    flags land; the shape stays {<flag>: bool, ...}.

    Currently:
      collabora_narrative_edit:
        true  → DOCX / PPTX artifacts open in the live Office iframe
                (LiveEditor / Collabora). XLSX already does this
                unconditionally.
        false → DOCX / PPTX open in the legacy TinyMCE DocEditor.
    """
    return JSONResponse(content={
        "collabora_narrative_edit": (
            (os.environ.get("COLLABORA_NARRATIVE_EDIT") or "").strip().lower()
            in ("1", "true", "yes", "on")
        ),
    }, status_code=200)


@app.get("/api/artifacts/{artifact_id}/map_embeds")
def artifact_map_embeds(artifact_id: str):
    """Sidecar that lists every map embedded in the artifact's deliverable,
    with each map's twips bounding box on its page / slide. The frontend
    LiveEditor reads this on document-loaded and overlays an Edit-Map
    button at the bottom-right of every map via iframe.contentWindow.
    app.map's _twipsToLatLng → latLngToContainerPoint conversion.

    Empty array (200) when the artifact carries no maps, vs 404 when
    the artifact dir / sidecar simply does not exist — the frontend
    handles both as "render no buttons".
    """
    aid = (artifact_id or "").strip()
    if not aid:
        raise HTTPException(status_code=400, detail="artifact_id required")
    sidecar = Path(intel_agent._UPLOAD_DIR) / "_autodraft" / aid / "map_embeds.json"
    if not sidecar.exists():
        return JSONResponse(content={"map_embeds": []}, status_code=200)
    try:
        return JSONResponse(
            content={"map_embeds": json.loads(sidecar.read_text())},
            status_code=200,
        )
    except Exception as e:
        raise HTTPException(status_code=500,
                            detail=f"map_embeds.json read/parse failed: {e}")


@app.get("/api/oob/resolved_latest")
def oob_resolved_latest():
    """Latest resolved RED OOB — the data source for the operations-map
    left-click unit-metadata panel.

    Returns the per-unit fused records the panel renders:
      - primary_position {lat, lon, last_seen}  → MGRS + DOI
      - composition_rollup {<equip>: count}      → linked equipment
      - current_activity / disposition           → last known activity
    keyed by kg_id (== the map feature's source_kbid) with name as a
    fallback join key.

    SERVED FROM scenario.KG["unit_red"] — the canonical scenario
    fixture (hand-authored from the exercise data-dump UNIT INFO sheets
    + OPORD v2 annexes), transformed at request time. The legacy
    external-feed resolve path that once wrote the
    oob_resolved_latest.json snapshot is dead; the unified data source
    replacing scenario fixtures wholesale is the explicit post-map-
    integration TODO.

    Wire shape is the same named_units contract the panel consumes:
    kg_id (== map feature source_kbid), name, echelon, parent_kg_id,
    current_activity, strength, sidc, primary_position {lat, lon,
    last_seen}. composition_rollup is intentionally absent — the panel
    omits the equipment section when the field is missing.
    """
    from utils import scenario as _scen
    from utils.coords import mgrs_to_latlon

    units = []
    for u in (_scen.KG.get("unit_red") or []):
        latlon = None
        if u.get("mgrs"):
            try:
                latlon = mgrs_to_latlon(u["mgrs"])
            except Exception:
                latlon = None
        units.append({
            "kg_id": u.get("id"),
            "name": u.get("name"),
            "echelon": u.get("echelon"),
            "parent_kg_id": u.get("parent"),
            "current_activity": u.get("activity"),
            "strength": u.get("strength"),
            "sidc": u.get("sidc"),
            "unit_class": u.get("class"),
            "class_template": u.get("class_template"),
            "location_name": u.get("loc"),
            "primary_position": {
                "lat": latlon[0] if latlon else None,
                "lon": latlon[1] if latlon else None,
                "last_seen": u.get("last_obs"),
            },
        })
    return JSONResponse(content={"units": units, "snapshot_ts_ms": None},
                        status_code=200)


@app.get("/api/operations/recent_signal_reports")
def operations_recent_signal_reports(max_age_hours: int = 24):
    """Recent KLIEGLIGHT + OSIR artifacts with geolocation — feeds the
    operations-map autofire-report icon overlay (sidebar toggles "Live
    KLIEGLIGHTs" / "Live OSIRs"). Each entry carries the artifact_id +
    one (lat, lon) point so the map can drop a clickable icon at the
    intercept / observation site; click opens the rendered artifact.

    Walks `uploads/_autodraft/(KLIEGLIGHT|OSIR)_*/schema_state.json`,
    extracts the geolocation field doctrinally specific to each report
    family (KLIEGLIGHT paragraph_4_geolocation_mgrs.mgrs; OSIR
    paragraph_3_event_attribution.geolocated_mgrs), converts MGRS →
    lat/lon via utils.coords, and returns the most-recent N artifacts
    that landed within `max_age_hours`.

    Response: {"reports": [{artifact_id, type, lat, lon, mgrs,
    fired_at_ms, summary}], "as_of_ms": <int>}. summary is a short
    analyst-facing label drawn from the artifact's primary identification
    paragraph (emitter name for KLIEGLIGHTs, headline / event for OSIRs)
    — used as the tooltip and as the icon's secondary text on the map.
    """
    from pathlib import Path as _P
    from utils import coords as _coords
    uploads_dir = _P("/app/uploads") if _P("/app/uploads").exists() else _P("uploads")
    autodraft = uploads_dir / "_autodraft"
    if not autodraft.is_dir():
        return JSONResponse(content={"reports": [], "as_of_ms": int(time.time() * 1000)},
                              status_code=200)
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - max_age_hours * 3600 * 1000
    # Sparse-cougaar fallback: when the SIGINT/COMMS record carries no
    # mgrs/lat/lon (live cougaar IDX vertices today only ship
    # emitter_name + signal_id; the rich event_data fixtures used to
    # carry coordinates), look up the bearer unit's location from the
    # resolved-OOB snapshot. The autofire stamps the source kbID into
    # source_metadata.signal_kg_id and the bearer unit kg_id into
    # source_metadata.bearer_unit_kg_id, so we can join either way.
    bearer_mgrs_by_kg: dict[str, str] = {}
    bearer_mgrs_by_name: dict[str, str] = {}
    try:
        if True:
            for u in oob.resolved_named_units():
                kgid = (u.get("id") or "").strip()
                nm = (u.get("name") or "").strip()
                m = (u.get("location_mgrs") or "").strip()
                if m and kgid:
                    bearer_mgrs_by_kg[kgid] = m
                if m and nm:
                    bearer_mgrs_by_name[nm.lower()] = m
    except Exception:
        pass

    def _is_placeholder(s: str) -> bool:
        if not s:
            return True
        upper = s.upper()
        return ("[ANALYST INPUT REQUIRED]" in upper
                  or "ANALYST INPUT REQUIRED" in upper
                  or "NOT REPORTED" in upper
                  or "N/A" == upper
                  or "TBD" == upper)

    # Join against the unified product-records registry — the dashboard's own
    # source — so each report carries the same autodraft_session_id + format
    # the dashboard row uses. The map's "Open in editor" then opens the exact
    # same session WhiteCellHome's openRow does.
    sess_by_artifact: dict[str, str] = {}
    fmt_by_artifact: dict[str, str] = {}
    try:
        from handlers import product_records as _pr
        for c in _pr.list_chains():
            aid = c.get("artifact_id")
            if not aid:
                continue
            if c.get("autodraft_session_id"):
                sess_by_artifact[aid] = c["autodraft_session_id"]
            if c.get("format"):
                fmt_by_artifact[aid] = c["format"]
    except Exception:
        log.debug("recent_signal_reports: product_records join failed", exc_info=True)

    reports: list[dict] = []
    for kind in ("KLIEGLIGHT", "OSIR"):
        for art_dir in autodraft.glob(f"{kind}_*"):
            schema = art_dir / "schema_state.json"
            if not schema.is_file():
                continue
            try:
                mt_ms = int(schema.stat().st_mtime * 1000)
            except Exception:
                continue
            if mt_ms < cutoff_ms:
                continue
            try:
                doc = json.loads(schema.read_text())
            except Exception:
                continue
            sd = doc.get("structured_data") or {}
            mgrs = ""
            summary = ""
            if kind == "KLIEGLIGHT":
                p4 = sd.get("paragraph_4_geolocation_mgrs") or {}
                if isinstance(p4, dict):
                    mgrs = (p4.get("mgrs") or "").strip()
                p1 = sd.get("paragraph_1_emitter_target_identification") or ""
                if isinstance(p1, dict):
                    p1 = p1.get("text") or p1.get("body") or ""
                summary = str(p1)[:160]
            elif kind == "OSIR":
                p3 = sd.get("paragraph_3_event_attribution") or {}
                if isinstance(p3, dict):
                    mgrs = (p3.get("geolocated_mgrs") or "").strip()
                p2 = sd.get("paragraph_2_summary_of_information") or ""
                if isinstance(p2, dict):
                    p2 = p2.get("text") or p2.get("body") or ""
                summary = str(p2)[:160]
            # Bearer-unit fallback when the report's own MGRS is missing
            # or a placeholder. source_metadata is stamped by the autofire
            # callback in handlers/{klieglight,osir}_autofire.py.
            if _is_placeholder(mgrs):
                sm = doc.get("source_metadata") or {}
                if not isinstance(sm, dict):
                    sm = {}
                bearer_kg = (sm.get("bearer_unit_kg_id") or
                              sm.get("parent_kg_id") or
                              sm.get("unit_kg_id") or "").strip()
                if bearer_kg and bearer_kg in bearer_mgrs_by_kg:
                    mgrs = bearer_mgrs_by_kg[bearer_kg]
                else:
                    bearer_nm = (sm.get("bearer_unit_name") or
                                  sm.get("parent_unit") or "").strip().lower()
                    if bearer_nm and bearer_nm in bearer_mgrs_by_name:
                        mgrs = bearer_mgrs_by_name[bearer_nm]
            if not mgrs or _is_placeholder(mgrs):
                continue
            latlon = _coords.mgrs_to_latlon(mgrs)
            if not latlon:
                continue
            artifact_id = doc.get("artifact_id") or art_dir.name
            reports.append({
                "artifact_id": artifact_id,
                "type": kind,
                "lat": latlon[0],
                "lon": latlon[1],
                "mgrs": mgrs,
                "fired_at_ms": mt_ms,
                "summary": summary,
                # Same session/format the dashboard opens (None when the
                # artifact has no registry chain yet — the panel disables
                # "Open in editor" in that case).
                "autodraft_session_id": sess_by_artifact.get(artifact_id),
                "format": fmt_by_artifact.get(artifact_id) or "docx",
            })
    reports.sort(key=lambda r: r["fired_at_ms"], reverse=True)
    return JSONResponse(content={"reports": reports, "as_of_ms": now_ms},
                         status_code=200)


@app.get("/api/graph/products")
def graph_products():
    """Products that saved COUGAAR tool_results — the Graph Viewer picker."""
    return JSONResponse(content={"products": cougaar_graph.list_products()},
                        status_code=200)


@app.get("/api/graph")
def graph_snapshot(artifact_id: str, limit: int = cougaar_graph.DEFAULT_LIMIT,
                   types: str = ""):
    """The COUGAAR entity graph (nodes + edges) a product pulled, read from
    its saved tool_results. `types` is an optional comma list to filter node
    types (unit,target,observation,control_measure,...)."""
    type_set = {t.strip() for t in types.split(",") if t.strip()} or None
    graph = cougaar_graph.build_from_artifact(artifact_id, limit=limit, types=type_set)
    if graph is None:
        raise HTTPException(status_code=404,
                            detail=f"no tool_results for artifact {artifact_id!r}")
    return JSONResponse(content=graph, status_code=200)


@app.get("/api/graph/live")
async def graph_live(limit: int = cougaar_graph.DEFAULT_LIMIT, types: str = "",
                      project_id: str = ""):
    """Live COUGAAR entity graph — runs the same fetches the DAG uses
    (get_oob RED+BLUE, list_targets, get_recent_intel, get_spatial_overlay)
    and feeds the SAME transform as the per-product snapshot. Falls back to
    scenario fixtures when COUGAAR is unreachable (the tool functions do this
    internally), so it always returns something."""
    ia = intel_agent
    from utils.project_context import set_active_project
    set_active_project(project_id)
    state = ia.State.fresh("graph-live", user_id="_graph",
                            project_id=project_id)
    tool_outputs: dict = {}

    try:
        red, blue = await asyncio.gather(
            ia.tool_get_oob({"force_type": "RED"}, state),
            ia.tool_get_oob({"force_type": "BLUE"}, state),
        )
        red = red or {}
        blue = blue or {}
        tool_outputs["get_oob"] = {
            **red,
            "units": [*(red.get("units") or []), *(blue.get("units") or [])],
        }
    except Exception as e:
        tool_outputs["get_oob"] = {"_error": f"{type(e).__name__}: {e}"}

    async def _safe(name, coro):
        try:
            tool_outputs[name] = await coro
        except Exception as e:
            tool_outputs[name] = {"_error": f"{type(e).__name__}: {e}"}

    await _safe("list_targets", ia.tool_list_targets({}, state))
    await _safe("get_recent_intel", ia.tool_get_recent_intel({}, state))
    try:
        tool_outputs["get_spatial_overlay"] = await asyncio.to_thread(
            ia.tool_get_spatial_overlay, {}, state)
    except Exception as e:
        tool_outputs["get_spatial_overlay"] = {"_error": f"{type(e).__name__}: {e}"}

    type_set = {t.strip() for t in types.split(",") if t.strip()} or None
    graph = cougaar_graph.build_graph(tool_outputs, limit=limit, types=type_set)
    graph["source"] = "live"
    return JSONResponse(content=graph, status_code=200)


@app.get("/api/graph/live_all")
async def graph_live_all(project_id: str = ""):
    """ALL live graph data for the Graph Viewer — uncapped.

    Distinct from /api/graph/live: the RED order-of-battle comes
    straight from the live IDXRedUnit vertex (every unit + its subordinate→
    superior org edge), NOT from tool_get_oob (which intentionally serves the
    scenario fixture to the map + products). Live targets / intel /
    control-measures are folded in via the existing read-only tool calls.
    Returns the same {nodes, edges, stats} shape the viewer renders, with no
    per-type cap so the full live laydown comes back.

    Self-contained + read-only — changes no existing handler. Each section
    degrades independently (empty / error) so a partial outage still renders.
    """
    from utils.project_context import set_active_project
    set_active_project(project_id)
    ia = intel_agent
    state = ia.State.fresh("graph-live-all", user_id="_graph",
                            project_id=project_id)
    tool_outputs: dict = {}

    # RED OOB from the resolved-OOB snapshot (the Cougaar GraphQL feed
    # is dead and disconnected). named_units carry id/name/parent/
    # echelon/category — the same fields build_graph resolves edges on.
    try:
        resolved = await oob.get_red_oob_resolved()
        units = list(resolved.get("named_units") or [])
        tool_outputs["get_oob"] = {"units": units}
        log.info("graph/live_all: resolved-OOB snapshot units=%d", len(units))
    except Exception as e:
        log.warning("graph/live_all: resolved-OOB snapshot read failed (%s)", e)
        tool_outputs["get_oob"] = {"_error": f"{type(e).__name__}: {e}"}

    # Other live entity types via the existing read-only tool calls (unchanged).
    async def _safe(name, coro):
        try:
            tool_outputs[name] = await coro
        except Exception as e:
            tool_outputs[name] = {"_error": f"{type(e).__name__}: {e}"}

    await _safe("list_targets", ia.tool_list_targets({}, state))
    await _safe("get_recent_intel", ia.tool_get_recent_intel({}, state))
    try:
        tool_outputs["get_spatial_overlay"] = await asyncio.to_thread(
            ia.tool_get_spatial_overlay, {}, state)
    except Exception as e:
        tool_outputs["get_spatial_overlay"] = {"_error": f"{type(e).__name__}: {e}"}

    # Uncapped — build_graph caps per-type at `limit`; pass a huge ceiling so
    # every live unit (and thus every edge) is returned.
    graph = cougaar_graph.build_graph(tool_outputs, limit=10**9)
    graph["source"] = "live_all"
    return JSONResponse(content=graph, status_code=200)


@app.post("/api/artifacts/{artifact_id}/refresh_map/{map_id}")
async def artifact_refresh_map(artifact_id: str, map_id: str,
                               body: Optional[dict] = Body(default=None)):
    """Live-edit map refresh — document-side embed of an edited map.

    On Done editing in the in-map edit session, geo-agent renders +
    persists the new JPG ITSELF and posts the done hook up with its
    `image_url`; the frontend POSTs here with {image_url}. We:
      1. Download the persisted render from geo-agent (no re-render —
         rendering is geo-agent's job; this side only embeds). Without
         an image_url (defensive fallback) the legacy replay-render
         path still fires.
      2. Replace the image bytes inside the deliverable (DOCX / PPTX zip)
         in place, atomically. Analyst's text / table edits in the live
         editor are preserved — we only swap the image part referenced
         by the map_embeds.json entry.
      3. Bump the file's mtime so the editor's next CheckFileInfo sees
         a fresh X-WOPI-ItemVersion and reloads the doc.

    Returns the new cache_bust token + replaced image relId so the
    frontend can postMessage `Action_Save` + iframe-reload to pick up
    the change. See handlers.artifact_map_refresh for the inner work.
    """
    aid = (artifact_id or "").strip()
    mid = (map_id or "").strip()
    if not aid or not mid:
        raise HTTPException(status_code=400, detail="artifact_id + map_id required")
    image_url = ((body or {}).get("image_url") or "").strip() or None
    from handlers import artifact_map_refresh
    try:
        result = await artifact_map_refresh.refresh_map(aid, mid, image_url=image_url)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return JSONResponse(content=result, status_code=200)


# /api/products/catalog REMOVED — unified /api/products/records is
# now the single dashboard data source. Every field the legacy
# CatalogRow shape exposed (longName, format, isXlsx, category,
# status, approvedBy, approvedAt) is enriched onto each ChainView
# server-side by product_records._chain_view / _serialize_record,
# and pre-registry on-disk artifacts are backfilled into _RECORDS
# at lifespan boot via product_records.backfill_pre_registry_artifacts.
# Frontend reads /api/products/records only.







class AnalystEventIn(BaseModel):
    event_type: str
    user_id: Optional[str] = None
    surface: Optional[str] = None
    product_type: Optional[str] = None
    session_id: Optional[str] = None
    artifact_id: Optional[str] = None
    value_num: Optional[int] = None
    detail: Optional[dict] = None


@app.post("/api/analyst_event")
def create_analyst_event(evt: AnalystEventIn):
    """Record one analyst-action telemetry row (product-improvement data).
    Best-effort: returns 202 and never raises into the caller's UX."""
    res = events.log_analyst_event(
        user_id=evt.user_id,
        event_type=evt.event_type,
        surface=evt.surface,
        product_type=evt.product_type,
        session_id=evt.session_id,
        artifact_id=evt.artifact_id,
        value_num=evt.value_num,
        detail=evt.detail,
    )
    return JSONResponse(content=res, status_code=202)


@app.get("/api/analyst_event")
def read_analyst_events(
    user_id: Optional[str] = None,
    surface: Optional[str] = None,
    event_type: Optional[str] = None,
    product_type: Optional[str] = None,
    session_id: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(database.get_db),
):
    """Query analyst_event for the product fix-list. All filters optional."""
    q = db.query(database.AnalystEvent)
    if user_id:
        q = q.filter(database.AnalystEvent.user_id == user_id)
    if surface:
        q = q.filter(database.AnalystEvent.surface == surface)
    if event_type:
        q = q.filter(database.AnalystEvent.event_type == event_type)
    if product_type:
        q = q.filter(database.AnalystEvent.product_type == product_type)
    if session_id:
        q = q.filter(database.AnalystEvent.session_id == session_id)
    if since:
        q = q.filter(database.AnalystEvent.timestamp >= since)
    if until:
        q = q.filter(database.AnalystEvent.timestamp <= until)
    rows = (
        q.order_by(database.AnalystEvent.timestamp.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "event_id": r.event_id,
            "user_id": r.user_id,
            "timestamp": r.timestamp,
            "surface": r.surface,
            "event_type": r.event_type,
            "product_type": r.product_type,
            "session_id": r.session_id,
            "artifact_id": r.artifact_id,
            "value_num": r.value_num,
            "detail": r.detail,
        }
        for r in rows
    ]


@app.get("/api/analyst_event/corpus")
def export_training_corpus(
    product_type: Optional[str] = None,
    since: Optional[datetime] = None,
    limit: int = 5000,
    db: Session = Depends(database.get_db),
):
    """Export the agent-draft -> analyst-final training corpus as JSONL — one
    record per edited field, sourced from the edit.field analyst_event rows.
    This is the fine-tuning dataset (Mechanism 2 of the plan)."""
    q = db.query(database.AnalystEvent).filter(
        database.AnalystEvent.event_type == "edit.field"
    )
    if product_type:
        q = q.filter(database.AnalystEvent.product_type == product_type)
    if since:
        q = q.filter(database.AnalystEvent.timestamp >= since)
    rows = q.order_by(database.AnalystEvent.timestamp.desc()).limit(limit).all()
    lines = []
    for r in rows:
        try:
            d = json.loads(r.detail) if r.detail else {}
        except Exception:
            d = {}
        lines.append(json.dumps({
            "artifact_id": r.artifact_id,
            "product_type": r.product_type,
            "session_id": r.session_id,
            "field": d.get("field"),
            "edit_distance": r.value_num,
            "draft": d.get("draft_value"),
            "final": d.get("final_value"),
            "timestamp": r.timestamp.isoformat() if r.timestamp else None,
        }, default=str))
    return PlainTextResponse(
        "\n".join(lines) + ("\n" if lines else ""),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": "attachment; filename=analyst_training_corpus.jsonl"
        },
    )





def _normalize_doctrine_link(link):
    """Make a doctrine `link` relative to the current host.

    Historical doctrine records were uploaded when FILE_HOST_API contained the
    full "http://localhost:8000" URL, so their `link` field is
    "http://localhost:8000/files/doctrine-<hash>.pdf". On a LAN-deployed analyst
    workstation `localhost` resolves to the analyst's OWN laptop (port 8000
    unused), so the click-through opens an unreachable URL. Strip the host so the
    browser builds the URL against the page's current host (through nginx to this
    backend). Non-string / already-relative links pass through unchanged.
    """
    if not isinstance(link, str):
        return link
    for prefix in ("http://localhost:8000", "https://localhost:8000", "http://127.0.0.1:8000"):
        if link.startswith(prefix + "/"):
            return link[len(prefix):]
    return link


@app.post("/api/query_doctrines")
def query_doctrines(document_query: DocumentQueryIn):
    result = search.get_documents_by_title(document_query.query, index_name=search.DOCTRINE_INDEX_NAME)
    if isinstance(result, list):
        for record in result:
            if isinstance(record, dict):
                record["link"] = _normalize_doctrine_link(record.get("link"))
    return JSONResponse(content=result, status_code=200)


@app.post("/api/doctrine/chat")
async def doctrine_chat(payload: DoctrineChatIn):
    """Answer a question grounded in the uploaded doctrine library.

    Retrieves the most similar doctrine chunks (optionally scoped to a selected
    set of publications), feeds them to the offline vLLM as context, and returns
    the answer plus the retrieved chunks as clickable citations. Fully local: ES
    kNN over EmbeddingGemma vectors + vLLM Gemma — no external services.
    """
    from utils import llm

    question = (payload.question or "").strip()
    if not question:
        return JSONResponse(content={"answer": "", "citations": []}, status_code=200)

    hits = search.top_k_similar_chunks(
        question,
        k=8,
        index_name=search.DOCTRINE_INDEX_NAME,
        doc_ids=payload.selected_doc_ids or None,
    )

    if not hits:
        return JSONResponse(
            content={
                "answer": "I couldn't find relevant doctrine for that question.",
                "citations": [],
            },
            status_code=200,
        )

    # Build the grounding context — one labeled block per retrieved chunk.
    blocks = []
    for h in hits:
        title = h.get("title") or h.get("doc_id") or "doctrine"
        page = h.get("page")
        label = f"{title} p.{page}" if page is not None else title
        blocks.append(f"[{label}]\n{h.get('content') or ''}")
    context = "\n\n".join(blocks)

    messages = [
        {
            "role": "system",
            "content": (
                "You answer questions about US military doctrine for an intelligence "
                "analyst. Use ONLY the provided doctrinal passages to answer. If the "
                "passages do not cover the question, say so plainly rather than guessing. "
                "Be concise and precise, and prefer the wording and definitions used in "
                "the passages. "
                "Write the answer as plain prose in short paragraphs. Do NOT use any "
                "Markdown formatting: no asterisks for bold or italics, no '#' headings, "
                "no '-' or '*' bullet lists, no numbered-list markers, no backticks or "
                "code fences. Use sentences and paragraphs only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Doctrinal passages:\n{context}\n\n"
                "Answer:"
            ),
        },
    ]

    answer = await llm.invoke_vllm_text_async(
        messages, temperature=0.1, max_tokens=1200, gate_label="doctrine_chat"
    )

    # Citations = the retrieved chunks, deduped by (doc_id, page). Each opens the
    # source PDF at the cited page on the frontend.
    citations = []
    seen = set()
    for h in hits:
        key = (h.get("doc_id"), h.get("page"))
        if key in seen:
            continue
        seen.add(key)
        snippet = (h.get("content") or "").strip().replace("\n", " ")
        citations.append({
            "doc_id": h.get("doc_id"),
            "title": h.get("title"),
            "page": h.get("page"),
            "link": _normalize_doctrine_link(h.get("link")),
            "snippet": snippet[:200],
        })

    return JSONResponse(content={"answer": answer, "citations": citations}, status_code=200)


@app.post("/api/upload_doctrine")
async def upload_doctrine(file: UploadFile = File(...), title: str = Form(...)):
    """Upload a doctrine file and index it into the doctrine_chunks ES index."""
    # Deterministic file_id from the original filename so re-uploads overwrite
    digest = hashlib.sha1(file.filename.encode("utf-8")).hexdigest()[:16]
    suffix = os.path.splitext(file.filename)[1].lower()
    file_id = f"doctrine-{digest}{suffix}"
    file_path = os.path.join(UPLOAD_DIR, file_id)

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Strip file extensions from the title (e.g. "FM 3-90.pdf" → "FM 3-90")
    title = os.path.splitext(title)[0] if title.lower().endswith(".pdf") else title

    link = FILE_HOST_API + file_id
    result = search.insert_by_file_id(
        user_id="__system__",
        file_id=file_id,
        title=title,
        link=link,
        index_name=search.DOCTRINE_INDEX_NAME,
        embed_title_in_content=True,
    )
    return JSONResponse(content=result, status_code=200)


@app.delete("/api/purge_doctrines")
def purge_doctrines():
    """Drop the entire doctrine_chunks ES index."""
    es = search.get_es()
    deleted = False
    if es.indices.exists(index=search.DOCTRINE_INDEX_NAME):
        es.indices.delete(index=search.DOCTRINE_INDEX_NAME)
        deleted = True
    return JSONResponse(content={"deleted": deleted}, status_code=200)





@app.post("/api/upload/")
async def upload_file(file: UploadFile = File(...)):
    # Sanitize: take only the basename (Path(...).name strips any
    # client-supplied directory components like "../../etc/passwd") then
    # collapse anything outside [A-Za-z0-9._-] to underscore. Prefix with
    # a millisecond DTG so the on-disk name is deterministic + chronologically
    # sortable rather than the prior 4-digit random prefix that collided
    # under load.
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', Path(file.filename).name)
    dtg_ms = int(time.time() * 1000)
    filename = f"{dtg_ms}_{safe_name}"
    print(f"Uploading file: {filename}")
    file_path = os.path.join(UPLOAD_DIR, filename)
    title = os.path.splitext(file.filename)[0].replace("_", " ").strip()

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return JSONResponse(content={
            "key": filename,
            "url": FILE_HOST_API + filename,
            "name": file.filename,
            "title": title
        }, status_code=200)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    
@app.post("/api/save/")
async def save_file(
    doc_link: str = Form(...),
    content: str = Form(...),
    session_id: str = Form(None),
    project_id: str = Form(None),
    product_name: str = Form(None),
    user_id: str = Form(None),
    header: str = Form(None),
    header_style: str = Form(None),
    watermark: str = Form(None),
    watermark_color: str = Form(None),
    watermark_font: str = Form(None),
    watermark_size: str = Form(None),
):
    """Save edited document as new HTML + DOCX files. Never overwrites originals."""
    import time as _time
    from utils.docx_converter import html_to_docx
    
    try:
        file_key = f"{int(_time.time())}-{random.randint(1, 1000)}"
        html_filename = f"{file_key}.html"
        html_path = os.path.abspath(os.path.join(UPLOAD_DIR, html_filename))
        html_url = FILE_HOST_API + html_filename

        with open(html_path, "w", encoding="utf-8") as f:
            f.write(content)

        docx_filename = f"{file_key}.docx"
        docx_path = os.path.abspath(os.path.join(UPLOAD_DIR, docx_filename))
        docx_url = FILE_HOST_API + docx_filename

        parsed_header_style = None
        if header_style:
            try:
                parsed_header_style = json.loads(header_style)
            except:
                pass

        # Pull pageOrientation out of the embedded pytho-doc-settings script the
        # DocEditor writes on save. convert.js will pick it up too — passing it
        # explicitly here is belt-and-suspenders so a malformed embedded JSON
        # doesn't silently lose orientation on save-back.
        orientation = None
        try:
            import re as _re
            m = _re.search(
                r'<script[^>]*id="pytho-doc-settings"[^>]*>([\s\S]*?)</script>',
                content,
                _re.IGNORECASE,
            )
            if m:
                settings = json.loads(m.group(1))
                po = settings.get("pageOrientation")
                if po in ("portrait", "landscape"):
                    orientation = po
        except Exception as _e:
            logging.warning(f"save_file: failed to read pageOrientation from embedded settings: {_e}")

        html_to_docx(
            content, docx_path,
            header=header,
            header_style=parsed_header_style,
            watermark=watermark,
            watermark_color=watermark_color,
            watermark_font=watermark_font,
            watermark_size=watermark_size,
            orientation=orientation,
        )

        timestamp = int(_time.time() * 1000)
        saved_message = f'<a href="{docx_url}" style="color:#90caf9;" target="_blank">Here</a> is the edited {product_name or "document"} you saved on {_time.strftime("%b %d, %Y, %I:%M %p")}.'

        if session_id and project_id:
            try:
                db = next(database.get_db())
                session_detail = database.SessionDetail(
                    session_id=session_id,
                    timestamp=timestamp,
                    project_id=project_id,
                    data=database.null_safe_json_dumps({
                        "answer": saved_message,
                        "htmlLink": html_url,
                        "docxLink": docx_url,
                        "isCustomProduct": True,
                        "productName": product_name or "document",
                        "userId": user_id,
                        "sessionId": session_id,
                        "timestamp": timestamp,
                    })
                )
                db.add(session_detail)
                db.commit()
                db.close()
            except Exception as e:
                logging.error(f'Failed to persist save message: {e}')

        return JSONResponse(content={
            "key": html_filename,
            "url": html_url,
            "htmlLink": html_url,
            "docxLink": docx_url,
            "timestamp": timestamp,
            "savedMessage": saved_message,
        }, status_code=200)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    
# ==================== MANUAL-DISPATCH PRODUCT GENERATION (Req 4) ====================

class GenerateProductIn(BaseModel):
    product_type: str
    project_id: str
    user_id: str
    session_id: Optional[str] = None
    context: Optional[str] = None


@app.post("/api/products/generate_DEPRECATED")
async def generate_product(req: GenerateProductIn):
    """Manual-dispatch entry point for the Generate-button UI in
    ProductLibraryModal. Wraps `run_operations_expert_loop` (via
    handlers.intel_agent.run_turn) with a synthesized analyst message so
    analysts can generate a doctrinal product without typing into chat.

    Same persistence path as `/ws/intel_agent` — load_state / run_turn /
    save_turn — plus an exporter call so the response carries `htmlLink` and
    `docxLink` (or `pptxLink`) the UI can hand straight to DocEditor.

    Path renamed with `_DEPRECATED` suffix so it can't be hit by accident
    during testing — the serial OE-loop is 90s-4min per product. Use the
    WS `generate_product` frame (DAG, ~12s) or /api/products/generate-async
    instead.
    """
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: product.generate(
                product_type=req.product_type,
                project_id=req.project_id,
                user_id=req.user_id,
                session_id=req.session_id,
                context=req.context,
            ),
        )
        if result.get("status") == "error":
            raise HTTPException(status_code=400, detail=result.get("error", "generation failed"))
        return JSONResponse(content=result, status_code=200)
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ==================== NOTIFICATIONS ENDPOINTS ====================

class OpenDraftIn(BaseModel):
    user_id: str
    project_id: str


@app.get("/api/notifications/{user_id}")
def list_notifications(user_id: str, limit: int = 100):
    """Return every notification visible to `user_id`, newest first.

    Broadcast notifications (user_id="*") are visible to every analyst.
    The KLIEGLIGHT auto-fire trigger writes broadcast rows since the KG
    poll runs at process level — no analyst owns the fire until they
    promote it via /api/notifications/{id}/open_draft.
    """
    rows = notifications_handler.list_notifications_for_user(user_id, limit=limit)
    return JSONResponse(content={"notifications": rows, "count": len(rows)}, status_code=200)


@app.post("/api/notifications/{notification_id}/read")
def mark_notification_read(notification_id: int):
    """Flip a notification's status to 'read' so the UI can grey it out."""
    result = notifications_handler.mark_notification_status(notification_id, "read")
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return JSONResponse(content=result, status_code=200)


@app.post("/api/notifications/{notification_id}/open_draft")
def open_notification_draft(notification_id: int, payload: OpenDraftIn):
    """Promote the _autodraft KLIEGLIGHT artifact into the analyst's
    namespace: new session_id, new project_id, owner_user_id rewritten,
    files copied from uploads/_autodraft/<aid> -> uploads/<user>/<aid>.

    Returns {session_id, artifact_id, htmlLink, docxLink} so the UI can
    navigate to /missioncommand/docs/<session_id> and DocEditorPage's
    GET /api/sessions/<id>/artifacts call finds the artifact.
    """
    result = notifications_handler.open_draft_in_new_session(
        notification_id,
        user_id=payload.user_id,
        project_id=payload.project_id,
    )
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return JSONResponse(content=result, status_code=200)


# ==================== DIAGNOSTICS ====================

@app.post("/api/diag/osir_autofire/refire")
async def diag_osir_autofire_refire(kbids: Optional[str] = None):
    """Force-refire OSIRs bypassing the existing-artifact dedup. Use after
    a schema / autofire / context-builder edit when you want fresh OSIRs
    even though prior ones already landed.

    Query:
      kbids=<uuid>,<uuid>,...  (comma-separated, optional). Omit to
                                 force-refire every IDXOpenSourceReport in
                                 the warm recent-intel cache.

    Sequential — parallel fires would saturate the writer LLM. Returns
    {candidates, fired, errors}.
    """
    kbid_list = [k.strip() for k in (kbids or "").split(",") if k.strip()] or None
    summary = await osir_autofire.force_refire(kbid_list)
    return JSONResponse(content=summary, status_code=200)


@app.post("/api/diag/klieglight_autofire/refire")
async def diag_klieglight_autofire_refire(kbids: Optional[str] = None):
    """Force-refire KLIEGLIGHTs bypassing the existing-artifact dedup. Use
    after a schema / writer-prompt / signal-class fix when you want fresh
    KLIEGLIGHTs even though prior ones already landed (40 candidates at
    startup all `already_fired` in the IBL audit case).

    Query:
      kbids=<uuid>,<uuid>,...  (comma-separated, optional). Omit to
                                 force-refire every qualifying SIGINT
                                 vertex (SignalOfInterestReport,
                                 TacElintReport with HVT designator,
                                 CommunicationsData) in the warm
                                 recent-intel cache.

    Sequential — parallel fires would saturate the writer LLM. Returns
    {candidates, fired, errors}. Mirrors /api/diag/osir_autofire/refire.
    """
    kbid_list = [k.strip() for k in (kbids or "").split(",") if k.strip()] or None
    summary = await klieglight_autofire.force_refire(kbid_list)
    return JSONResponse(content=summary, status_code=200)


@app.post("/api/diag/product_autodraft/resume")
async def diag_product_autodraft_resume(force: bool = False):
    """Force-run the autodraft sweep on demand. Same function the startup
    hook calls — fires every rollup product missing from the pool, leaves
    existing entries alone. Use when:
      - Startup sweep deferred behind a cold vllm and the backoff
        schedule exhausted before vllm finished warming.
      - You restarted vllm mid-exercise and want the pool refreshed.
      - You want a sweep without waiting for the next backoff tick.

    Bypasses the vllm health gate (the operator hit this BECAUSE they
    know vllm is up). If vllm is still cold, individual fires will error
    and `errors` will be > 0 in the response — re-run after vllm warms.

    Returns the same shape as the startup sweep result:
      {products_total, products_eligible, passes_run, fired, errors,
       deferred_final, per_pass}
    """
    result = await product_autodraft.resume_sweep(force=force)
    return JSONResponse(content=result, status_code=200)


@app.post("/api/diag/product_autodraft/refire")
async def diag_product_autodraft_refire(products: str, mode: str = "full",
                                          force: bool = False,
                                          project_id: str = ""):
    """Refire autodrafts for the given products. Two modes.

    Query:
      products=MCOO,SITEMP,Threat%20Overlay,...  (comma-separated, URL-encoded names matching PRODUCT_SCHEMAS keys).
      mode=full      (default) — purge + re-run the dep-aware sweep (full LLM regen).
      mode=map_only  — re-render the cached map image(s) only, no LLM.
                       Reads render_params_<field>.json that tool_render_map
                       wrote during the original generation. Iteration helper
                       for renderer work: ~5-10s per product vs minutes.
      force=1        — bypass the autodraft-pool dedup so a mode=full refire
                       lands a fresh chain even if a prior chain for the same
                       product is still in-flight. Ignored for map_only /
                       export_only (those don't touch the dedup gate).

    Returns:
      full      → {requested, unknown, purged: {product: rows}, sweep: {...}}
      map_only  → {mode, requested, unknown, results: {product: {fields:[...]}}}
    """
    product_types = [p.strip() for p in products.split(",") if p.strip()]
    if not product_types:
        return JSONResponse(content={"error": "no products specified"}, status_code=400)
    if mode == "map_only":
        result = await product_autodraft.refire_product_maps(product_types, project_id=project_id)
    elif mode == "export_only":
        # Re-export DOCX/PPTX/HTML from cached structured_data, no LLM,
        # no map regen. For exporter-only changes (table style, XML
        # diet, chrome edits): much cheaper than mode=full.
        result = await product_autodraft.refire_product_exports(product_types, project_id=project_id)
    elif mode == "full":
        result = await product_autodraft.refresh_drafts(product_types, force=force, project_id=project_id)
    else:
        return JSONResponse(
            content={"error": f"unknown mode {mode!r}; expected 'full', 'map_only', or 'export_only'"},
            status_code=400,
        )
    return JSONResponse(content=result, status_code=200)


@app.get("/api/scenario-clock")
def scenario_clock_snapshot(project_id: str = ""):
    """Authoritative frozen scenario time. Every product DTG, every map render
    timestamp, every "current time" display in the UI consults THIS endpoint
    (or the in-process `utils.scenario_clock` for Python callers) — never
    host wall-clock.

    Frozen value = max(timestamps in scenario.KG + PROJECT_DOCS) + 30s. Re-
    freezes when manual_injects watcher picks up a new docx whose DTGs
    extend the timeline.

    The scenario-NTP container (`scenario_ntp_server.py`, UDP/123) serves
    this same value over the NTP wire protocol for non-Python clients.

    Response shape:
      {iso, dtg, unix_ms, source_anchor, freeze_offset_seconds,
       default_scenario_month_year}
    """
    from utils import scenario_clock
    from utils.project_context import set_active_project
    set_active_project(project_id)
    return JSONResponse(content=scenario_clock.snapshot(), status_code=200)


@app.post("/api/scenario-clock/refreeze")
def scenario_clock_refreeze(project_id: str = ""):
    """Force a re-freeze of ONE project's clock. Called manually after
    dropping a new OPORD/FRAGO into the manual_injects folder when the
    watcher's 30s poll hasn't fired yet, or as a debug hook."""
    from utils import scenario_clock
    from utils.project_context import set_active_project
    set_active_project(project_id)
    scenario_clock.refreeze()
    return JSONResponse(content=scenario_clock.snapshot(), status_code=200)


# ==================== PUBLISH ARTIFACT ENDPOINT (Req 3) ====================

@app.post("/api/artifacts/{artifact_id}/publish")
async def publish_artifact_endpoint(
    artifact_id: str, user_id: str = "", approved_by: str = "",
):
    """Approve/finalize an artifact: flip header.status to 'published' and
    stamp the approver (`approved_by` display name + `user_id`). Mirrors
    the operations-expert tool `publish_artifact` for the UI / external
    callers. The patch is persisted, so it survives session reloads.
    """
    approver = {"id": user_id, "name": approved_by} if (user_id or approved_by) else None
    db = database.SessionLocal()
    try:
        result = intel_agent.publish_artifact_in_db(db, artifact_id, approver=approver)
    finally:
        db.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    # jobs-unification-005 wire-up: drop the artifact header cache so the
    # next /api/products/records read sees the fresh approval state
    # (status=published, approved_by, approved_at). Without this, the
    # dashboard row would keep rendering status=draft until the cache
    # entry naturally aged out (which never happens).
    try:
        from handlers import product_records as _pr
        _pr.invalidate_artifact_header_cache(artifact_id)
    except Exception:
        logger.exception("invalidate_artifact_header_cache failed post-publish")
    return JSONResponse(content={
        "ok": True,
        "artifact_id": artifact_id,
        "artifact_type": result.get("artifact_type", ""),
        "status": "published",
        "session_id": result.get("session_id"),
        "already_published": result.get("already_published", False),
        "approved_by": result.get("approved_by", ""),
        "approved_at": result.get("approved_at", 0),
    }, status_code=200)


@app.post("/api/artifacts/{artifact_id}/revert")
async def revert_artifact_endpoint(artifact_id: str):
    """Un-approve an artifact: flip header.status back to 'draft'. The file
    is NOT moved — only the status changes. Powers the editor's approval
    toggle (toggling FINAL → DRAFT). Mirror of the publish endpoint.
    """
    db = database.SessionLocal()
    try:
        result = intel_agent.revert_artifact_in_db(db, artifact_id)
    finally:
        db.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    # jobs-unification-005 wire-up: drop the cache so the next chain view
    # read sees status=draft instead of stale status=published.
    try:
        from handlers import product_records as _pr
        _pr.invalidate_artifact_header_cache(artifact_id)
    except Exception:
        logger.exception("invalidate_artifact_header_cache failed post-revert")
    return JSONResponse(content={
        "ok": True,
        "artifact_id": artifact_id,
        "artifact_type": result.get("artifact_type", ""),
        "status": "draft",
        "session_id": result.get("session_id"),
        "already_draft": result.get("already", False),
    }, status_code=200)


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact_endpoint(artifact_id: str):
    """Permanently delete one artifact (session row + on-disk files).
    Powers the dashboard's per-row trash button. See
    intel_agent.delete_artifact_in_db.
    """
    db = database.SessionLocal()
    try:
        result = intel_agent.delete_artifact_in_db(db, artifact_id)
    finally:
        db.close()
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("error", "not found"))
    return JSONResponse(content=result, status_code=200)


# Persistent analyst config — single source of truth for HVT_WORTHY
# category override (and any future schema config_fields). Read by
# EVERY product fire path (dashboard Generate, autodraft sweep,
# autofire, map-driven right-click) so the analyst's gear-button
# selection is honored regardless of trigger.
_PERSISTENT_CONFIG_DIR = "/app/state/product_configs"


def _read_persistent_config(product_type: str) -> dict:
    import os, json as _json
    path = f"{_PERSISTENT_CONFIG_DIR}/{product_type}.json"
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            v = _json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _write_persistent_config(product_type: str, cfg: dict) -> None:
    import os, json as _json
    os.makedirs(_PERSISTENT_CONFIG_DIR, exist_ok=True)
    path = f"{_PERSISTENT_CONFIG_DIR}/{product_type}.json"
    with open(path, "w") as f:
        _json.dump(cfg, f, indent=2)


@app.get("/api/products/config_fields/{product_type}")
async def products_config_fields_endpoint(product_type: str):
    """Schema config_fields block + persisted-value pre-fill. Frontend
    reads once when opening the gear modal; pre-selects from
    current_values when present, falls through to spec.default
    otherwise."""
    from templates import PRODUCT_SCHEMAS
    spec = PRODUCT_SCHEMAS.get(product_type)
    if not isinstance(spec, dict):
        raise HTTPException(status_code=404,
                              detail=f"unknown product_type {product_type!r}")
    return {"product_type": product_type,
              "config_fields": spec.get("config_fields") or {},
              "current_values": _read_persistent_config(product_type)}


@app.post("/api/products/config/{product_type}")
async def products_config_set_endpoint(product_type: str, req: Request):
    """Save gear-button selections. Used by autofire / sweep /
    map-driven / dashboard fires alike — single source of truth."""
    from templates import PRODUCT_SCHEMAS
    if product_type not in PRODUCT_SCHEMAS:
        raise HTTPException(status_code=404,
                              detail=f"unknown product_type {product_type!r}")
    body = await req.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    _write_persistent_config(product_type, body)
    return {"ok": True, "product_type": product_type, "config": body}


async def _generate_with_config(*, product_type: str, by: str,
                                   target_focus: dict | None,
                                   analyst_config: dict,
                                   force: bool = False,
                                   project_id: str = "") -> None:
    """Apply per-request HVT_WORTHY override via contextvar, then call
    refresh_drafts. Merges request-supplied config_json with the on-
    disk persisted config so autofire / sweep / right-click paths
    that DON'T pass config_json still pick up the analyst's saved
    selection. Persisted config lives at /app/state/product_configs/
    {product_type}.json — written by /api/products/config/{type} POST."""
    from utils import equipment_doctrine as _ed
    persisted = _read_persistent_config(product_type)
    merged = dict(persisted)
    merged.update(analyst_config or {})
    tok = None
    badge_tok = None
    cats = merged.get("hvt_worthy_categories")
    if isinstance(cats, list) and cats:
        tok = _ed.HVT_WORTHY_CATEGORIES_OVERRIDE.set(frozenset(cats))
    # show_coa_role_badges: bool. Default False is enforced by the
    # contextvar's default; only set the override when analyst opted in.
    show_badges = merged.get("show_coa_role_badges")
    if show_badges is True:
        badge_tok = _ed.SHOW_COA_ROLE_BADGES_OVERRIDE.set(True)
    try:
        await product_autodraft.refresh_drafts(
            [product_type], by=by, target_focus=target_focus, force=force,
            project_id=project_id,
        )
    finally:
        if tok is not None:
            _ed.HVT_WORTHY_CATEGORIES_OVERRIDE.reset(tok)
        if badge_tok is not None:
            _ed.SHOW_COA_ROLE_BADGES_OVERRIDE.reset(badge_tok)


@app.post("/api/products/generate")
async def generate_product_endpoint(product_type: str, by: str = "",
                                     target_entity_id: str | None = None,
                                     config_json: str | None = None,
                                     force: bool = False,
                                     project_id: str = ""):
    """Generate a fresh draft of `product_type` into the shared autodraft
    pool, in the background. Returns 202 immediately — the new artifact
    lands in /api/products/records (state='complete', newest timestamp,
    so it sorts to the top) once the DAG completes, or surfaces as a
    state='failed' row if it errors. The dashboard subscribes via the
    product_records WS broadcast to pick it up.

    `by` is the triggering analyst's email — attribution carried in the
    record's source_metadata + the completion notification. Duplicate
    concurrent fires are allowed (the UI warns first); each gets its own
    record_id via register_and_run so they track independently.

    `target_entity_id` (optional) — for per-target products (currently
    TIDAT; TMP/CDE/BDA phase-2) the analyst initiates from the
    operations map by right-clicking a red mil_unit. The frontend POSTs
    target_entity_id=<kg entity_id> here; this endpoint packages it
    into a target_focus dict and threads it through refresh_drafts →
    _autodraft_one_registered → _autodraft_one, where the writer prompt
    binds the artifact to that single target (no fanout, one artifact
    per click). When absent — the dashboard Generate button path or a
    sweep — the call is unbound and per-target products skip in the
    writer prompt with a gap notice. See
    scratch/docs/ibl/map-only-target-products.md for the full design.

    Reuses the same machinery as the startup autodraft sweep
    (product_autodraft.refresh_drafts → _autodraft_one_registered), so the
    artifact is owned by the _autodraft pool and visible in /api/products/records
    without any project/session plumbing. SITEMP fans out per-COA as usual.
    """
    from templates import PRODUCT_SCHEMAS
    if product_type not in PRODUCT_SCHEMAS:
        raise HTTPException(status_code=404,
                            detail=f"unknown product_type {product_type!r}")

    # Per-target products (shape=header_only — TIDAT / TMP / CDE / BDA /
    # CA) MUST carry a target_entity_id. Without one the writer has no
    # binding and emits a malformed artifact (empty target_id, picked-by-
    # writer-from-dep-payload target name, no AGM row to echo). Reject
    # the POST so callers that forgot to plumb the binding get a clear
    # 400 instead of a polluted artifact landing in the chain table.
    # The right-click gesture on a red mil_unit ALWAYS supplies the
    # entity_id; sweep paths are blocked separately by _SKIP_PRODUCTS.
    _spec = PRODUCT_SCHEMAS[product_type]
    _is_per_target = (isinstance(_spec, dict)
                      and _spec.get("shape") == "header_only")
    if _is_per_target and not target_entity_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"product_type {product_type!r} is per-target (shape="
                f"header_only); target_entity_id is required. Right-click "
                f"a red mil_unit on the operations map to bind a target, "
                f"or pass &target_entity_id=<kg_id> explicitly."
            ),
        )

    # Build the target_focus dict only when the caller actually bound
    # one — passing an always-present empty dict downstream would defeat
    # the `if target_focus and product_type in _PER_TARGET_PRODUCTS:` gate
    # in _autodraft_one's writer-context block. Side defaults to "red"
    # because phase-1 right-click is gated to red mil_units only; phase-2
    # may parameterize this when neutral/unknown target gestures land.
    target_focus = None
    if target_entity_id:
        target_focus = {"entity_id": target_entity_id, "side": "red"}
    # Schema-driven analyst config — gear button next to Generate sends
    # one entry per config_fields key. Parsed once here, threaded into
    # the autodraft via a contextvar (so equipment_doctrine.is_hvt_worthy
    # picks it up wherever it's called inside the DAG).
    analyst_config: dict = {}
    if config_json:
        try:
            import json as _json
            parsed = _json.loads(config_json)
            if isinstance(parsed, dict):
                analyst_config = parsed
        except Exception as _e:
            log.warning("generate: config_json parse failed: %s", _e)
    # Fire-and-forget on the event loop. refresh_drafts catches per-product
    # errors internally (they become state='failed' records via product_records),
    # so the task never raises into an unretrieved-exception warning.
    asyncio.create_task(_generate_with_config(
        product_type=product_type, by=by, target_focus=target_focus,
        analyst_config=analyst_config, force=force, project_id=project_id,
    ))
    return JSONResponse(content={
        "ok": True,
        "product_type": product_type,
        "status": "generating",
        "target_entity_id": target_entity_id or None,
        "force": force,
    }, status_code=202)


# ==================== SCENARIO INGESTION (OPORD / FRAGO PARSE) ====================

@app.post("/api/scenario/regenerate-from-opord")
async def regenerate_scenario_from_opord():
    """Parse the latest OPORD + FRAGOs + ANNEX_L_INTEL via Gemma4 and apply
    the result to the geo-agent master map (operations overlay) and the
    in-memory scenario module (civil / threat disposition / phases).

    Synchronous from the caller's perspective — blocks for 30-90s while
    Gemma4 chews. Returns the parse summary on success.

    Raises HTTP 500 on parse failure with the offending payload in the
    error body so the analyst sees exactly which constraint failed.

    `scenario.py` source file is NEVER modified. The next process restart
    discards the in-memory overlay/civil/threat/phases — that is the
    intended behavior: this is a derived view, re-fired on demand.
    """
    # Lazy import to avoid pulling the Gemma4 / geo-agent path into main.py
    # startup cost; analysts won't hit this endpoint until they click the
    # green refresh icon.
    from handlers import scenario_ingest

    # No analyst RunState here — list_project_docs falls back to the static
    # PROJECT_DOCS catalog when state is None. Published artifacts will be
    # picked up automatically once we wire in user-scoped state.
    try:
        result = await scenario_ingest.parse_and_apply_opord(state=None)
    except scenario_ingest.ScenarioIngestError as e:
        # Surface the offending payload so the analyst can diagnose why
        # Gemma4's output failed validation (or the geo-agent sync failed).
        raise HTTPException(
            status_code=500,
            detail={"error": str(e), "payload": getattr(e, "payload", None)},
        )
    return JSONResponse(content=result, status_code=200)


# ==================== MAP REFRESH ENDPOINT (Req 2d) ====================

@app.post("/api/artifacts/{artifact_id}/refresh_map")
async def refresh_artifact_map(artifact_id: str,
                               body: Optional[dict] = Body(default=None)):
    """Thin route wrapper. Business logic lives in `handlers/map.py`.

    Optional JSON body {image_url, map_id} — the in-map Done-editing hook:
    geo-agent already rendered + persisted the JPG; the handler downloads
    and embeds it for the matching map field instead of re-rendering.

    Dispatched to a worker thread: the handler runs sync DB queries and
    calls `render_map_to_png_sync` (which uses `asyncio.run`), neither of
    which can share the request's event loop.
    """
    from handlers import map as map_handler
    image_url = ((body or {}).get("image_url") or "").strip() or None
    hook_map_id = ((body or {}).get("map_id") or "").strip() or None
    try:
        result = await asyncio.to_thread(
            map_handler.refresh_artifact_map, artifact_id,
            None, image_url, hook_map_id,
        )
        return JSONResponse(content=result, status_code=200)
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


# ==================== AAR ENDPOINTS ====================








# ==================== TRANSCRIPT ENDPOINTS ====================





# ==================== CHAT ENDPOINTS ====================




# Configuration
OUTPUT_DIR = Path("transcripts")
OUTPUT_DIR.mkdir(exist_ok=True)

# Global Whisper model (load once at startup)
whisper_model = None

def load_whisper_model():
    """Runs in a background thread — safe for CPU-bound loading"""
    global whisper_model
    try:
        print("=" * 60)
        print("Loading Whisper model in background thread...")
        print("This may take a few minutes on first run (downloading model)...")
        print("=" * 60)

        whisper_model = WhisperModel(
            config.Config.AUDIO_MODEL(),
            device="cpu",
            compute_type="int8",
            download_root="./models"
        )

        # Publish into core.startup's global too — the ported cloud
        # transcription routes (routers/api/files.py, routers/ws/aar.py)
        # read core.startup.get_whisper_model(); ONE loader, one model.
        from core import startup as _cloud_core_startup
        _cloud_core_startup.whisper_model = whisper_model

        print("\nWhisper model loaded successfully")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nFailed to load Whisper model: {str(e)}")


# startup/shutdown wiring lives in the `lifespan` async context manager
# at the top of this module (passed to FastAPI(lifespan=lifespan)).
# The body of what used to be @app.on_event("startup") + @app.on_event("shutdown")
# is the body of `lifespan` — same imports, same task launches, same
# teardown sequence, just unified under the supported lifespan API.

@app.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """
    Receive WAV audio chunk and transcribe with Whisper
    No conversion needed - WAV goes directly to Whisper!
    """
    
    temp_file = None
    
    try:
        # Validate upload
        if not audio.filename:
            raise HTTPException(status_code=400, detail="No audio file provided")
        
        # Validate it's a WAV file
        if not audio.filename.endswith('.wav'):
            raise HTTPException(
                status_code=400, 
                detail="Only WAV files accepted. Frontend should send WAV format."
            )
        
        # Read audio data
        audio_data = await audio.read()
        file_size_kb = len(audio_data) / 1024
        
        print(f"\n{'='*60}")
        print(f"Received: {audio.filename} ({file_size_kb:.2f} KB)")
        
        # Save WAV file temporarily
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.wav',
            dir=OUTPUT_DIR
        )
        temp_file.write(audio_data)
        temp_file.close()
        
        print(f"Processing WAV file...")
        
        # Transcribe with Whisper (no conversion needed!)
        # The model loads in a background thread at startup (see
        # load_whisper_model), so it may not be ready if a transcript
        # request arrives in the first minute or two of boot. Return
        # 503 with a friendly message rather than 500 on AttributeError —
        # the frontend can retry once the model is warm.
        if whisper_model is None:
            raise HTTPException(
                status_code=503,
                detail="Speech-to-text model is still warming up. "
                       "Please retry in a moment."
            )
        print(f"Transcribing with Whisper...")
        segments, info = whisper_model.transcribe(
            temp_file.name,
            beam_size=5,
            language="en"
        )
        
        # Combine all segments into full text
        transcription_text = " ".join([segment.text for segment in segments])
        
        # Get audio info
        duration = info.duration if hasattr(info, 'duration') else None
        
        print(f"Transcription complete!")
        print(f"Text: {transcription_text[:80]}...")
        print(f"Duration: {duration:.2f}s" if duration else "")
        print(f"{'='*60}\n")
        
        # Prepare response
        response = {
            "status": "success",
            "transcription": transcription_text.strip(),
            "duration_seconds": round(duration, 2) if duration else None,
            "audio_language": info.language if hasattr(info, 'language') else "en"
        }
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"\n✗ Error: {str(e)}\n")
        raise HTTPException(
            status_code=500,
            detail=f"Processing error: {str(e)}"
        )
    finally:
        pass
        # Clean up temp file
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except:
                pass

    
@app.websocket("/ws/chat/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    import queue as _queue
    await manager.connect(websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())

            # Handle doc_translate separately — no question/session needed
            if data.get('type') == 'doc_translate':
                print(f"[ws/chat] doc_translate request received for language: {data.get('targetLanguage')}")
                result = await asyncio.get_event_loop().run_in_executor(
                    None, chat.doc_translate, data
                )
                await manager.send_personal_message(result, websocket)
                continue

            status_queue = _queue.Queue()

            future = asyncio.get_event_loop().run_in_executor(
                None, chat.main, data, lambda msg: status_queue.put(msg)
            )

            while not future.done():
                try:
                    msg = status_queue.get(timeout=0.5)
                    await manager.send_personal_message(
                        json.dumps({"type": "status", "message": msg}), websocket
                    )
                except _queue.Empty:
                    await asyncio.sleep(0.1)

            # Drain remaining status messages
            while not status_queue.empty():
                msg = status_queue.get_nowait()
                await manager.send_personal_message(
                    json.dumps({"type": "status", "message": msg}), websocket
                )

            result = future.result()
            await manager.send_personal_message(result, websocket)
    except WebSocketDisconnect:
        pass

@app.websocket("/ws/agents/{client_id}")
async def websocket_agents(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            await agents.main(data, manager, websocket)
            # await manager.broadcast(f"Client #{client_id} says: {data}")
    except WebSocketDisconnect:
        pass
        # manager.disconnect(websocket)
        # await manager.broadcast(f"Client #{client_id} left the chat")


@app.websocket("/ws/intel_agent/{client_id}")
async def websocket_intel_agent(websocket: WebSocket, client_id: str):
    """Doctrinal intel-agent dispatches (the 15 schema-backed product types).

    Frame routing — the frontend (IntelAgentWs.tsx) sends three frame shapes:
      - {type: "ping"}                — 20s heartbeat. Reply {type: "pong"}.
      - {type: "resume", sessionId,
          session_id, last_seen_event_id} — sent on reconnect for each active
          session subscription. ACK-only today; replay support is TBD.
      - {type: "generate_product",   — parallel-DAG doctrinal product gen.
         product_type, sessionId,       Currently INTSUM-only at the DAG
         userId, projectId, context}    layer; routed through tool_generate_product.
                                        Job survives WS disconnect via
                                        asyncio.create_task (see below).
      - {question: {prompt: "..."},   — real analyst dispatch.
         sessionId, userId, ...}

    Without this branching, EVERY frame (including pings and resume frames)
    would fall through to `intel_agent.main` which would extract an empty
    `question`, fire `run_turn`, and burn a 15-30s LLM call producing a
    hallucinated "Standing by" response. The frontend reconnect loop then
    re-fires the resume frame and the cycle continues — the "thinking loop"
    symptom that crashed UX usability.
    """
    await manager.connect(websocket)
    _ACTIVE_INTEL_SOCKETS.add(websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            frame_type = (data.get("type") or "").lower() if isinstance(data, dict) else ""
            if frame_type == "ping":
                await manager.send_personal_message(json.dumps({"type": "pong"}), websocket)
                continue
            if frame_type == "resume":
                # ACK only — replay support not yet wired. The frontend
                # falls back to /api/jobs/{id} on mid-generation refresh
                # for chat resilience.
                await manager.send_personal_message(json.dumps({
                    "type": "resume_ack",
                    "session_id": data.get("session_id") or data.get("sessionId") or "",
                    "supported": False,
                }), websocket)
                continue
            if frame_type == "generate_product":
                await _dispatch_generate_product(data, manager, websocket)
                continue
            if frame_type == "request_snapshot":
                # Frontend (IntelAgentWs) sends this on every connect /
                # reconnect. Broadcast a product_records_snapshot frame
                # via _ws_broadcast — every connected subscription's
                # useProductChains reducer treats it as a full reset of
                # the chain map. Closes the wave-1 coordination gap
                # (fd-2 backend side).
                from handlers import product_records as _pr
                await _pr.emit_snapshot()
                continue
            await intel_agent.main(data, manager, websocket)
    except WebSocketDisconnect:
        pass
    finally:
        _ACTIVE_INTEL_SOCKETS.discard(websocket)


# ============================================================================
# Parallel-DAG dispatcher (the generate_product frame)
# ============================================================================
async def _dispatch_generate_product(data: dict, manager_obj, websocket):
    """Handle a `{type: "generate_product"}` frame. Mints a job_id, ACKs it
    immediately to the websocket, and fires the DAG runner as an
    `asyncio.create_task` so the run survives if the client disconnects
    mid-generation. Honors all five DESIGN.md §12 contracts:

      1. args dict IS the request_payload shape (frame body verbatim).
      2. job_id minted at dispatch, written to the in-memory _JOBS dict
         so /api/jobs/{id} (chat-resilience refresh hook) can resolve it.
      3. Events publish through `_make_publisher` chokepoint only —
         the runner NEVER touches manager.send_personal_message.
      4. Same code path the OE's `generate_product` tool call uses
         (handlers.intel_agent.tool_generate_product → run_dag).
      5. CONTAINER_ID env var stamped on every job entry.
    """
    import uuid as _uuid
    from handlers.intel_agent import (
        _job_register, _job_update, load_state, tool_generate_product,
    )
    from utils import database as _db

    job_id = str(_uuid.uuid4())
    container_id = os.environ.get("CONTAINER_ID", "docker-api")
    now_ms = int(time.time() * 1000)
    product_type = data.get("product_type") or ""
    session_id = (data.get("sessionId") or data.get("session_id") or "").strip()
    user_id = (data.get("userId") or data.get("user_id") or "").strip()
    project_id = (data.get("projectId") or data.get("project_id") or "").strip()
    logging.getLogger("pytho.dispatch").info(
        "[dispatch_generate_product] frame product_type=%r session=%s project=%s user=%s frame_keys=%s",
        product_type, session_id, project_id, user_id, sorted((data or {}).keys()))

    _job_register(job_id, extra={
        "request_type": "generate_product",
        "request_payload": data,           # EXACT websocket frame
        "session_id": session_id,
        "user_id": user_id,
        "project_id": project_id,
        "manual_product_type": product_type,
        "container_id": container_id,
        "status": "in_progress",
        "started_at_ms": now_ms,
        "last_event_at_ms": now_ms,
        "phase": "queued",
        "product_type": product_type,
        "context": data.get("context") or "",
    })

    # ACK immediately so the frontend has job_id BEFORE we kick off the run.
    # `session_id` is stamped so the singleton WS subscriber on the frontend
    # routes this frame to the right ChatAgent subscription.
    try:
        await manager_obj.send_personal_message(json.dumps({
            "type": "generate_product_started",
            "job_id": job_id,
            "product_type": product_type,
            "container_id": container_id,
            "started_at_ms": now_ms,
            "session_id": session_id,
            "sessionId": session_id,
        }), websocket)
    except Exception:
        pass

    publisher = _make_publisher(job_id, manager_obj, websocket, container_id,
                                  session_id=session_id)

    # Fire-and-forget. The task does NOT hold the websocket; if the client
    # disconnects, publisher.send swallows errors and the run continues.
    asyncio.create_task(_run_and_finalize(
        job_id=job_id,
        data=data,
        session_id=session_id,
        user_id=user_id,
        project_id=project_id,
        product_type=product_type,
        publisher=publisher,
    ))


# ----------------------------------------------------------------------------
# Per-session WS send queue
# ----------------------------------------------------------------------------
# The publisher used to schedule every WS send as a bare
# `asyncio.create_task(send)`. That meant N concurrent `send` coroutines for N
# DAG events — the asyncio scheduler is free to run them in any order, so the
# frontend would see frames arrive out of DAG order (e.g. `artifact_complete`
# landing before `phase=writer`). User requirement: "execute and display in
# DAG order."
#
# Fix: one FIFO queue per session_id, drained by a single dedicated task that
# awaits each send sequentially. Cross-session sends never block each other
# (each session has its own queue + drainer), but within a session frames go
# out in the order they were enqueued.
#
# Sentinel `None` enqueued by the drainer when the queue's last item is sent
# would let us shut down idle drainers, but in practice a session's drainer
# lives for the WS lifetime and is small (one coro, one queue).
_SESSION_SEND_QUEUES: dict = {}  # session_id -> asyncio.Queue
_SESSION_DRAIN_TASKS: dict = {}  # session_id -> asyncio.Task
# Fallback bucket for events without a session_id — still FIFO, just shared.
_NO_SESSION_KEY = "__no_session__"


def _get_or_start_session_drainer(session_key: str):
    """Return (queue, task) for `session_key`, starting the drainer if needed.
    Idempotent — safe to call once per publish."""
    queue = _SESSION_SEND_QUEUES.get(session_key)
    task = _SESSION_DRAIN_TASKS.get(session_key)
    if queue is None:
        queue = asyncio.Queue()
        _SESSION_SEND_QUEUES[session_key] = queue
    if task is None or task.done():
        task = asyncio.create_task(
            _session_send_drainer(session_key, queue),
            name=f"ws_send_drainer:{session_key}",
        )
        _SESSION_DRAIN_TASKS[session_key] = task
    return queue, task


async def _session_send_drainer(session_key: str, queue):
    """Drain `queue` sequentially. Each item is a tuple of
    (manager_obj, websocket, payload_str). Awaits each send before pulling
    the next, guaranteeing FIFO delivery to the WS. Send failures (stale
    websocket) are logged and the loop continues — the job registry is the
    authoritative record."""
    while True:
        try:
            item = await queue.get()
        except asyncio.CancelledError:
            return
        if item is None:
            # Sentinel — drainer asked to exit.
            return
        manager_obj, websocket, payload_str = item
        try:
            await manager_obj.send_personal_message(payload_str, websocket)
        except Exception as exc:
            # WS likely closed. Don't drop the drainer — subsequent enqueues
            # for the same session still need to run their registry updates;
            # the send arm just becomes a no-op.
            log.debug(
                "ws send_drainer[%s]: send failed (%s) — continuing",
                session_key, exc,
            )
        finally:
            queue.task_done()


def _make_publisher(job_id: str, manager_obj, websocket, container_id: str,
                     session_id: str = ""):
    """Build the on_event chokepoint for a single DAG run. Every event
    callback gets stamped with job_id + monotonically-increasing seq_id +
    container_id + emitted_at_ms + session_id before sending over the WS
    and bumping the in-memory job registry.

    Per-session FIFO ordering: the WS send is enqueued onto the session's
    queue and a single drainer task awaits each send in order. Within a
    session, frames are guaranteed to arrive at the frontend in the order
    publish() was called. Across sessions, sends do not block each other.

    `session_id` is stamped on every event so the frontend's singleton WS
    subscriber (IntelAgentWs.tsx) routes the frame to the matching
    ChatAgent session subscription. Without it, DAG events would land in
    the broadcast bucket and only reach the UI by accident-of-singleton.
    """
    from handlers.intel_agent import _job_update
    seq = {"n": 0}
    session_key = session_id or _NO_SESSION_KEY

    def publish(event: dict):
        seq["n"] += 1
        if not isinstance(event, dict):
            event = {"type": "unknown", "raw": str(event)}
        event = dict(event)
        event.setdefault("job_id", job_id)
        event["seq_id"] = seq["n"]
        event["container_id"] = container_id
        event["emitted_at_ms"] = int(time.time() * 1000)
        if session_id:
            event.setdefault("session_id", session_id)
            event.setdefault("sessionId", session_id)

        # Bump the job registry. Captures phase + artifact_id when emitted.
        update_fields = {
            "phase": event.get("type", ""),
            "last_event_type": event.get("type", ""),
            "last_event_at_ms": event["emitted_at_ms"],
            "events_count_at_least": seq["n"],
        }
        if event.get("type") == "artifact_emitted" and event.get("artifact_id"):
            update_fields["artifact_id"] = event["artifact_id"]
        try:
            _job_update(job_id, **update_fields)
        except Exception:
            pass

        # Enqueue onto the session's FIFO send queue. The drainer
        # (one task per session) awaits each send sequentially so frames
        # arrive at the frontend in DAG order.
        try:
            queue, _task = _get_or_start_session_drainer(session_key)
            payload_str = json.dumps(event, default=str)
            queue.put_nowait((manager_obj, websocket, payload_str))
        except Exception:
            log.exception(
                "ws publish: failed to enqueue event session=%s type=%s",
                session_id, event.get("type"),
            )

    return publish


async def _run_and_finalize(*, job_id: str, data: dict, session_id: str,
                              user_id: str, project_id: str,
                              product_type: str, publisher):
    """Run the requested generation and stamp terminal status on the job
    registry. Detached from the websocket — if the client disconnects, this
    keeps running and the artifact still lands on disk + in the DB.

    Every product goes through the parallel-DAG runner (tool_generate_product
    → runtime.dag_executor.run_dag). The earlier OE-manual fallback for
    non-INTSUM products is gone — manual generation MUST go through the DAG.
    Events stream through the `publisher` chokepoint built in
    _dispatch_generate_product.
    """
    from handlers.intel_agent import _job_update, load_state, tool_generate_product
    from utils import database as _db

    # ---- DAG path (generic across PRODUCT_SCHEMAS) -----------------------
    db = _db.SessionLocal()
    try:
        state = load_state(session_id, db, user_id=user_id, project_id=project_id,
                            manual_product_type=product_type)
        # tool_generate_product expects args in the request_payload shape;
        # the websocket frame already IS that shape (contract #1).
        result = await tool_generate_product(data, state, on_event=publisher)
        if isinstance(result, dict) and result.get("error"):
            _job_update(job_id, status="failed", phase="dag_error",
                         error=f"{result.get('error')}: {result.get('detail') or result.get('fallback') or ''}",
                         ended_at_ms=int(time.time() * 1000))
            publisher({"type": "error", "where": "dag_run",
                        "error": result.get("error"),
                        "detail": result.get("detail") or result.get("fallback")})
            return
        artifact_id = (result or {}).get("artifact_id")
        rendered = (result or {}).get("rendered") or {}
        _job_update(job_id,
                     status="completed",
                     phase="completed",
                     artifact_id=artifact_id,
                     ended_at_ms=int(time.time() * 1000),
                     gap_count=len((result or {}).get("gaps") or []),
                     turn_total_ms=(result or {}).get("turn_total_ms"))
        publisher({
            "type": "artifact_complete",
            "artifact_id": artifact_id,
            "product_type": product_type,
            "htmlLink": rendered.get("htmlLink") if isinstance(rendered, dict) else None,
            "docxLink": rendered.get("docxLink") if isinstance(rendered, dict) else None,
            "pptxLink": rendered.get("pptxLink") if isinstance(rendered, dict) else None,
            "xlsxLink": rendered.get("xlsxLink") if isinstance(rendered, dict) else None,
            "turn_total_ms": (result or {}).get("turn_total_ms"),
            "gap_count": len((result or {}).get("gaps") or []),
        })
    except Exception as e:
        _job_update(job_id, status="failed", phase="exception",
                     error=f"{type(e).__name__}: {e}",
                     ended_at_ms=int(time.time() * 1000))
        try:
            publisher({"type": "error", "where": "dispatcher",
                       "error": f"{type(e).__name__}: {e}"})
        except Exception:
            pass
    finally:
        try:
            db.close()
        except Exception:
            pass


# ==================== FEEDBACK ENDPOINTS ====================

@app.post("/api/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
def create_feedback(
    feedback_data: FeedbackCreate,
    request: Request,
    db: Session = Depends(database.get_db)
):
    user_email = request.headers.get("x-user-email", "unknown")
    feedback = database.Feedback(
        user_email=user_email,
        sentiment=feedback_data.sentiment,
        question=feedback_data.question,
        answer=feedback_data.answer,
        comment=feedback_data.comment,
        session_id=feedback_data.session_id,
        project_id=feedback_data.project_id,
    )
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return feedback

@app.patch("/api/feedback/{feedback_id}", response_model=FeedbackResponse)
def update_feedback_comment(
    feedback_id: int,
    update_data: FeedbackUpdate,
    db: Session = Depends(database.get_db)
):
    feedback = db.query(database.Feedback).filter(database.Feedback.id == feedback_id).first()
    if not feedback:
        raise HTTPException(status_code=404, detail="Feedback not found")
    feedback.comment = update_data.comment
    db.commit()
    db.refresh(feedback)
    return feedback

@app.get("/api/feedback", response_model=List[FeedbackResponse])
def get_feedback(
    project_id: Optional[str] = Query(None),
    db: Session = Depends(database.get_db)
):
    query = db.query(database.Feedback).order_by(database.Feedback.created_at.desc())
    if project_id:
        query = query.filter(database.Feedback.project_id == project_id)
    return query.all()

# ==================== WEBSOCKET ENDPOINT ====================

# Create a thread pool for blocking operations
executor = ThreadPoolExecutor(max_workers=10)

@app.websocket("/ws/aar")
async def websocket_summarize(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection established")
    
    try:
        while True:
            # Receive data from client
            data = await websocket.receive_json()
            action = data.get("action")
            
            if not action:
                await websocket.send_json({
                    "status": "error",
                    "error": "No action specified"
                })
                continue
            
            logger.info(f"Received action: {action}")
            
            if action == "summary":
                transcript_entries = data.get("transcript", [])
                
                if not transcript_entries:
                    await websocket.send_json({
                        "action": "summary",
                        "status": "error",
                        "error": "No transcript entries provided"
                    })
                    continue
                
                logger.info(f"Processing summary for {len(transcript_entries)} entries")
                
                # Send processing acknowledgment
                await websocket.send_json({
                    "action": "summary",
                    "status": "processing",
                    "message": "Generating summary..."
                })
                
                # Run blocking operation in thread pool
                
                summary_dict =  aar.generate_summary_sync(transcript_entries)
    
                
                logger.info(f"Summary generated: {summary_dict.get('status')}")
                await websocket.send_json(summary_dict)
                
            elif action == "chat":
                question = data.get("question")
                transcript_entries = data.get("transcript", [])
                
                if not question:
                    await websocket.send_json({
                        "action": "chat",
                        "status": "error",
                        "error": "No question provided"
                    })
                    continue
                
                if not transcript_entries:
                    await websocket.send_json({
                        "action": "chat",
                        "status": "error",
                        "error": "No transcript available for context"
                    })
                    continue
                
                logger.info(f"Processing chat question: {question[:50]}...")
                
                # Send processing acknowledgment
                await websocket.send_json({
                    "action": "chat",
                    "status": "processing",
                    "message": "Generating response..."
                })
                
                # Run blocking operation in thread pool
                loop = asyncio.get_event_loop()
                chat_dict = await loop.run_in_executor(
                    executor,
                    aar.generate_chat_response_sync,
                    question,
                    transcript_entries
                )
                
                logger.info(f"Chat response generated: {chat_dict.get('status')}")
                await websocket.send_json(chat_dict)

            elif action == "analyze":
                question = data.get("question")
                content = data.get("content", "")

                if not question or not str(question).strip():
                    await websocket.send_json({
                        "status": "error",
                        "error": "No question provided"
                    })
                    continue
                if not content or not str(content).strip():
                    await websocket.send_json({
                        "status": "error",
                        "error": "No content available to analyze"
                    })
                    continue

                logger.info("Processing analyze request")
                await websocket.send_json({
                    "status": "processing",
                    "message": "Analyzing..."
                })

                loop = asyncio.get_event_loop()
                analyze_dict = await loop.run_in_executor(
                    executor,
                    aar.generate_analyze_response_sync,
                    str(question).strip(),
                    str(content).strip(),
                )
                logger.info(f"Analyze response: {analyze_dict.get('status')}")
                await websocket.send_json(analyze_dict)

            else:
                await websocket.send_json({
                    "status": "error",
                    "error": f"Unknown action: {action}"
                })
                
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected normally")
    except Exception as e:
        logger.error(f"WebSocket error: {str(e)}", exc_info=True)
        try:
            await websocket.send_json({
                "status": "error",
                "error": str(e)
            })
        except Exception as send_error:
            logger.error(f"Failed to send error message: {str(send_error)}")
 


# ---- Cloud-surface routers (edge+cloud unification, 2026-07-07) ----
# The full main-lineage cloud SPA surface: /api projects/documents/
# events/aar/transcripts/chat/files/rfis/seminars/injects/
# coa_submissions/notifications/agent_proxy plus the /ws realtime plane
# (chat/agents/aar/transcribe/rfi/notifications). Included AFTER every
# inline route above, so where a path is deliberately kept inline
# (/api/, /api/save/, /api/transcribe — the airgap-evolved versions),
# the inline registration wins by order. Stale inline duplicates of
# router paths were REMOVED, so the routers own auth/projects/aar/
# documents/sessions/event_log.
from routers.api import api_router as _cloud_api_router
from routers.ws import ws_router as _cloud_ws_router

app.include_router(_cloud_api_router)
app.include_router(_cloud_ws_router, prefix="/ws")
# The cloud startup/shutdown (rfi migrations + thread-pool executor)
# runs inside the lifespan handler above — this FastAPI is
# lifespan-only (on_event/add_event_handler removed upstream).


if __name__ == "__main__":
    uvicorn.run("main:app", port=8000, reload=True, env_file='./app/.env.local')