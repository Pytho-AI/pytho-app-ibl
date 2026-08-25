"""World State generation utilities using LLM."""
import json
import logging
import traceback
from typing import List, Optional, Dict, Any

from utils import llm, database
from utils.file_handler import get_context_from_attachments, find_bucket_for_file

logger = logging.getLogger(__name__)


def get_past_world_states(db, seminar_id: str, limit: int = 3) -> List[Dict[str, Any]]:
    """
    Get past world states for a specific seminar through the mapping table.
    
    Args:
        db: Database session
        seminar_id: Seminar ID to get world states for
        limit: Maximum number of past states to retrieve
    
    Returns:
        List of past world state dicts
    """
    # Query through the mapping table to get world states for this seminar
    mappings = (db.query(database.SeminarWorldState)
        .filter(database.SeminarWorldState.seminar_id == seminar_id)
        .order_by(database.SeminarWorldState.created_at.desc())
        .limit(limit)
        .all())
    
    result = []
    for mapping in mappings:
        state = mapping.world_state
        if state:
            result.append({
                "summary_state": state.summary_state,
                "detailed_state": state.detailed_state,
                "created_at": mapping.created_at.isoformat() if mapping.created_at else None
            })
    
    return result


def get_current_world_state(db, seminar_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the current (latest) world state for a seminar.
    
    Args:
        db: Database session
        seminar_id: Seminar ID
    
    Returns:
        Current world state dict or None
    """
    mapping = (db.query(database.SeminarWorldState)
        .filter(database.SeminarWorldState.seminar_id == seminar_id)
        .order_by(database.SeminarWorldState.created_at.desc())
        .first())
    
    if not mapping or not mapping.world_state:
        return None
    
    state = mapping.world_state
    return {
        "id": state.id,
        "summary_state": state.summary_state,
        "detailed_state": state.detailed_state,
        "created_at": state.created_at.isoformat() if state.created_at else None
    }


def get_sent_injects(db, project_id: str, seminar_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Get sent injects for a project, optionally filtered by seminar.
    
    Args:
        db: Database session
        project_id: Project ID
        seminar_id: Optional seminar ID to filter injects for that specific seminar
    
    Returns:
        List of inject dicts with title, description, and files
    """
    injects = (db.query(database.SeminarInject)
        .filter(
            database.SeminarInject.project_id == project_id,
            database.SeminarInject.status == 'sent'
        )
        .order_by(database.SeminarInject.index.asc())
        .all())
    
    result = []
    for inject in injects:
        # If seminar_id is provided, filter to only include injects for that seminar
        if seminar_id:
            seminar_ids = inject.seminar_ids if inject.seminar_ids else []
            # Include if "all" or if seminar_id is in the list
            if seminar_ids != ["all"] and seminar_id not in seminar_ids:
                continue
        
        result.append({
            "index": inject.index,
            "title": inject.title,
            "description": inject.description,
            "files": inject.files  # Array of {key, url, name} or None
        })
    
    return result


def get_approved_coa(db, seminar_id: str) -> Optional[Dict[str, Any]]:
    """Get the latest approved COA for a seminar.
    
    Args:
        db: Database session
        seminar_id: The seminar ID to get approved COA for
        
    Returns:
        Dict with approved COA details or None if no approved COA exists
    """
    # Find the most recent COA submission with an approved_coa for this seminar
    submission = (db.query(database.COASubmission)
        .filter(
            database.COASubmission.seminar_id == seminar_id,
            database.COASubmission.approved_coa.isnot(None)
        )
        .order_by(database.COASubmission.updated_at.desc())
        .first())
    
    if not submission or not submission.approved_coa:
        return None
    
    return {
        "approved_coa": submission.approved_coa,
        "coa_options": submission.coa_options,
        "status": submission.status,
        "approved_at": submission.updated_at.isoformat() if submission.updated_at else None
    }


def get_project_data(db, project_id: str) -> Dict[str, Any]:
    """Get project data from database."""
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()
    
    if not project:
        return {}
    
    try:
        return json.loads(project.data) if isinstance(project.data, str) else project.data
    except (json.JSONDecodeError, TypeError):
        return {}


def build_world_state_prompt(
    project_data: Dict[str, Any],
    past_world_states: List[Dict[str, Any]],
    file_context: Optional[str] = None,
    sent_injects: Optional[List[Dict[str, Any]]] = None,
    inject_file_context: Optional[Dict[str, str]] = None,
    approved_coa_data: Optional[Dict[str, Any]] = None
) -> List[Dict[str, str]]:
    """Build the LLM prompt for generating a world state."""
    
    system_prompt = """You are an expert scenario designer for tabletop exercises. Generate a world state based on the project description, sent injects, approved course of action decisions, and uploaded documents.

Output Format - respond with JSON only:
{
    "summary_state": "A 5-6 sentence summary of the current situation",
    "detailed_state": "A comprehensive description of the current world state. Make it as detailed as possible."
}"""

    # Extract fields from project data
    name = project_data.get('name', 'Unknown')
    description = project_data.get('description', '')

    user_content = f"""## Project
Name: {name}
Description: {description}
"""

    
    # Add sent injects with embedded file context
    if sent_injects:
        user_content += "\n## Sent Injects\n"
        user_content += "These injects have been sent to participants and should inform the current world state:\n\n"
        for inject in sent_injects:
            inject_index = inject.get('index', '?')
            user_content += f"### Inject {inject_index}: {inject.get('title', 'Untitled')}\n"
            if inject.get('description'):
                user_content += f"{inject['description']}\n"
            if inject.get('files'):
                file_names = [f.get('name', 'Unknown file') for f in inject['files']]
                user_content += f"Attached files: {', '.join(file_names)}\n"
            
            # Embed file context for this inject
            if inject_file_context and inject_index in inject_file_context:
                user_content += f"\n**File Contents:**\n{inject_file_context[inject_index]}\n"
            
            user_content += "\n"

    # Add approved COA decision
    if approved_coa_data:
        user_content += "\n## Approved Course of Action Decision\n"
        user_content += "The White Cell has reviewed and approved the following course of action. This decision should significantly influence the current world state:\n\n"
        
        approved_coa = approved_coa_data.get('approved_coa', {})
        if isinstance(approved_coa, dict):
            user_content += f"**Approved COA:** {approved_coa.get('title', 'Unknown')}\n"
            if approved_coa.get('description'):
                user_content += f"**Description:** {approved_coa['description']}\n"
        
                user_content += "\n**IMPORTANT - 1st Order Effects:** The approved course of action is now being implemented. Update the world state to reflect the immediate, direct consequences of this decision taking effect. Consider:\n"
                user_content += "- What changes occur as a direct result of implementing this COA?\n"
                user_content += "- How do key actors/entities respond immediately to this action?\n"
                user_content += "- What resources are being deployed or affected?\n"
                user_content += "- What is the immediate operational impact on the situation?\n"
        
        # if approved_coa_data.get('approved_at'):
        #     user_content += f"**Approved At:** {approved_coa_data['approved_at']}\n"
        
        # # Include other COA options that were considered
        # coa_options = approved_coa_data.get('coa_options', [])
        # if coa_options and len(coa_options) > 1:
        #     user_content += "\n**Other Considered Options:**\n"
        #     for opt in coa_options:
        #         if isinstance(opt, dict) and opt.get('title') != approved_coa.get('title'):
        #             user_content += f"- {opt.get('title', 'Unknown')}\n"
        
        user_content += "\n"

    # Add past world states
    if past_world_states:
        user_content += "\n## Previous World States\n"
        for state in past_world_states:
            user_content += f"""
From {state.get('created_at', 'Unknown')}:
Summary: {state.get('summary_state', '')}
Details: {state.get('detailed_state', '')}
---
"""

    # Add file context from docs
    if file_context:
        user_content += f"\n## Project Document Context\n{file_context}\n"

    user_content += "\nGenerate an updated world state as JSON."

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content}
    ]


