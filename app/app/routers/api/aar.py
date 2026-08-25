"""AAR (After Action Review) endpoints."""
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime
from typing import Optional
import json
from utils import database, events
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class AARCreate(BaseModel):
    name: str
    description: Optional[str] = None
    event_id: str
    user_id: str
    type: Optional[str] = "audio"  # "audio" or "text"
    text_content: Optional[str] = None  # For manually entered text observations
    file_key: Optional[str] = None  # File reference for uploaded documents
    file_url: Optional[str] = None  # File URL
    file_name: Optional[str] = None  # Original filename


class AARUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    event_id: Optional[str] = None
    user_id: Optional[str] = None
    type: Optional[str] = None
    text_content: Optional[str] = None
    file_key: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None


class AARResponse(BaseModel):
    id: int
    name: str
    description: Optional[str]
    event_id: str
    user_id: str
    type: str
    text_content: Optional[str] = None
    file_key: Optional[str] = None
    file_url: Optional[str] = None
    file_name: Optional[str] = None
    contributor_name: Optional[str] = None
    contributor_role: Optional[str] = None  # "white" or "participant"
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class AARStatsResponse(BaseModel):
    total_aars: int
    unique_contributors: int


def get_contributor_info(db: Session, user_id: str) -> tuple[Optional[str], Optional[str]]:
    user = db.query(database.User).filter(database.User.id == user_id).first()
    if user:
        # Parse role from JSON blob
        try:
            role_data = json.loads(user.role) if isinstance(user.role, str) else user.role
            role = role_data.get("role", "participant")
        except:
            role = "participant"
        return user.name, role
    return None, None


@router.post("/aar", response_model=AARResponse, status_code=status.HTTP_201_CREATED)
def create_aar(
    aar_data: AARCreate,
    db: Session = Depends(database.get_db)
):
    try:
        new_aar = database.AAR(
            name=aar_data.name.strip(),
            description=aar_data.description.strip() if aar_data.description else None,
            event_id=aar_data.event_id.strip(),
            user_id=aar_data.user_id.strip(),
            type=aar_data.type or "audio",
            text_content=aar_data.text_content.strip() if aar_data.text_content else None,
            file_key=aar_data.file_key,
            file_url=aar_data.file_url,
            file_name=aar_data.file_name
        )
        db.add(new_aar)
        db.commit()
        db.refresh(new_aar)
        
        # Log event (non-blocking - don't fail AAR creation if logging fails)
        try:
            events.log_event(new_aar.user_id, f"event:{new_aar.event_id}", f"Created aar {new_aar.name}")
        except Exception as log_error:
            logger.error(f"Failed to log AAR creation event: {str(log_error)}")
        
        # Get contributor name and role
        contributor_name, contributor_role = get_contributor_info(db, new_aar.user_id)
        
        return AARResponse(
            id=new_aar.id,
            name=new_aar.name,
            description=new_aar.description,
            event_id=new_aar.event_id,
            user_id=new_aar.user_id,
            type=new_aar.type,
            text_content=new_aar.text_content,
            file_key=new_aar.file_key,
            file_url=new_aar.file_url,
            file_name=new_aar.file_name,
            contributor_name=contributor_name,
            contributor_role=contributor_role,
            created_at=new_aar.created_at,
            updated_at=new_aar.updated_at
        )
    
    except HTTPException:
        # Re-raise HTTP exceptions
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create AAR: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred while creating the AAR: {str(e)}"
        )


@router.put("/aar/{aar_id}", response_model=AARResponse)
def update_aar(
    aar_id: int,
    aar_data: AARUpdate,
    db: Session = Depends(database.get_db)
):
    """Update an existing AAR"""
    try:
        # Find the AAR
        aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
        if not aar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="AAR not found"
            )
        
        # Update fields if provided
        if aar_data.name is not None:
            aar.name = aar_data.name.strip()
        if aar_data.description is not None:
            aar.description = aar_data.description.strip() if aar_data.description else None
        if aar_data.event_id is not None:
            aar.event_id = aar_data.event_id.strip()
        if aar_data.user_id is not None:
            aar.user_id = aar_data.user_id.strip()
        if aar_data.type is not None:
            aar.type = aar_data.type
        if aar_data.text_content is not None:
            aar.text_content = aar_data.text_content.strip() if aar_data.text_content else None
        if aar_data.file_key is not None:
            aar.file_key = aar_data.file_key
        if aar_data.file_url is not None:
            aar.file_url = aar_data.file_url
        if aar_data.file_name is not None:
            aar.file_name = aar_data.file_name
        
        # Update the updated_at timestamp
        aar.updated_at = datetime.utcnow()
        
        db.commit()
        db.refresh(aar)
        
        # Log event (non-blocking - don't fail AAR update if logging fails)
        try:
            events.log_event(aar.user_id, f"event:{aar.event_id}", f"Updated aar {aar.name}")
        except Exception as log_error:
            logger.error(f"Failed to log AAR update event: {str(log_error)}")
        
        # Get contributor name and role
        contributor_name, contributor_role = get_contributor_info(db, aar.user_id)
        
        return AARResponse(
            id=aar.id,
            name=aar.name,
            description=aar.description,
            event_id=aar.event_id,
            user_id=aar.user_id,
            type=aar.type,
            text_content=aar.text_content,
            file_key=aar.file_key,
            file_url=aar.file_url,
            file_name=aar.file_name,
            contributor_name=contributor_name,
            contributor_role=contributor_role,
            created_at=aar.created_at,
            updated_at=aar.updated_at
        )
    
    except HTTPException:
        # Re-raise HTTP exceptions
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to update AAR: {str(e)}", exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An unexpected error occurred while updating the AAR: {str(e)}"
        )


