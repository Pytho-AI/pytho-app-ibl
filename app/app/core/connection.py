from fastapi import WebSocket
from typing import Dict, Set
import json
import logging

logger = logging.getLogger(__name__)

class ConnectionManager:
    """Manages WebSocket connections with room-based broadcasting."""
    
    def __init__(self):
        # Structure: {project_id: {user_id: websocket}}
        self.active_connections: Dict[str, Dict[str, WebSocket]] = {}
    
    async def connect(self, websocket: WebSocket):
        """Accept a WebSocket connection."""
        await websocket.accept()
    
    async def connect_to_room(self, websocket: WebSocket, project_id: str, user_id: str):
        """Add a connection to a project room."""
        await self.connect(websocket)
        
        if project_id not in self.active_connections:
            self.active_connections[project_id] = {}
        
        self.active_connections[project_id][user_id] = websocket
        logger.info(f"User {user_id} connected to project {project_id}. Total connections: {len(self.active_connections[project_id])}")
    
    async def disconnect_from_room(self, websocket: WebSocket, project_id: str, user_id: str):
        """Remove a connection from a project room."""
        if project_id in self.active_connections:
            if user_id in self.active_connections[project_id]:
                del self.active_connections[project_id][user_id]
                logger.info(f"User {user_id} disconnected from project {project_id}")
            
            # Clean up empty project rooms
            if not self.active_connections[project_id]:
                del self.active_connections[project_id]
    
    async def send_personal_message(self, message: str, websocket: WebSocket):
        """Send a message to a specific WebSocket connection."""
        try:
            await websocket.send_text(message)
        except Exception as e:
            logger.error(f"Error sending personal message: {e}")
    
    async def broadcast_to_project(self, project_id: str, message: dict):
        """Broadcast a message to all connections in a project."""
        if project_id not in self.active_connections:
            logger.debug(f"No active connections for project {project_id}")
            return
        
        message_str = json.dumps(message)
        disconnected_users = []
        
        for user_id, websocket in self.active_connections[project_id].items():
            try:
                await websocket.send_text(message_str)
            except Exception as e:
                logger.error(f"Error broadcasting to user {user_id} in project {project_id}: {e}")
                disconnected_users.append(user_id)
        
        # Clean up disconnected users
        for user_id in disconnected_users:
            if project_id in self.active_connections and user_id in self.active_connections[project_id]:
                del self.active_connections[project_id][user_id]
        
        logger.info(f"Broadcasted message to {len(self.active_connections[project_id])} users in project {project_id}")
    
    async def broadcast_to_users(self, project_id: str, message: dict, target_user_ids: Set[str]):
        """Broadcast a message only to specific users in a project (e.g. seminar + white cell)."""
        if project_id not in self.active_connections:
            logger.debug(f"No active connections for project {project_id}")
            return

        message_str = json.dumps(message)
        disconnected_users = []
        sent_count = 0

        for user_id, websocket in self.active_connections[project_id].items():
            if user_id in target_user_ids:
                try:
                    await websocket.send_text(message_str)
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Error broadcasting to user {user_id} in project {project_id}: {e}")
                    disconnected_users.append(user_id)

        # Clean up disconnected users
        for user_id in disconnected_users:
            if project_id in self.active_connections and user_id in self.active_connections[project_id]:
                del self.active_connections[project_id][user_id]

        logger.info(f"Broadcasted message to {sent_count} targeted users in project {project_id}")

    async def broadcast_to_white_cell(self, project_id: str, message: dict, white_cell_user_ids: Set[str]):
        """Broadcast a message only to white cell members in a project."""
        if project_id not in self.active_connections:
            logger.debug(f"No active connections for project {project_id}")
            return
        
        message_str = json.dumps(message)
        disconnected_users = []
        
        for user_id, websocket in self.active_connections[project_id].items():
            if user_id in white_cell_user_ids:
                try:
                    await websocket.send_text(message_str)
                except Exception as e:
                    logger.error(f"Error broadcasting to white cell user {user_id} in project {project_id}: {e}")
                    disconnected_users.append(user_id)
        
        # Clean up disconnected users
        for user_id in disconnected_users:
            if project_id in self.active_connections and user_id in self.active_connections[project_id]:
                del self.active_connections[project_id][user_id]
        
        broadcast_count = len([uid for uid in self.active_connections[project_id].keys() if uid in white_cell_user_ids])
        logger.info(f"Broadcasted message to {broadcast_count} white cell members in project {project_id}")


# Global singleton instance for RFI WebSocket connections
rfi_connection_manager = ConnectionManager()


class NotificationConnectionManager:
    """Manages WebSocket connections for user notifications."""
    
    def __init__(self):
        # Structure: {user_id: websocket}
        self.active_connections: Dict[str, WebSocket] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        """Accept a WebSocket connection for a user."""
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected to notifications. Total connections: {len(self.active_connections)}")
    
    async def disconnect(self, user_id: str):
        """Remove a user's connection."""
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected from notifications")
    
    async def send_notification(self, user_id: str, notification: dict):
        """Send a notification to a specific user."""
        if user_id not in self.active_connections:
            logger.debug(f"No active notification connection for user {user_id}")
            return False
        
        try:
            message_str = json.dumps(notification)
            await self.active_connections[user_id].send_text(message_str)
            logger.info(f"Sent notification to user {user_id}")
            return True
        except Exception as e:
            logger.error(f"Error sending notification to user {user_id}: {e}")
            # Clean up broken connection
            if user_id in self.active_connections:
                del self.active_connections[user_id]
            return False
    
    async def broadcast_to_users(self, user_ids: list, notification: dict):
        """Send a notification to multiple users."""
        message_str = json.dumps(notification)
        disconnected_users = []
        sent_count = 0
        
        for user_id in user_ids:
            if user_id in self.active_connections:
                try:
                    await self.active_connections[user_id].send_text(message_str)
                    sent_count += 1
                except Exception as e:
                    logger.error(f"Error sending notification to user {user_id}: {e}")
                    disconnected_users.append(user_id)
        
        # Clean up disconnected users
        for user_id in disconnected_users:
            if user_id in self.active_connections:
                del self.active_connections[user_id]
        
        logger.info(f"Broadcasted notification to {sent_count} users")
        return sent_count


# Global singleton instance for notification WebSocket connections
notification_connection_manager = NotificationConnectionManager()

