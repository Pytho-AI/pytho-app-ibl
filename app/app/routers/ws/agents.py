"""Agents WebSocket endpoint."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.connection import ConnectionManager
from handlers import agents
import json

router = APIRouter()
manager = ConnectionManager()


@router.websocket("/agents/{client_id}")
async def websocket_agents(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            await agents.main(data, manager, websocket)
    except WebSocketDisconnect:
        pass
