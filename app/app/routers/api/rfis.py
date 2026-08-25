"""RFI API endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional, Set
from datetime import datetime
import json
import asyncio
import logging

from utils import database
from utils import events
from utils import notifications
from utils.file_handler import (
    find_bucket_for_file,
    get_context_from_attachments,
)
from utils.world_state import get_current_world_state, get_project_data, load_docs_context, get_sent_injects, load_inject_files_context
from core.connection import rfi_connection_manager
from utils.llm import fetch_llm_response
import tiktoken

# Tracks RFI IDs that currently have a background generation in flight.
# Prevents thundering-herd: multiple concurrent GETs for the same unanswered RFI
# each spawning their own LLM call, causing Bedrock throttling and a spin loop.
_rfi_generation_in_progress: Set[str] = set()

def get_bucket_from_response_attachments(db: Session, rfi_id: str) -> Optional[str]:
    """Try to find S3 bucket by attempting to access files from response attachments."""
    # Get all response attachments for this RFI
    responses = db.query(database.RFIResponse).filter(
        database.RFIResponse.rfi_id == rfi_id
    ).all()
    
    # Get first attachment with a file_key to try finding the bucket
    for response in responses:
        attachments = db.query(database.RFIResponseAttachment).filter(
            database.RFIResponseAttachment.response_id == response.id
        ).all()
        
        for attachment in attachments:
            if attachment.file_key:
                bucket = find_bucket_for_file(attachment.file_key)
                if bucket:
                    return bucket
    
    return None


def get_previous_responses_context(responses, bucket: str) -> str:
    """Build context from previous RFI responses including their text and attachments."""
    if not responses:
        return ""
    
    context = "\n\n--- Previous RFI Responses ---\n"
    
    for i, response in enumerate(responses, 1):
        # Get user info for display
        response_type = "Internal Note" if response.is_internal else "Response"
        if response.is_official_response:
            response_type = "Official Response"
        
        context += f"\n[{response_type} #{i}] ({response.created_at.strftime('%Y-%m-%d %H:%M')}):\n"
        context += f"{response.content}\n"
        
        # Load response attachments if any
        if response.attachments and bucket:
            att_result = get_context_from_attachments(response.attachments, bucket, f"Response#{i}")
            if att_result.get("context"):
                context += att_result["context"]
    
    context += "\n--- End of Previous Responses ---\n"
    return context


router = APIRouter()


# Pydantic models for request/response
class RFICreate(BaseModel):
    project_id: str
    title: str
    description: Optional[str] = None
    destination: Optional[str] = None
    attachments: Optional[List[dict]] = []


class RFIStatusUpdate(BaseModel):
    status: str


class RFIResponseCreate(BaseModel):
    content: str
    is_internal: bool = False
    is_official_response: bool = False
    attachments: Optional[List[dict]] = []


class RFIResponseUpdate(BaseModel):
    content: str
    is_internal: Optional[bool] = None
    is_official_response: Optional[bool] = None


def generate_rfi_number(project_id: str, user_id: str, db: Session) -> str:
    """Generate an RFI number with format {SeminarName}-{MMDDYYYY}-{HHMM}[-seq].

    Falls back to RFI-{MMDDYYYY}-{HHMM}[-seq] if the user has no seminar.
    A sequence suffix (-2, -3, …) is appended when multiple RFIs share
    the same seminar+minute combination.
    """
    # Look up the creator's seminar
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "participant"
    ).first()

    seminar_prefix = "RFI"
    if member and member.seminar_id:
        seminar = db.query(database.Seminar).filter(
            database.Seminar.id == member.seminar_id
        ).first()
        if seminar:
            # Replace spaces/special chars with hyphens for a clean ID
            seminar_prefix = seminar.name.strip().replace(" ", "-")

    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    date_part = now.strftime("%m%d%Y")
    time_part = now.strftime("%H%M")
    base_number = f"{seminar_prefix}-{date_part}-{time_part}"

    # Check for collisions and append sequence suffix if needed
    existing_count = db.query(database.RFI).filter(
        database.RFI.project_id == project_id,
        database.RFI.rfi_number.like(f"{base_number}%")
    ).count()

    if existing_count == 0:
        return base_number

    return f"{base_number}-{existing_count + 1}"


def get_user_role(user_id: str, db: Session) -> str:
    """Get user's role from database"""
    user = db.query(database.User).filter(database.User.id == user_id).first()
    if not user:
        return "participant"
    
    try:
        role_data = json.loads(user.role) if isinstance(user.role, str) else user.role
        return role_data.get("role", "participant")
    except:
        return "participant"


def is_white_cell_member(user_id: str, project_id: str, db: Session) -> bool:
    """Check if user is a white cell member for the project"""
    user_role = get_user_role(user_id, db)
    
    if user_role != "white":
        return False
    
    # Check if user is a project member with white cell role
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "white"
    ).first()
    
    return member is not None


def get_white_cell_members(project_id: str, db: Session) -> List[str]:
    """Get all white cell member user IDs for a project"""
    members = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.role == "white"
    ).all()
    
    return [member.user_id for member in members]


def get_participants(project_id: str, db: Session) -> List[str]:
    """Get all participant user IDs for a project"""
    members = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.role == "participant"
    ).all()
    
    return [member.user_id for member in members]


def get_all_project_members(project_id: str, db: Session) -> List[str]:
    """Get all user IDs for a project (both white cell and participants)"""
    members = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id
    ).all()
    
    return [member.user_id for member in members]


def get_white_cell_user_ids(project_id: str, db: Session) -> Set[str]:
    """Get all white cell user IDs for a project as a set"""
    members = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.role == "white"
    ).all()

    return {member.user_id for member in members}


