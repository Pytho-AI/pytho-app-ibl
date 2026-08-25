"""Chat message endpoints for AAR."""
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Literal
from utils import database
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatCreate(BaseModel):
    message: str
    role: Literal['user', 'assistant']
    type: Literal['message', 'summary'] = 'message'


class ChatResponse(BaseModel):
    id: int
    aar_id: int
    message: str
    role: str
    type: str
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/aar/{aar_id}/chat", response_model=ChatResponse, status_code=status.HTTP_201_CREATED)
def add_chat_message(
    aar_id: int,
    chat_data: ChatCreate,
    db: Session = Depends(database.get_db)
):
    """Add a chat message to an AAR"""
    try:
        # Verify AAR exists
        aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
        if not aar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="AAR not found"
            )
        
        new_chat = database.AARChat(
            aar_id=aar_id,
            message=chat_data.message,
            role=chat_data.role,
            type=chat_data.type
        )
        db.add(new_chat)
        db.commit()
        db.refresh(new_chat)
        logger.info(f"Added chat message to AAR {aar_id} (type: {chat_data.type})")
        return new_chat
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create chat message: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create chat message: {str(e)}"
        )


@router.get("/aar/{aar_id}/chat", response_model=List[ChatResponse])
def get_chat_history(
    aar_id: int,
    db: Session = Depends(database.get_db)
):
    """Get all chat messages for an AAR"""
    # Verify AAR exists
    aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
    if not aar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AAR not found"
        )
    
    # Get all chat messages ordered by creation time
    chat_messages = db.query(database.AARChat)\
        .filter(database.AARChat.aar_id == aar_id)\
        .order_by(database.AARChat.created_at.asc())\
        .all()
    
    return chat_messages


@router.delete("/aar/{aar_id}/chat/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_message(
    aar_id: int,
    chat_id: int,
    db: Session = Depends(database.get_db)
):
    """Delete a specific chat message"""
    chat = db.query(database.AARChat)\
        .filter(
            database.AARChat.id == chat_id,
            database.AARChat.aar_id == aar_id
        ).first()
    
    if not chat:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Chat message not found"
        )
    
    try:
        db.delete(chat)
        db.commit()
        logger.info(f"Deleted chat message: {chat_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete chat message: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete chat message: {str(e)}"
        )
