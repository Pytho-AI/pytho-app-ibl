"""Application startup event handlers."""
from faster_whisper import WhisperModel
from threading import Thread
from concurrent.futures import ThreadPoolExecutor
from utils import database, config

# Global Whisper model (load once at startup)
whisper_model = None

# Global thread pool executor for blocking operations
executor = None


def load_whisper_model():
    """Runs in a background thread — safe for CPU-bound loading"""
    global whisper_model
    try:
        print("=" * 60)
        print("Loading Whisper model in background thread...")
        print("This may take a few minutes on first run (downloading model)...")
        print("=" * 60)

        whisper_model = WhisperModel(
            config.Config.AUDIO_MODEL(),
            device="cpu",
            compute_type="int8",
            download_root="./models"
        )

        print("\nWhisper model loaded successfully")
        print("=" * 60 + "\n")

    except Exception as e:
        print(f"\nFailed to load Whisper model: {str(e)}")


def get_whisper_model():
    """Get the global Whisper model instance."""
    return whisper_model


def get_executor():
    """Get the global ThreadPoolExecutor instance."""
    return executor


async def startup_event(start_whisper: bool = True):
    """FastAPI startup event handler.

    start_whisper=False when the caller owns whisper loading — ibl's
    lifespan starts its own loader (edge-tuned, pre-staged model dir)
    and publishes the model into this module's global so
    get_whisper_model() serves both loaders.
    """
    global executor

    database.Base.metadata.create_all(bind=database.engine)

    # Migration: add ai_answer column to rfis table if not exists
    try:
        from sqlalchemy import text
        with database.engine.connect() as conn:
            conn.execute(text("ALTER TABLE rfis ADD COLUMN ai_answer TEXT"))
            conn.commit()
            print("Migration: added ai_answer column to rfis table")
    except Exception:
        pass  # Column already exists

    # Migration: add is_ai_response column to rfi_responses table if not exists
    try:
        from sqlalchemy import text as text2
        with database.engine.connect() as conn:
            conn.execute(text2("ALTER TABLE rfi_responses ADD COLUMN is_ai_response INTEGER DEFAULT 0"))
            conn.commit()
            print("Migration: added is_ai_response column to rfi_responses table")
    except Exception:
        pass  # Column already exists

    # Migration: add seminar_id column to rfis table if not exists
    try:
        from sqlalchemy import text as text3
        with database.engine.connect() as conn:
            conn.execute(text3("ALTER TABLE rfis ADD COLUMN seminar_id TEXT"))
            conn.commit()
            print("Migration: added seminar_id column to rfis table")
    except Exception:
        pass  # Column already exists

    # Migration: backfill seminar_id on RFIs where it's NULL
    # Old RFIs created before seminar scoping have no seminar_id,
    # making them invisible to participants filtered by seminar.
    try:
        from sqlalchemy import text as text4
        with database.engine.connect() as conn:
            result = conn.execute(text4("""
                UPDATE rfis
                SET seminar_id = (
                    SELECT pm.seminar_id
                    FROM project_members pm
                    WHERE pm.user_id = rfis.created_by_user_id
                      AND pm.project_id = rfis.project_id
                      AND pm.role = 'participant'
                      AND pm.seminar_id IS NOT NULL
                    LIMIT 1
                )
                WHERE rfis.seminar_id IS NULL
                  AND EXISTS (
                    SELECT 1 FROM project_members pm
                    WHERE pm.user_id = rfis.created_by_user_id
                      AND pm.project_id = rfis.project_id
                      AND pm.role = 'participant'
                      AND pm.seminar_id IS NOT NULL
                  )
            """))
            conn.commit()
            if result.rowcount > 0:
                print(f"Migration: backfilled seminar_id on {result.rowcount} RFIs")
    except Exception as e:
        print(f"Migration: seminar_id backfill skipped or failed: {e}")

    # Initialize thread pool executor
    executor = ThreadPoolExecutor(max_workers=10)
    print("Thread pool executor initialized")

    if start_whisper and not config.Config.IS_CLOUD_ENV():
        """Launch model loading in a separate thread — non-blocking"""
        thread = Thread(target=load_whisper_model, daemon=True)
        thread.start()
        print("Startup event triggered — Whisper model loading in background thread.")


async def shutdown_event():
    """FastAPI shutdown event handler."""
    global executor
    
    # Properly shutdown thread pool executor
    if executor is not None:
        print("Shutting down thread pool executor...")
        executor.shutdown(wait=True, cancel_futures=False)
        print("Thread pool executor shut down complete")
