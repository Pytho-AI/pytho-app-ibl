"""Application configuration constants."""
import os
from pathlib import Path

# TODO: put it in env
UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

def FILE_HOST_API():
    return os.environ['FILE_HOST_API']

OUTPUT_DIR = Path("transcripts")
OUTPUT_DIR.mkdir(exist_ok=True)
