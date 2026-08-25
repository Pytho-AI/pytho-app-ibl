# config.py
from dotenv import load_dotenv
import os

class Config:
    def SERVICE_HOST(): 
        return os.getenv("SERVICE_HOST")
    def UPLOAD_DIR():
        return os.getenv("UPLOAD_DIR")
    def ES_ACCESS_KEY():
        return os.getenv("ES_ACCESS_KEY")
    def ES_ACCESS_ID():
        return os.getenv("ES_ACCESS_ID")
    def ES_HOST():
        return os.getenv("ES_HOST", os.getenv("SERVICE_HOST"))
    def Q_AND_A_MODEL():
        return os.getenv("Q_AND_A_MODEL")
    def AUDIO_MODEL():
        return os.getenv("AUDIO_MODEL")
    def DOCGEN_SCAFFOLD_MODEL():
        return os.getenv("DOCGEN_SCAFFOLD_MODEL", "gemma3:27b-it-qat")
    def DOCGEN_WRITER_MODEL():
        return os.getenv("DOCGEN_WRITER_MODEL", "gemma3:27b-it-qat")
    def IMAGE_API():
        host = os.getenv("IMAGE_API_HOST", os.getenv("SERVICE_HOST"))
        port = os.getenv("IMAGE_API_PORT", "7860")
        return f'http://{host}:{port}/sdapi/v1/txt2img'
    def VLLM_API():
        return os.getenv("VLLM_API")