def get_seminar_participant_ids(project_id: str, seminar_id: str, db: Session) -> Set[str]:
    """Get all participant user IDs for a specific seminar in a project."""
    members = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.role == "participant",
        database.ProjectMember.seminar_id == seminar_id
    ).all()
    return {member.user_id for member in members}


def get_seminar_and_whitecell_ids(project_id: str, seminar_id: Optional[str], db: Session) -> Set[str]:
    """Get user IDs for white cell + participants in a specific seminar.

    Used for scoped WebSocket broadcasts so only relevant users receive updates.
    If seminar_id is None, returns all project members (fallback).
    """
    white_cell_ids = get_white_cell_user_ids(project_id, db)
    if not seminar_id:
        # No seminar — fall back to all members
        all_ids = {m.user_id for m in db.query(database.ProjectMember).filter(
            database.ProjectMember.project_id == project_id
        ).all()}
        return all_ids
    seminar_ids = get_seminar_participant_ids(project_id, seminar_id, db)
    return white_cell_ids | seminar_ids


def get_user_seminar_id(user_id: str, project_id: str, db: Session) -> Optional[str]:
    """Get the seminar_id for a participant in a project. Returns None if not found."""
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "participant"
    ).first()
    return member.seminar_id if member else None


def _safe_broadcast(coro):
    """Safely create an async task for broadcasting with error handling."""
    try:
        task = asyncio.create_task(coro)
        # Add error handling callback
        def handle_task_error(task):
            try:
                task.result()  # This will raise if task failed
            except Exception as e:
                logging.error(f"Error in broadcast task: {e}", exc_info=True)
        task.add_done_callback(handle_task_error)
    except Exception as e:
        logging.error(f"Error creating broadcast task: {e}", exc_info=True)


@router.post("/rfis", status_code=status.HTTP_201_CREATED)
async def create_rfi(
    rfi_data: RFICreate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Create a new RFI. Only participants can create RFIs."""
    
    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == rfi_data.project_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Generate unique RFI number
    rfi_number = generate_rfi_number(rfi_data.project_id, user_id, db)

    # Look up the creator's seminar
    creator_seminar_id = get_user_seminar_id(user_id, rfi_data.project_id, db)

    # Create RFI
    new_rfi = database.RFI(
        project_id=rfi_data.project_id,
        rfi_number=rfi_number,
        created_by_user_id=user_id,
        seminar_id=creator_seminar_id,
        title=rfi_data.title,
        description=rfi_data.description,
        destination=rfi_data.destination,
        status="Not Started"
    )
    
    db.add(new_rfi)
    db.commit()
    db.refresh(new_rfi)
    
    # Create attachments if provided
    if rfi_data.attachments:
        for attachment in rfi_data.attachments:
            new_attachment = database.RFIAttachment(
                rfi_id=new_rfi.id,
                file_key=attachment.get("file_key"),
                file_name=attachment.get("file_name"),
                file_size=attachment.get("file_size")
            )
            db.add(new_attachment)
        db.commit()
    
    # Log event
    events.log_event(user_id, f"event:{rfi_data.project_id}", f"Created RFI {rfi_number}: {rfi_data.title}")
    
    # Get creator info for response
    creator = db.query(database.User).filter(database.User.id == user_id).first()
    creator_name = creator.name if creator else "Unknown"
    
    # Look up seminar name for response
    seminar_name = None
    if creator_seminar_id:
        seminar = db.query(database.Seminar).filter(database.Seminar.id == creator_seminar_id).first()
        if seminar:
            seminar_name = seminar.name

    # Prepare response data (include all fields expected by frontend)
    response_data = {
        "id": new_rfi.id,
        "project_id": new_rfi.project_id,
        "rfi_number": new_rfi.rfi_number,
        "created_by_user_id": new_rfi.created_by_user_id,
        "created_by_name": creator_name,
        "seminar_id": creator_seminar_id,
        "seminar_name": seminar_name,
        "title": new_rfi.title,
        "description": new_rfi.description,
        "destination": new_rfi.destination,
        "status": new_rfi.status,
        "response_count": 0,  # New RFI has no responses yet
        "created_at": new_rfi.created_at.isoformat(),
        "updated_at": new_rfi.updated_at.isoformat()
    }

    # Broadcast RFI creation to seminar members + white cell only
    scoped_user_ids = get_seminar_and_whitecell_ids(rfi_data.project_id, creator_seminar_id, db)
    _safe_broadcast(
        rfi_connection_manager.broadcast_to_users(
            rfi_data.project_id,
            {"type": "rfi_created", "data": response_data},
            scoped_user_ids
        )
    )
    
    # Notify white cell members about new RFI
    white_cell_user_ids = get_white_cell_members(rfi_data.project_id, db)
    if white_cell_user_ids:
        await notifications.broadcast_notification_to_users(
            user_ids=white_cell_user_ids,
            display_text=f"New RFI {rfi_number}: {rfi_data.title}",
            action={
                "type": "navigate",
                "route": f"/thewarroom/projects/whitecell-rfi/{rfi_data.project_id}/{new_rfi.id}"
            },
            db=db
        )

    # Fire-and-forget AI answer generation
    rfi_title = rfi_data.title
    rfi_project_id = rfi_data.project_id
    rfi_id_for_bg = new_rfi.id

    def _thread_generate_ai_answer(rfi_id: str):
        """Runs entirely in thread pool — creates its own DB session."""
        logging.info(f"[BG-CREATE] Starting background AI generation for RFI {rfi_id}")
        bg_db = database.SessionLocal()
        try:
            bg_rfi = bg_db.query(database.RFI).filter(database.RFI.id == rfi_id).first()
            if bg_rfi:
                result = _do_generate_ai_answer(bg_rfi, bg_db)
                bg_db.commit()
                logging.info(f"Background AI answer generated for RFI {rfi_id}")
                return result
        except Exception as e:
            logging.error(f"Background AI answer generation failed for RFI {rfi_id}: {e}", exc_info=True)
        finally:
            bg_db.close()
        return None

    async def _background_generate_ai_answer(rfi_id: str):
        loop = asyncio.get_event_loop()
        generated = await loop.run_in_executor(None, _thread_generate_ai_answer, rfi_id)

        # Broadcast AI answer to white cell only — participants should not see AI drafts
        if generated:
            try:
                bg_db2 = database.SessionLocal()
                bg_project_data = get_project_data(bg_db2, rfi_project_id)
                bg_docs = bg_project_data.get('docs', []) if bg_project_data else []
                bg_ai_answer_docs = [{"name": d.get("name", ""), "key": d.get("key", "")} for d in bg_docs]
                bg_wc_ids = get_white_cell_user_ids(rfi_project_id, bg_db2)
                bg_db2.close()
                await rfi_connection_manager.broadcast_to_white_cell(
                    rfi_project_id,
                    {
                        "type": "rfi_updated",
                        "data": {
                            "id": rfi_id,
                            "ai_answer": generated,
                            "ai_answer_docs": bg_ai_answer_docs,
                        }
                    },
                    bg_wc_ids
                )
            except Exception as e:
                logging.error(f"Failed to broadcast AI answer for RFI {rfi_id}: {e}", exc_info=True)

    _safe_broadcast(_background_generate_ai_answer(rfi_id_for_bg))

    return response_data


@router.get("/rfis/{project_id}")
async def get_rfis_for_project(
    project_id: str,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Get RFIs for a project. Participants see only their seminar's RFIs; white cell sees all."""

    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )

    is_white_cell = is_white_cell_member(user_id, project_id, db)

    # Build query — filter by seminar for participants
    rfi_query = db.query(database.RFI).filter(
        database.RFI.project_id == project_id
    )
    if not is_white_cell:
        user_seminar = get_user_seminar_id(user_id, project_id, db)
        if user_seminar:
            rfi_query = rfi_query.filter(database.RFI.seminar_id == user_seminar)
        # If participant has no seminar, they see only RFIs with no seminar (legacy/unassigned)
        else:
            rfi_query = rfi_query.filter(database.RFI.seminar_id == None)

    rfis = rfi_query.order_by(database.RFI.created_at.desc()).all()

    # Pre-load seminar names for white cell view
    seminar_name_cache = {}
    if is_white_cell:
        all_seminars = db.query(database.Seminar).filter(
            database.Seminar.project_id == project_id
        ).all()
        seminar_name_cache = {s.id: s.name for s in all_seminars}

    # Format response
    result = []
    for rfi in rfis:
        # Get creator info
        creator = db.query(database.User).filter(database.User.id == rfi.created_by_user_id).first()
        creator_name = creator.name if creator else "Unknown"

        # Count responses
        response_count = db.query(database.RFIResponse).filter(
            database.RFIResponse.rfi_id == rfi.id
        ).count()

        # Map status for participants — hide AI-internal statuses
        visible_status = rfi.status
        if not is_white_cell and rfi.status in ("AI Answered", "AI Resolved", "Escalated"):
            visible_status = "Not Started"

        result.append({
            "id": rfi.id,
            "project_id": rfi.project_id,
            "rfi_number": rfi.rfi_number,
            "created_by_user_id": rfi.created_by_user_id,
            "created_by_name": creator_name,
            "seminar_id": rfi.seminar_id,
            "seminar_name": seminar_name_cache.get(rfi.seminar_id) if rfi.seminar_id else None,
            "title": rfi.title,
            "description": rfi.description,
            "destination": rfi.destination,
            "status": visible_status,
            "response_count": response_count,
            "created_at": rfi.created_at.isoformat(),
            "updated_at": rfi.updated_at.isoformat()
        })

    return result


