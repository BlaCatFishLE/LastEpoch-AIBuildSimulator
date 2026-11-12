"""Dataset diff deletion confirmation boundaries.

workguide §12.3 / §13「更新删除边界」: a removal may only be reported as
confirmed when a full successful enumeration of the source is available;
otherwise it stays "undetermined".  ``le_data.report.diff`` receives that
decision explicitly via ``full_enumeration``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.report.diff import diff_databases  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    upsert_dataset_meta,
)


def build_entity_db(path: Path, entity_ids: list[str], dataset_id: str = "diff-ds") -> Path:
    conn = connect(path)
    apply_migrations(conn)
    upsert_dataset_meta(conn, dataset_id, status="valid")
    for entity_id in entity_ids:
        insert_row(
            conn,
            "entities",
            {
                "entity_id": entity_id,
                "kind": "skill",
                "canonical_name": entity_id,
                "record_status": "active",
                "primary_source": "tunklab",
                "created_at": "2026-01-01T00:00:00+00:00",
            },
        )
    conn.commit()
    conn.close()
    return path


class DiffDeletionBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_removals_are_undetermined_without_full_enumeration(self):
        old = build_entity_db(self.tmp / "old.sqlite", ["e1", "e2", "e3"])
        new = build_entity_db(self.tmp / "new.sqlite", ["e1"])
        result = diff_databases(old, new, full_enumeration=False)
        confirmation = result["deletion_confirmation"]
        self.assertFalse(confirmation["confirmed"])
        self.assertEqual(confirmation["removed_candidates"], 2)
        self.assertIn("undetermined", confirmation["note"])
        self.assertFalse(result["full_enumeration_available"])
        self.assertEqual(result["entities"]["removed_count"], 2)
        self.assertEqual(result["entities"]["added_count"], 0)
        self.assertEqual(result["changed_tables"]["entities"]["removed"], 2)

    def test_additions_do_not_confirm_deletions(self):
        """A partial/failed crawl that adds rows must not confirm any removal."""
        old = build_entity_db(self.tmp / "old2.sqlite", ["e1", "e2"])
        new = build_entity_db(self.tmp / "new2.sqlite", ["e1", "e3"])
        result = diff_databases(old, new, full_enumeration=False)
        self.assertFalse(result["deletion_confirmation"]["confirmed"])
        self.assertEqual(result["deletion_confirmation"]["removed_candidates"], 1)
        self.assertEqual(result["entities"]["added_count"], 1)
        self.assertEqual(result["entities"]["removed_count"], 1)

    def test_identical_databases_have_no_deletions(self):
        old = build_entity_db(self.tmp / "same1.sqlite", ["e1", "e2"])
        new = build_entity_db(self.tmp / "same2.sqlite", ["e1", "e2"])
        result = diff_databases(old, new, full_enumeration=False)
        self.assertTrue(result["identical"])
        self.assertEqual(result["entities"]["removed_count"], 0)
        self.assertFalse(result["deletion_confirmation"]["confirmed"])

    def test_full_enumeration_confirms_removals(self):
        """BUG task-5-bug-1: ``diff_databases(full_enumeration=True)`` crashes.

        Repro: two DBs where ``new`` has one fewer entity; call
        ``diff_databases(old, new, full_enumeration=True)``.
        Actual: ``KeyError: 'added'`` at ``report/diff.py`` (it reads
        ``entity_level["added"]`` but the dict uses ``added_count``); after that
        the condition ``not entity_level["added"] == 0`` would still confirm a
        deletion only when rows were *added*, which is backwards.  Expected: run
        without error and report ``confirmed=True`` for this full-scan removal.
        """
        old = build_entity_db(self.tmp / "full_old.sqlite", ["e1", "e2"])
        new = build_entity_db(self.tmp / "full_new.sqlite", ["e1"])
        result = diff_databases(old, new, full_enumeration=True)
        self.assertTrue(result["full_enumeration_available"])
        self.assertTrue(result["deletion_confirmation"]["confirmed"])
        self.assertEqual(result["deletion_confirmation"]["removed_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