def parse_response(response: str) -> Dict[str, str]:
    """Parse LLM response to extract world state fields."""
    try:
        start = response.find('{')
        end = response.rfind('}') + 1
        if start != -1 and end > start:
            parsed = json.loads(response[start:end])
            return {
                "summary_state": parsed.get("summary_state", "").strip(),
                "detailed_state": parsed.get("detailed_state", "").strip()
            }
    except json.JSONDecodeError:
        pass
    
    return {
        "summary_state": response[:200].strip() + "..." if len(response) > 200 else response.strip(),
        "detailed_state": response.strip()
    }


def load_docs_context(docs: List[Dict[str, Any]], s3_bucket: Optional[str] = None) -> str:
    """
    Load context from project docs array.
    
    Args:
        docs: List of doc objects with key, url, name
        s3_bucket: Optional S3 bucket name
    
    Returns:
        Combined context string from all docs
    """
    if not docs:
        return ""
    
    # Try to find bucket if not provided
    bucket = s3_bucket
    if not bucket and docs:
        first_doc = docs[0]
        if first_doc.get('key'):
            bucket = find_bucket_for_file(first_doc['key'])
    
    if not bucket:
        logger.warning("No S3 bucket found for loading docs")
        return ""
    
    # Create attachment proxies for file handler
    class AttachmentProxy:
        def __init__(self, doc):
            self.file_key = doc.get('key', '')
            self.file_name = doc.get('name', 'document')
    
    attachments = [AttachmentProxy(doc) for doc in docs]
    result = get_context_from_attachments(attachments, bucket, "Project Document")
    
    return result.get("context", "")