@router.get("/rfis/{project_id}/{rfi_id}")
async def get_rfi(
    project_id: str,
    rfi_id: str,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Get a single RFI with all responses and attachments."""
    
    # Get RFI
    rfi = db.query(database.RFI).filter(
        database.RFI.id == rfi_id,
        database.RFI.project_id == project_id
    ).first()
    
    if not rfi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFI not found"
        )
    
    # Check permissions
    is_white_cell = is_white_cell_member(user_id, project_id, db)

    is_participant = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "participant"
    ).first() is not None

    if not is_white_cell and not is_participant:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view RFIs in this project"
        )

    # Participants can only view RFIs from their own seminar
    if is_participant and rfi.seminar_id:
        user_seminar = get_user_seminar_id(user_id, project_id, db)
        if user_seminar != rfi.seminar_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to view this RFI"
            )
    
    # Get creator info
    creator = db.query(database.User).filter(database.User.id == rfi.created_by_user_id).first()
    
    # Get responses
    responses_query = db.query(database.RFIResponse).filter(
        database.RFIResponse.rfi_id == rfi_id
    )
    
    # Filter out internal notes for participants
    if not is_white_cell:
        responses_query = responses_query.filter(database.RFIResponse.is_internal == 0)
    
    responses = responses_query.order_by(database.RFIResponse.created_at.asc()).all()
    
    # Format responses
    formatted_responses = []
    for response in responses:
        response_user = db.query(database.User).filter(database.User.id == response.user_id).first()
        
        # Get response attachments
        response_attachments = db.query(database.RFIResponseAttachment).filter(
            database.RFIResponseAttachment.response_id == response.id
        ).all()
        
        formatted_response_attachments = []
        for attachment in response_attachments:
            formatted_response_attachments.append({
                "id": attachment.id,
                "file_key": attachment.file_key,
                "file_name": attachment.file_name,
                "file_size": attachment.file_size,
                "created_at": attachment.created_at.isoformat()
            })
        
        formatted_responses.append({
            "id": response.id,
            "user_id": response.user_id,
            "user_name": response_user.name if response_user else "Unknown",
            "content": response.content,
            "is_internal": bool(response.is_internal),
            "is_official_response": bool(response.is_official_response),
            "is_ai_response": bool(response.is_ai_response),
            "created_at": response.created_at.isoformat(),
            "attachments": formatted_response_attachments
        })
    
    # Get attachments
    attachments = db.query(database.RFIAttachment).filter(
        database.RFIAttachment.rfi_id == rfi_id
    ).all()
    
    formatted_attachments = []
    for attachment in attachments:
        formatted_attachments.append({
            "id": attachment.id,
            "file_key": attachment.file_key,
            "file_name": attachment.file_name,
            "file_size": attachment.file_size,
            "created_at": attachment.created_at.isoformat()
        })
    
    # If AI answer is missing and viewer is white cell, kick off background generation
    ai_answer = rfi.ai_answer
    if not ai_answer and is_white_cell:
        rfi_id_for_bg = rfi.id
        rfi_project_id = rfi.project_id

        def _thread_generate_for_get(rfi_id_inner: str):
            """Runs entirely in thread pool — own DB session, no cross-thread sharing."""
            logging.info(f"[BG-GET] Starting background AI generation for RFI {rfi_id_inner}")
            bg_db = database.SessionLocal()
            try:
                bg_rfi = bg_db.query(database.RFI).filter(database.RFI.id == rfi_id_inner).first()
                if bg_rfi:
                    generated = _do_generate_ai_answer(bg_rfi, bg_db)
                    bg_db.commit()
                    logging.info(f"Background AI answer generated for RFI {rfi_id_inner} (on-demand)")
                    bg_project_data = get_project_data(bg_db, rfi_project_id)
                    bg_docs = bg_project_data.get('docs', []) if bg_project_data else []
                    bg_ai_answer_docs = [{"name": d.get("name", ""), "key": d.get("key", "")} for d in bg_docs]
                    return generated, bg_ai_answer_docs
            except Exception as e:
                logging.error(f"Background AI answer generation failed for RFI {rfi_id_inner}: {e}", exc_info=True)
            finally:
                bg_db.close()
            return None, []

        async def _background_generate_ai_answer_for_get(rfi_id_inner: str):
            loop = asyncio.get_event_loop()
            try:
                generated, bg_ai_answer_docs = await loop.run_in_executor(None, _thread_generate_for_get, rfi_id_inner)
            finally:
                _rfi_generation_in_progress.discard(rfi_id_inner)
            if generated:
                try:
                    broadcast_data = {
                        "id": rfi_id_inner,
                        "ai_answer": generated,
                        "ai_answer_docs": bg_ai_answer_docs,
                    }
                    bg_db2 = database.SessionLocal()
                    try:
                        bg_wc_ids = get_white_cell_user_ids(rfi_project_id, bg_db2)
                    finally:
                        bg_db2.close()
                    await rfi_connection_manager.broadcast_to_white_cell(
                        rfi_project_id,
                        {
                            "type": "rfi_updated",
                            "data": broadcast_data
                        },
                        bg_wc_ids
                    )
                except Exception as e:
                    logging.error(f"Failed to broadcast AI answer for RFI {rfi_id_inner}: {e}", exc_info=True)

        if rfi_id_for_bg not in _rfi_generation_in_progress:
            _rfi_generation_in_progress.add(rfi_id_for_bg)
            _safe_broadcast(_background_generate_ai_answer_for_get(rfi_id_for_bg))
        else:
            logging.info(f"[BG-GET] Generation already in progress for RFI {rfi_id_for_bg}, skipping duplicate spawn")

    # Build AI answer data — only for white cell
    ai_answer_docs = []
    if is_white_cell and ai_answer:
        try:
            project_data = get_project_data(db, rfi.project_id)
            docs = project_data.get('docs', []) if project_data else []
            ai_answer_docs = [{"name": d.get("name", ""), "key": d.get("key", "")} for d in docs]
        except Exception:
            pass

    # Map status for participants — hide AI-internal statuses
    visible_status = rfi.status
    if not is_white_cell and rfi.status in ("AI Answered", "AI Resolved", "Escalated"):
        visible_status = "Not Started"

    return {
        "id": rfi.id,
        "project_id": rfi.project_id,
        "rfi_number": rfi.rfi_number,
        "created_by_user_id": rfi.created_by_user_id,
        "created_by_name": creator.name if creator else "Unknown",
        "title": rfi.title,
        "description": rfi.description,
        "destination": rfi.destination,
        "status": visible_status,
        "created_at": rfi.created_at.isoformat(),
        "updated_at": rfi.updated_at.isoformat(),
        "responses": formatted_responses,
        "attachments": formatted_attachments,
        "ai_answer": ai_answer if is_white_cell else None,
        "ai_answer_docs": ai_answer_docs if is_white_cell else []
    }


@router.put("/rfis/{rfi_id}")
async def update_rfi_status(
    rfi_id: str,
    update_data: RFIStatusUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update RFI status. Only white cell members can update status."""
    
    # Get RFI
    rfi = db.query(database.RFI).filter(database.RFI.id == rfi_id).first()
    
    if not rfi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFI not found"
        )
    
    # Verify user is white cell member
    if not is_white_cell_member(user_id, rfi.project_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only white cell members can update RFI status"
        )
    
    # Validate status
    valid_statuses = ["Not Started", "In Review", "Closed"]
    if update_data.status not in valid_statuses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid status. Must be one of: {', '.join(valid_statuses)}"
        )
    
    # Update status
    rfi.status = update_data.status
    rfi.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(rfi)
    
    # Log event
    events.log_event(user_id, f"event:{rfi.project_id}", f"Updated RFI {rfi.rfi_number} status to {update_data.status}")
    
    response_data = {
        "id": rfi.id,
        "rfi_number": rfi.rfi_number,
        "status": rfi.status,
        "updated_at": rfi.updated_at.isoformat()
    }
    
    # Broadcast RFI status update to seminar + white cell (fire and forget)
    scoped_ids = get_seminar_and_whitecell_ids(rfi.project_id, rfi.seminar_id, db)
    _safe_broadcast(
        rfi_connection_manager.broadcast_to_users(
            rfi.project_id,
            {"type": "rfi_updated", "data": response_data},
            scoped_ids
        )
    )

    return response_data


