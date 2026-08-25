"""WebSocket routers module."""
from fastapi import APIRouter
from . import chat, agents, aar, transcribe, rfi, notifications

ws_router = APIRouter()

# Include all WebSocket routers
ws_router.include_router(chat.router)
ws_router.include_router(agents.router)
ws_router.include_router(aar.router)
ws_router.include_router(transcribe.router)
ws_router.include_router(rfi.router)
ws_router.include_router(notifications.router)

