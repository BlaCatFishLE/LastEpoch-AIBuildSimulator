#!/usr/bin/env python
"""Launcher so the toolkit works from the repository root without installation.

Usage::

    python tools/affix_db/run.py query --affix fire_resistance --base "Body Armor" --tier 7

The package itself is importable as ``affix_db`` when ``tools/`` is on
``PYTHONPATH`` (or when running from the ``tools/`` directory).
"""

from __future__ import annotations

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1]
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from affix_db.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
