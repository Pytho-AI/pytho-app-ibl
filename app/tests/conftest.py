import sys
from pathlib import Path

# Add app/app/ to Python path so `from utils import database` etc. works
sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
