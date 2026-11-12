"""Unit conversion tests: seconds->ms and percent->ratio, raw + normalized kept.

workguide §13 row「单位转换」: known percent/ratio, seconds/milliseconds samples;
store the raw value and an explicit normalized value, never convert twice.

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


class UnitConversionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = support.temp_dir(self)
        self.conn = support.new_temp_db(str(self.tmp))
        support.load_and_normalize(
            self.conn, "synthetic_boundary_records.jsonl", "synthetic-snap-boundary"
        )

    def tearDown(self):
        self.conn.close()

    def _effects(self):
        rows = self.conn.execute(
            "SELECT e.extensions, v.value_kind, v.value_numeric, v.unit, v.raw "
            "FROM effect_values v JOIN effects e ON e.effect_id = v.effect_id "
            "WHERE e.owner_entity_id = ?",
            (NODE_ID,),
        ).fetchall()
        result = {}
        for row in rows:
            property_id = json.loads(row["extensions"])["property"]
            result[property_id] = dict(row)
        return result

    def test_seconds_are_converted_to_milliseconds_with_raw_value(self):
        ailment = self.conn.execute(
            "SELECT * FROM ailments WHERE entity_id = ?", (AILMENT_ID,)
        ).fetchone()
        self.assertEqual(ailment["duration_ms"], 2500)
        rows = support.evidence_rows(self.conn, AILMENT_ID, "duration_ms")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["raw_value"], "2.5")
        self.assertEqual(row["normalized_value"], "2500")
        self.assertEqual(row["unit"], "ms")
        self.assertEqual(row["evidence_status"], "known")

    def test_seconds_are_not_converted_twice(self):
        ailment = self.conn.execute(
            "SELECT duration_ms FROM ailments WHERE entity_id = ?", (AILMENT_ID,)
        ).fetchone()
        self.assertNotEqual(ailment["duration_ms"], 2_500_000)
        self.assertEqual(ailment["duration_ms"], 2_500)

    def test_percent_value_is_stored_as_ratio_with_raw_text(self):
        effects = self._effects()
        percent = effects[10007]
        self.assertEqual(percent["value_kind"], "percent_modifier")
        self.assertAlmostEqual(percent["value_numeric"], 0.15, places=6)
        self.assertEqual(percent["unit"], "ratio")
        self.assertIn("15%", percent["raw"])

    def test_percent_evidence_keeps_raw_and_normalized(self):
        rows = support.evidence_rows(self.conn, NODE_ID, "stats[0].value")
        self.assertEqual(rows[0]["raw_value"], "15%")
        self.assertEqual(rows[0]["normalized_value"], "0.15")
        self.assertEqual(rows[0]["unit"], "ratio")

    def test_zero_percent_is_known_zero_not_absent(self):
        effects = self._effects()
        zero = effects[42]
        self.assertEqual(zero["value_kind"], "percent_modifier")
        self.assertEqual(zero["value_numeric"], 0.0)
        self.assertEqual(zero["unit"], "ratio")
        rows = support.evidence_rows(self.conn, NODE_ID, "stats[2].value")
        self.assertEqual(rows[0]["raw_value"], "0%")
        self.assertEqual(rows[0]["normalized_value"], "0.0")
        self.assertEqual(rows[0]["evidence_status"], "known")

    def test_negative_percent_keeps_sign(self):
        effects = self._effects()
        self.assertAlmostEqual(effects[43]["value_numeric"], -0.1, places=6)

    def test_flat_number_keeps_number_unit(self):
        effects = self._effects()
        flat = effects[10008]
        self.assertEqual(flat["value_kind"], "flat")
        self.assertEqual(flat["value_numeric"], 7.0)
        self.assertEqual(flat["unit"], "number")

    def test_unparsed_value_is_not_coerced_to_zero(self):
        effects = self._effects()
        unparsed = effects[5]
        self.assertEqual(unparsed["value_kind"], "unparsed")
        self.assertIsNone(unparsed["value_numeric"])
        self.assertIsNone(unparsed["unit"])
        rows = support.evidence_rows(self.conn, NODE_ID, "stats[4].value")
        self.assertEqual(rows[0]["evidence_status"], "unparsed")
        self.assertIsNone(rows[0]["normalized_value"])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM data_issues WHERE issue_type = 'unparsed_stat_value' "
            "AND field_path = 'stats[4].value'"
        ).fetchone()[0]
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
