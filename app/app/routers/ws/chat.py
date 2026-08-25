"""Chat WebSocket endpoint."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from core.connection import ConnectionManager
from handlers import chat
import json

router = APIRouter()
manager = ConnectionManager()


@router.websocket("/chat/{client_id}")
async def websocket_endpoint(websocket: WebSocket, client_id: str):
    await manager.connect(websocket)
    try:
        while True:
            data = json.loads(await websocket.receive_text())
            await manager.send_personal_message(chat.main(data), websocket)
    except WebSocketDisconnect:
        pass
