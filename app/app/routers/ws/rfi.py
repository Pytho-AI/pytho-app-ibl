"""RFI WebSocket endpoint."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from core.connection import rfi_connection_manager
from utils import database
from sqlalchemy.orm import Session
from typing import List, Dict, Any
import json
import logging
import asyncio

router = APIRouter()

logger = logging.getLogger(__name__)


def is_project_member(user_id: str, project_id: str, db: Session) -> bool:
    """Check if user is a member of the project (either white cell or participant)."""
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id
    ).first()
    return member is not None


@router.websocket("/rfi/{project_id}")
async def rfi_websocket_endpoint(websocket: WebSocket, project_id: str, user_id: str = Query(...)):
    """WebSocket endpoint for real-time RFI updates in a project."""
    
    # Get database session for validation only
    db = next(database.get_db())
    
    try:
        # Verify project exists
        project = db.query(database.Project).filter(
            database.Project.project_id == project_id
        ).first()
        
        if not project:
            logger.warning(f"WebSocket connection rejected: Project {project_id} not found for user {user_id}")
            await websocket.close(code=1008, reason="Project not found")
            return
        
        # Verify user is a member of the project
        if not is_project_member(user_id, project_id, db):
            logger.warning(f"WebSocket connection rejected: User {user_id} is not a member of project {project_id}")
            await websocket.close(code=1008, reason="User is not a member of this project")
            return
    except Exception as e:
        logger.error(f"Error validating WebSocket connection for user {user_id} in project {project_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011, reason="Internal server error during validation")
        except:
            pass
        return
    finally:
        # Close database session immediately after validation
        db.close()
    
    # Now handle the WebSocket connection (database session is closed)
    try:
        # Connect to the project room
        await rfi_connection_manager.connect_to_room(websocket, project_id, user_id)
        
        logger.info(f"RFI WebSocket connected: user {user_id} to project {project_id}")
        
        # Keep connection alive - this is a server-to-client only WebSocket
        # FastAPI handles ping/pong frames automatically
        try:
            while True:
                try:
                    # Wait for any message with a timeout to detect disconnections
                    # Timeout of 30 seconds - if no message, continue loop (connection still alive)
                    message = await asyncio.wait_for(websocket.receive(), timeout=30.0)
                    
                    # Check for disconnect message type
                    msg_type = message.get("type", "")
                    if msg_type == "websocket.disconnect":
                        logger.info(f"Client disconnected: user {user_id} in project {project_id}")
                        break
                    
                    # Handle different message types
                    if "text" in message:
                        # Client sent text (unexpected but handle gracefully)
                        logger.debug(f"Received text from user {user_id} in project {project_id}: {message['text']}")
                    elif "bytes" in message:
                        # Client sent bytes (unexpected but handle gracefully)
                        logger.debug(f"Received bytes from user {user_id} in project {project_id}")
                except asyncio.TimeoutError:
                    # Timeout is normal - connection is still alive, just no message received
                    # Continue the loop to keep connection alive
                    continue
                except WebSocketDisconnect:
                    # Client disconnected
                    raise
        except WebSocketDisconnect:
            logger.info(f"RFI WebSocket disconnected: user {user_id} from project {project_id}")
        except Exception as e:
            logger.error(f"Error in RFI WebSocket for user {user_id} in project {project_id}: {e}", exc_info=True)
        finally:
            # Clean up connection
            await rfi_connection_manager.disconnect_from_room(websocket, project_id, user_id)
    except Exception as e:
        logger.error(f"Error in RFI WebSocket connection for user {user_id} in project {project_id}: {e}", exc_info=True)
        try:
            await websocket.close(code=1011, reason="Internal server error")
        except:
            pass


