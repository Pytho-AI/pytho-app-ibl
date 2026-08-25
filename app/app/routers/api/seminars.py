"""Seminar API endpoints."""
import json
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

from utils import database
from utils import events

router = APIRouter()


class WorldStateUpdate(BaseModel):
    summary_state: Optional[str] = None
    detailed_state: Optional[str] = None


# Pydantic models for request/response
class SeminarCreate(BaseModel):
    project_id: str
    name: str
    description: Optional[str] = None


class SeminarUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class SeminarResponse(BaseModel):
    id: str
    project_id: str
    name: str
    description: Optional[str]
    created_at: str
    updated_at: str



@router.post("/seminars", status_code=status.HTTP_201_CREATED)
def create_seminar(
    seminar_data: SeminarCreate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Create a new seminar."""
    
    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == seminar_data.project_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Create seminar
    new_seminar = database.Seminar(
        project_id=seminar_data.project_id,
        name=seminar_data.name,
        description=seminar_data.description
    )
    
    db.add(new_seminar)
    db.commit()
    db.refresh(new_seminar)
    
    # Log event
    events.log_event(user_id, f"seminar:{seminar_data.project_id}", f"Created seminar: {seminar_data.name}")
    
    return {
        "id": new_seminar.id,
        "index": new_seminar.index,
        "project_id": new_seminar.project_id,
        "name": new_seminar.name,
        "description": new_seminar.description,
        "created_at": new_seminar.created_at.isoformat(),
        "updated_at": new_seminar.updated_at.isoformat()
    }


@router.get("/seminars/{project_id}")
def get_seminars_for_project(
    project_id: str,
    user_id: Optional[str] = Query(None),
    db: Session = Depends(database.get_db)
):
    """Get seminars for a project. Participants only see their own seminar."""

    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()

    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )

    # If user_id provided, check if participant and filter to their seminar
    participant_seminar_id = None
    if user_id:
        user = db.query(database.User).filter(database.User.id == user_id).first()
        if user:
            try:
                role_data = json.loads(user.role) if isinstance(user.role, str) else user.role
                role_value = role_data.get("role", "participant")
            except Exception:
                role_value = "participant"

            if role_value == "participant":
                # Find which seminar this participant belongs to
                member = db.query(database.ProjectMember).filter(
                    database.ProjectMember.project_id == project_id,
                    database.ProjectMember.user_id == user_id,
                    database.ProjectMember.role == "participant"
                ).first()
                if member and member.seminar_id:
                    participant_seminar_id = member.seminar_id

    # Get seminars — filtered for participants, all for white cell
    query = db.query(database.Seminar).filter(
        database.Seminar.project_id == project_id
    )
    if participant_seminar_id:
        query = query.filter(database.Seminar.id == participant_seminar_id)
    seminars = query.order_by(database.Seminar.index.asc()).all()
    
    # Get all injects for the project
    all_injects = db.query(database.SeminarInject).filter(
        database.SeminarInject.project_id == project_id
    ).order_by(database.SeminarInject.created_at.desc()).all()
    
    # Format response
    result = []
    for seminar in seminars:
        # Get latest inject for this seminar (where seminar_id is in seminar_ids array or "all")
        latest_inject = None
        for inject in all_injects:
            seminar_ids = inject.seminar_ids if inject.seminar_ids else []
            if seminar.id in seminar_ids or "all" in seminar_ids:
                latest_inject = {
                    "id": inject.id,
                    "title": inject.title,
                    "description": inject.description,
                    "inject_type": inject.inject_type,
                    "status": inject.status,
                    "created_at": inject.created_at.isoformat()
                }
                break  # Already ordered by created_at desc, so first match is latest
        
        # Get latest world state for this seminar through mapping table
        latest_mapping = db.query(database.SeminarWorldState).filter(
            database.SeminarWorldState.seminar_id == seminar.id
        ).order_by(database.SeminarWorldState.created_at.desc()).first()
        
        latest_world_state_data = None
        if latest_mapping and latest_mapping.world_state:
            ws = latest_mapping.world_state
            latest_world_state_data = {
                "id": ws.id,
                "summary_state": ws.summary_state,
                "detailed_state": ws.detailed_state,
                "created_at": latest_mapping.created_at.isoformat()
            }
        
        # Get initial world state (latest world state before any injects were sent)
        initial_world_state_data = None
        
        # Find the first sent inject for this seminar
        first_sent_inject = None
        for inject in reversed(all_injects):  # Reverse since all_injects is ordered desc
            seminar_ids = inject.seminar_ids if inject.seminar_ids else []
            if (seminar.id in seminar_ids or "all" in seminar_ids) and inject.status == 'sent':
                if first_sent_inject is None or inject.updated_at < first_sent_inject.updated_at:
                    first_sent_inject = inject
        
        if first_sent_inject:
            # Get the latest world state created before the first sent inject
            initial_mapping = db.query(database.SeminarWorldState).filter(
                database.SeminarWorldState.seminar_id == seminar.id,
                database.SeminarWorldState.created_at < first_sent_inject.updated_at
            ).order_by(database.SeminarWorldState.created_at.desc()).first()
            
            if initial_mapping and initial_mapping.world_state:
                ws = initial_mapping.world_state
                initial_world_state_data = {
                    "id": ws.id,
                    "summary_state": ws.summary_state,
                    "detailed_state": ws.detailed_state,
                    "created_at": initial_mapping.created_at.isoformat()
                }
        else:
            # No injects sent yet, so initial world state is the same as latest
            initial_world_state_data = latest_world_state_data
        
        # Get all sent injects for this seminar (for chronological history)
        sent_injects_list = []
        for inject in all_injects:
            seminar_ids = inject.seminar_ids if inject.seminar_ids else []
            if (seminar.id in seminar_ids or "all" in seminar_ids) and inject.status == 'sent':
                sent_injects_list.append({
                    "id": inject.id,
                    "index": inject.index,
                    "title": inject.title,
                    "description": inject.description,
                    "inject_type": inject.inject_type,
                    "status": inject.status,
                    "created_at": inject.created_at.isoformat(),
                    "updated_at": inject.updated_at.isoformat() if inject.updated_at else inject.created_at.isoformat()
                })
        
        # Get COA submissions for this seminar
        coa_submissions = db.query(database.COASubmission).filter(
            database.COASubmission.seminar_id == seminar.id
        ).order_by(database.COASubmission.updated_at.desc()).all()
        
        coa_submissions_list = []
        
        for coa in coa_submissions:
            coa_submissions_list.append({
                "id": coa.id,
                "submitter_user_id": coa.submitter_user_id,
                "reviewer_user_id": coa.reviewer_user_id,
                "coa_options": coa.coa_options,
                "status": coa.status,
                "approved_coa": coa.approved_coa,
                "recommended_coa": coa.recommended_coa,
                "created_at": coa.created_at.isoformat(),
                "updated_at": coa.updated_at.isoformat() if coa.updated_at else coa.created_at.isoformat()
            })
        
        # Determine status based on the first (most recent) COA submission
        # awaiting_decision: there is a COA submission without approved_coa
        # decision_sent: the first COA submission has approved_coa
        if coa_submissions_list:
            first_coa = coa_submissions_list[0]
            if first_coa.get("approved_coa"):
                status = "decision_sent"
            else:
                status = "awaiting_decision"
        else:
            status = None  # No COA submissions
        
        result.append({
            "id": seminar.id,
            "index": seminar.index,
            "project_id": seminar.project_id,
            "name": seminar.name,
            "description": seminar.description,
            "created_at": seminar.created_at.isoformat(),
            "updated_at": seminar.updated_at.isoformat(),
            "latest_inject": latest_inject,
            "latest_world_state": latest_world_state_data,
            "initial_world_state": initial_world_state_data,
            "coa_submissions": coa_submissions_list,
            "sent_injects": sent_injects_list,
            "status": status
        })
    
    return result


@router.get("/seminars/{project_id}/{seminar_id}")
def get_seminar(
    project_id: str,
    seminar_id: str,
    db: Session = Depends(database.get_db)
):
    """Get a single seminar."""
    
    # Get seminar
    seminar = db.query(database.Seminar).filter(
        database.Seminar.id == seminar_id,
        database.Seminar.project_id == project_id
    ).first()
    
    if not seminar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seminar not found"
        )
    
    return {
        "id": seminar.id,
        "index": seminar.index,
        "project_id": seminar.project_id,
        "name": seminar.name,
        "description": seminar.description,
        "created_at": seminar.created_at.isoformat(),
        "updated_at": seminar.updated_at.isoformat()
    }


@router.put("/seminars/{seminar_id}")
def update_seminar(
    seminar_id: str,
    update_data: SeminarUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update a seminar."""
    
    # Get seminar
    seminar = db.query(database.Seminar).filter(
        database.Seminar.id == seminar_id
    ).first()
    
    if not seminar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seminar not found"
        )
    
    # Update fields
    if update_data.name is not None:
        seminar.name = update_data.name
    if update_data.description is not None:
        seminar.description = update_data.description
    
    seminar.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(seminar)
    
    # Log event
    events.log_event(user_id, f"seminar:{seminar.project_id}", f"Updated seminar: {seminar.name}")
    
    return {
        "id": seminar.id,
        "index": seminar.index,
        "project_id": seminar.project_id,
        "name": seminar.name,
        "description": seminar.description,
        "created_at": seminar.created_at.isoformat(),
        "updated_at": seminar.updated_at.isoformat()
    }


