"""Unknown fields / unknown enums: preserved in extensions/raw, reported, no crash.

workguide §13 row「未知枚举／字段」: add unknown attributes and enum values to a
fixture, the raw content must survive and be reported; no crash and no silent drop.

Fixture: ``tools/ingest/fixtures/synthetic_boundary_records.jsonl`` (synthetic).
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import synthetic_support as support  # noqa: E402

NODE_ID = "node:tunklab:42:12"
AILMENT_ID = "ailment:tunklab:_:100"
GENERIC_ID = "future_entity:tunklab:future:f1"


class UnknownFieldAndEnumTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = support.new_temp_db(str(self.tmp))
        self.summary = support.load_and_normalize(
            self.conn, "synthetic_boundary_records.jsonl", "synthetic-snap-boundary"
        )

    def tearDown(self):
        self.conn.close()

    def test_unknown_entity_kind_is_stored_generically_with_issue(self):
        entity = support.entity_row(self.conn, GENERIC_ID)
        self.assertIsNotNone(entity)
        self.assertEqual(entity["kind"], "future_entity")
        extensions = json.loads(entity["extensions"])
        self.assertEqual(extensions["raw"]["futureField"]["nested"], [1, 2, 3])
        self.assertEqual(extensions["raw"]["zeroFlag"], 0)
        self.assertIsNone(extensions["raw"]["maybeNull"])
        issue = self.conn.execute(
            "SELECT issue_type, severity, message FROM data_issues "
            "WHERE issue_type = 'unmapped_entity_kind'"
        ).fetchone()
        self.assertIsNotNone(issue)
        self.assertEqual(issue["severity"], "info")
        self.assertIn("future_entity", issue["message"])

    def test_unknown_ability_field_is_kept_in_raw_source_record(self):
        row = self.conn.execute(
            "SELECT raw_payload FROM source_records "
            "WHERE entity_kind = 'ability' AND source_id = 'ab-synth'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertIn("unknownAbilityField", row["raw_payload"])
        self.assertEqual(json.loads(row["raw_payload"])["unknownAbilityField"], {"x": 1})

    def test_unknown_damage_type_index_does_not_crash(self):
        """A damage slot beyond the known enum list is kept as its raw index."""
        ailment = self.conn.execute(
            "SELECT * FROM ailments WHERE entity_id = ?", (AILMENT_ID,)
        ).fetchone()
        self.assertEqual(ailment["damage_type"], "10")
        self.assertAlmostEqual(ailment["base_damage"], 12.5, places=6)

    def test_unknown_stat_property_enum_is_kept_in_effect_extensions(self):
        rows = self.conn.execute(
            "SELECT e.extensions, v.value_numeric FROM effect_values v "
            "JOIN effects e ON e.effect_id = v.effect_id "
            "WHERE e.owner_entity_id = ?",
            (NODE_ID,),
        ).fetchall()
        unknown = [
            row for row in rows if json.loads(row["extensions"])["property"] == 999999
        ]
        self.assertEqual(len(unknown), 1)
        self.assertEqual(unknown[0]["value_numeric"], 7.0)

    def test_unknown_enum_still_reports_parse_problem(self):
        issue = self.conn.execute(
            "SELECT message FROM data_issues WHERE issue_type = 'unparsed_stat_value'"
        ).fetchone()
        self.assertIsNotNone(issue)
        self.assertIn("5-10%", issue["message"])

    def test_unknowns_do_not_stop_normalization(self):
        self.assertEqual(self.summary["entity_counts"]["future_entity"], 1)
        self.assertEqual(self.summary["entity_counts"]["node"], 3)
        integrity = self.conn.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(integrity, "ok")


if __name__ == "__main__":
    unittest.main()
