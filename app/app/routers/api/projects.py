"""Project management endpoints."""
from fastapi import APIRouter, HTTPException, Depends, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import Optional
from utils import database, events, world_state, slack, notifications
import json
import logging
import uuid
import time

logger = logging.getLogger(__name__)

router = APIRouter()


class ProjectIn(BaseModel):
    project_id: str
    user_id: str
    timestamp: int
    data: str


class AddMemberRequest(BaseModel):
    user_id: str
    seminar_id: Optional[str] = None


@router.get("/scenario_datasets")
def scenario_datasets():
    """Shipped scenario dataset slugs — the CreateProject picker's
    source. Each slug is a document under PYTHO_SCENARIO_DATASET_DIR
    exported by scripts/export_scenario_dataset.py."""
    from utils.project_scenario import list_datasets
    return JSONResponse(content={"datasets": list_datasets()})


@router.get("/sessions/{project_id}")
def read_sessions(project_id: str, db: Session = Depends(database.get_db)):
    results = (db.query(database.Session)
        .filter(database.Session.project_id == project_id)
        .order_by(database.Session.timestamp.desc())  # Sort by timestamp (newest first)
        .limit(100)  # Limit to top 100
        .all())
    return [result.data for result in results]


@router.post("/projects")
async def create_project(project: ProjectIn, db: Session = Depends(database.get_db)):
    # async so the bootstrap task can be scheduled on the running loop
    # (a sync endpoint executes in a worker thread with no event loop).
    # Check if project already exists
    existing_project = db.query(database.Project).filter(
        database.Project.project_id == project.project_id
    ).first()
    
    if existing_project:
        raise HTTPException(status_code=400, detail="Project already exists. Use PATCH to update.")
    
    project_data = json.loads(project.data)
    seminar_count = project_data.get("seminars", 0)
    seminar_names = project_data.get("seminarNames", [])

    # The event's scenario dataset is a REQUIRED creation field — an
    # event without scenario data has nothing to produce. Validated
    # up front so a bad request writes nothing.
    scenario_dataset = (project_data.get("scenario") or "").strip()
    from utils.project_scenario import list_datasets
    if not scenario_dataset or scenario_dataset not in list_datasets():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=("project data must carry a valid 'scenario' dataset "
                    f"slug; available: {list_datasets()}"))

    # Create new project
    new_project = database.Project(
        project_id=project.project_id,
        user_id=project.user_id,
        timestamp=project.timestamp,
        seminar_count=seminar_count,
        data=project.data
    )
    db.add(new_project)
    db.commit()

    events.log_event(project.user_id, "event", f"Created  event {project_data['name']}")
    slack.post_notification({"text": f"New project created: {json.dumps(project_data)}"})
    
    # Automatically add the owner to white cell team
    owner_member = database.ProjectMember(
        project_id=project.project_id,
        user_id=project.user_id,
        role="white"
    )
    db.add(owner_member)
    db.commit()
    
    # Create seminars numbered from 1 to n (use custom names if provided)
    for i in range(1, seminar_count + 1):
        name = seminar_names[i - 1] if i <= len(seminar_names) and seminar_names[i - 1] else f"Seminar {i}"
        seminar = database.Seminar(
            project_id=project.project_id,
            index=i,
            name=name
        )
        db.add(seminar)
    db.commit()
    

    # PROJECT BOOTSTRAP — the event's producers all run ONCE, here.
    # Everything analyst-facing is scoped per project/event: the event
    # map's uuid IS the projectId (geo-agent auto-creates the map on
    # first write/open), the artifact pool is the project's own, and
    # scenario data is static until scenario-update machinery exists —
    # so there is no poller and no startup sweep; project creation is
    # the one moment the scenario gets materialized for the event:
    #   1. OOB floor (RED/BLUE units, targets, control measures) onto
    #      the project map.
    #   2. Signal-report autofires (KLIEGLIGHT per SIGINT row, OSIR per
    #      OSINT row) into the project pool.
    #   3. The dep-aware product autodraft sweep into the project pool
    #      (rollup products find their single-INT deps from step 2).
    # Sequential inside one background task — the fires share the writer
    # LLM; the endpoint returns 201 immediately and progress surfaces
    # through /api/products/records like every other chain.
    import asyncio as _asyncio

    _asyncio.create_task(
        bootstrap_project(project.project_id, scenario_dataset),
        name=f"project_bootstrap_{project.project_id}",
    )

    return JSONResponse(content={"project": project.data}, status_code=status.HTTP_201_CREATED)


