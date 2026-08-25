"""Notification utility for creating user notifications from backend APIs."""
from typing import Optional, Dict, Any, List
from utils import database
from core.connection import notification_connection_manager
import asyncio


def get_notifications(
    user_id: str,
    limit: int = 50,
    unread_only: bool = False
) -> List[Dict[str, Any]]:
    """
    Gets the latest notifications for a user.

    Args:
        user_id (str): ID of the user to get notifications for.
        limit (int): Maximum number of notifications to return (default 50).
        unread_only (bool): If True, only return unread notifications.

    Returns:
        list: List of notification dictionaries.
    """
    db = None
    try:
        db = next(database.get_db())
        
        query = db.query(database.Notification).filter(
            database.Notification.user_id == user_id
        )
        
        if unread_only:
            query = query.filter(database.Notification.status == "unread")
        
        notifications = query.order_by(
            database.Notification.created_at.desc()
        ).limit(limit).all()
        
        return [
            {
                "id": n.id,
                "user_id": n.user_id,
                "display_text": n.display_text,
                "status": n.status,
                "action": n.action,
                "created_at": n.created_at.isoformat()
            }
            for n in notifications
        ]
    except Exception as e:
        print(f"Failed to get notifications: {e}")
        return []
    finally:
        if db:
            db.close()


def mark_notification_read(notification_id: str) -> Dict[str, Any]:
    """
    Marks a notification as read.

    Args:
        notification_id (str): ID of the notification to mark as read.

    Returns:
        dict: The updated notification data or an error message.
    """
    db = None
    try:
        db = next(database.get_db())
        
        notification = db.query(database.Notification).filter(
            database.Notification.id == notification_id
        ).first()
        
        if not notification:
            return {"error": "Notification not found"}
        
        notification.status = "read"
        db.commit()
        db.refresh(notification)
        
        return {
            "id": notification.id,
            "user_id": notification.user_id,
            "display_text": notification.display_text,
            "status": notification.status,
            "action": notification.action,
            "created_at": notification.created_at.isoformat()
        }
    except Exception as e:
        print(f"Failed to mark notification as read: {e}")
        return {"error": str(e)}
    finally:
        if db:
            db.close()


def mark_all_notifications_read(user_id: str) -> Dict[str, Any]:
    """
    Marks all notifications as read for a user.

    Args:
        user_id (str): ID of the user.

    Returns:
        dict: Count of updated notifications or an error message.
    """
    db = None
    try:
        db = next(database.get_db())
        
        updated_count = db.query(database.Notification).filter(
            database.Notification.user_id == user_id,
            database.Notification.status == "unread"
        ).update({"status": "read"})
        
        db.commit()
        
        return {"updated_count": updated_count}
    except Exception as e:
        print(f"Failed to mark all notifications as read: {e}")
        return {"error": str(e)}
    finally:
        if db:
            db.close()


def create_notification(
    user_id: str,
    display_text: str,
    action: Optional[Dict[str, Any]] = None,
    db=None
) -> Dict[str, Any]:
    """
    Creates a notification for a user.

    Args:
        user_id (str): ID of the user to notify.
        display_text (str): The notification message to display.
        action (dict, optional): Action for the frontend to execute on click.
            Examples:
                {"type": "navigate", "route": "/event/123"}
                {"type": "open_modal", "modal": "rfi", "id": "abc"}
        db: Optional existing DB session to reuse. Pass the caller's session
            to avoid opening a second SQLite connection, which causes
            "database is locked" errors when the caller's implicit
            transaction holds a SHARED lock.

    Returns:
        dict: The created notification data or an error message.
    """
    owns_db = db is None
    try:
        if owns_db:
            db = next(database.get_db())
        
        notification = database.Notification(
            user_id=user_id,
            display_text=display_text,
            action=action
        )
        
        db.add(notification)
        db.commit()
        db.refresh(notification)
        
        notification_data = {
            "id": notification.id,
            "user_id": notification.user_id,
            "display_text": notification.display_text,
            "status": notification.status,
            "action": notification.action,
            "created_at": notification.created_at.isoformat()
        }
        
        # Send via WebSocket
        try:
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(
                notification_connection_manager.send_notification(user_id, notification_data),
                loop
            )
        except RuntimeError:
            # No running event loop, try to run directly
            try:
                asyncio.run(
                    notification_connection_manager.send_notification(user_id, notification_data)
                )
            except Exception as ws_error:
                print(f"Failed to send WebSocket notification: {ws_error}")
        
        return notification_data
    except Exception as e:
        print(f"Failed to create notification: {e}")
        return {"error": str(e)}
    finally:
        if owns_db and db:
            db.close()


async def create_notification_async(
    user_id: str,
    display_text: str,
    action: Optional[Dict[str, Any]] = None,
    db=None
) -> Dict[str, Any]:
    """
    Creates a notification for a user and broadcasts via WebSocket.
    Use this async version when called from async context.

    Args:
        user_id (str): ID of the user to notify.
        display_text (str): The notification message to display.
        action (dict, optional): Action for the frontend to execute on click.
        db: Optional existing DB session to reuse. Pass the caller's session
            to avoid opening a second SQLite connection, which causes
            "database is locked" errors when the caller's implicit
            transaction holds a SHARED lock.

    Returns:
        dict: The created notification data or an error message.
    """
    owns_db = db is None
    try:
        if owns_db:
            db = next(database.get_db())
        
        notification = database.Notification(
            user_id=user_id,
            display_text=display_text,
            action=action
        )
        
        db.add(notification)
        db.commit()
        db.refresh(notification)
        
        notification_data = {
            "id": notification.id,
            "user_id": notification.user_id,
            "display_text": notification.display_text,
            "status": notification.status,
            "action": notification.action,
            "created_at": notification.created_at.isoformat()
        }
        
        # Broadcast via WebSocket
        await notification_connection_manager.send_notification(user_id, notification_data)
        
        return notification_data
    except Exception as e:
        print(f"Failed to create notification: {e}")
        return {"error": str(e)}
    finally:
        if owns_db and db:
            db.close()


async def broadcast_notification_to_users(
    user_ids: List[str],
    display_text: str,
    action: Optional[Dict[str, Any]] = None,
    db=None
) -> Dict[str, Any]:
    """
    Creates notifications for multiple users and broadcasts via WebSocket.

    Args:
        user_ids (list[str]): List of user IDs to notify.
        display_text (str): The notification message to display.
        action (dict, optional): Action for the frontend to execute on click.
        db: Optional existing DB session to reuse. Pass the caller's session
            to avoid opening a second SQLite connection, which causes
            "database is locked" errors when the caller's implicit
            transaction holds a SHARED lock.

    Returns:
        dict: Summary of created notifications.
    """
    owns_db = db is None
    try:
        if owns_db:
            db = next(database.get_db())
        
        notifications_data = []
        for user_id in user_ids:
            notification = database.Notification(
                user_id=user_id,
                display_text=display_text,
                action=action
            )
            db.add(notification)
            db.commit()
            db.refresh(notification)
            
            notification_data = {
                "id": notification.id,
                "user_id": notification.user_id,
                "display_text": notification.display_text,
                "status": notification.status,
                "action": notification.action,
                "created_at": notification.created_at.isoformat()
            }
            notifications_data.append(notification_data)
            
            # Broadcast via WebSocket
            await notification_connection_manager.send_notification(user_id, notification_data)
        
        return {
            "created_count": len(notifications_data),
            "user_ids": user_ids
        }
    except Exception as e:
        print(f"Failed to broadcast notifications: {e}")
        return {"error": str(e)}
    finally:
        if owns_db and db:
            db.close()