@router.post("/rfis/{rfi_id}/responses", status_code=status.HTTP_201_CREATED)
async def create_rfi_response(
    rfi_id: str,
    response_data: RFIResponseCreate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Create a response to an RFI."""
    
    # Get RFI
    rfi = db.query(database.RFI).filter(database.RFI.id == rfi_id).first()
    
    if not rfi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFI not found"
        )
    
    # Check permissions
    is_white_cell = is_white_cell_member(user_id, rfi.project_id, db)
    
    # Only white cell members can create internal notes
    if response_data.is_internal and not is_white_cell:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only white cell members can create internal notes"
        )
    
    # Only white cell members can mark as official response
    if response_data.is_official_response and not is_white_cell:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only white cell members can create official responses"
        )
    
    # Check if this is the first public response from white cell (for status auto-update)
    status_changed = False
    if is_white_cell and not response_data.is_internal:
        # Check if this is the first public response from white cell
        existing_public_responses = db.query(database.RFIResponse).filter(
            database.RFIResponse.rfi_id == rfi_id,
            database.RFIResponse.is_internal == 0
        ).count()
        
        # If this is the first public response and RFI is "Not Started", change to "In Review"
        if existing_public_responses == 0 and rfi.status == "Not Started":
            rfi.status = "In Review"
            rfi.updated_at = datetime.utcnow()
            status_changed = True
        # When white cell responds to any non-closed RFI, move to In Review
        elif rfi.status not in ("In Review", "Closed"):
            rfi.status = "In Review"
            rfi.updated_at = datetime.utcnow()
            status_changed = True
    
    # If this is an official response, change status to "Closed" (unless already closed)
    if response_data.is_official_response and rfi.status != "Closed":
        rfi.status = "Closed"
        rfi.updated_at = datetime.utcnow()
        status_changed = True
    
    # Create response
    new_response = database.RFIResponse(
        rfi_id=rfi_id,
        user_id=user_id,
        content=response_data.content,
        is_internal=1 if response_data.is_internal else 0,
        is_official_response=1 if response_data.is_official_response else 0
    )
    
    db.add(new_response)
    db.commit()
    db.refresh(new_response)
    db.refresh(rfi)  # Refresh to get updated status
    
    # Create response attachments if provided
    formatted_attachments = []
    if response_data.attachments:
        attachment_objects = []
        for attachment in response_data.attachments:
            new_attachment = database.RFIResponseAttachment(
                response_id=new_response.id,
                file_key=attachment.get("file_key"),
                file_name=attachment.get("file_name"),
                file_size=attachment.get("file_size")
            )
            db.add(new_attachment)
            attachment_objects.append(new_attachment)
        db.flush()  # Flush to get IDs
        db.commit()
        # Refresh to get created_at
        for att in attachment_objects:
            db.refresh(att)
            formatted_attachments.append({
                "id": att.id,
                "file_key": att.file_key,
                "file_name": att.file_name,
                "file_size": att.file_size,
                "created_at": att.created_at.isoformat()
            })
    
    # Log event
    response_type = "internal note" if response_data.is_internal else "response"
    events.log_event(user_id, f"event:{rfi.project_id}", f"Added {response_type} to RFI {rfi.rfi_number}")
    
    # Get user info
    user = db.query(database.User).filter(database.User.id == user_id).first()
    
    response_result = {
        "id": new_response.id,
        "rfi_id": new_response.rfi_id,
        "rfi_number": rfi.rfi_number,
        "user_id": new_response.user_id,
        "user_name": user.name if user else "Unknown",
        "content": new_response.content,
        "is_internal": bool(new_response.is_internal),
        "is_official_response": bool(new_response.is_official_response),
        "is_ai_response": bool(new_response.is_ai_response),
        "created_at": new_response.created_at.isoformat(),
        "attachments": formatted_attachments
    }

    # Broadcast response creation with role-based + seminar filtering
    white_cell_user_ids = get_white_cell_user_ids(rfi.project_id, db)

    if response_data.is_internal:
        # Internal notes: broadcast only to white cell members
        _safe_broadcast(
            rfi_connection_manager.broadcast_to_white_cell(
                rfi.project_id,
                {"type": "rfi_response_created", "data": response_result},
                white_cell_user_ids
            )
        )
    else:
        # Public responses: broadcast to seminar members + white cell
        scoped_ids = get_seminar_and_whitecell_ids(rfi.project_id, rfi.seminar_id, db)
        _safe_broadcast(
            rfi_connection_manager.broadcast_to_users(
                rfi.project_id,
                {"type": "rfi_response_created", "data": response_result},
                scoped_ids
            )
        )
    
    # Broadcast RFI status update if status changed (scoped to seminar + white cell)
    if status_changed:
        rfi_update_data = {
            "id": rfi.id,
            "rfi_number": rfi.rfi_number,
            "status": rfi.status,
            "updated_at": rfi.updated_at.isoformat()
        }
        status_scoped_ids = get_seminar_and_whitecell_ids(rfi.project_id, rfi.seminar_id, db)
        _safe_broadcast(
            rfi_connection_manager.broadcast_to_users(
                rfi.project_id,
                {"type": "rfi_updated", "data": rfi_update_data},
                status_scoped_ids
            )
        )

    # Send notifications for RFI responses (skip internal notes)
    if not response_data.is_internal:
        if is_white_cell:
            # White cell responded - notify the RFI creator (participant)
            if rfi.created_by_user_id != user_id:  # Don't notify yourself
                await notifications.create_notification_async(
                    user_id=rfi.created_by_user_id,
                    display_text=f"New response to RFI {rfi.rfi_number}: {rfi.title}",
                    action={
                        "type": "navigate",
                        "route": f"/thewarroom/projects/participant-rfi/{rfi.project_id}/{rfi.id}"
                    },
                    db=db
                )
        else:
            # Participant responded - notify all white cell members
            white_cell_members = get_white_cell_members(rfi.project_id, db)
            if white_cell_members:
                await notifications.broadcast_notification_to_users(
                    user_ids=white_cell_members,
                    display_text=f"New response to RFI {rfi.rfi_number}: {rfi.title}",
                    action={
                        "type": "navigate",
                        "route": f"/thewarroom/projects/whitecell-rfi/{rfi.project_id}/{rfi.id}"
                    },
                    db=db
                )
    
    return response_result


@router.put("/rfis/{rfi_id}/responses/{response_id}")
async def update_rfi_response(
    rfi_id: str,
    response_id: str,
    update_data: RFIResponseUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update an RFI response. Only response creator or white cell members can update."""
    
    # Get response
    response = db.query(database.RFIResponse).filter(
        database.RFIResponse.id == response_id,
        database.RFIResponse.rfi_id == rfi_id
    ).first()
    
    if not response:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Response not found"
        )
    
    # Get RFI
    rfi = db.query(database.RFI).filter(database.RFI.id == rfi_id).first()
    
    # Check permissions
    is_white_cell = is_white_cell_member(user_id, rfi.project_id, db)
    is_creator = response.user_id == user_id
    
    if not (is_creator or is_white_cell):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to update this response"
        )
    
    # Update fields
    response.content = update_data.content
    
    if update_data.is_internal is not None:
        if update_data.is_internal and not is_white_cell:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only white cell members can mark responses as internal"
            )
        response.is_internal = 1 if update_data.is_internal else 0
    
    if update_data.is_official_response is not None:
        if update_data.is_official_response and not is_white_cell:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only white cell members can mark responses as official"
            )
        response.is_official_response = 1 if update_data.is_official_response else 0
    
    db.commit()
    db.refresh(response)
    
    # Log event
    events.log_event(user_id, f"event:{rfi.project_id}", f"Updated response to RFI {rfi.rfi_number}")
    
    # Get user info
    user = db.query(database.User).filter(database.User.id == user_id).first()
    
    # Get response attachments
    response_attachments = db.query(database.RFIResponseAttachment).filter(
        database.RFIResponseAttachment.response_id == response.id
    ).all()
    
    formatted_response_attachments = []
    for attachment in response_attachments:
        formatted_response_attachments.append({
            "id": attachment.id,
            "file_key": attachment.file_key,
            "file_name": attachment.file_name,
            "file_size": attachment.file_size,
            "created_at": attachment.created_at.isoformat()
        })
    
    response_result = {
        "id": response.id,
        "rfi_id": response.rfi_id,
        "rfi_number": rfi.rfi_number,
        "user_id": response.user_id,
        "user_name": user.name if user else "Unknown",
        "content": response.content,
        "is_internal": bool(response.is_internal),
        "is_official_response": bool(response.is_official_response),
        "is_ai_response": bool(response.is_ai_response),
        "created_at": response.created_at.isoformat(),
        "attachments": formatted_response_attachments
    }

    # Broadcast response update with role-based + seminar filtering (fire and forget)
    white_cell_user_ids = get_white_cell_user_ids(rfi.project_id, db)

    if response.is_internal:
        # Internal notes: broadcast only to white cell members
        _safe_broadcast(
            rfi_connection_manager.broadcast_to_white_cell(
                rfi.project_id,
                {"type": "rfi_response_updated", "data": response_result},
                white_cell_user_ids
            )
        )
    else:
        # Public responses: broadcast to seminar members + white cell
        scoped_ids = get_seminar_and_whitecell_ids(rfi.project_id, rfi.seminar_id, db)
        _safe_broadcast(
            rfi_connection_manager.broadcast_to_users(
                rfi.project_id,
                {"type": "rfi_response_updated", "data": response_result},
                scoped_ids
            )
        )

    return response_result


