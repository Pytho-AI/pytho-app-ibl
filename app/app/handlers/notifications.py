"""Analyst notification surface — persist + broadcast + open-draft.

One module owns three concerns:

  1. Persist:   _persist_notification() writes a row to the
                database.Notification table.

  2. Broadcast: _ws_broadcast() sends a {type:"notification", ...} frame
                across every open WebSocket connected to /ws/intel_agent.
                The IntelAgentWs singleton on the frontend already routes
                sessionless frames to every subscriber, which the new
                NotificationsProvider taps to populate
                useAppContext().notifications.

  3. Promote:   open_draft_in_new_session() copies the _autodraft artifact
                from KLIEGLIGHT auto-fire into the requesting analyst's
                user/project namespace, mints a fresh chat session, and
                returns the new session_id so the UI can navigate to
                /missioncommand/docs/<session_id>.

Glue points
-----------
  main.py @startup: registers the notification emitter on
      handlers.klieglight_autofire and snapshots the active
      ConnectionManager so _ws_broadcast can reach analyst sockets.

  main.py /api/notifications/{user_id}     GET   list  (NotificationsProvider)
  main.py /api/notifications/{id}/open_draft POST  open-draft promote
  main.py /api/notifications/{id}/read     POST   mark read

The _autodraft artifact storage convention (handlers/klieglight_autofire
+ handlers/product.render_artifact_files):

    uploads/_autodraft/<artifact_id>/
        emit_dispatch.json   <- the product_writer_dispatches[] entry
        index.html           <- rendered artifact
        artifact.docx        <- rendered artifact

open_draft_in_new_session promotes by:
  - re-reading the existing _autodraft SessionDetail row (autodraft_session_id)
  - rewriting structured_data.header.owner_user_id + .status
  - persisting a new SessionDetail row under the analyst's user_id +
    project_id with a fresh session_id
  - rsync-ing uploads/_autodraft/<aid> -> uploads/<user_id>/<aid>
    (so /files/<user>/<aid>/index.html resolves)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from utils import database

log = logging.getLogger(__name__)


# Set by main.py at startup so _ws_broadcast can reach the live socket
# pool. We use a weakref-style holder so test code can swap it without
# touching the global module state directly.
_CONNECTION_MANAGER = None
_ACTIVE_SOCKETS: set = set()


def register_connection_manager(manager, active_sockets: set) -> None:
    """main.py calls this with the ConnectionManager instance + a shared
    set of currently-open WebSockets. The set is mutated in place by the
    ws/intel_agent handler — we just hold a reference so broadcast can
    iterate it.
    """
    global _CONNECTION_MANAGER, _ACTIVE_SOCKETS
    _CONNECTION_MANAGER = manager
    _ACTIVE_SOCKETS = active_sockets


async def _ws_broadcast(frame: dict) -> None:
    """Best-effort fan-out of `frame` to every currently-connected
    /ws/intel_agent socket. Failures per-socket are logged + swallowed —
    a wedged socket should not stop the broadcast.
    """
    if not _ACTIVE_SOCKETS or _CONNECTION_MANAGER is None:
        return
    payload = json.dumps(frame, default=str)
    # Iterate a snapshot so removals during iteration don't trip us up.
    for sock in list(_ACTIVE_SOCKETS):
        try:
            await _CONNECTION_MANAGER.send_personal_message(payload, sock)
        except Exception:
            log.debug("ws broadcast failed for one socket; ignoring", exc_info=True)


def _persist_notification(payload: dict) -> dict:
    """Write one Notification row + return it as a serializable dict.

    For KLIEGLIGHT auto-fire the user_id is "*" (broadcast); other
    producers may scope to a specific analyst.
    """
    db = database.SessionLocal()
    try:
        row = database.Notification(
            user_id=payload.get("user_id") or "*",
            kind=payload.get("type") or "generic",
            artifact_id=payload.get("artifact_id") or None,
            autodraft_session_id=payload.get("autodraft_session_id") or None,
            payload=json.dumps(payload, default=str),
            status="unread",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "user_id": row.user_id,
            "kind": row.kind,
            "artifact_id": row.artifact_id,
            "autodraft_session_id": row.autodraft_session_id,
            "payload": json.loads(row.payload or "{}"),
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
    finally:
        db.close()


async def emit_notification(payload: dict) -> dict:
    """Persist + broadcast. The two-step is deliberate: persisting first
    means the notification survives a WS-broadcast failure (and replays
    when the analyst's UI calls GET /api/notifications/<user_id>).

    This is the emitter wired into handlers.klieglight_autofire via
    register_notification_emitter().
    """
    persisted = _persist_notification(payload)
    # Add row id so the UI can call open_draft with the database id.
    frame = {
        **payload,
        "id": persisted["id"],
        "created_at": persisted["created_at"],
    }
    await _ws_broadcast(frame)
    return persisted


def list_notifications_for_user(user_id: str, *, limit: int = 100) -> list[dict]:
    """Return the most recent notifications visible to `user_id`.

    Visibility: every row where (user_id == requested OR user_id == "*").
    Broadcast notifications (user_id="*") are visible to every analyst —
    that's how KLIEGLIGHT auto-fires reach everyone without being scoped
    to one user.
    """
    db = database.SessionLocal()
    try:
        rows = (
            db.query(database.Notification)
            .filter(
                (database.Notification.user_id == user_id)
                | (database.Notification.user_id == "*")
            )
            .order_by(database.Notification.id.desc())
            .limit(limit)
            .all()
        )
        out = []
        for row in rows:
            out.append({
                "id": row.id,
                "user_id": row.user_id,
                "kind": row.kind,
                "artifact_id": row.artifact_id,
                "autodraft_session_id": row.autodraft_session_id,
                "payload": json.loads(row.payload or "{}"),
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            })
        return out
    finally:
        db.close()


def mark_notification_status(notification_id: int, status: str) -> dict:
    """Update a notification row's status. Returns the updated row or
    {"error": ...} when not found.
    """
    if status not in ("unread", "read", "consumed"):
        return {"error": f"unknown status: {status}"}
    db = database.SessionLocal()
    try:
        row = (
            db.query(database.Notification)
            .filter(database.Notification.id == notification_id)
            .first()
        )
        if row is None:
            return {"error": f"notification {notification_id} not found"}
        row.status = status
        db.commit()
        db.refresh(row)
        return {
            "id": row.id,
            "status": row.status,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Open draft in new chat session
# ---------------------------------------------------------------------------

_UPLOAD_DIR = Path(os.environ.get("PYTHO_UPLOADS_DIR", "uploads"))


def open_draft_in_new_session(
    notification_id: int,
    *,
    user_id: str,
    project_id: str,
) -> dict:
    """Promote the _autodraft KLIEGLIGHT artifact pointed at by
    `notification_id` into the analyst's namespace.

    Steps:
      1. Resolve the notification row -> artifact_id + autodraft_session_id.
      2. Read the autodraft SessionDetail row to extract the
         product_writer_dispatches entry (carrying the emit's
         structured_data, gaps, dtg, etc.).
      3. Mint a fresh session_id.
      4. Rewrite structured_data.header.owner_user_id to the requesting
         analyst; keep status="draft" so the artifact is still
         draft-scoped (only visible to the new owner via privatedocs).
      5. Write a new SessionDetail row keyed by (new session_id,
         user_id, project_id) with the rewritten dispatch under
         agent_trace.product_writer_dispatches. load_state() walks this
         shape so _project_docs_index will surface the artifact.
      6. Copy uploads/_autodraft/<artifact_id>/ ->
         uploads/<user_id>/<artifact_id>/ so the URLs the new
         SessionDetail row references resolve through StaticFiles.
      7. Mark the notification "consumed".
      8. Return {session_id, artifact_id, project_id, htmlLink, docxLink}.

    Returns {"error": ...} on failure with a 4xx-shaped reason string.
    """
    if not user_id:
        return {"error": "user_id required"}
    if not project_id:
        return {"error": "project_id required"}

    db = database.SessionLocal()
    try:
        note = (
            db.query(database.Notification)
            .filter(database.Notification.id == notification_id)
            .first()
        )
        if note is None:
            return {"error": f"notification {notification_id} not found"}
        artifact_id = note.artifact_id
        autodraft_session_id = note.autodraft_session_id
        if not artifact_id or not autodraft_session_id:
            return {"error": f"notification {notification_id} has no artifact to open"}

        # Locate the source SessionDetail row in the autodraft session.
        src_rows = (
            db.query(database.SessionDetail)
            .filter(database.SessionDetail.session_id == autodraft_session_id)
            .order_by(database.SessionDetail.timestamp.asc())
            .all()
        )
        if not src_rows:
            return {"error": f"autodraft session {autodraft_session_id} has no SessionDetail rows"}

        # Find the dispatch carrying our artifact_id.
        src_dispatch = None
        src_data = None
        for row in src_rows:
            try:
                d = json.loads(row.data or "{}")
            except Exception:
                continue
            trace = d.get("agent_trace") or {}
            for disp in trace.get("product_writer_dispatches") or []:
                emit = disp.get("emit") or {}
                if emit.get("artifact_id") == artifact_id:
                    src_dispatch = disp
                    src_data = d
                    break
            if src_dispatch:
                break
        if src_dispatch is None:
            return {"error": f"artifact {artifact_id} not found in autodraft session {autodraft_session_id}"}

        # Rewrite ownership + clone the dispatch dict so we don't mutate
        # the persisted autodraft row.
        new_dispatch = json.loads(json.dumps(src_dispatch, default=str))
        emit = new_dispatch.get("emit") or {}
        sd = emit.get("structured_data") or {}
        header = dict(sd.get("header") or {})
        header["owner_user_id"] = user_id
        # Still draft — analyst can promote to "published" later via the
        # existing /api/artifacts/<id>/publish endpoint.
        header["status"] = "draft"
        sd["header"] = header
        emit["structured_data"] = sd
        new_dispatch["emit"] = emit

        new_session_id = str(uuid.uuid4())
        timestamp = int(time.time() * 1000)
        new_data = {
            "userId": user_id,
            "projectId": project_id,
            "sessionId": new_session_id,
            "timestamp": timestamp,
            "type": "intel_agent",
            "title": f"KLIEGLIGHT draft {artifact_id}",
            "question": (src_data.get("question") if src_data else "") or
                        "Auto-fired KLIEGLIGHT draft promoted from notification.",
            "answer": (src_data.get("answer") if src_data else "") or
                      f"KLIEGLIGHT artifact registered: {artifact_id}",
            "agent_trace": {
                "operations_expert_messages": [],
                "intel_agent_dispatches": [],
                "product_writer_dispatches": [new_dispatch],
            },
            "iter_timings": [],
            "turn_total_ms": 0,
        }
        detail = database.SessionDetail(
            session_id=new_session_id,
            timestamp=timestamp,
            project_id=project_id,
            data=database.null_safe_json_dumps(new_data),
        )
        db.add(detail)

        # Also create a Session row so the chat UI lists this session
        # under the analyst's projects.
        session_row = database.Session(
            session_id=new_session_id,
            timestamp=timestamp,
            project_id=project_id,
            data=database.null_safe_json_dumps({
                "userId": user_id,
                "projectId": project_id,
                "sessionId": new_session_id,
                "timestamp": timestamp,
                "type": "intel_agent",
                "title": f"KLIEGLIGHT draft {artifact_id}",
                "agent": {"active_product_type": "KLIEGLIGHT"},
            })
        )
        db.add(session_row)

        # Mark the notification consumed.
        note.status = "consumed"

        db.commit()

        # Copy the rendered artifact files. _autodraft/<aid>/ -> <user>/<aid>/
        src_dir = _UPLOAD_DIR / "_autodraft" / artifact_id
        dst_dir = _UPLOAD_DIR / user_id / artifact_id
        copied = False
        if src_dir.is_dir():
            try:
                dst_dir.parent.mkdir(parents=True, exist_ok=True)
                if dst_dir.exists():
                    shutil.rmtree(dst_dir)
                shutil.copytree(src_dir, dst_dir)
                copied = True
            except Exception:
                log.exception("failed to copy autodraft files for %s", artifact_id)
        else:
            log.warning("autodraft source dir missing: %s", src_dir)

        # Build links to match the exporter's URL convention:
        # handlers/product.py:render_artifact_files writes
        #   uploads/<user_id>/<artifact_id>/artifact.html
        #   uploads/<user_id>/<artifact_id>/artifact.docx
        # which StaticFiles serves at /files/<user_id>/<artifact_id>/...
        html_link = f"/files/{user_id}/{artifact_id}/artifact.html"
        docx_link = f"/files/{user_id}/{artifact_id}/artifact.docx"

        return {
            "ok": True,
            "session_id": new_session_id,
            "project_id": project_id,
            "artifact_id": artifact_id,
            "htmlLink": html_link,
            "docxLink": docx_link,
            "files_copied": copied,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Module wiring (called from main.py at startup)
# ---------------------------------------------------------------------------

def wire_klieglight_autofire() -> None:
    """Bridge: install emit_notification() as the post-fire emitter on
    handlers.klieglight_autofire. Idempotent.
    """
    from handlers import klieglight_autofire
    klieglight_autofire.register_notification_emitter(emit_notification)


def wire_osir_autofire() -> None:
    """Bridge: install emit_notification() as the post-fire emitter on
    handlers.osir_autofire. Identical contract to wire_klieglight_autofire;
    the OSIR autofire is the OSINT-side analog. Idempotent.
    """
    from handlers import osir_autofire
    osir_autofire.register_notification_emitter(emit_notification)
