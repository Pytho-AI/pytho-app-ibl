"""Transcript endpoints for AAR."""
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session
from datetime import datetime
from typing import List, Optional
from utils import database
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


class TranscriptCreate(BaseModel):
    speaker: str
    timestamp: str
    text: str
    audio_duration: Optional[int] = None


class TranscriptResponse(BaseModel):
    id: int
    aar_id: int
    speaker: str
    timestamp: str
    text: str
    audio_duration: Optional[int]
    created_at: datetime

    class Config:
        from_attributes = True


@router.post("/aar/{aar_id}/transcript", response_model=TranscriptResponse, status_code=status.HTTP_201_CREATED)
def add_transcript_entry(
    aar_id: int,
    transcript_data: TranscriptCreate,
    db: Session = Depends(database.get_db)
):
    """Add a transcript entry to an AAR"""
    try:
        # Verify AAR exists
        aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
        if not aar:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="AAR not found"
            )
        
        new_transcript = database.AARTranscript(
            aar_id=aar_id,
            speaker=transcript_data.speaker,
            timestamp=transcript_data.timestamp,
            text=transcript_data.text,
            audio_duration=transcript_data.audio_duration
        )
        db.add(new_transcript)
        db.commit()
        db.refresh(new_transcript)
        logger.info(f"Added transcript entry to AAR {aar_id}")
        return new_transcript
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to create transcript entry: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create transcript entry: {str(e)}"
        )


@router.get("/aar/{aar_id}/transcript", response_model=List[TranscriptResponse])
def get_transcript_entries(
    aar_id: int,
    db: Session = Depends(database.get_db)
):
    """Get all transcript entries for an AAR"""
    # Verify AAR exists
    aar = db.query(database.AAR).filter(database.AAR.id == aar_id).first()
    if not aar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="AAR not found"
        )
    
    # Get all transcript entries ordered by creation time
    transcripts = db.query(database.AARTranscript)\
        .filter(database.AARTranscript.aar_id == aar_id)\
        .order_by(database.AARTranscript.created_at.asc())\
        .all()
    
    return transcripts


@router.delete("/aar/{aar_id}/transcript/{transcript_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_transcript_entry(
    aar_id: int,
    transcript_id: int,
    db: Session = Depends(database.get_db)
):
    """Delete a specific transcript entry"""
    transcript = db.query(database.AARTranscript)\
        .filter(
            database.AARTranscript.id == transcript_id,
            database.AARTranscript.aar_id == aar_id
        ).first()
    
    if not transcript:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Transcript entry not found"
        )
    
    try:
        db.delete(transcript)
        db.commit()
        logger.info(f"Deleted transcript entry: {transcript_id}")
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to delete transcript entry: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete transcript entry: {str(e)}"
        )
