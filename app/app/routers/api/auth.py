"""Authentication endpoints."""
from fastapi import APIRouter, HTTPException, status, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
from utils.database import get_db, User
from utils.slack import post_notification
from typing import Optional
import json
import uuid

router = APIRouter()


class UserResponse(BaseModel):
    userId: str
    email: str
    name: str
    role: dict
    firstName: str
    lastName: str
    mapId: str

    class Config:
        from_attributes = True


class UserIn(BaseModel):
    email: str
    # Optional — the EDGE SPA has no Cognito on the closed network and
    # sends the password for local verification here. The CLOUD SPA
    # verifies the password against Cognito first and sends email only.
    password: Optional[str] = None


class SignUpRequest(BaseModel):
    name: str
    email: str
    role: str  # "white" or "participant"
    do_not_track: Optional[bool] = False


@router.get("/")
def read_root():
    return {"Hello": "World"}


@router.post("/auth", response_model=UserResponse)
def auth_user(userIn: UserIn, db: Session = Depends(get_db)):
    """Sign in — ONE endpoint, both deployment contracts.

    CLOUD SPA (pytho-vite): Cognito verified the password upstream;
    sends {email} only and this resolves the user record.
    EDGE SPA (pytho-app ui, closed network, no Cognito): sends
    {email, password} and this verifies the password locally against
    the users table.

    Response is the rich cloud shape (role/mapId/name) — a superset of
    the edge SPA's expectations (userId/email/firstName/lastName), so
    both frontends read what they need from the same payload.
    """
    email = (userIn.email or "").strip()
    user = db.query(User).filter(User.email == email).first()
    if user is None:
        # Edge rows are stored lowercased; retry the edge convention.
        user = db.query(User).filter(User.email == email.lower()).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Edge contract: verify the locally stored password when one is sent.
    if userIn.password is not None and user.password != userIn.password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
        )

    # Create map_id if user doesn't have one
    if not user.map_id:
        user.map_id = str(uuid.uuid4())
        db.commit()
        db.refresh(user)

    # Parse role JSON
    try:
        role = json.loads(user.role) if isinstance(user.role, str) else user.role
        if not isinstance(role, dict):
            role = {"role": "participant"}
    except Exception:
        role = {"role": "participant"}  # Default fallback

    # firstName/lastName: prefer the edge columns when populated (edge
    # signups store them directly); fall back to splitting the cloud
    # `name` field (safe for single-word names).
    name_parts = user.name.split(" ", 1) if user.name else ["", ""]
    first_name = user.first_name or (name_parts[0] if len(name_parts) > 0 else "")
    last_name = user.last_name or (name_parts[1] if len(name_parts) > 1 else "")

    return {
        "userId": str(user.id) if user.id is not None else user.email,
        "email": user.email,
        "name": user.name or f"{first_name} {last_name}".strip(),
        "firstName": first_name,
        "lastName": last_name,
        "role": role,
        "mapId": user.map_id
    }


@router.post("/auth/signup", response_model=UserResponse)
def signup_user(signup_request: SignUpRequest, db: Session = Depends(get_db)):
    """Sign up endpoint - creates a new user account."""
    # Validate role
    if signup_request.role not in ["white", "participant"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role must be either 'white' or 'participant'",
        )
    
    # Check if email already exists
    existing_user = db.query(User).filter(User.email == signup_request.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )
    
    # Create role JSON blob (include doNotTrack for DNT users)
    role_dict = {"role": signup_request.role}
    if signup_request.do_not_track:
        role_dict["doNotTrack"] = True
    role_json = json.dumps(role_dict)

    # Create new user with auto-generated ID
    new_user = User(
        id=str(uuid.uuid4()),
        name=signup_request.name,
        email=signup_request.email,
        role=role_json,
        map_id=str(uuid.uuid4()),
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # Parse role for response
    role = json.loads(new_user.role)
    
    # Send Slack notification for new signup
    post_notification({
        "text": f"🎉 New user signup: {new_user.name} ({new_user.email}) - Role: {signup_request.role}"
    })

    # Parse name into firstName and lastName (safe for single-word names)
    name_parts = new_user.name.split(" ", 1) if new_user.name else ["", ""]
    first_name = name_parts[0] if len(name_parts) > 0 else ""
    last_name = name_parts[1] if len(name_parts) > 1 else ""

    return {
        "userId": new_user.id,
        "email": new_user.email,
        "name": new_user.name,
        "firstName": first_name,
        "lastName": last_name,
        "role": role,
        "mapId": new_user.map_id
    }


@router.get("/users/{user_id}/do-not-track")
def get_do_not_track(user_id: str, db: Session = Depends(get_db)):
    """Return doNotTrack flag for a user (used by Lambda to skip persisting session history)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    try:
        role = json.loads(user.role) if isinstance(user.role, str) else user.role
        do_not_track = role.get("doNotTrack", False) is True
    except (TypeError, ValueError):
        do_not_track = False
    return {"doNotTrack": do_not_track}


@router.get("/users")
def get_users(
    domain: Optional[str] = Query(None, description="Filter by email domain"),
    role: Optional[str] = Query(None, description="Filter by role: 'white' or 'participant'"),
    db: Session = Depends(get_db)
):
    """Get users by domain and optionally filter by role."""
    query = db.query(User)
    
    # Filter by domain if provided
    if domain:
        query = query.filter(User.email.like(f"%@{domain}"))
    
    users = query.all()
    
    result = []
    for user in users:
        try:
            user_role = json.loads(user.role) if isinstance(user.role, str) else user.role
            role_value = user_role.get("role", "participant")
        except:
            role_value = "participant"
        
        # Filter by role if provided
        if role and role_value != role:
            continue
        
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
            "role": user_role
        })
    
    result.sort(key=lambda x: (x.get("lastName") or "").lower())
    return result
