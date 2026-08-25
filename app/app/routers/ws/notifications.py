"""Notification WebSocket endpoint."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from core.connection import notification_connection_manager
import logging
import asyncio

router = APIRouter()

logger = logging.getLogger(__name__)


@router.websocket("/notifications")
async def notification_websocket_endpoint(websocket: WebSocket, user_id: str = Query(...)):
    """WebSocket endpoint for real-time notifications for a user."""
    
    try:
        # Connect user to notifications
        await notification_connection_manager.connect(websocket, user_id)
        
        logger.info(f"Notification WebSocket connected: user {user_id}")
        
        # Keep connection alive
        try:
            while True:
                try:
                    # Wait for any message with a timeout to detect disconnections
                    message = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                    
                    # Check for disconnect message type
                    msg_type = message.get("type", "")
                    if msg_type == "websocket.disconnect":
                        logger.info(f"Client disconnected notification connection: user {user_id}")
                        break
                except asyncio.TimeoutError:
                    # Timeout is normal - connection is still alive
                    continue
                except WebSocketDisconnect:
                    raise
        except WebSocketDisconnect:
            logger.info(f"Notification WebSocket disconnected: user {user_id}")
        except Exception as e:
            logger.error(f"Error in notification WebSocket for user {user_id}: {e}", exc_info=True)
        finally:
            await notification_connection_manager.disconnect(user_id)
    except Exception as e:
        logger.error(f"Error in notification WebSocket connection for user {user_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass

