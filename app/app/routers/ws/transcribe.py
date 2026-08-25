"""Streaming transcription WebSocket endpoint with auto-reconnect."""
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import json
from amazon_transcribe.client import TranscribeStreamingClient
from amazon_transcribe.handlers import TranscriptResultStreamHandler
from amazon_transcribe.model import TranscriptEvent

router = APIRouter()

# Rotate Transcribe sessions before the 4-hour AWS hard limit (3h 50m).
SESSION_MAX_SECONDS = 13800


class TranscriptionHandler(TranscriptResultStreamHandler):
    """Handles transcription events and sends results back to frontend"""

    def __init__(self, transcript_result_stream, websocket: WebSocket):
        super().__init__(transcript_result_stream)
        self.websocket = websocket

    async def handle_transcript_event(self, transcript_event: TranscriptEvent):
        """Process transcription results as they arrive"""
        results = transcript_event.transcript.results

        for result in results:
            if not result.is_partial:
                for alt in result.alternatives:
                    transcript = alt.transcript.strip()
                    if transcript:
                        print(f"Transcription: {transcript}")
                        await self.websocket.send_json({
                            "action": "transcription",
                            "status": "success",
                            "transcription": transcript,
                            "is_final": True
                        })


async def create_transcribe_stream():
    """Create a new AWS Transcribe streaming session."""
    client = TranscribeStreamingClient(region="us-east-1")
    stream = await client.start_stream_transcription(
        language_code="en-US",
        media_sample_rate_hz=16000,
        media_encoding="pcm",
    )
    return stream


@router.websocket("/transcribe-stream")
async def transcribe_stream_endpoint(websocket: WebSocket):
    await websocket.accept()
    print("WebSocket connected - streaming transcription ready")

    stop_requested = False
    current_stream = None

    try:
        while not stop_requested:
            # Start a (new) Transcribe session
            try:
                current_stream = await create_transcribe_stream()
            except Exception as e:
                print(f"Failed to create Transcribe stream: {e}")
                # Notify frontend so it can show the user
                try:
                    await websocket.send_json({
                        "action": "transcription",
                        "status": "error",
                        "error": f"Transcribe connection failed: {e}"
                    })
                except Exception:
                    pass
                # Wait before retrying
                await asyncio.sleep(2)
                continue

            session_start = asyncio.get_event_loop().time()
            print("Transcribe stream started (new session)")

            # Tell frontend the stream is (re)connected
            try:
                await websocket.send_json({
                    "action": "stream_status",
                    "status": "connected"
                })
            except Exception:
                pass

            handler = TranscriptionHandler(current_stream.output_stream, websocket)
            session_done = asyncio.Event()

            async def forward_audio():
                """Receive audio from frontend, forward to Transcribe."""
                nonlocal stop_requested
                try:
                    while not session_done.is_set():
                        elapsed = asyncio.get_event_loop().time() - session_start
                        if elapsed >= SESSION_MAX_SECONDS:
                            print(f"Session approaching 4h limit ({elapsed:.0f}s), rotating...")
                            session_done.set()
                            return

                        try:
                            message = await asyncio.wait_for(
                                websocket.receive(), timeout=5.0
                            )
                        except asyncio.TimeoutError:
                            continue

                        if "bytes" in message:
                            try:
                                await current_stream.input_stream.send_audio_event(
                                    audio_chunk=message["bytes"]
                                )
                            except Exception as e:
                                # Transcribe input stream broke — trigger reconnect
                                print(f"Error sending audio to Transcribe: {e}")
                                session_done.set()
                                return
                        elif "text" in message:
                            data = json.loads(message["text"])
                            if data.get("action") == "stop":
                                print("Stop signal received")
                                stop_requested = True
                                session_done.set()
                                return

                except WebSocketDisconnect:
                    print("Audio stream disconnected")
                    stop_requested = True
                    session_done.set()

            async def handle_transcriptions():
                """Process transcription results until session ends."""
                try:
                    await handler.handle_events()
                except Exception as e:
                    print(f"Transcription handler error: {e}")
                finally:
                    session_done.set()

            audio_task = asyncio.create_task(forward_audio())
            transcription_task = asyncio.create_task(handle_transcriptions())

            await session_done.wait()

            # End the current Transcribe stream gracefully
            try:
                await current_stream.input_stream.end_stream()
            except Exception:
                pass

            audio_task.cancel()
            transcription_task.cancel()
            for task in [audio_task, transcription_task]:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            # Small delay to let final transcriptions flush
            await asyncio.sleep(0.5)

            if not stop_requested:
                print("Reconnecting to Transcribe (session rotation or recovery)...")

    except WebSocketDisconnect:
        print("WebSocket disconnected by client")
    except Exception as e:
        print(f"Streaming error: {e}")
        try:
            await websocket.send_json({
                "action": "transcription",
                "status": "error",
                "error": str(e)
            })
        except Exception:
            pass
    finally:
        if current_stream:
            try:
                await current_stream.input_stream.end_stream()
            except Exception:
                pass

        try:
            await websocket.send_json({"action": "end", "status": "complete"})
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass

        print("Streaming transcription ended")