def _do_generate_ai_answer(rfi, db: Session) -> str:
    """Core AI answer generation logic. Builds context, calls LLM, saves result to rfi.ai_answer."""

    # Get seminar_id from the RFI creator's project membership
    seminar_id = None
    creator_membership = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == rfi.project_id,
        database.ProjectMember.user_id == rfi.created_by_user_id
    ).first()
    if creator_membership:
        seminar_id = creator_membership.seminar_id

    # Get previous RFI responses to determine the current question
    previous_responses = db.query(database.RFIResponse).filter(
        database.RFIResponse.rfi_id == rfi.id
    ).order_by(database.RFIResponse.created_at.asc()).all()

    # Get S3 bucket from RFIResponseAttachment URLs
    s3_bucket = get_bucket_from_response_attachments(db, rfi.id)

    # Build the original RFI question
    rfi_question = rfi.title
    if rfi.description:
        rfi_question += f". {rfi.description}"

    # Find the last message from a non-white cell member (participant)
    latest_participant_message = None
    if previous_responses:
        for response in reversed(previous_responses):
            if not is_white_cell_member(response.user_id, rfi.project_id, db):
                latest_participant_message = response.content
                logging.info(f"Found last non-white cell message from participant")
                break

    # Use the latest participant message if it exists, otherwise use original RFI
    current_question = latest_participant_message if latest_participant_message else rfi_question

    # Get RFI attachments
    rfi_attachments = db.query(database.RFIAttachment).filter(
        database.RFIAttachment.rfi_id == rfi.id
    ).all()

    # Get context from RFI attachments
    rfi_att_context = {"context": "", "sources_map": {}}
    if rfi_attachments and s3_bucket:
        try:
            rfi_att_context = get_context_from_attachments(rfi_attachments, s3_bucket, "RFI")
        except Exception as e:
            logging.error(f"Error getting context from RFI attachments: {e}", exc_info=True)

    # Get context from previous responses
    previous_responses_context = ""
    if previous_responses:
        try:
            previous_responses_context = get_previous_responses_context(previous_responses, s3_bucket)
        except Exception as e:
            logging.error(f"Error getting context from previous responses: {e}", exc_info=True)

    # Combine all context
    context_text = ""
    if rfi_att_context.get("context"):
        context_text = rfi_att_context["context"]

    sources_map = rfi_att_context.get("sources_map", {})

    # Build source reference information for the prompt
    source_info = ""
    if sources_map and len(sources_map) > 0:
        source_info = "\n\nAvailable source documents (ONLY reference these exact document names if you use them):\n"
        seen_docs = set()
        for source_id, source_data in sources_map.items():
            doc_title = source_data.get('title', 'Document')
            if doc_title not in seen_docs:
                page_info = source_data.get('linkLabel', '').replace('Page ', '') if 'linkLabel' in source_data else 'N/A'
                source_info += f"- {doc_title} (Page {page_info})\n"
                seen_docs.add(doc_title)

    # Get world state context
    world_state_context = ""
    if seminar_id:
        world_state = get_current_world_state(db, seminar_id)
        if world_state:
            logging.info(f"Using world state from seminar {seminar_id}")
            world_state_context = f"\nCurrent World State:\n{world_state.get('summary_state', '')}\n\nDetailed State:\n{world_state.get('detailed_state', '')}"
    else:
        latest_world_state = db.query(database.WorldState).filter(
            database.WorldState.project_id == rfi.project_id
        ).order_by(database.WorldState.created_at.desc()).first()
        if latest_world_state:
            logging.info(f"Using latest project-level world state for project {rfi.project_id}")
            world_state_context = f"\nCurrent World State:\n{latest_world_state.summary_state or ''}\n\nDetailed State:\n{latest_world_state.detailed_state or ''}"

    # Get sent injects for the seminar (scenario-specific documents)
    injects_context = ""
    sent_injects = []
    try:
        sent_injects = get_sent_injects(db, rfi.project_id, seminar_id)
        if sent_injects:
            inject_file_context = load_inject_files_context(sent_injects, s3_bucket) if s3_bucket else {}
            injects_parts = []
            for inject in sent_injects:
                inject_index = inject.get('index', '?')
                part = f"### Inject {inject_index}: {inject.get('title', 'Untitled')}"
                if inject.get('description'):
                    part += f"\n{inject['description']}"
                if inject.get('files'):
                    file_names = [f.get('name', 'Unknown file') for f in inject['files']]
                    part += f"\nAttached files: {', '.join(file_names)}"
                if inject_file_context and inject_index in inject_file_context:
                    part += f"\n\nFile Contents:\n{inject_file_context[inject_index]}"
                injects_parts.append(part)
            if injects_parts:
                injects_context = "\n\n".join(injects_parts)
                logging.info(f"Including {len(sent_injects)} sent injects in RFI context ({len(injects_context)} characters)")
    except Exception as e:
        logging.error(f"Error loading injects for RFI {rfi.id}: {e}", exc_info=True)

    # Two-pass: (1) ask LLM which docs are relevant, (2) load those docs for full answer
    project_docs_context = ""
    project_doc_names = []
    try:
        project_data = get_project_data(db, rfi.project_id)
        docs = project_data.get('docs', []) if project_data else []
        if docs:
            project_doc_names = [d.get('name', 'Unknown') for d in docs]
            doc_names_list = "\n".join(f"- {name}" for name in project_doc_names)

            # Pass 1: Ask LLM which documents are likely relevant
            pass1_prompt = f"""Given the following question and world state context, identify which of the listed documents are most likely to contain the answer.

Question: {current_question}

{world_state_context if world_state_context else "No world state available."}

Available documents:
{doc_names_list}

Reply with ONLY the exact document names that are relevant, one per line. If unsure, include the document. Do not add any other text."""

            logging.info(f"Pass 1: Asking LLM to identify relevant docs from {len(docs)} documents")
            pass1_response = fetch_llm_response(pass1_prompt)

            if pass1_response.startswith("[ERROR]"):
                logging.error(f"Pass 1 LLM failed: {pass1_response}, falling back to all docs")
                relevant_docs = docs
            else:
                # Match LLM response lines to actual doc names
                response_lines = [line.strip().lstrip('- ') for line in pass1_response.strip().split('\n') if line.strip()]
                relevant_docs = []
                for doc in docs:
                    doc_name = doc.get('name', '')
                    for line in response_lines:
                        if doc_name.lower() in line.lower() or line.lower() in doc_name.lower():
                            relevant_docs.append(doc)
                            break

                if not relevant_docs:
                    relevant_docs = docs
                    logging.info(f"Pass 1 matched no docs, falling back to all {len(docs)}")
                else:
                    logging.info(f"Pass 1 selected {len(relevant_docs)}/{len(docs)} docs: {[d.get('name') for d in relevant_docs]}")

            # Load relevant docs, enforce token limit
            MAX_PROMPT_TOKENS = 900_000  # Leave ~148K tokens for response + overhead out of 1M
            enc = tiktoken.get_encoding("cl100k_base")
            project_docs_context = load_docs_context(relevant_docs)
            doc_tokens = len(enc.encode(project_docs_context))
            logging.info(f"Pass 2: Loaded relevant docs: {doc_tokens} tokens")
            if doc_tokens > MAX_PROMPT_TOKENS:
                logging.warning(f"Docs exceed {MAX_PROMPT_TOKENS} tokens ({doc_tokens}), truncating")
                encoded = enc.encode(project_docs_context)
                project_docs_context = enc.decode(encoded[:MAX_PROMPT_TOKENS])
                logging.info(f"Truncated to {MAX_PROMPT_TOKENS} tokens")
    except Exception as e:
        logging.error(f"Error loading project documents for RFI {rfi.id}: {e}", exc_info=True)

    # Log context information for debugging
    logging.info(f"Context text length: {len(context_text)} characters")
    logging.info(f"Original RFI: {rfi_question}")
    if sources_map:
        logging.info(f"Source documents details: {[(source_data.get('title', 'Unknown'), source_data.get('linkLabel', 'N/A')) for source_data in sources_map.values()]}")
    if previous_responses_context:
        logging.info(f"Including {len(previous_responses)} previous responses in context")
    if world_state_context:
        logging.info(f"Including world state context ({len(world_state_context)} characters)")

    # Build prompt
    prompt_parts = []

    if world_state_context:
        prompt_parts.append(world_state_context)

    prompt_parts.append(f"Question to answer:\n{current_question}")

    if project_docs_context:
        prompt_parts.append(f"\nEvent Documents:\n{project_docs_context}")

    if injects_context:
        prompt_parts.append(f"\nSeminar Injects (scenario updates sent to participants):\n{injects_context}")

    if context_text:
        prompt_parts.append(f"\nAttachments:\n{context_text}")

    if previous_responses_context:
        prompt_parts.append(previous_responses_context)

    # Build available source documents list from project docs + injects + any attachment sources
    all_source_names = list(project_doc_names)
    if sent_injects:
        for inject in sent_injects:
            inject_title = f"Inject {inject.get('index', '?')}: {inject.get('title', 'Untitled')}"
            all_source_names.append(inject_title)
    if sources_map:
        seen_docs = set()
        for source_data in sources_map.values():
            doc_title = source_data.get('title', 'Document')
            if doc_title not in seen_docs:
                all_source_names.append(doc_title)
                seen_docs.add(doc_title)

    source_list = ""
    if all_source_names:
        source_list = "\n\nAvailable source documents (use ONLY these exact names when citing sources):\n"
        for name in all_source_names:
            source_list += f"- {name}\n"
        prompt_parts.append(source_list)

    context_section = "\n".join(prompt_parts)

    prompt = f"""You are drafting a response to a Request for Information (RFI).

{context_section}

Generate a professional RFI response. Follow these guidelines:

1. Answer the question directly and concisely.

2. If a world state is provided, use it as background context for understanding the current situation.

3. If event documents, seminar injects, or attachments are provided, use them to inform your answer. Injects are scenario updates sent during the exercise and may contain critical situational information.

4. Be as concise as possible while fully answering the question. Avoid unnecessary elaboration.

5. Use a professional tone appropriate for the intended recipient ({rfi.destination or "the requester"}).

6. At the end of your response, cite the exact source document name and section where the information was found (e.g. "The requested information was found in the [section] section of [exact document name]."). If the information was NOT found in any event document, clearly state that.

CRITICAL RULES:
- Output ONLY the RFI response text with source citation. No headers, labels, or meta-commentary.
- Use ONLY exact document names from the "Available source documents" list for citations.
- Do NOT make up or invent document names. Do NOT cite "the provided context" or "Detailed State" — cite the actual document filename.
- Always include source attribution at the end of your response.

Generate the RFI response:"""

    # Call LLM
    generated_answer = fetch_llm_response(prompt)

    if generated_answer.startswith("[ERROR]"):
        raise Exception(generated_answer)

    generated_answer = generated_answer.strip()

    if not generated_answer:
        raise Exception("Received empty response from AI")

    # Save to rfi.ai_answer
    rfi.ai_answer = generated_answer
    db.commit()

    return generated_answer