@router.get("/aar", response_model=list[AARResponse])
def list_aars_by_project(
    event_id: str,
    user_id: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(database.get_db)
):
    """List AARs for a project. Optionally filter by user_id for participant view."""
    query = db.query(database.AAR)
    
    # Filter by event_id (required)
    query = query.filter(database.AAR.event_id == event_id)
    
    # Filter by user_id if provided (for participant view)
    if user_id:
        query = query.filter(database.AAR.user_id == user_id)
    
    # Order by most recent first
    aars = query.order_by(database.AAR.created_at.desc()).offset(skip).limit(limit).all()
    
    # Build response with contributor names and roles
    result = []
    for aar in aars:
        contributor_name, contributor_role = get_contributor_info(db, aar.user_id)
        result.append(AARResponse(
            id=aar.id,
            name=aar.name,
            description=aar.description,
            event_id=aar.event_id,
            user_id=aar.user_id,
            type=aar.type,
            text_content=aar.text_content,
            file_key=aar.file_key,
            file_url=aar.file_url,
            file_name=aar.file_name,
            contributor_name=contributor_name,
            contributor_role=contributor_role,
            created_at=aar.created_at,
            updated_at=aar.updated_at
        ))
    
    return result


@router.get("/aar/stats/{event_id}", response_model=AARStatsResponse)
def get_aar_stats(
    event_id: str,
    user_id: Optional[str] = None,
    db: Session = Depends(database.get_db)
):
    """Get aggregate statistics for AARs in an event."""
    query = db.query(database.AAR).filter(database.AAR.event_id == event_id)
    
    # Filter by user_id if provided (for participant view)
    if user_id:
        query = query.filter(database.AAR.user_id == user_id)
    
    # Get total count
    total_aars = query.count()
    
    # Get unique contributors
    unique_contributors = db.query(func.count(func.distinct(database.AAR.user_id))).filter(
        database.AAR.event_id == event_id
    )
    if user_id:
        unique_contributors = unique_contributors.filter(database.AAR.user_id == user_id)
    unique_contributors = unique_contributors.scalar() or 0
    
    return AARStatsResponse(
        total_aars=total_aars,
        unique_contributors=unique_contributors
    )


@router.get("/aar/{aar_id}", response_model=AARResponse)
def get_aar(
    aar_id: int,
    db: Session = Depends(database.get_db)
):
    """Get a specific AAR by ID"""
    aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
    if not aar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AAR not found"
        )
    
    contributor_name, contributor_role = get_contributor_info(db, aar.user_id)
    
    return AARResponse(
        id=aar.id,
        name=aar.name,
        description=aar.description,
        event_id=aar.event_id,
        user_id=aar.user_id,
        type=aar.type,
        text_content=aar.text_content,
        file_key=aar.file_key,
        file_url=aar.file_url,
        file_name=aar.file_name,
        contributor_name=contributor_name,
        contributor_role=contributor_role,
        created_at=aar.created_at,
        updated_at=aar.updated_at
    )


@router.delete("/aar/{aar_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_aar(
    aar_id: int,
    db: Session = Depends(database.get_db)
):
    """Delete an AAR and all associated data (cascade)"""
    aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
    if not aar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AAR not found"
        )
    # Log event (non-blocking - don't fail AAR creation if logging fails)
    try:
        events.log_event(aar.user_id, f"event:{aar.event_id}", f"Deleted aar {aar.name}")
    except Exception as log_error:
        logger.error(f"Failed to log AAR deletion event: {str(log_error)}")
    
    try:
        db.delete(aar)
        db.commit()
        logger.info(f"Deleted AAR: {aar_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete AAR: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete AAR: {str(e)}"
        )