def load_inject_files_context(sent_injects: List[Dict[str, Any]], s3_bucket: Optional[str] = None) -> Dict[str, str]:
    """
    Load context from files attached to sent injects.
    
    Args:
        sent_injects: List of inject dicts with files array
        s3_bucket: Optional S3 bucket name
    
    Returns:
        Dict mapping inject index (string) to file context string
    """
    result: Dict[str, str] = {}
    
    # Find bucket from first file if not provided
    bucket = s3_bucket
    if not bucket:
        for inject in sent_injects:
            files = inject.get('files')
            if files:
                first_file = files[0]
                if first_file.get('key'):
                    bucket = find_bucket_for_file(first_file['key'])
                    break
    
    if not bucket:
        # No bucket found, return empty result
        return result
    
    # Create attachment proxy class
    class AttachmentProxy:
        def __init__(self, file_info):
            self.file_key = file_info.get('key', '')
            self.file_name = file_info.get('name', 'document')
    
    # Process each inject's files
    for inject in sent_injects:
        inject_index = inject.get('index')
        files = inject.get('files')
        
        if not files or inject_index is None:
            continue
        
        # Load context for all files of this inject
        attachments = [AttachmentProxy(f) for f in files]
        context_result = get_context_from_attachments(attachments, bucket, "Attachment")
        
        if context_result.get("context"):
            result[inject_index] = context_result["context"]
    
    return result



def get_current_world_state_groups(db, project_id: str, target_seminar_ids: Optional[List[str]] = None) -> Dict[str, List[str]]:
    """
    Get groups of seminars that share the same current world state.
    
    Args:
        db: Database session
        project_id: Project ID
        target_seminar_ids: Optional list of seminar IDs to filter. If None, gets all seminars.
    
    Returns:
        Dict mapping world_state_id -> list of seminar_ids that share it
    """
    # Get all seminars for the project
    query = db.query(database.Seminar).filter(
        database.Seminar.project_id == project_id
    )
    if target_seminar_ids:
        query = query.filter(database.Seminar.id.in_(target_seminar_ids))
    
    seminars = query.all()
    
    # Group seminars by their current world state
    world_state_groups: Dict[str, List[str]] = {}
    seminars_without_state: List[str] = []
    
    for seminar in seminars:
        # Get the current (latest) world state mapping for this seminar
        latest_mapping = (db.query(database.SeminarWorldState)
            .filter(database.SeminarWorldState.seminar_id == seminar.id)
            .order_by(database.SeminarWorldState.created_at.desc())
            .first())
        
        if latest_mapping:
            ws_id = latest_mapping.world_state_id
            if ws_id not in world_state_groups:
                world_state_groups[ws_id] = []
            world_state_groups[ws_id].append(seminar.id)
        else:
            seminars_without_state.append(seminar.id)
    
    # Seminars without a world state form their own group
    if seminars_without_state:
        world_state_groups["__none__"] = seminars_without_state
    
    return world_state_groups


