import json
import uuid
import requests
from utils import database
from sqlalchemy.orm import Session
from fastapi import Depends


def log_analyst_event(
    user_id: str,
    event_type: str,
    *,
    surface: str = None,
    product_type: str = None,
    session_id: str = None,
    artifact_id: str = None,
    value_num: int = None,
    detail=None,
):
    """Persist one analyst-action telemetry row (database.AnalystEvent).

    Non-fatal by design: a telemetry write must never affect the caller's
    flow, so all exceptions are swallowed + logged. `detail` may be a dict
    (json-encoded here) or a string.
    """
    db = None
    try:
        db = next(database.get_db())
        if detail is not None and not isinstance(detail, str):
            detail = json.dumps(detail, default=str)
        evt = database.AnalystEvent(
            event_id=str(uuid.uuid4()),
            user_id=user_id,
            event_type=event_type,
            surface=surface,
            product_type=product_type,
            session_id=session_id,
            artifact_id=artifact_id,
            value_num=value_num,
            detail=detail,
        )
        db.add(evt)
        db.commit()
        return {"ok": True, "event_id": evt.event_id}
    except Exception as e:
        print(f"Failed to log analyst_event: {e}")
        return {"error": str(e)}
    finally:
        if db is not None:
            db.close()


def log_event(user_id: str, tag: str, details: str = ""):
    """
    Sends an event log entry to the FastAPI event_log endpoint.

    Args:
        user_id (str): ID of the user performing the action.
        tag (str): Short event type label (e.g., 'login', 'upload', 'error').
        details (str, optional): More descriptive info about the event.
        
    Returns:
        dict: The API response JSON or an error message.
    """
    try:
        db = next(database.get_db())
        payload = {
            "user_id": user_id,
            "tag": tag,
            "details": details
        }

        new_event = database.EventLog(**payload)
        db.add(new_event)
        db.commit()
        db.refresh(new_event)
        return payload
    except Exception as e:
        print(f"Failed to log event: {e}")
        return {"error": str(e)}
    finally:
        db.close()