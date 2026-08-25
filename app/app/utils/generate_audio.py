from openai import OpenAI
from utils import config
import random
import os

def synthesize_speech(
    text: str,
    voice: str,  
):
    """
    Generate speech audio using the local OpenAI-compatible API.

    Args:
        text (str): The input text to synthesize.
        voice (str): Voice or voice combination (e.g., 'af_sky+af_bella').
    """
    # Kokoro TTS is a docker-compose service; docker-api reaches it via
    # Docker DNS at kokoro-tts:8880 (TTS_HOST env in compose). Fall back
    # to SERVICE_HOST (host.docker.internal) for local-host testing
    # against an externally-launched Kokoro.
    tts_host = os.getenv("TTS_HOST", "kokoro-tts")
    tts_port = os.getenv("TTS_PORT", "8880")
    base_url = f"http://{tts_host}:{tts_port}/v1"
    filename = str(random.randint(0, 9999)) + ".mp3"

    audio_file_path = os.path.join(config.Config.UPLOAD_DIR(), filename)
    audio_file_url = f'http://localhost:8000/files/{filename}'

    
    client = OpenAI(base_url=base_url, api_key="")

    with client.audio.speech.with_streaming_response.create(
        model="kokoro",
        voice=voice,
        input=text
    ) as response:
        response.stream_to_file(audio_file_path)
        return f"""<span class="font-bold">Transcript</span><br/>{text}<br/>
<audio controls>
  <source src="{audio_file_url}" type="audio/mpeg">
  Your browser does not support the audio element.
</audio>"""
