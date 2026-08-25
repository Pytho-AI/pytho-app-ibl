"""File upload and transcription endpoints."""
from fastapi import APIRouter, HTTPException, File, UploadFile, Form
from fastapi.responses import JSONResponse
from core.config import UPLOAD_DIR, FILE_HOST_API, OUTPUT_DIR
from core.startup import get_whisper_model
import os
import shutil
import random
import tempfile

router = APIRouter()


@router.post("/upload")
async def upload_file(file: UploadFile = File(...), title: str = Form(None)):
    filename = str(random.randint(0, 9999)) + file.filename.replace(" ", "_")
    print(f"Uploading file: {filename}")
    file_path = os.path.join(UPLOAD_DIR, filename)
    # Use provided title or derive from filename
    if not title:
        title = os.path.splitext(file.filename)[0].replace("_", " ").strip()

    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        return JSONResponse(content={
            "key": filename,
            "url": FILE_HOST_API() + filename,
            "name": file.filename,
            "title": title
        }, status_code=200)
    except Exception as e:
        print(f"Error uploading file: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    

@router.post("/save/")
async def save_file(doc_link: str = Form(...), content: str = Form(...)):
    print(f"Updating: {doc_link}")
    filename = os.path.basename(doc_link)
    file_path = os.path.join(UPLOAD_DIR, filename)
    
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return JSONResponse(content={
            "key": filename,
            "url": doc_link,
        }, status_code=200)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """
    Receive WAV audio chunk and transcribe with Whisper
    No conversion needed - WAV goes directly to Whisper!
    """
    
    temp_file = None
    
    try:
        # Validate upload
        if not audio.filename:
            raise HTTPException(status_code=400, detail="No audio file provided")
        
        # Validate it's a WAV file
        if not audio.filename.endswith('.wav'):
            raise HTTPException(
                status_code=400, 
                detail="Only WAV files accepted. Frontend should send WAV format."
            )
        
        # Read audio data
        audio_data = await audio.read()
        file_size_kb = len(audio_data) / 1024
        
        print(f"\n{'='*60}")
        print(f"Received: {audio.filename} ({file_size_kb:.2f} KB)")
        
        # Save WAV file temporarily
        temp_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix='.wav',
            dir=OUTPUT_DIR
        )
        temp_file.write(audio_data)
        temp_file.close()
        
        print(f"Processing WAV file...")
        
        # Get Whisper model
        whisper_model = get_whisper_model()
        if whisper_model is None:
            raise HTTPException(
                status_code=503,
                detail="Whisper model not loaded yet. Please try again in a moment."
            )
        
        # Transcribe with Whisper (no conversion needed!)
        print(f"Transcribing with Whisper...")
        segments, info = whisper_model.transcribe(
            temp_file.name,
            beam_size=5,
            language="en"
        )
        
        # Combine all segments into full text
        transcription_text = " ".join([segment.text for segment in segments])
        
        # Get audio info
        duration = info.duration if hasattr(info, 'duration') else None
        
        print(f"Transcription complete!")
        print(f"Text: {transcription_text[:80]}...")
        print(f"Duration: {duration:.2f}s" if duration else "")
        print(f"{'='*60}\n")
        
        # Prepare response
        response = {
            "status": "success",
            "transcription": transcription_text.strip(),
            "duration_seconds": round(duration, 2) if duration else None,
            "audio_language": info.language if hasattr(info, 'language') else "en"
        }
        
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"\n✗ Error: {str(e)}\n")
        raise HTTPException(
            status_code=500,
            detail=f"Processing error: {str(e)}"
        )
    finally:
        # Clean up temp file
        if temp_file and os.path.exists(temp_file.name):
            try:
                os.unlink(temp_file.name)
            except:
                pass
