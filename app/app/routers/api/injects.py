"""Seminar Inject API endpoints."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime
import asyncio
import logging
import json
import re

from utils import database
from utils import events
from utils import world_state
from utils import notifications
from utils import llm
from utils import slack

logger = logging.getLogger(__name__)

router = APIRouter()


def _safe_broadcast(coro):
    """Safely create an async task for broadcasting with error handling."""
    try:
        task = asyncio.create_task(coro)
        def handle_task_error(task):
            try:
                task.result()
            except Exception as e:
                logging.error(f"Error in inject broadcast task: {e}", exc_info=True)
        task.add_done_callback(handle_task_error)
    except RuntimeError:
        logging.warning("No running event loop for inject broadcast")


# File reference model
class FileRef(BaseModel):
    key: str
    url: str
    name: str


# Pydantic models for request/response
class InjectCreate(BaseModel):
    project_id: str
    index: str  # User-provided inject identifier (e.g., "1", "1A", "2B")
    seminar_ids: List[str]  # List of seminar IDs or ["all"] for all seminars
    title: str
    description: Optional[str] = None
    inject_type: Optional[str] = None
    status: Optional[str] = None
    files: Optional[List[FileRef]] = None


class InjectUpdate(BaseModel):
    project_id: Optional[str] = None
    index: Optional[str] = None  # User-provided inject identifier (e.g., "1", "1A", "2B")
    seminar_ids: Optional[List[str]] = None  # List of seminar IDs or ["all"] for all seminars
    title: Optional[str] = None
    description: Optional[str] = None
    inject_type: Optional[str] = None
    status: Optional[str] = None
    files: Optional[List[FileRef]] = None


class InjectResponse(BaseModel):
    id: str
    index: str  # User-provided inject identifier
    project_id: str
    seminar_ids: List[str]
    title: str
    description: Optional[str]
    inject_type: Optional[str]
    status: Optional[str]
    files: Optional[List[FileRef]]
    created_at: str
    updated_at: str


class GenerateInjectRequest(BaseModel):
    """Request model for AI inject generation."""
    project_id: str
    objective: str  # e.g., "stress logistics", "introduce ambiguity", "test ISR prioritization"
    inject_type: str  # e.g., "intel", "news", "text", "tweet", "facebook", "audio", "video"
    seminar_ids: List[str]  # List of seminar IDs or ["all"] for all seminars


class GeneratedInjectResponse(BaseModel):
    """Response model for AI-generated inject content."""
    title: str
    description: str
    inject_type: str
    rationale: str


def get_inject_type_format(inject_type: str) -> str:
    """Get formatting instructions based on inject type."""
    formats = {
        "intel": """Format as an intelligence report with:
- SITUATION section describing the observed events
- ASSESSMENT section with analysis and implications
- RECOMMENDATION section with suggested actions""",
        "news": """Format as a news article with:
- Headline
- Dateline (location and date)
- Lead paragraph with key facts
- Body with supporting details and quotes""",
        "text": """Format as a plain text message or communication:
- Clear subject or topic
- Concise but informative content
- Relevant details for decision-making""",
        "tweet": """Format as a social media post (Twitter/X style):
- Short, punchy message (under 280 characters ideally)
- Include hashtags if relevant
- Could include @mentions""",
        "facebook": """Format as a Facebook post:
- More detailed than a tweet
- Can include personal perspective
- Natural, conversational tone""",
        "audio": """Format as an audio transcript:
- Speaker identification if multiple speakers
- Natural spoken language
- Include [pause], [static], or other audio cues as relevant""",
        "video": """Format as a video description or transcript:
