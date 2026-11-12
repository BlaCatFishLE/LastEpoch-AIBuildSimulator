"""Tests for the storage layer: migrations, inserts, fingerprints, integrity."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    integrity_report,
    logical_fingerprint,
    table_counts,
    upsert_dataset_meta,
)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "test.sqlite"
        self.conn = connect(self.path)
        apply_migrations(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_schema_tables_exist(self):
        names = {
            row[0]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in ("dataset_meta", "source_snapshots", "entities", "skills", "nodes",
                      "buffs", "affixes", "field_evidence", "data_issues"):
            self.assertIn(table, names)

    def test_unknown_column_rejected(self):
        with self.assertRaises(ValueError):
            insert_row(self.conn, "entities", {"entity_id": "x", "nope": 1})

    def test_integrity_and_foreign_keys(self):
        report = integrity_report(self.conn)
        self.assertTrue(report["integrity_ok"])
        self.assertTrue(report["foreign_keys_ok"])

    def test_fingerprint_is_content_deterministic(self):
        def build(path: Path, order: list[str]) -> str:
            conn = connect(path)
            apply_migrations(conn)
            upsert_dataset_meta(conn, "ds-1", status="staging")
            for kind in order:
                insert_row(
                    conn,
                    "entities",
                    {
                        "entity_id": f"skill:test:scope:{kind}",
                        "kind": "skill",
                        "canonical_name": kind,
                        "record_status": "active",
                        "primary_source": "test",
                        "created_at": "2026-01-01T00:00:00+00:00",
                    },
                )
            conn.commit()
            digest = logical_fingerprint(conn)["logical_sha256"]
            conn.close()
            return digest

        first = build(Path(tempfile.mkdtemp()) / "a.sqlite", ["b", "a"])
        second = build(Path(tempfile.mkdtemp()) / "b.sqlite", ["a", "b"])
        self.assertEqual(first, second)

    def test_counts(self):
        upsert_dataset_meta(self.conn, "ds-1", status="staging")
        self.conn.commit()
        counts = table_counts(self.conn, ["dataset_meta", "entities"])
        self.assertEqual(counts["dataset_meta"], 1)
        self.assertEqual(counts["entities"], 0)


if __name__ == "__main__":
    unittest.main()