async def bootstrap_project(pid: str, dataset_slug: str) -> None:
        from handlers import intel_agent as _ia
        from handlers import klieglight_autofire as _kl
        from handlers import osir_autofire as _osir
        from handlers import product_autodraft as _pa
        from utils.project_context import set_active_project
        from utils.project_scenario import seed_project_scenario
        # Scenario FIRST — every producer below reads the project's
        # scenario document; seeding failure aborts the bootstrap loudly.
        seed_project_scenario(pid, dataset_slug)
        set_active_project(pid)
        # World-state generation (wargaming seminars) rides the cloud
        # LLM; on boxes without those credentials it fails here, loudly,
        # WITHOUT blocking event creation — the map/product producers
        # below don't depend on it.
        try:
            from utils.database import SessionLocal as _SessionLocal
            _ws_db = _SessionLocal()
            try:
                world_state.create_world_state_for_project(_ws_db, pid)
            finally:
                _ws_db.close()
        except Exception:
            logger.exception("project bootstrap %s: world-state generation failed", pid)
        try:
            await _ia.push_oob_to_project_map(pid)
        except Exception:
            logger.exception("project bootstrap %s: OOB push failed", pid)
        try:
            await _kl.sweep_for_project(pid)
        except Exception:
            logger.exception("project bootstrap %s: KLIEGLIGHT sweep failed", pid)
        try:
            await _osir.sweep_for_project(pid)
        except Exception:
            logger.exception("project bootstrap %s: OSIR sweep failed", pid)
        try:
            await _pa.sweep_for_project(pid)
        except Exception:
            logger.exception("project bootstrap %s: product sweep failed", pid)
        logger.info("project bootstrap %s: complete", pid)