- Scene descriptions in brackets
- Dialogue or narration
- Visual cues where relevant"""
    }
    return formats.get(inject_type, formats["text"])


@router.post("/injects/generate", response_model=GeneratedInjectResponse)
def generate_inject_with_ai(
    request: GenerateInjectRequest,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Generate an inject title and description using AI based on objective, type, and world state context.
    
    This endpoint:
    1. Retrieves the latest world states for the specified seminars
    2. Uses an LLM to generate inject content tailored to the current situation
    3. Returns the generated title, description, and AI rationale
    """
    
    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == request.project_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get seminar IDs to process
    if request.seminar_ids == ["all"]:
        # Get all seminars for the project
        seminars = db.query(database.Seminar).filter(
            database.Seminar.project_id == request.project_id
        ).all()
        seminar_ids_to_process = [s.id for s in seminars]
    else:
        seminar_ids_to_process = request.seminar_ids
    
    if not seminar_ids_to_process:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No seminars found to generate inject for"
        )
    
    # Collect world states for all specified seminars using grouped approach
    world_states_context = []
    world_state_groups = world_state.get_current_world_state_groups(db, request.project_id, seminar_ids_to_process)
    
    for ws_id, group_seminar_ids in world_state_groups.items():
        # Get seminar names for this group
        seminars_in_group = db.query(database.Seminar).filter(
            database.Seminar.id.in_(group_seminar_ids)
        ).all()
        seminar_names = [s.name for s in seminars_in_group]
        
        if ws_id == "__none__":
            # Seminars without a world state
            continue
        
        # Get the world state data
        ws = db.query(database.WorldState).filter(
            database.WorldState.id == ws_id
        ).first()
        
        if ws:
            world_states_context.append({
                "seminar_names": seminar_names,
                "seminar_ids": group_seminar_ids,
                "summary": ws.summary_state or "",
                "detailed": ws.detailed_state or ""
            })
    
    # Build context from world states
    if world_states_context:
        world_state_text = ""
        for ws in world_states_context:
            seminar_label = ", ".join(ws['seminar_names'])
            world_state_text += f"""
--- World State for {seminar_label} ---
Summary: {ws['summary']}

Detailed State:
{ws['detailed']}
"""
    else:
        world_state_text = "No current world state available. Generate content based on the objective alone."
    
    # Get format instructions for the inject type
    format_instructions = get_inject_type_format(request.inject_type)
    
    # Build the LLM prompt
    prompt = f"""You are an exercise controller (White Cell) creating an inject for a military or crisis simulation exercise.

CURRENT WORLD STATE:
{world_state_text}

INJECT OBJECTIVE:
{request.objective}

INJECT TYPE: {request.inject_type}

FORMAT INSTRUCTIONS:
{format_instructions}

YOUR TASK:
Generate an inject that:
1. Aligns with the specified objective
2. Is consistent with the current world state
3. Creates a realistic decision-making challenge for participants
4. Follows the format for the specified inject type

Respond with a JSON object containing:
{{
    "title": "A concise, descriptive title for the inject",
    "description": "The full inject content following the format instructions",
    "rationale": "Brief explanation of why this inject serves the objective and how it relates to the world state"
}}

Respond ONLY with the JSON object, no additional text."""

    try:
        # Call the LLM
        response_text = llm.fetch_llm_response(prompt)
        
        # Parse the JSON response
        # Try to extract JSON from the response (handle potential markdown code blocks)
        json_text = response_text.strip()
        if json_text.startswith("```"):
            # Remove markdown code block
            lines = json_text.split("\n")
            json_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        
        try:
            generated = json.loads(json_text)
        except json.JSONDecodeError:
            # Try to find JSON object in the response
            json_match = re.search(r'\{[^{}]*\}', response_text, re.DOTALL)
            if json_match:
                generated = json.loads(json_match.group())
            else:
                raise ValueError("Could not parse LLM response as JSON")
        
        # Log the generation event
        events.log_event(
            user_id, 
            f"inject:{request.project_id}", 
            f"Generated AI inject: {generated.get('title', 'Untitled')}"
        )
        slack.post_notification({"text": f"Inject generated: {json.dumps(generated)}"})
        
        return GeneratedInjectResponse(
            title=generated.get("title", "Generated Inject"),
            description=generated.get("description", ""),
            inject_type=request.inject_type,
            rationale=generated.get("rationale", "AI-generated inject based on objective and world state.")
        )
        
    except Exception as e:
        logger.error(f"Error generating inject with AI: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate inject: {str(e)}"
        )
    

