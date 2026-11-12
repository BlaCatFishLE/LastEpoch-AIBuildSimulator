"""Shared helpers for the synthetic boundary/failure-mode tests.

The fixtures under ``tools/ingest/fixtures/synthetic_*.jsonl`` are artificial:
they are shaped like Tunklab source records but contain no real game data.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Iterable

INGEST_DIR = Path(__file__).resolve().parents[1]
FIXTURES_DIR = INGEST_DIR / "fixtures"
SRC_DIR = INGEST_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from le_data.normalize.base import Normalizer  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    upsert_dataset_meta,
)

TUNKLAB = "tunklab"


def new_db(path: Path):
    conn = connect(path)
    apply_migrations(conn)
    return conn


def new_temp_db(tmpdir: str, name: str = "synthetic.sqlite"):
    return new_db(Path(tmpdir) / name)


def insert_snapshot(conn, snapshot_id: str, source: str = TUNKLAB, status: str = "ok") -> None:
    insert_row(
        conn,
        "source_snapshots",
        {"source_snapshot_id": snapshot_id, "source": source, "status": status},
        or_ignore=True,
    )


def records_from_fixture(name: str) -> list[SourceRecord]:
    return [SourceRecord.from_dict(row) for row in read_jsonl(FIXTURES_DIR / name)]


def insert_source_records(conn, records: Iterable[SourceRecord]) -> None:
    """Mirror what ``le_data.cli.cmd_normalize`` does before mapping.

    Keeping the raw payload in ``source_records`` is what guarantees that
    unknown source fields are not lost even when a typed mapping drops them.
    """
    for record in records:
        insert_row(
            conn,
            "source_records",
            {
                "record_id": record.record_id,
                "source_snapshot_id": record.source_snapshot_id,
                "source": record.source,
                "resource_name": record.resource_name,
                "entity_kind": record.entity_kind,
                "source_scope": record.source_scope,
                "source_id": record.source_id,
                "locator": record.locator,
                "raw_payload": json.dumps(record.raw, ensure_ascii=False),
                "parse_status": record.parse_status,
                "notes": "; ".join(record.notes) if record.notes else None,
            },
            or_ignore=True,
        )


def normalize_records(conn, snapshot_id: str, records: list[SourceRecord]):
    return Normalizer(conn, TUNKLAB, snapshot_id).normalize_records(records)


def load_and_normalize(conn, fixture_name: str, snapshot_id: str):
    """Insert the fixture's raw records and normalize them; returns summary."""
    records = records_from_fixture(fixture_name)
    insert_snapshot(conn, snapshot_id)
    insert_source_records(conn, records)
    summary = normalize_records(conn, snapshot_id, records)
    upsert_dataset_meta(conn, "synthetic-ds", status="staging")
    conn.commit()
    return summary


def build_normalized_db(
    path: Path,
    fixture_name: str,
    snapshot_id: str,
    *,
    reverse: bool = False,
    mutate=None,
):
    """Build a fresh normalized database from a synthetic fixture.

    ``mutate`` is an optional callable ``(records) -> None`` applied before
    normalization, used to prove that content changes alter the fingerprint.
    """
    conn = new_db(path)
    records = records_from_fixture(fixture_name)
    if reverse:
        # Keep the skill_tree record first: nodes have a NOT NULL foreign key on
        # the tree entity, so a rebuild only reorders the independent records.
        records = records[:1] + list(reversed(records[1:]))
    if mutate is not None:
        mutate(records)
    insert_snapshot(conn, snapshot_id)
    insert_source_records(conn, records)
    normalize_records(conn, snapshot_id, records)
    upsert_dataset_meta(conn, "synthetic-ds", status="staging")
    conn.commit()
    return conn


def temp_dir(testcase) -> Path:
    tmp = tempfile.TemporaryDirectory()
    testcase.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def entity_row(conn, entity_id: str):
    row = conn.execute(
        "SELECT * FROM entities WHERE entity_id = ?", (entity_id,)
    ).fetchone()
    return row


def evidence_rows(conn, entity_id: str, field_path: str):
    return [
        dict(row)
        for row in conn.execute(
            "SELECT field_path, raw_value, normalized_value, unit, evidence_status "
            "FROM field_evidence WHERE entity_id = ? AND field_path = ? "
            "ORDER BY evidence_id",
            (entity_id, field_path),
        )
    ]