@router.delete("/seminars/{seminar_id}")
def delete_seminar(
    seminar_id: str,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Delete a seminar."""
    
    # Get seminar
    seminar = db.query(database.Seminar).filter(
        database.Seminar.id == seminar_id
    ).first()
    
    if not seminar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seminar not found"
        )
    
    # Store seminar name for logging before deletion
    seminar_name = seminar.name
    project_id = seminar.project_id
    
    # Delete seminar (cascade will handle related records)
    db.delete(seminar)
    db.commit()
    
    # Log event
    events.log_event(user_id, f"seminar:{project_id}", f"Deleted seminar: {seminar_name}")
    
    return {
        "message": f"Seminar {seminar_id} deleted successfully",
        "id": seminar_id
    }


@router.put("/seminars/{seminar_id}/world-state")
def update_world_state(
    seminar_id: str,
    update_data: WorldStateUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update the latest world state for a seminar."""
    
    # Get seminar
    seminar = db.query(database.Seminar).filter(
        database.Seminar.id == seminar_id
    ).first()
    
    if not seminar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seminar not found"
        )
    
    # Get latest world state mapping for this seminar
    latest_mapping = db.query(database.SeminarWorldState).filter(
        database.SeminarWorldState.seminar_id == seminar_id
    ).order_by(database.SeminarWorldState.created_at.desc()).first()
    
    if not latest_mapping or not latest_mapping.world_state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No world state found for this seminar"
        )
    
    world_state = latest_mapping.world_state
    
    # Update fields
    if update_data.summary_state is not None:
        world_state.summary_state = update_data.summary_state
    if update_data.detailed_state is not None:
        world_state.detailed_state = update_data.detailed_state
    
    world_state.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(world_state)
    
    # Log event
    events.log_event(user_id, f"seminar:{seminar.project_id}", f"Updated world state for seminar: {seminar.name}")
    
    return {
        "id": world_state.id,
        "project_id": world_state.project_id,
        "summary_state": world_state.summary_state,
        "detailed_state": world_state.detailed_state,
        "created_at": world_state.created_at.isoformat(),
        "updated_at": world_state.updated_at.isoformat()
    }