@router.post("/rfis/{rfi_id}/generate-answer")
async def generate_rfi_answer(
    rfi_id: str,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Generate an AI answer for an RFI. Only white cell members can generate answers."""

    # Get RFI
    rfi = db.query(database.RFI).filter(database.RFI.id == rfi_id).first()

    if not rfi:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="RFI not found"
        )

    # Permission check MUST be first
    if not is_white_cell_member(user_id, rfi.project_id, db):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only white cell members can generate AI answers"
        )

    try:
        generated_answer = _do_generate_ai_answer(rfi, db)
        db.commit()
        # Broadcast AI answer to white cell only — participants should not see AI drafts
        try:
            project_data = get_project_data(db, rfi.project_id)
            docs = project_data.get("docs", []) if project_data else []
            ai_answer_docs = [{"name": d.get("name", ""), "key": d.get("key", "")} for d in docs]
            broadcast_data = {
                "id": rfi_id,
                "ai_answer": generated_answer,
                "ai_answer_docs": ai_answer_docs,
            }
            gen_wc_ids = get_white_cell_user_ids(rfi.project_id, db)
            _safe_broadcast(
                rfi_connection_manager.broadcast_to_white_cell(
                    rfi.project_id,
                    {"type": "rfi_updated", "data": broadcast_data},
                    gen_wc_ids
                )
            )
        except Exception as e:
            logging.error(f"Failed to broadcast AI answer after generate-answer for RFI {rfi_id}: {e}", exc_info=True)
        return {"answer": generated_answer}
    except Exception as e:
        logging.error(f"Error generating RFI answer: {e}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate AI answer: {str(e)}"
        )
