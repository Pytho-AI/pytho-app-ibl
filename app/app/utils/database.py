from sqlalchemy import Boolean, DateTime, ForeignKey, create_engine, Column, Integer, String, Text, PrimaryKeyConstraint, inspect, text, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import json

DATABASE_URL = "sqlite:///./data/pytho.db"

import os
os.makedirs("./data", exist_ok=True)

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# Dependency to get the DB session
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def null_safe_json_dumps(data):
    filtered_data = {k: v for k, v in data.items() if v is not None}
    return json.dumps(filtered_data)

def generate_uuid():
    """String-uuid PK default for the cloud-surface tables below."""
    import uuid
    return str(uuid.uuid4())

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    # email = login + app-wide userId. New cols nullable so _ensure_columns
    # can ALTER them in; uniqueness enforced in the signup route.
    email = Column(String, unique=True, index=True, nullable=True)
    # TODO(main-006, indefinitely deferred per user 2026-06-03): passwords
    # are stored + compared in plaintext. Intentional for the closed-
    # network exercise deployment. The cloud SST path uses Cognito (not
    # this table), so this only affects the air-gapped IBL/IDX-26
    # deployment where the network boundary is the trust boundary.
    # When ready to flip: replace with argon2-cffi password_hash column;
    # swap aws-amplify Auth in ui/src/ for a simple /api/auth flow
    # returning a session JWT. Big refactor — separate workflow.
    password = Column(String, nullable=True)
    first_name = Column(String, nullable=True)
    last_name = Column(String, nullable=True)
    created_at = Column(Integer, nullable=True)
    # Cloud-surface columns (routers/api/auth.py — edge+cloud
    # unification 2026-07-07). Nullable so _ensure_columns can ALTER
    # them into populated edge databases. role = JSON blob
    # {"role": "white"} / {"role": "participant"}; map_id = the old
    # cloud contract's per-user map uuid (cloud maps are PROJECT-scoped
    # now — the column persists only for that response shape).
    #
    # PK note: the model keeps Integer (edge databases have INTEGER
    # PRIMARY KEY DDL and edge signups rely on autoincrement). Cloud
    # signups set explicit uuid STRINGS — cloud databases created by
    # main's old model have a String PK DDL, and SQLAlchemy's Integer
    # type passes string binds/results through unmodified on sqlite.
    # Do NOT "clean this up" to String: that breaks edge autoincrement
    # inserts against INTEGER PRIMARY KEY tables.
    role = Column(Text, nullable=True)
    map_id = Column(String, nullable=True, index=True)

class Session(Base):
    __tablename__ = "sessions"

    session_id = Column(String, primary_key=True)
    timestamp = Column(Integer)
    project_id = Column(String, index=True)
    data = Column(Text)

class SessionDetail(Base):
    __tablename__ = "session_details"

    session_id = Column(String, primary_key=True)
    timestamp = Column(Integer, primary_key=True)
    project_id = Column(String)
    data = Column(Text)

class Project(Base):
    __tablename__ = "projects"

    project_id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    timestamp = Column(Integer)
    data = Column(Text)
    # Soft-delete flag. main.delete_project flips this rather than
    # issuing a SQL DELETE so chained audit data (sessions, artifacts,
    # JobEvent rows) keeps its FK target. _ensure_columns at init_db
    # backfills this column on existing tables (nullable + default False
    # so the ALTER TABLE works on populated databases).
    is_deleted = Column(Boolean, nullable=True, default=False)
    # Cloud surface (seminars router). Nullable for _ensure_columns.
    seminar_count = Column(Integer, nullable=True, default=0)

    # Relationships for cascade delete (cloud-surface tables below).
    seminars = relationship("Seminar", back_populates="project", cascade="all, delete-orphan", passive_deletes=True)
    seminar_injects = relationship("SeminarInject", back_populates="project", cascade="all, delete-orphan", passive_deletes=True)
    world_states = relationship("WorldState", back_populates="project", cascade="all, delete-orphan", passive_deletes=True)

