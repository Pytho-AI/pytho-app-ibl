"""COA Submission API endpoints."""
import json
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List, Any
from datetime import datetime

from utils import database
from utils import events
from utils import world_state
from utils import notifications
from utils import slack

logger = logging.getLogger(__name__)

router = APIRouter()


# Pydantic models for request/response
class COAOption(BaseModel):
    title: str
    description: Optional[str] = None
    sequence_number: int


class COASubmissionCreate(BaseModel):
    seminar_id: str
    project_id: str
    submitter_user_id: str
    coa_options: List[COAOption]
    reviewer_user_id: Optional[str] = None
    status: Optional[str] = "Submitted"


class COASubmissionUpdate(BaseModel):
    reviewer_user_id: Optional[str] = None
    coa_options: Optional[List[COAOption]] = None
    status: Optional[str] = None
    approved_coa: Optional[Any] = None


class COASubmissionResponse(BaseModel):
    id: str
    seminar_id: str
    project_id: str
    submitter_user_id: str
    reviewer_user_id: Optional[str]
    coa_options: List[Any]
    status: str
    approved_coa: Optional[Any]
    recommended_coa: Optional[Any]
    created_at: str
    updated_at: str


def serialize_submission(submission: database.COASubmission) -> dict:
    """Helper to serialize a COA submission to dict."""
    return {
        "id": submission.id,
        "seminar_id": submission.seminar_id,
        "project_id": submission.project_id,
        "submitter_user_id": submission.submitter_user_id,
        "reviewer_user_id": submission.reviewer_user_id,
        "coa_options": submission.coa_options,
        "status": submission.status,
        "approved_coa": submission.approved_coa,
        "recommended_coa": submission.recommended_coa,
        "created_at": submission.created_at.isoformat(),
        "updated_at": submission.updated_at.isoformat()
    }


@router.post("/coa-submissions", status_code=status.HTTP_201_CREATED)
def create_coa_submission(
    submission_data: COASubmissionCreate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Create a new COA submission for a seminar."""

    # Convert COA options to dict format for JSON storage
    coa_options_json = [opt.model_dump() for opt in submission_data.coa_options]
    
    # Create submission
    new_submission = database.COASubmission(
        seminar_id=submission_data.seminar_id,
        project_id=submission_data.project_id,
        submitter_user_id=submission_data.submitter_user_id,
        reviewer_user_id=submission_data.reviewer_user_id,
        coa_options=coa_options_json,
        status=submission_data.status or "Submitted"
    )
    
    db.add(new_submission)
    db.commit()
    db.refresh(new_submission)
    slack.post_notification({"text": f"COA submission created: {json.dumps(coa_options_json)}"})
    
    # Log event
    events.log_event(
        user_id, 
        f"coa_submission:{submission_data.project_id}", 
        f"Created COA submission for seminar {submission_data.seminar_id}"
    )
    
    # Notify all white cell members of the COA submission
    try:
        # Get seminar name for the notification
        seminar = db.query(database.Seminar).filter(
            database.Seminar.id == submission_data.seminar_id
        ).first()
        seminar_name = seminar.name if seminar else "a seminar"
        
        # Get the latest world state for this seminar
        latest_world_state_text = ""
        latest_mapping = db.query(database.SeminarWorldState).filter(
            database.SeminarWorldState.seminar_id == submission_data.seminar_id
        ).order_by(database.SeminarWorldState.created_at.desc()).first()
        if latest_mapping and latest_mapping.world_state:
            ws = latest_mapping.world_state
            latest_world_state_text = ws.detailed_state or ws.summary_state or ""
        
        # Get all white cell members for this project
        white_cell_members = db.query(database.ProjectMember).filter(
            database.ProjectMember.project_id == submission_data.project_id,
            database.ProjectMember.role == "white"
        ).all()
        
        # Create notification for each white cell member
        for member in white_cell_members:
                notifications.create_notification(
                    user_id=member.user_id,
                    display_text=f"New COA submission from {seminar_name} awaiting review",
                    action={
                        "type": "open_coa_review_modal",
                        "project_id": submission_data.project_id,
                        "seminar_id": submission_data.seminar_id,
                        "seminar_name": seminar_name,
                        "coa_options": coa_options_json,
                        "submission_id": new_submission.id,
                        "world_state": latest_world_state_text
                    },
                    db=db
                )
        
        logger.info(f"Sent COA submission notifications to {len(white_cell_members)} white cell members")
    except Exception as notify_error:
        # Log but don't fail the request if notifications fail
        logger.error(f"Error sending COA submission notifications: {notify_error}")
    
    return serialize_submission(new_submission)



@router.put("/coa-submissions/{submission_id}")
def update_coa_submission(
    submission_id: str,
    update_data: COASubmissionUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update a COA submission."""
    
    submission = db.query(database.COASubmission).filter(
        database.COASubmission.id == submission_id
    ).first()
    
    if not submission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="COA submission not found"
        )
    
    # Update fields if provided
    if update_data.reviewer_user_id is not None:
        submission.reviewer_user_id = update_data.reviewer_user_id
    
    if update_data.coa_options is not None:
        submission.coa_options = [opt.model_dump() for opt in update_data.coa_options]
    
    if update_data.status is not None:
        submission.status = update_data.status
    
    if update_data.approved_coa is not None:
        submission.approved_coa = update_data.approved_coa
        submission.status = "Approved"
    
    submission.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(submission)
    slack.post_notification({"text": f"COA submission updated: {json.dumps(submission.coa_options)}"})
    
    # Log event
    events.log_event(
        user_id, 
        f"coa_submission:{submission.project_id}", 
        f"Updated COA submission {submission_id}"
    )
    
    # If a COA was approved, trigger world state update and notify participants
    if update_data.approved_coa is not None:
        try:
            world_state.create_world_state_for_project(
                db,
                submission.project_id,
                seminar_ids=[submission.seminar_id]
            )
        except Exception as e:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"COA approved but failed to update world state: {str(e)}"
            )
        
        # Notify participants who are members of this seminar
        try:
            # Get seminar name for the notification
            seminar = db.query(database.Seminar).filter(
                database.Seminar.id == submission.seminar_id
            ).first()
            seminar_name = seminar.name if seminar else "your seminar"
            
            # Get approved COA title
            approved_coa_title = ""
            if isinstance(update_data.approved_coa, dict):
                approved_coa_title = update_data.approved_coa.get("title", "")
            
            # Get participants assigned to this seminar
            participants = db.query(database.ProjectMember).filter(
                database.ProjectMember.project_id == submission.project_id,
                database.ProjectMember.role == "participant",
                database.ProjectMember.seminar_id == submission.seminar_id
            ).all()
            
            # Create notification for each participant
            for participant in participants:
                display_text = f"COA approved for {seminar_name}"
                if approved_coa_title:
                    display_text += f": {approved_coa_title}"
                
                notifications.create_notification(
                    user_id=participant.user_id,
                    display_text=display_text,
                    action={
                        "type": "open_coa_modal",
                        "project_id": submission.project_id,
                        "seminar_id": submission.seminar_id,
                        "seminar_name": seminar_name,
                        "coa_options": submission.coa_options,
                        "approved_coa": submission.approved_coa
                    },
                    db=db
                )
            
            logger.info(f"Sent COA approval notifications to {len(participants)} participants for seminar {submission.seminar_id}")
        except Exception as notify_error:
            # Log but don't fail the request if notifications fail
            logger.error(f"Error sending COA approval notifications: {notify_error}")
    
    return serialize_submission(submission)