@router.post("/projects/{project_id}/bootstrap")
async def rerun_project_bootstrap(project_id: str, db: Session = Depends(database.get_db)):
    """Operator re-run of the project bootstrap (scenario seed + OOB push
    + signal sweeps + product sweep). Idempotent: the sweeps dedup via
    the per-project fired logs and the populated pool, and the OOB push
    is a transactional REPLACE. Use after a failed creation-time
    bootstrap or a producer fix."""
    row = db.query(database.Project).filter(
        database.Project.project_id == project_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    try:
        pdata = json.loads(row.data) if isinstance(row.data, str) else (row.data or {})
    except Exception:
        pdata = {}
    dataset_slug = (pdata.get("scenario") or "").strip()
    from utils.project_scenario import list_datasets
    if not dataset_slug or dataset_slug not in list_datasets():
        raise HTTPException(
            status_code=400,
            detail=(f"project carries no valid 'scenario' dataset slug "
                    f"(got {dataset_slug!r}); available: {list_datasets()}"))
    import asyncio as _asyncio
    _asyncio.create_task(
        bootstrap_project(project_id, dataset_slug),
        name=f"project_bootstrap_{project_id}",
    )
    return JSONResponse(content={"ok": True, "status": "bootstrapping",
                                   "project_id": project_id,
                                   "scenario": dataset_slug}, status_code=202)


@router.patch("/projects/{project_id}")
def update_project(project_id: str, project: ProjectIn, db: Session = Depends(database.get_db)):
    # Verify project exists
    existing_project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()
    
    if not existing_project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Verify user has permission (must be the owner)
    if existing_project.user_id != project.user_id:
        raise HTTPException(status_code=403, detail="You do not have permission to update this project")
    
    # Parse old and new project data
    try:
        old_project_data = json.loads(existing_project.data) if isinstance(existing_project.data, str) else existing_project.data
    except (json.JSONDecodeError, TypeError):
        old_project_data = {}
    
    project_data = json.loads(project.data)
    new_seminar_count = project_data.get("seminars", 0)
    current_seminar_count = existing_project.seminar_count or 0
    seminar_names = project_data.get("seminarNames", [])
    
    # Check if docs/files have changed
    old_docs = old_project_data.get("docs", [])
    new_docs = project_data.get("docs", [])
    docs_changed = old_docs != new_docs
    
    # Update project using merge
    db.merge(database.Project(
        project_id=project_id,
        user_id=project.user_id,
        timestamp=project.timestamp,
        seminar_count=new_seminar_count,
        data=project.data
    ))
    db.commit()
    slack.post_notification({"text": f"Project updated: {json.dumps(project_data)}"})

    # Backfill existing seminar names when seminarNames is provided
    if seminar_names:
        existing_seminars = db.query(database.Seminar).filter(
            database.Seminar.project_id == project_id,
            database.Seminar.index <= current_seminar_count
        ).order_by(database.Seminar.index.asc()).all()
        for sem in existing_seminars:
            idx = sem.index - 1
            if idx < len(seminar_names) and seminar_names[idx]:
                sem.name = seminar_names[idx]
        db.commit()

    # Handle seminar changes
    if new_seminar_count > current_seminar_count:
        # Create new seminars for indices (current_count + 1) to new_count (use custom names if provided)
        new_seminar_ids = []
        for i in range(current_seminar_count + 1, new_seminar_count + 1):
            name = seminar_names[i - 1] if i <= len(seminar_names) and seminar_names[i - 1] else f"Seminar {i}"
            seminar = database.Seminar(
                project_id=project_id,
                index=i,
                name=name
            )
            db.add(seminar)
            db.flush()  # Get the seminar ID
            new_seminar_ids.append(seminar.id)
        db.commit()
        
        # Generate world state only for the new seminars (branching)
        try:
            world_state.create_world_state_for_project(db, project_id, seminar_ids=new_seminar_ids)
        except Exception as e:
            logger.error(f"Error generating world state for project {project_id}: {e}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"Failed to update event: {str(e)}"
            )
    elif new_seminar_count < current_seminar_count:
        # Delete seminars with index > new_count
        db.query(database.Seminar).filter(
            database.Seminar.project_id == project_id,
            database.Seminar.index > new_seminar_count
        ).delete(synchronize_session=False)
        db.commit()

    # If docs/files changed, regenerate world state for all existing seminars
    if docs_changed:
        # Get existing seminar IDs (excluding any newly created ones which already got their world state)
        existing_seminars = db.query(database.Seminar).filter(
            database.Seminar.project_id == project_id,
            database.Seminar.index <= current_seminar_count
        ).all()
        existing_seminar_ids = [s.id for s in existing_seminars]
        
        if existing_seminar_ids:
            try:
                logger.info(f"Docs changed for project {project_id}, regenerating world states for {len(existing_seminar_ids)} existing seminars")
                world_state.create_world_state_for_project(db, project_id, seminar_ids=existing_seminar_ids)
            except Exception as e:
                logger.error(f"Error regenerating world state after docs change for project {project_id}: {e}")
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"Failed to update event: {str(e)}"
                )

    events.log_event(project.user_id, "event:" + project_id, f"Updated event {project_data['name']}")
    
    return JSONResponse(content={"project": project.data}, status_code=status.HTTP_200_OK)


class DuplicateProjectRequest(BaseModel):
    user_id: str
    name: str