class ProductType(Base):
    __tablename__ = "product_types"

    product_type_id = Column(String, primary_key=True)
    user_id = Column(String, index=True)
    email = Column(String, nullable=True)
    role = Column(String, nullable=True)
    product_name = Column(String, nullable=False)
    product_overview = Column(Text, nullable=True)
    file_key = Column(String, nullable=True)
    file_name = Column(String, nullable=True)
    is_dow = Column(String, nullable=False, default="false")
    created_at = Column(Integer, nullable=True)
    updated_at = Column(Integer, nullable=True)

class EventLog(Base):
    __tablename__ = "event_log"

    user_id = Column(String, nullable=False)
    tag = Column(String, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    details = Column(Text)

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "tag", "timestamp"),
    )


class AnalystEvent(Base):
    """Analyst-action telemetry, for improving the operational workflow.

    Successor to EventLog (which stays untouched for back-compat). One row
    per meaningful analyst action across the LIVE surfaces (Dashboard,
    Collabora editor, Operations Map, chat sidebar, Doctrines, Feedback,
    plus server-side generation lifecycle). Drives the product fix-list —
    which products the agent gets wrong (edit distance), where analysts
    abandon, what fails / lags — and indexes the draft->final training
    pairs that live on disk in schema_state.json + history/.

    Auto-created by init_db(); new columns auto-added by _ensure_columns().
    `value_num` carries the single salient number for the event type
    (edit_distance | dwell_ms | turn_count | result_count | fields_changed);
    everything else goes in `detail` as JSON.
    """
    __tablename__ = "analyst_event"

    event_id = Column(String, primary_key=True)            # uuid4
    user_id = Column(String, nullable=True, index=True)    # null for server/system-emitted events
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    surface = Column(String, nullable=True, index=True)    # dashboard|editor|map|chat|doctrines|feedback|generation|system
    event_type = Column(String, nullable=False, index=True)
    product_type = Column(String, nullable=True, index=True)
    session_id = Column(String, nullable=True, index=True)
    artifact_id = Column(String, nullable=True, index=True)
    value_num = Column(Integer, nullable=True)
    detail = Column(Text, nullable=True)                   # JSON blob for type-specific fields


class AAR(Base):
    """After Action Report model"""
    __tablename__ = "aar"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    name = Column(String(255), nullable=False, index=True)
    description = Column(Text, nullable=True)
    event_id = Column(String(255), nullable=True, index=True)
    user_id = Column(String(255), nullable=False, default="unknown")
    type = Column(String(50), nullable=False, default="text")  # "audio" or "text"
    text_content = Column(Text, nullable=True)
    file_key = Column(String(255), nullable=True)
    file_url = Column(String(512), nullable=True)
    file_name = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    # Relationship to chat messages
    chat_messages = relationship("AARChat", back_populates="aar", cascade="all, delete-orphan")
    transcripts = relationship("AARTranscript", back_populates="aar", cascade="all, delete-orphan")

class AARChat(Base):
    """AAR Chat messages model"""
    __tablename__ = "aar_chat"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    aar_id = Column(Integer, ForeignKey("aar.id", ondelete="CASCADE"), nullable=False, index=True)
    message = Column(Text, nullable=False)
    role = Column(String(50), default="user", nullable=False)  # 'user' or 'assistant'
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    type = Column(String(50), default="message", nullable=False) # 'summary' or 'message'
    # Relationship back to AAR
    aar = relationship("AAR", back_populates="chat_messages")
    

