#!/usr/bin/env python3
"""EdgeLab: live sysfs instruments + paired actual EdgeCtx experiments.

  python3 scripts/ui/edgelab_demo.py

The bench requires its declared Python dependencies; use --lab-python /path/to
venv/bin/python if they are installed in a separate environment. No paid LLM,
physical devices or existing virtual machines are used.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.ui.atlas_demo import main  # noqa: E402

if __name__ == "__main__":
    sys.argv.append("--lab")
    sys.exit(main())
