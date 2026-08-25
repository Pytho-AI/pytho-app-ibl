"""Event logging endpoints."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session
from utils import database
from datetime import datetime
from typing import Optional

router = APIRouter()


@router.get("/event_log/{user_id}")
def read_event_logs(
    user_id: str,
    tag: Optional[str] = None,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(database.get_db)
):
    query = db.query(database.EventLog).filter(database.EventLog.user_id == user_id)
    if tag:
        query = query.filter(database.EventLog.tag == tag)
    if since:
        query = query.filter(database.EventLog.timestamp >= since)
    if until:
        query = query.filter(database.EventLog.timestamp <= until)

    results = (
        query.order_by(database.EventLog.timestamp.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        {
            "user_id": r.user_id,
            "tag": r.tag,
            "timestamp": r.timestamp,
            "details": r.details,
        }
        for r in results
    ]
