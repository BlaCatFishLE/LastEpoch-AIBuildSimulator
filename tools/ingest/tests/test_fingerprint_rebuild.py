"""Logical fingerprint: rebuilds of the same input must compare equal.

workguide §13「离线重建」/「重复执行」: normalized content is compared by logical
fingerprint, not file bytes; timestamps and autoincrement ids are excluded so
two rebuilds from the same synthetic snapshot match, while a real content change
still changes the fingerprint.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic_support as support  # noqa: E402
from le_data.store import (  # noqa: E402
    integrity_report,
    logical_fingerprint,
    table_counts,
)

FIXTURE = "synthetic_boundary_records.jsonl"
SNAPSHOT = "synthetic-snap-boundary"


class FingerprintRebuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)

    def _build(self, name: str, **kwargs):
        return support.build_normalized_db(
            self.tmp / name, FIXTURE, SNAPSHOT, **kwargs
        )

    def test_two_rebuilds_have_the_same_logical_fingerprint(self):
        first = self._build("rebuild-a.sqlite")
        second = self._build("rebuild-b.sqlite", reverse=True)
        try:
            fp_a = logical_fingerprint(first)
            fp_b = logical_fingerprint(second)
            self.assertEqual(fp_a["logical_sha256"], fp_b["logical_sha256"])
            self.assertEqual(table_counts(first), table_counts(second))
            for table, meta in fp_a["tables"].items():
                self.assertEqual(
                    meta["sha256"], fp_b["tables"][table]["sha256"], f"table {table}"
                )
        finally:
            first.close()
            second.close()

    def test_timestamps_are_excluded_from_the_fingerprint(self):
        conn = self._build("rebuild-ts.sqlite")
        try:
            before = logical_fingerprint(conn)["logical_sha256"]
            for table, column in (
                ("entities", "created_at"),
                ("source_id_map", "created_at"),
                ("dataset_meta", "created_at"),
            ):
                conn.execute(
                    f"UPDATE {table} SET {column} = '1999-01-01T00:00:00+00:00'"
                )
            conn.commit()
            after = logical_fingerprint(conn)["logical_sha256"]
            self.assertEqual(before, after)
        finally:
            conn.close()

    def test_content_change_changes_the_fingerprint(self):
        baseline = self._build("rebuild-base.sqlite")
        mutated = self._build("rebuild-mutated.sqlite", mutate=_change_node_points)
        try:
            self.assertNotEqual(
                logical_fingerprint(baseline)["logical_sha256"],
                logical_fingerprint(mutated)["logical_sha256"],
            )
        finally:
            baseline.close()
            mutated.close()

    def test_rebuilt_database_is_integrity_clean(self):
        conn = self._build("rebuild-integrity.sqlite")
        try:
            report = integrity_report(conn)
            self.assertTrue(report["integrity_ok"])
            self.assertTrue(report["foreign_keys_ok"])
        finally:
            conn.close()

    def test_rebuild_does_not_duplicate_rows(self):
        """Same fixture into a fresh DB twice gives equal counts (no inflation)."""
        first = self._build("rebuild-count-a.sqlite")
        second = self._build("rebuild-count-b.sqlite")
        try:
            self.assertEqual(table_counts(first), table_counts(second))
            self.assertEqual(
                first.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0],
                second.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0],
            )
        finally:
            first.close()
            second.close()


def _change_node_points(records) -> None:
    for record in records:
        if record.entity_kind == "skill_tree_node" and record.source_id == "12":
            record.raw["maxPoints"] = 3


if __name__ == "__main__":
    unittest.main()
