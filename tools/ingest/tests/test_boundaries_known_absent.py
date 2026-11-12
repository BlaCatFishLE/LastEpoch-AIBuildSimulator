"""Synthetic boundary tests: known vs absent/not_applicable, 0/false/null.

workguide §13 rows:
  * 缺失与 0：合成 fixture 包含 0、false、null、空描述与不存在字段
  * 未知值不填 0，状态与实际值分开

The data comes from ``tools/ingest/fixtures/synthetic_boundary_records.jsonl``
(artificial, not real Last Epoch / Tunklab data).
"""
from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic_support as support  # noqa: E402
from le_data.normalize.base import EntityPlan, Normalizer, make_entity_id  # noqa: E402


class KnownAbsentBoundaryTests(unittest.TestCase):
    """0 / false / null / empty string / missing field distinction."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = support.new_temp_db(str(self.tmp))
        self.snapshot = "synthetic-snap-boundary"
        self.summary = support.load_and_normalize(
            self.conn, "synthetic_boundary_records.jsonl", self.snapshot
        )

    def tearDown(self):
        self.conn.close()

    def node(self, node_id: str):
        return self.conn.execute(
            "SELECT * FROM nodes WHERE entity_id = ?",
            (f"node:tunklab:42:{node_id}",),
        ).fetchone()

    def test_missing_numeric_field_stays_null_not_zero(self):
        """A field absent from the source is NULL, never a fabricated 0."""
        node = self.node("11")
        self.assertIsNone(node["max_points"])
        self.assertIsNone(node["description"])
        zero_count = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE entity_id = ? AND max_points = 0",
            (f"node:tunklab:42:11",),
        ).fetchone()[0]
        self.assertEqual(zero_count, 0)

    def test_explicit_zero_is_preserved_as_known_zero(self):
        """0 in the source is real data: stored as 0 with known evidence."""
        node = self.node("12")
        self.assertEqual(node["max_points"], 0)
        rows = support.evidence_rows(self.conn, "node:tunklab:42:12", "max_points")
        self.assertEqual(rows[0]["evidence_status"], "known")
        self.assertEqual(rows[0]["raw_value"], "0")
        self.assertEqual(rows[0]["normalized_value"], "0")

    def test_empty_description_produces_absent_evidence(self):
        """``description: ""`` is recorded as absent, not as a known value."""
        rows = support.evidence_rows(self.conn, "node:tunklab:42:10", "description")
        statuses = [row["evidence_status"] for row in rows]
        self.assertIn("absent", statuses)
        absent = next(row for row in rows if row["evidence_status"] == "absent")
        self.assertEqual(absent["raw_value"], "")
        # the raw payload really contains "" so the typed column keeps it
        self.assertEqual(self.node("10")["description"], "")

    def test_null_description_produces_absent_evidence(self):
        rows = support.evidence_rows(self.conn, "node:tunklab:42:11", "description")
        self.assertTrue(any(row["evidence_status"] == "absent" for row in rows))
        self.assertIsNone(self.node("11")["description"])

    def test_false_is_preserved_as_false_not_dropped(self):
        """``positive: false`` stays a meaningful boolean (debuff), not absent."""
        buff = self.conn.execute(
            "SELECT * FROM buffs WHERE entity_id = ?", ("buff:tunklab:_:200",)
        ).fetchone()
        self.assertEqual(buff["buff_category"], "debuff")
        rows = support.evidence_rows(self.conn, "buff:tunklab:_:200", "buff_category")
        self.assertEqual(rows[0]["raw_value"], "false")
        self.assertEqual(rows[0]["normalized_value"], "debuff")
        lifecycle = json.loads(buff["lifecycle"])
        self.assertIs(lifecycle["noFixedDuration"], True)
        # false boolean in entity extensions is kept verbatim
        node_ext = json.loads(
            support.entity_row(self.conn, "node:tunklab:42:10")["extensions"]
        )
        self.assertIs(node_ext["no_scaling_type"], False)

    def test_zero_cap_is_unknown_semantics_not_uncapped(self):
        """Ailment maxInstances=0 is a UI display value with unknown mechanics.

        The UI's 0 is not evidence of an uncapped stack rule (review P1-6), so no
        numeric value is stored and the evidence is ``unparsed`` with a note.  The
        raw 0 is kept in evidence and in stack_rule; neither the evidence note nor
        the stack_rule may claim "uncapped".
        """
        rows = support.evidence_rows(self.conn, "ailment:tunklab:_:100", "max_stacks")
        self.assertEqual(rows[0]["evidence_status"], "unparsed")
        self.assertIsNone(rows[0]["normalized_value"])
        self.assertEqual(rows[0]["raw_value"], "0")
        notes = self.conn.execute(
            "SELECT notes FROM field_evidence WHERE entity_id = ? AND field_path = ?",
            ("ailment:tunklab:_:100", "max_stacks"),
        ).fetchone()["notes"]
        self.assertIn("not established", notes or "")
        ailment = self.conn.execute(
            "SELECT * FROM ailments WHERE entity_id = ?", ("ailment:tunklab:_:100",)
        ).fetchone()
        stack_rule = json.loads(ailment["stack_rule"])
        self.assertEqual(stack_rule["maxInstances"], 0)
        self.assertEqual(stack_rule["ui_semantics"], "unknown")
        self.assertNotIn("uncapped", json.dumps(stack_rule))

    def test_ability_extension_keeps_explicit_zero(self):
        entity = support.entity_row(self.conn, "ability:tunklab:ability:ab-synth")
        extensions = json.loads(entity["extensions"])
        self.assertEqual(extensions["manaCost"], 0)
        self.assertEqual(entity["canonical_name"], "Synthetic Ability")

    def test_unknown_never_filled_with_zero_summary(self):
        # 3 nodes, one tree; no synthetic default values were injected
        self.assertEqual(self.summary["entity_counts"]["node"], 3)
        self.assertEqual(self.summary["entity_counts"]["tree"], 1)

    def test_missing_max_points_should_have_absent_evidence(self):
        """BUG task-5-bug-2: missing numeric fields are currently marked known.

        ``mapping_tunklab._map_node`` emits ``_evidence("max_points", None, None)``
        with the default ``known`` status instead of ``absent`` (and the same
        pattern exists for description/player_ability_id/ability_id/max_stacks).
        The database therefore contains "known" evidence rows whose
        normalized_value is NULL, which the validator reports as a warning.
        Expected fix: use ``absent`` for None/empty values (or drop the row when
        a dedicated absent row already exists).
        """
        rows = support.evidence_rows(self.conn, "node:tunklab:42:11", "max_points")
        self.assertTrue(
            any(row["evidence_status"] == "absent" for row in rows),
            f"expected an absent evidence row, got {rows}",
        )


class EvidenceStatusFrameworkTests(unittest.TestCase):
    """The framework must store known/absent/not_applicable as distinct statuses."""

    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = support.new_temp_db(str(self.tmp))
        support.insert_snapshot(self.conn, "synthetic-snap-status")
        support.insert_source_records(
            self.conn,
            [
                support.SourceRecord(
                    source="tunklab",
                    source_snapshot_id="synthetic-snap-status",
                    resource_name="synthetic_status.jsonl",
                    entity_kind="fixture_entity",
                    source_scope="scope",
                    source_id="statuses",
                    locator="synthetic_status.jsonl#statuses",
                    raw={"name": "Status Fixture"},
                )
            ],
        )
        self.module = types.ModuleType("le_data.normalize.mapping_tunklab")

        def map_fixture_entity(record):
            entity_id = make_entity_id(
                "fixture_entity", record.source, record.source_scope, record.source_id
            )
            return EntityPlan(
                source_record=record,
                kind="fixture_entity",
                entity_id=entity_id,
                canonical_name=record.raw["name"],
                evidence=[
                    {"field_path": "known_field", "raw_value": 1,
                     "normalized_value": 1, "evidence_status": "known"},
                    {"field_path": "absent_field", "raw_value": None,
                     "normalized_value": None, "evidence_status": "absent"},
                    {"field_path": "na_field", "raw_value": None,
                     "normalized_value": None, "evidence_status": "not_applicable"},
                ],
            )

        self.module.map_fixture_entity = map_fixture_entity
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def test_statuses_are_kept_distinct(self):
        with mock.patch(
            "le_data.normalize.base.load_mapping_module", return_value=self.module
        ):
            normalizer = Normalizer(self.conn, "tunklab", "synthetic-snap-status")
            normalizer.normalize_records(
                [
                    support.SourceRecord(
                        source="tunklab",
                        source_snapshot_id="synthetic-snap-status",
                        resource_name="synthetic_status.jsonl",
                        entity_kind="fixture_entity",
                        source_scope="scope",
                        source_id="statuses",
                        locator="synthetic_status.jsonl#statuses",
                        raw={"name": "Status Fixture"},
                    )
                ]
            )
        self.conn.commit()
        statuses = {
            row["field_path"]: row["evidence_status"]
            for row in self.conn.execute(
                "SELECT field_path, evidence_status FROM field_evidence "
                "WHERE entity_id = 'fixture_entity:tunklab:scope:statuses'"
            )
        }
        self.assertEqual(
            statuses,
            {
                "known_field": "known",
                "absent_field": "absent",
                "na_field": "not_applicable",
            },
        )


if __name__ == "__main__":
    unittest.main()
