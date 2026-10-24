"""Run the versioned snapshot quality evaluation.

This wrapper keeps the documented command working from a fresh checkout without
requiring an editable install:

    python scripts/evaluate.py --baseline evaluation/corpus/v1/baseline.json
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agentwriter.evaluation import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