def create_world_state_for_project(
    db,
    project_id: str,
    s3_bucket: Optional[str] = None,
    seminar_ids: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    Generate and save world states for seminars in a project.
    
    Groups seminars by their current world state and generates new world states
    for each group based on their specific inject history.
    
    If seminar_ids is None (meaning "all" seminars), updates all seminars.
    If seminar_ids is a specific list, only updates those seminars.
    
    Seminars that currently share a world state will get a new shared world state
    (maintaining their grouping). This preserves the branching structure.
    
    Args:
        db: Database session
        project_id: Project ID
        s3_bucket: Optional S3 bucket for file loading
        seminar_ids: Optional list of specific seminar IDs to update.
    
    Returns:
        List of created world state records
    """
    # Get project data
    project_data = get_project_data(db, project_id)
    if not project_data:
        raise ValueError(f"Project {project_id} not found")
    
    # Load file context from docs (shared across all)
    docs = project_data.get('docs', [])
    file_context = load_docs_context(docs, s3_bucket)
    
    # Get current world state groups
    world_state_groups = get_current_world_state_groups(db, project_id, seminar_ids)
    
    created_mappings = []
    
    if not world_state_groups:
        # No seminars, but still save a world state for the project
        logger.info(f"No seminars found for project {project_id}, creating project-level world state")
        try:
            # Build prompt with just project data and file context (no injects or COAs)
            messages = build_world_state_prompt(
                project_data, 
                past_world_states=[], 
                file_context=file_context,
                sent_injects=[],
                inject_file_context={},
                approved_coa_data=None
            )
            
            # Call LLM
            prompt = "\n\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in messages])
            response = llm.fetch_llm_response(prompt)
            if response.startswith("[ERROR]"):
                raise Exception(response)
            
            # Parse response
            world_state_data = parse_response(response)
            
            # Create new world state for the project (no seminar mappings)
            new_state = database.WorldState(
                project_id=project_id,
                summary_state=world_state_data["summary_state"],
                detailed_state=world_state_data["detailed_state"]
            )
            db.add(new_state)
            db.commit()
            
            created_mappings.append({
                "id": new_state.id,
                "seminar_ids": [],
                "summary_state": new_state.summary_state,
                "detailed_state": new_state.detailed_state,
                "created_at": new_state.created_at.isoformat() if new_state.created_at else None
            })
            
            logger.info(f"Created project-level world state {new_state.id} for project {project_id}")
        except Exception as e:
            error_msg = f"Failed to create project-level world state for project {project_id}: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
        
        return created_mappings
    
    # Process each group of seminars that share a world state
    for current_ws_id, group_seminar_ids in world_state_groups.items():
        try:
            # Get the current world state for context (if exists)
            past_states = []
            if current_ws_id != "__none__":
                current_ws = db.query(database.WorldState).filter(
                    database.WorldState.id == current_ws_id
                ).first()
                if current_ws:
                    past_states = [{
                        "summary_state": current_ws.summary_state,
                        "detailed_state": current_ws.detailed_state,
                        "created_at": current_ws.created_at.isoformat() if current_ws.created_at else None
                    }]
            
            # Get sent injects for these specific seminars
            # Use the first seminar to get the inject list (they share the same world state,
            # so they should have the same inject history)
            first_seminar_id = group_seminar_ids[0]
            sent_injects = get_sent_injects(db, project_id, first_seminar_id)
            inject_file_context = load_inject_files_context(sent_injects, s3_bucket) if sent_injects else {}
            
            # Get approved COA for this seminar group (if any)
            approved_coa_data = get_approved_coa(db, first_seminar_id)
            
            logger.info(f"Building world state for group of {len(group_seminar_ids)} seminars with {len(sent_injects)} injects, approved_coa={bool(approved_coa_data)}")
            
            # Build prompt
            messages = build_world_state_prompt(
                project_data, 
                past_states, 
                file_context,
                sent_injects,
                inject_file_context,
                approved_coa_data
            )
            
            # Call LLM
            prompt = "\n\n".join([f"{msg['role'].upper()}: {msg['content']}" for msg in messages])
            response = llm.fetch_llm_response(prompt)
            if response.startswith("[ERROR]"):
                raise Exception(response)
            
            # Parse response
            world_state_data = parse_response(response)
            
            # Create new shared world state for this group
            new_state = database.WorldState(
                project_id=project_id,
                summary_state=world_state_data["summary_state"],
                detailed_state=world_state_data["detailed_state"]
            )
            db.add(new_state)
            db.flush()  # Get the ID
            
            # Create mappings for all seminars in this group
            for sem_id in group_seminar_ids:
                mapping = database.SeminarWorldState(
                    seminar_id=sem_id,
                    world_state_id=new_state.id
                )
                db.add(mapping)
            
            db.commit()
            
            created_mappings.append({
                "id": new_state.id,
                "seminar_ids": group_seminar_ids,
                "summary_state": new_state.summary_state,
                "detailed_state": new_state.detailed_state,
                "created_at": new_state.created_at.isoformat() if new_state.created_at else None
            })
            
            logger.info(f"Created world state {new_state.id} for {len(group_seminar_ids)} seminars")
            
        except Exception as e:
            error_msg = f"Failed to create world state for seminar group {group_seminar_ids}: {str(e)}"
            logger.error(error_msg)
            raise Exception(error_msg)
    
    logger.info(f"Created {len(created_mappings)} world states for project {project_id}")
    return created_mappings
