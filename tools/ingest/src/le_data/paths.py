"""Path resolution for the Last Epoch data pipeline.

All project paths are derived from this file's location so that the CLI does
not depend on the caller's current directory: ``tools/ingest/src/le_data`` ->
project root is four levels up.
"""
from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
INGEST_DIR = PACKAGE_DIR.parents[1]          # tools/ingest
PROJECT_ROOT = PACKAGE_DIR.parents[3]        # project root
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGING_DIR = DATA_DIR / "staging"
NORMALIZED_DIR = DATA_DIR / "normalized"
RELEASES_DIR = DATA_DIR / "releases"
MAPPINGS_DIR = DATA_DIR / "mappings"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"
MIGRATIONS_DIR = INGEST_DIR / "migrations"
FIXTURES_DIR = INGEST_DIR / "fixtures"
REFERENCES_DIR = PROJECT_ROOT / "references"


def dataset_db_path(dataset_id: str) -> Path:
    return NORMALIZED_DIR / dataset_id / "data.sqlite"


def staging_dir(run_id: str) -> Path:
    return STAGING_DIR / run_id


def raw_source_dir(source: str, snapshot_id: str) -> Path:
    return RAW_DIR / source / snapshot_id


def report_dir(run_id: str) -> Path:
    return REPORTS_DIR / run_id


def ensure_dirs(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def tool_env(name: str, default: str | None = None) -> str | None:
    """Read an env var used for tool discovery (never mutated by this project)."""
    return os.environ.get(name, default)
