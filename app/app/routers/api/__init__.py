"""API routers module — the cloud SPA surface, unified into ibl.

Ported from main during the edge+cloud unification (2026-07-07). Two
deliberate exclusions from main's list:
  - tiles: the consumer tile proxy is RETIRED — the map unification
    moved all tile serving inside geo-agent; consumers never fetch
    tiles (geo-agent CLAUDE.md consumer contract).
  - product_types: ibl's own evolved routers/api/product_types.py is
    already wired in main.py with prefix /api — including it here too
    would register duplicate routes.
"""
from fastapi import APIRouter
from . import auth, projects, documents, events, aar, transcripts, chat, files, rfis, seminars, injects, coa_submissions, notifications, agent_proxy

api_router = APIRouter(prefix="/api")

api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(documents.router, tags=["documents"])
api_router.include_router(events.router, tags=["events"])
api_router.include_router(aar.router, tags=["aar"])
api_router.include_router(transcripts.router, tags=["transcripts"])
api_router.include_router(chat.router, tags=["chat"])
api_router.include_router(files.router, tags=["files"])
api_router.include_router(rfis.router, tags=["rfis"])
api_router.include_router(seminars.router, tags=["seminars"])
api_router.include_router(injects.router, tags=["injects"])
api_router.include_router(coa_submissions.router, tags=["coa_submissions"])
api_router.include_router(notifications.router, tags=["notifications"])
api_router.include_router(agent_proxy.router, tags=["agent_proxy"])
