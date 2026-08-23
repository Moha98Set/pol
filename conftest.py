"""
pytest bootstrap.

The project's modules live at the repo root and are imported by bare name
(`import arbmath`). Tests live in tests/, so the root must be on sys.path
before they import anything.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
