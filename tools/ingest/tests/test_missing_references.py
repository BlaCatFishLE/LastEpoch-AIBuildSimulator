"""Missing references: keep raw_target_ref + issue, never fabricate entities.

workguide §13 row「引用缺失」: a relationship pointing at a non-existent
sub-skill/buff must keep the raw target reference and produce an issue instead of
inventing an entity.

Fixture: ``tools/ingest/fixtures/synthetic_reference_records.jsonl`` (synthetic).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic_support as support  # noqa: E402
from le_data.validation.checks import Validator  # noqa: E402

SNAPSHOT = "synthetic-snap-refs"


class MissingReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = support.new_temp_db(str(self.tmp))
        self.summary = support.load_and_normalize(
            self.conn, "synthetic_reference_records.jsonl", SNAPSHOT
        )

    def tearDown(self):
        self.conn.close()

    def test_no_entity_is_fabricated_for_missing_targets(self):
        for missing in ("missing-skill", "missing-buff", "tree-missing-999"):
            row = self.conn.execute(
                "SELECT COUNT(*) FROM entities WHERE entity_id LIKE ?",
                (f"%{missing}%",),
            ).fetchone()[0]
            self.assertEqual(row, 0, f"fabricated entity for {missing}")
        kinds = {
            row["kind"]
            for row in self.conn.execute("SELECT kind FROM entities")
        }
        self.assertNotIn("skill", kinds)
        self.assertNotIn("buff", kinds)

    def test_missing_node_prerequisite_keeps_raw_reference(self):
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM node_edges WHERE raw LIKE '%999%'"
            )
        ]
        self.assertEqual(len(rows), 1)
        edge = rows[0]
        self.assertIsNone(edge["from_node_entity_id"])
        self.assertEqual(edge["to_node_entity_id"], "node:tunklab:77:2")
        self.assertEqual(edge["points_required"], 5)
        self.assertEqual(json.loads(edge["raw"]), {"nodeId": 999, "requirement": 5})

    def test_known_node_prerequisite_is_linked(self):
        rows = [
            dict(row)
            for row in self.conn.execute(
                "SELECT * FROM node_edges WHERE raw LIKE '%nodeId\": 1%'"
            )
        ]
        self.assertEqual(len(rows), 1)
        edge = rows[0]
        self.assertEqual(edge["from_node_entity_id"], "node:tunklab:77:1")
        self.assertEqual(edge["to_node_entity_id"], "node:tunklab:77:2")
        self.assertEqual(edge["points_required"], 2)

    def test_unresolved_class_tree_relationship_keeps_raw_target_ref(self):
        row = self.conn.execute(
            "SELECT to_entity_id, raw_target_ref, resolution_status "
            "FROM entity_relationships WHERE relationship_type = 'has_tree'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["to_entity_id"])
        self.assertEqual(row["resolution_status"], "unresolved")
        self.assertIn("tree-missing-999", row["raw_target_ref"])

    def test_unresolved_relation_record_keeps_both_ends(self):
        row = self.conn.execute(
            "SELECT from_entity_id, to_entity_id, raw_target_ref, resolution_status "
            "FROM entity_relationships WHERE relationship_type = 'grants_buff'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["from_entity_id"])
        self.assertIsNone(row["to_entity_id"])
        self.assertEqual(row["resolution_status"], "unresolved")
        self.assertIn("missing-skill", row["raw_target_ref"])
        self.assertIn("missing-buff", row["raw_target_ref"])

    def test_validation_rules_for_unresolved_references_pass(self):
        result = Validator(self.conn, {}).run_all()
        checks = {item["check_id"]: item["status"] for item in result["checks"]}
        for check_id in (
            "sqlite_integrity",
            "foreign_keys",
            "relationships_unresolved_have_ref",
            "relationships_resolved_have_target",
            "nodes_tree_reference",
            "entities_source_mapped",
        ):
            self.assertEqual(checks.get(check_id), "pass", f"{check_id}: {checks.get(check_id)}")


if __name__ == "__main__":
    unittest.main()