class AARTranscript(Base):
    """AAR Transcript entries model"""
    __tablename__ = "aar_transcript"
    
    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    aar_id = Column(Integer, ForeignKey("aar.id", ondelete="CASCADE"), nullable=False, index=True)
    speaker = Column(String(100), default="Speaker", nullable=False)
    timestamp = Column(String(20), nullable=False)  # Format: "0:15"
    text = Column(Text, nullable=False)
    audio_duration = Column(Integer, nullable=True)  # Duration in seconds
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    aar = relationship("AAR", back_populates="transcripts")


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    user_email = Column(String, nullable=False)
    sentiment = Column(String(10), nullable=False)  # 'liked' or 'disliked'
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    comment = Column(Text, nullable=True)
    session_id = Column(String, nullable=True)
    project_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class Notification(Base):
    """Analyst-visible notification, broadcast or per-user.

    Today the only producer is the KLIEGLIGHT auto-fire trigger
    (handlers/klieglight_autofire.py). The notification carries everything
    the UI needs to render a one-line "X happened" entry + a "View draft"
    action that opens the auto-fire artifact in a new chat session via
    /api/notifications/{id}/open_draft.

    Persistence: every fire writes one row. The Notifications.tsx panel
    fetches via GET /api/notifications/{user_id} (broadcast notifications
    have user_id="*"); the IntelAgentWs WS broadcasts the same row as a
    {type:"notification"} frame for live ones.

    payload column holds the full producer-shape JSON (kbid, mgrs,
    emitter_name, artifact_id, autodraft_session_id, ...). Schema-bound
    columns (kind, artifact_id, autodraft_session_id) are denormalized
    out of payload so the open_draft endpoint doesn't have to parse JSON
    to resolve the artifact.
    """
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    # "*" = broadcast to every analyst. KLIEGLIGHT auto-fires are
    # broadcast — no analyst owns them until open_draft is called.
    user_id = Column(String(255), nullable=False, default="*", index=True)
    kind = Column(String(64), nullable=False, default="generic", index=True)
    # The artifact this notification points at. Resolved by open_draft to
    # find the source SessionDetail row in the _autodraft session.
    artifact_id = Column(String(255), nullable=True, index=True)
    autodraft_session_id = Column(String(255), nullable=True, index=True)
    payload = Column(Text, nullable=False, default="{}")
    # Status: "unread" -> "read" -> "consumed". consumed means open_draft
    # has been called on this notification (so the UI can grey it out).
    status = Column(String(32), nullable=False, default="unread", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)



# ---- Cloud-surface tables (ported from main during the edge+cloud
# unification, 2026-07-07; consumed by routers/api + routers/ws) ----