@router.post("/projects/{project_id}/duplicate")
def duplicate_project(project_id: str, request: DuplicateProjectRequest, db: Session = Depends(database.get_db)):
    """Create a copy of an existing event with its documents under a new name."""
    # Find the source project
    source_project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()

    if not source_project:
        raise HTTPException(status_code=404, detail="Source project not found")

    # Parse source project data
    try:
        source_data = json.loads(source_project.data) if isinstance(source_project.data, str) else source_project.data
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=500, detail="Failed to parse source project data")

    # Build new project data, copying docs and settings from the source
    new_project_id = str(uuid.uuid4())
    timestamp = int(time.time() * 1000)

    new_data = {
        "projectId": new_project_id,
        "userId": request.user_id,
        "name": request.name,
        "baseline": source_data.get("baseline", {"docs": True, "links": True}),
        "docs": source_data.get("docs", []),
        "events": [],
        "dataSources": source_data.get("dataSources", []),
        "status": "Not Started",
        "email": source_data.get("email", ""),
        "creator": source_data.get("creator", {}),
        "eventDate": source_data.get("eventDate", ""),
        "description": source_data.get("description", ""),
        "projectPrompt": source_data.get("projectPrompt", ""),
        "seminars": 0,
        "seminarNames": [],
        "isDeleted": False,
        "createdAt": timestamp,
        "updatedAt": timestamp,
    }

    # Create the new project record
    new_project = database.Project(
        project_id=new_project_id,
        user_id=request.user_id,
        timestamp=timestamp,
        seminar_count=0,
        data=json.dumps(new_data)
    )
    db.add(new_project)
    db.commit()

    events.log_event(request.user_id, "event", f"Duplicated event '{source_data.get('name', '')}' as '{request.name}'")

    # Add the owner to white cell team
    owner_member = database.ProjectMember(
        project_id=new_project_id,
        user_id=request.user_id,
        role="white"
    )
    db.add(owner_member)
    db.commit()

    return JSONResponse(content={"project": json.dumps(new_data)}, status_code=status.HTTP_201_CREATED)


def _is_ibl_hardcoded_project(project_id: str) -> bool:
    """True if `project_id` is the IBL single-project sentinel
    (`default-{userId}`, bare 'default' for empty userId) — matches the
    prefix handlers/agents.py + intel_agent.py use."""
    if not project_id:
        return False
    return project_id == "default" or project_id.startswith("default-")


