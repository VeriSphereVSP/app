import sys
from pathlib import Path

# Add <repo>/app to PYTHONPATH so `import app.*` works
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

