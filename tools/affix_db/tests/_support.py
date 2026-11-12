"""Shared paths/helpers for the affix_db test-suite (stdlib unittest only)."""

from __future__ import annotations

import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
AFFIX_DB_DIR = TESTS_DIR.parent
TOOLS_DIR = AFFIX_DB_DIR.parent
REPO_ROOT = TOOLS_DIR.parent

if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

SNAPSHOT_DIR = REPO_ROOT / "data" / "affix-db" / "v1" / "source-snapshot"
DATA_DIR = REPO_ROOT / "data" / "affix-db" / "v1"
DB_PATH = DATA_DIR / "normalized" / "affix-db.sqlite"
MANIFEST_PATH = DATA_DIR / "manifest.json"
FIXTURES_DIR = TESTS_DIR / "fixtures"
VERIFICATION_DIR = DATA_DIR / "verification"


def load_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8", errors="replace")