@router.delete("/projects/{project_id}")
def delete_project(project_id: str, user_id: str, db: Session = Depends(database.get_db)):
    # No-op + warning for the IBL hardcoded project — the edge deployment
    # only ever has one project per user and deleting it would wipe every
    # chained artifact / session row.
    if _is_ibl_hardcoded_project(project_id):
        logger.warning(
            "delete_project: refusing to delete IBL hardcoded project "
            "project_id=%s user_id=%s (no-op)", project_id, user_id)
        return JSONResponse(
            content={
                "message": f"Project {project_id} is the IBL default project and cannot be deleted.",
                "soft_deleted": False,
                "noop": True,
            },
            status_code=200,
        )

    # Find the project
    project = db.query(database.Project).filter(database.Project.project_id == project_id, database.Project.user_id == user_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Capture data BEFORE commit — attribute loads after flush need an
    # open session (DetachedInstanceError otherwise).
    try:
        project_data = json.loads(project.data) if project.data else {}
    except Exception:
        project_data = {}
    project_name = project_data.get("name") or project_id

    # Soft delete — flip the flag, keep the row so chained audit data
    # (sessions, artifacts, JobEvent rows) keeps its FK target.
    # _ensure_columns at boot guarantees the column exists.
    project.is_deleted = True
    db.commit()

    # Log the deletion event
    events.log_event(user_id, "event", f"Deleted event {project_name}")

    # Return confirmation
    return JSONResponse(
        content={"message": f"Project {project_id} deleted successfully.", "soft_deleted": True},
        status_code=200
    )


@router.get("/projects/{user_id}")
def read_projects(user_id: str, db: Session = Depends(database.get_db)):
    """Get projects for a user based on their role.
    - Participants: projects where user is in ProjectMember with role='participant'
    - White cell: projects where user is owner OR in ProjectMember with role='white'
    - Edge users (no role blob; userId == email): owner listing — the
      edge deployment has no membership model.
    """
    # Get user's role. Cloud rows key on User.id (uuid); edge rows key on
    # email (the edge session blob's userId IS the email).
    user = db.query(database.User).filter(database.User.id == user_id).first()
    if not user:
        user = db.query(database.User).filter(database.User.email == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Parse user role. Empty/absent role = edge lineage → owner listing
    # (the participant branch would return [] for every edge user).
    if not user.role:
        role_value = "white"
    else:
        try:
            user_role = json.loads(user.role) if isinstance(user.role, str) else user.role
            role_value = user_role.get("role", "participant")
        except Exception:
            role_value = "participant"
    
    project_ids = set()

    # Ownership/membership rows key on EITHER identity depending on
    # lineage (cloud rows: User.id uuid; edge rows: email). Match the
    # resolved user's full identity set, not the raw path param —
    # /api/projects/1 with rows owned by the email returned [] before.
    identity_set = {v for v in (str(user.id) if user.id is not None else None,
                                 user.email, user_id) if v}

    if role_value == "participant":
        # Participants: only projects they're invited to
        members = db.query(database.ProjectMember).filter(
            database.ProjectMember.user_id.in_(identity_set),
            database.ProjectMember.role == "participant"
        ).all()
        project_ids = {member.project_id for member in members}
    else:
        # White cell: projects they own OR are invited to as white cell
        # Get owned projects
        owned_projects = db.query(database.Project).filter(
            database.Project.user_id.in_(identity_set)
        ).all()
        project_ids = {project.project_id for project in owned_projects}
        
        # Get projects where user is white cell member
        whitecell_members = db.query(database.ProjectMember).filter(
            database.ProjectMember.user_id.in_(identity_set),
            database.ProjectMember.role == "white"
        ).all()
        project_ids.update({member.project_id for member in whitecell_members})
    
    if not project_ids:
        return []
    
    # Get all projects. is_deleted.isnot(True) keeps soft-deleted rows
    # out while passing legacy NULLs (pre-soft-delete rows).
    results = (db.query(database.Project)
        .filter(database.Project.project_id.in_(project_ids))
        .filter(database.Project.is_deleted.isnot(True))
        .order_by(database.Project.timestamp.desc())  # Sort by timestamp (newest first)
        .limit(100)  # Limit to top 100
        .all())
    
    # Parse JSON strings to objects before returning
    parsed_results = []
    for result in results:
        try:
            project_data = json.loads(result.data)
            # Sort event documents alphabetically by name
            if "docs" in project_data and isinstance(project_data["docs"], list):
                project_data["docs"] = sorted(
                    project_data["docs"],
                    key=lambda d: (d.get("name") or "").lower()
                )
            parsed_results.append(project_data)
        except (json.JSONDecodeError, TypeError) as e:
            # Log error but skip malformed entries
            print(f"Error parsing project data for project_id {result.project_id}: {e}")
            continue
    
    return parsed_results


@router.get("/session_details/{session_id}")
def read_session(session_id: str, db: Session = Depends(database.get_db)):
    results = (db.query(database.SessionDetail)
        .filter(database.SessionDetail.session_id == session_id)
        .order_by(database.SessionDetail.timestamp.desc())  # Sort by timestamp (newest first)
        .limit(100)  # Limit to top 100
        .all())
    
    results = sorted(results, key=lambda p: p.timestamp)
    results = [json.loads(result.data) for result in results]
    
    session = []
    messages = []
    steps = []
    
    for result in results:
        # it's an agent step
        if 'executedStepIndex' in result:
            steps.append(result)
            continue

        messages.append({
            "type": "question",
            "content": result.get("question")["prompt"]
        })

        messages.append({
            "type": "answer",
            "content": result.get("answer"),
            "productType": result.get("productType"),
            "isShared": result.get("isShared"),
            "timestamp": result.get("timestamp"),
            "sessionId": result.get("sessionId"), 
            "userId": result.get("userId"),
            "title": result.get("title"),
            "plan": result.get("plan"),
        })

        session.append([result.get("sessionPrompt"), result.get("sessionAnswer"), result.get("sourcesMap")])

    return json.dumps({
        "session": session,
        "messages": messages,
        "steps": steps,
    })


@router.get("/projects/{project_id}/whitecell")
def get_whitecell_members(project_id: str, db: Session = Depends(database.get_db)):
    """Get all white cell members for a project."""
    # Get the project to find the owner
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()
    
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    # Get all white cell members from ProjectMember table
    members = (db.query(database.ProjectMember, database.User)
              .join(database.User, database.ProjectMember.user_id == database.User.id)
              .filter(database.ProjectMember.project_id == project_id)
              .filter(database.ProjectMember.role == "white")
              .all())
    
    result = []
    for member, user in members:
        try:
            role = json.loads(user.role) if isinstance(user.role, str) else user.role
        except:
            role = {"role": "white"}
        
        # Parse name into firstName and lastName
        name_parts = user.name.split(" ", 1) if user.name else ["", ""]
        firstName = name_parts[0] if len(name_parts) > 0 else ""
        lastName = name_parts[1] if len(name_parts) > 1 else ""
        
        result.append({
            "userId": user.id,
            "email": user.email,
            "name": user.name,
            "firstName": firstName,
            "lastName": lastName,
            "role": role
        })
    
    result.sort(key=lambda x: (x.get("lastName") or "").lower())
    return result


@router.get("/projects/{project_id}/participants")
def get_participants(project_id: str, db: Session = Depends(database.get_db)):
    """Get all participants for a project."""
    members = (db.query(database.ProjectMember, database.User)
              .join(database.User, database.ProjectMember.user_id == database.User.id)
              .filter(database.ProjectMember.project_id == project_id)
              .filter(database.ProjectMember.role == "participant")
              .all())
    
    result = []
    for member, user in members:
        try:
            role = json.loads(user.role) if isinstance(user.role, str) else user.role
        except:
            role = {"role": "participant"}
        
        # Parse name into firstName and lastName
        name_parts = user.name.split(" ", 1) if user.name else ["", ""]
        firstName = name_parts[0] if len(name_parts) > 0 else ""
        lastName = name_parts[1] if len(name_parts) > 1 else ""
        
        result.append({
            "userId": user.id,
            "email": user.email,
            "name": user.name,
            "firstName": firstName,
            "lastName": lastName,
            "role": role,
            "seminarId": member.seminar_id
        })
    
    result.sort(key=lambda x: (x.get("lastName") or "").lower())
    return result


@router.post("/projects/{project_id}/whitecell")
def add_whitecell_member(project_id: str, request: AddMemberRequest, db: Session = Depends(database.get_db)):
    """Add a white cell member to a project."""
    # Check if user exists
    user = db.query(database.User).filter(database.User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # Check if member already exists
    existing = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == request.user_id,
        database.ProjectMember.role == "white"
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="User is already a white cell member")
    
    # Add member
    member = database.ProjectMember(
        project_id=project_id,
        user_id=request.user_id,
        role="white"
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    
    # Notify the added user
    project = db.query(database.Project).filter(database.Project.project_id == project_id).first()
    if project:
        try:
            project_data = json.loads(project.data) if isinstance(project.data, str) else project.data
            project_name = project_data.get("name", "Unknown Event")
        except:
            project_name = "Unknown Event"
        
        notifications.create_notification(
            user_id=request.user_id,
            display_text=f"You have been added to event {project_name} as a white cell member",
            action={
                "type": "navigate",
                "route": f"/thewarroom/projects/{project_id}/dashboard",
                "refreshEvents": True
            },
            db=db
        )
    
    # Return user info
    try:
        role = json.loads(user.role) if isinstance(user.role, str) else user.role
    except:
        role = {"role": "white"}
    
    name_parts = user.name.split(" ", 1) if user.name else ["", ""]
    firstName = name_parts[0] if len(name_parts) > 0 else ""
    lastName = name_parts[1] if len(name_parts) > 1 else ""
    
    return {
        "userId": user.id,
        "email": user.email,
        "name": user.name,
        "firstName": firstName,
        "lastName": lastName,
        "role": role
    }


@router.post("/projects/{project_id}/participants")
def add_participant(project_id: str, request: AddMemberRequest, db: Session = Depends(database.get_db)):
    """Add a participant to a project. Optionally associate with a seminar."""
    # Check if user exists
    user = db.query(database.User).filter(database.User.id == request.user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    
    # If seminar_id is provided, validate it exists and belongs to the project
    if request.seminar_id:
        seminar = db.query(database.Seminar).filter(
            database.Seminar.id == request.seminar_id,
            database.Seminar.project_id == project_id
        ).first()
        
        if not seminar:
            raise HTTPException(
                status_code=404, 
                detail="Seminar not found or does not belong to this project"
            )
    
    # Check if member already exists
    existing = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == request.user_id,
        database.ProjectMember.role == "participant"
    ).first()
    
    if existing:
        raise HTTPException(status_code=400, detail="User is already a participant")
    
    # Add member
    member = database.ProjectMember(
        project_id=project_id,
        user_id=request.user_id,
        role="participant",
        seminar_id=request.seminar_id
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    
    # Notify the added user
    project = db.query(database.Project).filter(database.Project.project_id == project_id).first()
    if project:
        try:
            project_data = json.loads(project.data) if isinstance(project.data, str) else project.data
            project_name = project_data.get("name", "Unknown Event")
        except:
            project_name = "Unknown Event"
        
        notifications.create_notification(
            user_id=request.user_id,
            display_text=f"You have been added to event {project_name} as a participant",
            action={
                "type": "navigate",
                "route": f"/thewarroom/projects/{project_id}/dashboard",
                "refreshEvents": True
            },
            db=db
        )
    
    # Return user info
    try:
        role = json.loads(user.role) if isinstance(user.role, str) else user.role
    except:
        role = {"role": "participant"}
    
    name_parts = user.name.split(" ", 1) if user.name else ["", ""]
    firstName = name_parts[0] if len(name_parts) > 0 else ""
    lastName = name_parts[1] if len(name_parts) > 1 else ""
    
    return {
        "userId": user.id,
        "email": user.email,
        "name": user.name,
        "firstName": firstName,
        "lastName": lastName,
        "role": role
    }


@router.delete("/projects/{project_id}/whitecell/{user_id}")
def remove_whitecell_member(project_id: str, user_id: str, db: Session = Depends(database.get_db)):
    """Remove a white cell member from a project."""
    # Check if this is the project owner
    project = db.query(database.Project).filter(
        database.Project.project_id == project_id
    ).first()
    
    if project and project.user_id == user_id:
        raise HTTPException(
            status_code=400, 
            detail="Cannot remove the event owner from the white cell team. Delete the event instead."
        )
    
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "white"
    ).first()
    
    if not member:
        raise HTTPException(status_code=404, detail="White cell member not found")
    
    db.delete(member)
    db.commit()
    
    return JSONResponse(
        content={"message": "White cell member removed successfully"},
        status_code=200
    )


@router.delete("/projects/{project_id}/participants/{user_id}")
def remove_participant(project_id: str, user_id: str, db: Session = Depends(database.get_db)):
    """Remove a participant from a project."""
    member = db.query(database.ProjectMember).filter(
        database.ProjectMember.project_id == project_id,
        database.ProjectMember.user_id == user_id,
        database.ProjectMember.role == "participant"
    ).first()
    
    if not member:
        raise HTTPException(status_code=404, detail="Participant not found")
    
    db.delete(member)
    db.commit()
    
    return JSONResponse(
        content={"message": "Participant removed successfully"},
        status_code=200
    )
