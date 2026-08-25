"""Notification API endpoints."""
from fastapi import APIRouter, Query
from typing import Optional

from utils import notifications

router = APIRouter()


@router.get("/notifications")
def get_notifications(
    user_id: str,
    limit: int = Query(default=50, le=100),
    unread_only: bool = Query(default=False)
):
    """Get latest notifications for a user."""
    return notifications.get_notifications(
        user_id=user_id,
        limit=limit,
        unread_only=unread_only
    )


@router.put("/notifications/{notification_id}/read")
def mark_notification_read(notification_id: str):
    """Mark a notification as read."""
    result = notifications.mark_notification_read(notification_id)
    if "error" in result:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.put("/notifications/read-all")
def mark_all_read(user_id: str):
    """Mark all notifications as read for a user."""
    return notifications.mark_all_notifications_read(user_id)

