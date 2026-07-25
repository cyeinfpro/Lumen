"""Image-job test package."""

import sys
from pathlib import Path

_IMAGE_JOB_ROOT = Path(__file__).resolve().parents[1]
if str(_IMAGE_JOB_ROOT) not in sys.path:
    sys.path.insert(0, str(_IMAGE_JOB_ROOT))
