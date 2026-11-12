"""SQLite storage layer: migrations, typed inserts, logical fingerprints.

The database is a first-class deliverable, so this module keeps the SQL small
and explicit.  Inserts go through :func:`insert_row`, which rejects unknown
column names instead of silently dropping data.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .paths import MIGRATIONS_DIR

SCHEMA_VERSION = "1"
NORMALIZER_VERSION = "0.1.0"

# Tables included in the logical content fingerprint.  Provenance timestamps
# and autoincrement row ids are excluded so that two rebuilds from the same
# raw snapshots compare equal even though they run at different times.
# Dataset identity tables are provenance, not content: a rebuild uses a new
# dataset_id by definition, so they must not affect the content fingerprint.
FINGERPRINT_TABLES = [
    "source_snapshots",
    "raw_resources",
    "source_records",
    "entities",
    "entity_names",
    "source_id_map",
    "classes",
    "masteries",
    "skills",
    "skill_components",
    "trees",
    "nodes",
    "node_edges",
    "tags",
    "entity_tags",
    "stats",
    "effects",
    "effect_values",
    "buffs",
    "ailments",
    "items",
    "affixes",
    "affix_tiers",
    "item_affixes",
    "set_memberships",
    "idols",
    "blessings",
    "season_entities",
    "minions",
    "entity_relationships",
    "field_evidence",
    "field_selections",
    "data_issues",
    "mapping_registry",
    "translations",
]

# Columns that are metadata about when/why a row was written, not content.
FINGERPRINT_IGNORED_COLUMNS = {
    "created_at",
    "resolved_at",
    "applied_at",
    "evidence_id",
    "selection_id",
    "name_id",
    "fetched_at",
    "fetch_started_at",
    "fetch_finished_at",
    "file_sha256",
    "logical_sha256",
    "dataset_id",
    "notes",
}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, readonly: bool = False) -> sqlite3.Connection:
    path = Path(path)
    if readonly:
        if not path.exists():
            raise FileNotFoundError(f"database not found: {path}")
        uri = f"file:{path.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if not readonly:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def apply_migrations(conn: sqlite3.Connection, migrations_dir: Path | None = None) -> list[int]:
    directory = Path(migrations_dir) if migrations_dir else MIGRATIONS_DIR
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    executed: list[int] = []
    for path in sorted(directory.glob("*.sql")):
        try:
            version = int(path.name.split("_", 1)[0])
        except ValueError:
            continue
        if version in applied:
            continue
        script = path.read_text(encoding="utf-8")
        conn.executescript(script)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now_utc()),
        )
        conn.commit()
        executed.append(version)
    return executed


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def insert_row(
    conn: sqlite3.Connection,
    table: str,
    values: dict[str, Any],
    *,
    or_ignore: bool = False,
    or_replace: bool = False,
) -> None:
    columns = table_columns(conn, table)
    unknown = set(values) - columns
    if unknown:
        raise ValueError(f"unknown columns for {table}: {sorted(unknown)}")
    data = {key: _coerce(value) for key, value in values.items()}
    placeholders = ", ".join("?" for _ in data)
    verb = "INSERT OR IGNORE" if or_ignore else "INSERT OR REPLACE" if or_replace else "INSERT"
    conn.execute(
        f"{verb} INTO {table} ({', '.join(data)}) VALUES ({placeholders})",
        list(data.values()),
    )


def insert_rows(
    conn: sqlite3.Connection,
    table: str,
    rows: Iterable[dict[str, Any]],
    *,
    or_ignore: bool = False,
) -> int:
    count = 0
    for row in rows:
        insert_row(conn, table, row, or_ignore=or_ignore)
        count += 1
    return count


def _coerce(value: Any) -> Any:
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None or isinstance(value, (str, int, float)):
        return value
    return str(value)


def upsert_dataset_meta(
    conn: sqlite3.Connection,
    dataset_id: str,
    *,
    status: str,
    target_game_version: str | None = None,
    target_season: str | None = None,
    compatibility: str | None = None,
    notes: str | None = None,
    mapping_revision: str = "1",
) -> None:
    insert_row(
        conn,
        "dataset_meta",
        {
            "dataset_id": dataset_id,
            "schema_version": SCHEMA_VERSION,
            "normalizer_version": NORMALIZER_VERSION,
            "mapping_revision": mapping_revision,
            "status": status,
            "created_at": now_utc(),
            "target_game_version": target_game_version,
            "target_season": target_season,
            "compatibility_conclusion": compatibility,
            "notes": notes,
        },
        or_replace=True,
    )


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# -- fingerprinting ---------------------------------------------------------
def table_rows_canonical(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        rows = list(conn.execute(f"SELECT * FROM {table}"))
    except sqlite3.OperationalError:
        return []
    rendered: list[str] = []
    for row in rows:
        data = {
            key: row[key]
            for key in row.keys()
            if key not in FINGERPRINT_IGNORED_COLUMNS
        }
        rendered.append(json.dumps(data, ensure_ascii=False, sort_keys=True, default=str))
    rendered.sort()
    return rendered


def logical_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    """Deterministic content hash over normalized tables (not the file bytes)."""
    per_table: dict[str, Any] = {}
    overall = hashlib.sha256()
    for table in FINGERPRINT_TABLES:
        rows = table_rows_canonical(conn, table)
        digest = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()
        per_table[table] = {"rows": len(rows), "sha256": digest}
        overall.update(table.encode("utf-8"))
        overall.update(digest.encode("utf-8"))
    return {
        "logical_sha256": overall.hexdigest(),
        "tables": per_table,
        "generated_at": now_utc(),
    }


def table_counts(conn: sqlite3.Connection, tables: Iterable[str] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in tables or FINGERPRINT_TABLES:
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
    return counts


def checkpoint(conn: sqlite3.Connection) -> None:
    """Flush WAL so the published .sqlite file is self-contained."""
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def integrity_report(conn: sqlite3.Connection) -> dict[str, Any]:
    integrity = [row[0] for row in conn.execute("PRAGMA integrity_check")]
    fk_rows = [tuple(row) for row in conn.execute("PRAGMA foreign_key_check")]
    return {
        "integrity_check": integrity,
        "integrity_ok": integrity == ["ok"],
        "foreign_key_violations": fk_rows,
        "foreign_keys_ok": not fk_rows,
    }