class ProjectMember(Base):
    """Project member model for white cell and participants"""
    __tablename__ = "project_members"
    
    project_id = Column(String, nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    role = Column(String, nullable=False)  # "white" or "participant"
    seminar_id = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    __table_args__ = (
        PrimaryKeyConstraint("project_id", "user_id", "role"),
    )


class RFI(Base):
    """Request for Information model"""
    __tablename__ = "rfis"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    project_id = Column(String, nullable=False, index=True)
    rfi_number = Column(String, nullable=False, index=True)  # Format: RFI-ABC123
    created_by_user_id = Column(String, nullable=False, index=True)
    seminar_id = Column(String, nullable=True, index=True)  # Seminar/cohort of the creator
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    destination = Column(String, nullable=True)  # RFI destination/intended recipient
    status = Column(String, default="Not Started", nullable=False)  # Not Started, In Review, Closed
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    ai_answer = Column(Text, nullable=True)

    # Relationships
    responses = relationship("RFIResponse", back_populates="rfi", cascade="all, delete-orphan")
    attachments = relationship("RFIAttachment", back_populates="rfi", cascade="all, delete-orphan")


class RFIResponse(Base):
    """RFI Response model"""
    __tablename__ = "rfi_responses"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    rfi_id = Column(String, ForeignKey("rfis.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String, nullable=False, index=True)
    content = Column(Text, nullable=False)
    is_internal = Column(Integer, default=0, nullable=False)  # SQLite uses 0/1 for boolean
    is_official_response = Column(Integer, default=0, nullable=False)  # SQLite uses 0/1 for boolean
    is_ai_response = Column(Integer, default=0, nullable=False)  # SQLite uses 0/1 for boolean
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    # Relationships
    rfi = relationship("RFI", back_populates="responses")
    attachments = relationship("RFIResponseAttachment", back_populates="response", cascade="all, delete-orphan")


class RFIAttachment(Base):
    """RFI Attachment model"""
    __tablename__ = "rfi_attachments"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    rfi_id = Column(String, ForeignKey("rfis.id", ondelete="CASCADE"), nullable=False, index=True)
    file_key = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationship
    rfi = relationship("RFI", back_populates="attachments")


class RFIResponseAttachment(Base):
    """RFI Response Attachment model"""
    __tablename__ = "rfi_response_attachments"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    response_id = Column(String, ForeignKey("rfi_responses.id", ondelete="CASCADE"), nullable=False, index=True)
    file_key = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    file_size = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationship
    response = relationship("RFIResponse", back_populates="attachments")


class Seminar(Base):
    """Seminar model - multiple seminars can be part of a project"""
    __tablename__ = "seminars"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    index = Column(Integer, nullable=False, index=True)
    project_id = Column(String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationships
    project = relationship("Project", back_populates="seminars")
    world_state_mappings = relationship("SeminarWorldState", back_populates="seminar", cascade="all, delete-orphan", passive_deletes=True)
    coa_submissions = relationship("COASubmission", back_populates="seminar", cascade="all, delete-orphan", passive_deletes=True)


class SeminarInject(Base):
    """Seminar Inject model - can be associated with multiple seminars or all seminars"""
    __tablename__ = "seminar_injects"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    index = Column(String, nullable=False)  # User-provided inject identifier (e.g., "1", "1A", "2B")
    seminar_ids = Column(JSON, nullable=False)  # Array of strings: ["seminar_id1", "seminar_id2"] or ["all"]
    project_id = Column(String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    inject_type = Column(String, nullable=True)
    status = Column(String, nullable=True)
    files = Column(JSON, nullable=True)  # Array of file objects: [{"key": "...", "url": "...", "name": "..."}]
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationship back to Project
    project = relationship("Project", back_populates="seminar_injects")


class WorldState(Base):
    """World State model - can be shared by multiple seminars through SeminarWorldState mapping"""
    __tablename__ = "world_states"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    project_id = Column(String, ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True)
    summary_state = Column(Text, nullable=True)  # Brief summary of the world state
    detailed_state = Column(Text, nullable=True)  # Detailed description of the world state
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationship to mapping table
    seminar_mappings = relationship("SeminarWorldState", back_populates="world_state", cascade="all, delete-orphan", passive_deletes=True)
    project = relationship("Project", back_populates="world_states")


class SeminarWorldState(Base):
    """Mapping between seminars and world states - allows world states to be shared by multiple seminars"""
    __tablename__ = "seminar_world_states"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    seminar_id = Column(String, ForeignKey("seminars.id", ondelete="CASCADE"), nullable=False, index=True)
    world_state_id = Column(String, ForeignKey("world_states.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    # Relationships
    seminar = relationship("Seminar", back_populates="world_state_mappings")
    world_state = relationship("WorldState", back_populates="seminar_mappings")


class COASubmission(Base):
    """COA Submission model - submissions linked to seminars with submitter and reviewer"""
    __tablename__ = "coa_submissions"
    
    id = Column(String, primary_key=True, index=True, default=generate_uuid)
    seminar_id = Column(String, ForeignKey("seminars.id", ondelete="CASCADE"), nullable=False, index=True)
    submitter_user_id = Column(String, nullable=False, index=True)  # User who submitted the COAs
    reviewer_user_id = Column(String, nullable=True, index=True)  # User who reviews the COAs (white cell)
    project_id = Column(String, nullable=False, index=True)
    coa_options = Column(JSON, nullable=False)  # JSON array of COA options: [{"title": "...", "description": "...", "sequence_number": 1}, ...]
    status = Column(String, default="Submitted", nullable=False)  # Submitted, Under Review, Reviewed, Approved, Rejected
    approved_coa = Column(JSON, nullable=True)  # COA 1, COA 2, COA 3
    recommended_coa = Column(JSON, nullable=True)  # Recommended COA option
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    # Relationship back to Seminar
    seminar = relationship("Seminar", back_populates="coa_submissions")


class JobEvent(Base):
    """Append-only audit log of every state transition in the unified
    jobs system (handlers/product_records.py).

    One row per transition (`started`, `gate_acquired`, `writer_started`,
    `phase`, `completed`, `failed`, `cancelled`, `retried`, `deleted`).
    Snapshot fields capture the record's state AFTER the event so a
    replay reconstructs the full timeline without back-referencing.

    Retention: FOREVER. Per the design's locked-in answer to Q5 — no
    pruning policy. The log is the audit trail for every retry +
    every cancellation, and the source the lifespan-replay hook reads
    to rehydrate `_RECORDS` after a docker-api restart.
    """
    __tablename__ = "job_event"

    seq = Column(Integer, primary_key=True, autoincrement=True)
    record_id = Column(String(64), nullable=False, index=True)
    chain_head_id = Column(String(64), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    state_after = Column(String(16), nullable=False)
    phase = Column(String(64), nullable=True)
    attempt_index = Column(Integer, nullable=True)
    artifact_id = Column(String(255), nullable=True, index=True)
    error = Column(Text, nullable=True)
    error_kind = Column(String(32), nullable=True)
    payload = Column(Text, nullable=False, default="{}")
    emitted_at = Column(Integer, nullable=False, index=True)


class SupportTicket(Base):
    """Analyst-submitted support ticket from the left-nav "Submit Feedback"
    form. NOT auto-attached to any failed chain (per the design's V5b
    lock-in). Plain title + description + optional category posted via
    POST /api/tickets and listed via GET /api/tickets.

    This table REPLACES the "Email Us" modal's mailto flow and the
    earlier retry-attached feedback panel design — single ticket form,
    saved to DB, viewable by engineering.
    """
    __tablename__ = "support_ticket"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_email = Column(String(255), nullable=True, index=True)
    category = Column(String(64), nullable=True)
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=False)
    status = Column(String(32), nullable=False, default="open", index=True)
    created_at = Column(Integer, nullable=False)


def _ensure_columns() -> None:
    """Add any columns declared on models that are missing from the actual tables.

    SQLAlchemy's create_all only creates missing tables — it does not patch existing
    ones. For SQLite we walk every table in Base.metadata and issue ALTER TABLE
    ADD COLUMN for each model column the live table is missing, so newly added
    fields show up without a manual migration.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue

            existing_cols = {col["name"] for col in inspector.get_columns(table.name)}

            for column in table.columns:
                if column.name in existing_cols:
                    continue

                col_type = column.type.compile(dialect=engine.dialect)
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'

                if not column.nullable:
                    # SQLite requires a default when adding a NOT NULL column to a
                    # populated table.
                    default_value = column.default.arg if column.default is not None else ""
                    if isinstance(default_value, str):
                        default_literal = f"'{default_value}'"
                    else:
                        default_literal = str(default_value)
                    ddl += f" NOT NULL DEFAULT {default_literal}"
                elif column.default is not None and not callable(getattr(column.default, "arg", None)):
                    default_value = column.default.arg
                    if isinstance(default_value, str):
                        ddl += f" DEFAULT '{default_value}'"
                    else:
                        ddl += f" DEFAULT {default_value}"

                conn.execute(text(ddl))


# Create tables
def init_db():
    # Imported here (not top-of-file) to register the ext-facade models on
    # Base without a circular import — ext_models imports Base from this module.
    from utils import ext_models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    # Patch existing tables with any newly declared columns.
    _ensure_columns()
    _seed_default_user()


def _seed_default_user():
    # Preserve the legacy login as a real user so existing data keyed to it
    # keeps working. Plaintext password by design (closed-network exercise).
    db = SessionLocal()
    try:
        existing = (db.query(User)
                    .filter(User.email == "missioncommand@pytho.ai")
                    .first())
        if existing is None:
            db.add(User(
                email="missioncommand@pytho.ai",
                password="missioncommand",
                first_name="Mission",
                last_name="Command",
                created_at=int(datetime.utcnow().timestamp() * 1000),
            ))
            db.commit()
    except Exception as e:
        print(f"_seed_default_user failed: {e}")
    finally:
        db.close()