@router.post("/injects", status_code=status.HTTP_201_CREATED)
def create_inject(
    inject_data: InjectCreate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Create a new inject for one or more seminars. Use ["all"] for all seminars.
    All seminar IDs must belong to the same project."""
    
    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == inject_data.project_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Create inject (JSON column will automatically serialize the list)
    new_inject = database.SeminarInject(
        project_id=inject_data.project_id,
        seminar_ids=inject_data.seminar_ids,
        title=inject_data.title,
        description=inject_data.description,
        inject_type=inject_data.inject_type,
        status=inject_data.status,
        index=inject_data.index,
        files=[f.model_dump() for f in inject_data.files] if inject_data.files else None
    )
    
    db.add(new_inject)
    db.commit()
    db.refresh(new_inject)
    
    # Log event
    events.log_event(user_id, f"inject:{inject_data.project_id}", f"Created inject: {inject_data.title}")
    slack.post_notification({"text": f"Inject created: {json.dumps({'title': inject_data.title, 'description': inject_data.description})}"})
    
    # seminar_ids is already a list (JSON column deserializes automatically)
    return {
        "id": new_inject.id,
        "index": new_inject.index,
        "project_id": new_inject.project_id,
        "seminar_ids": new_inject.seminar_ids,
        "title": new_inject.title,
        "description": new_inject.description,
        "inject_type": new_inject.inject_type,
        "status": new_inject.status,
        "files": new_inject.files,
        "created_at": new_inject.created_at.isoformat(),
        "updated_at": new_inject.updated_at.isoformat()
    }


@router.get("/injects/project/{project_id}")
def get_injects_for_project(
    project_id: str,
    db: Session = Depends(database.get_db)
):
    """Get all injects for a project."""
    
    # Verify project exists
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()
    
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found"
        )
    
    # Get all injects for this project
    injects = db.query(database.SeminarInject).filter(
        database.SeminarInject.project_id == project_id
    ).order_by(
        database.SeminarInject.created_at.asc()
    ).all()
    
    result = []
    for inject in injects:
        seminar_ids_list = inject.seminar_ids if inject.seminar_ids else []
        result.append({
            "id": inject.id,
            "index": inject.index,
            "project_id": inject.project_id,
            "seminar_ids": seminar_ids_list,
            "title": inject.title,
            "description": inject.description,
            "inject_type": inject.inject_type,
            "status": inject.status,
            "files": inject.files,
            "created_at": inject.created_at.isoformat(),
            "updated_at": inject.updated_at.isoformat()
        })
    
    return result


@router.get("/injects/{seminar_id}")
def get_injects_for_seminar(
    seminar_id: str,
    db: Session = Depends(database.get_db)
):
    """Get all injects for a seminar (includes injects with ["all"] and injects that include this seminar_id)."""
    
    # Verify seminar exists
    seminar = db.query(database.Seminar).filter(
        database.Seminar.id == seminar_id
    ).first()
    
    if not seminar:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Seminar not found"
        )
    
    # Get all injects and filter in Python (since we're using JSON storage)
    all_injects = db.query(database.SeminarInject).order_by(
        database.SeminarInject.created_at.asc()
    ).all()
    
    # Filter injects that apply to this seminar
    result = []
    for inject in all_injects:
        # seminar_ids is already a list (JSON column deserializes automatically)
        seminar_ids_list = inject.seminar_ids if inject.seminar_ids else []
        
        # Include if "all" or if seminar_id is in the list
        if seminar_ids_list == ["all"] or seminar_id in seminar_ids_list:
            result.append({
                "id": inject.id,
                "index": inject.index,
                "project_id": inject.project_id,
                "seminar_ids": seminar_ids_list,
                "title": inject.title,
                "description": inject.description,
                "inject_type": inject.inject_type,
                "status": inject.status,
                "files": inject.files,
                "created_at": inject.created_at.isoformat(),
                "updated_at": inject.updated_at.isoformat()
            })
    
    return result


@router.get("/injects/{seminar_id}/{inject_id}")
def get_inject(
    seminar_id: str,
    inject_id: str,
    db: Session = Depends(database.get_db)
):
    """Get a single inject. Verifies it applies to the specified seminar."""
    
    # Get inject
    inject = db.query(database.SeminarInject).filter(
        database.SeminarInject.id == inject_id
    ).first()
    
    if not inject:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inject not found"
        )
    
    # seminar_ids is already a list (JSON column deserializes automatically)
    seminar_ids_list = inject.seminar_ids if inject.seminar_ids else []
    
    # Verify inject applies to this seminar
    if seminar_ids_list != ["all"] and seminar_id not in seminar_ids_list:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inject not found for this seminar"
        )
    
    return {
        "id": inject.id,
        "index": inject.index,
        "project_id": inject.project_id,
        "seminar_ids": seminar_ids_list,
        "title": inject.title,
        "description": inject.description,
        "inject_type": inject.inject_type,
        "status": inject.status,
        "files": inject.files,
        "created_at": inject.created_at.isoformat(),
        "updated_at": inject.updated_at.isoformat()
    }


@router.put("/injects/{inject_id}")
async def update_inject(
    inject_id: str,
    update_data: InjectUpdate,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Update an inject."""

    # Get inject
    inject = db.query(database.SeminarInject).filter(
        database.SeminarInject.id == inject_id
    ).first()

    if not inject:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inject not found"
        )

    # Update project_id if provided
    if update_data.project_id is not None:
        inject.project_id = update_data.project_id

    # Update index if provided
    if update_data.index is not None:
        inject.index = update_data.index

    # Update seminar_ids if provided
    if update_data.seminar_ids is not None:
        inject.seminar_ids = update_data.seminar_ids

    # Update other fields
    if update_data.title is not None:
        inject.title = update_data.title
    if update_data.description is not None:
        inject.description = update_data.description
    if update_data.inject_type is not None:
        inject.inject_type = update_data.inject_type
    if update_data.status is not None:
        inject.status = update_data.status
    if update_data.files is not None:
        inject.files = [f.model_dump() for f in update_data.files]

    inject.updated_at = datetime.utcnow()

    # Send notifications synchronously (fast — just DB inserts) before committing
    # so they're part of the same transaction
    if update_data.status == 'sent':
        inject_seminar_ids = inject.seminar_ids if inject.seminar_ids else []
        try:
            if inject_seminar_ids == ["all"]:
                participants = db.query(database.ProjectMember).filter(
                    database.ProjectMember.project_id == inject.project_id,
                    database.ProjectMember.role == "participant"
                ).all()
            else:
                participants = db.query(database.ProjectMember).filter(
                    database.ProjectMember.project_id == inject.project_id,
                    database.ProjectMember.role == "participant",
                    database.ProjectMember.seminar_id.in_(inject_seminar_ids)
                ).all()

            for participant in participants:
                notifications.create_notification(
                    user_id=participant.user_id,
                    display_text=f"New inject received: {inject.title}",
                    action={
                        "type": "open_inject_modal",
                        "project_id": inject.project_id,
                        "inject_id": inject.id,
                        "inject_index": inject.index,
                        "inject_title": inject.title,
                        "inject_description": inject.description,
                        "inject_files": inject.files,
                        "inject_seminar_ids": inject.seminar_ids
                    },
                    db=db
                )
            logger.info(f"Sent notifications to {len(participants)} participants for inject {inject_id}")
        except Exception as notify_error:
            logger.error(f"Error sending notifications for inject: {notify_error}")

    db.commit()
    db.refresh(inject)

    # Log event
    events.log_event(user_id, f"inject:{inject.project_id}", f"Updated inject: {inject.title}")
    slack.post_notification({"text": f"Inject updated: {json.dumps({'title': inject.title, 'description': inject.description, 'status': inject.status})}"})

    # Capture response before kicking off background work
    response_data = {
        "id": inject.id,
        "index": inject.index,
        "project_id": inject.project_id,
        "seminar_ids": inject.seminar_ids,
        "title": inject.title,
        "description": inject.description,
        "inject_type": inject.inject_type,
        "status": inject.status,
        "files": inject.files,
        "created_at": inject.created_at.isoformat(),
        "updated_at": inject.updated_at.isoformat()
    }

    # Fire-and-forget world state generation in background (LLM call)
    if update_data.status == 'sent':
        inject_project_id = inject.project_id
        inject_seminar_ids = inject.seminar_ids if inject.seminar_ids else []
        target_seminar_ids = None if inject_seminar_ids == ["all"] else inject_seminar_ids

        def _thread_generate_world_state():
            """Runs in thread pool with its own DB session."""
            logger.info(f"[BG-INJECT] Starting background world state generation for inject {inject_id}")
            bg_db = database.SessionLocal()
            try:
                world_state.create_world_state_for_project(bg_db, inject_project_id, seminar_ids=target_seminar_ids)
                logger.info(f"[BG-INJECT] Generated world state after inject {inject_id} sent for seminars: {inject_seminar_ids}")
            except Exception as e:
                logger.error(f"[BG-INJECT] Failed to generate world state for inject {inject_id}: {e}", exc_info=True)
            finally:
                bg_db.close()

        async def _background_generate_world_state():
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, _thread_generate_world_state)

        _safe_broadcast(_background_generate_world_state())

    return response_data


@router.delete("/injects/{inject_id}")
def delete_inject(
    inject_id: str,
    user_id: str,
    db: Session = Depends(database.get_db)
):
    """Delete an inject."""
    
    # Get inject
    inject = db.query(database.SeminarInject).filter(
        database.SeminarInject.id == inject_id
    ).first()
    
    if not inject:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Inject not found"
        )
    
    # Store inject name and project_id for logging before deletion
    inject_title = inject.title
    project_id = inject.project_id
    
    # Delete inject
    db.delete(inject)
    db.commit()
    
    # Log event
    events.log_event(user_id, f"inject:{project_id}", f"Deleted inject: {inject_title}")
    
    return {
        "message": f"Inject {inject_id} deleted successfully",
        "id": inject_id
    }

