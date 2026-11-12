"""Monster level damage-reduction integration tests."""

from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

import support

from le_calc import monster_dr
from le_calc.report_ui import present
from le_calc.report_ui.shell import render as render_html
from le_calc.report import build_result
from le_calc.ruleset import load_ruleset

V3_PATH = Path(__file__).resolve().parents[1] / "rules" / "ruleset-bh-stage1-v4.json"


class MonsterDRTableTest(unittest.TestCase):
    def test_known_table_values(self):
        self.assertEqual(monster_dr.dr_for_level(0), 0.0)
        self.assertEqual(monster_dr.dr_for_level(1), 0.0)
        self.assertEqual(monster_dr.dr_for_level(6), 0.0)
        self.assertAlmostEqual(monster_dr.dr_for_level(7), 0.015, places=12)
        self.assertAlmostEqual(monster_dr.dr_for_level(50), 0.54, places=12)
        self.assertAlmostEqual(monster_dr.dr_for_level(100), 0.87, places=12)
        self.assertAlmostEqual(monster_dr.factor_for_level(100), 0.13, places=12)

    def test_invalid_level_rejected(self):
        with self.assertRaises(monster_dr.MonsterDRError):
            monster_dr.dr_for_level(101)
        with self.assertRaises(monster_dr.MonsterDRError):
            monster_dr.dr_for_level(-1)

    def test_table_covers_1_to_100_and_matches_ruleset_hash(self):
        data = monster_dr.load_monster_dr()
        self.assertEqual(sorted(data["levels_int"]), list(range(1, 101)))
        ruleset = load_ruleset(V3_PATH)
        recorded = ruleset.target_side["monster_dr"]["data_sha256"]
        actual = hashlib.sha256(monster_dr.DATA_PATH.read_bytes()).hexdigest()
        self.assertEqual(recorded, actual)


class MonsterDRIntegrationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = support.make_snapshot()
        cls.ruleset = load_ruleset(V3_PATH)
        cls.res0 = build_result(
            cls.snapshot,
            cls.ruleset,
            run_id="test-md-0",
            generated_at="2026-11-05T00:00:00+00:00",
            monster_level=0,
        )
        cls.res100 = build_result(
            cls.snapshot,
            cls.ruleset,
            run_id="test-md-100",
            generated_at="2026-11-05T00:00:00+00:00",
            monster_level=100,
        )

    def test_level_100_applies_13_percent_factor(self):
        t0 = self.res0["single_cast"]["totals"]
        t100 = self.res100["single_cast"]["totals"]
        self.assertAlmostEqual(
            t100["single_cast_total"], t0["single_cast_total"] * 0.13, places=6
        )
        self.assertAlmostEqual(t100["body_total"], t0["body_total"] * 0.13, places=6)
        self.assertAlmostEqual(t100["ignite_total"], t0["ignite_total"] * 0.13, places=6)
        self.assertEqual(self.res100["scenario"]["target"]["monster_level"], 100)
        self.assertAlmostEqual(
            self.res100["scenario"]["target"]["monster_damage_reduction"], 0.87, places=12
        )
        self.assertEqual(self.res100["monster_dr"]["monster_level"], 100)

    def test_dps_by_level_table(self):
        md = self.res100["monster_dr"]
        self.assertEqual(len(md["dps_by_level"]), 101)
        self.assertAlmostEqual(
            md["dps_by_level"]["0"]["single_cast_total"],
            self.res0["single_cast"]["totals"]["single_cast_total"],
            places=6,
        )
        self.assertAlmostEqual(
            md["dps_by_level"]["100"]["single_cast_total"],
            self.res100["single_cast"]["totals"]["single_cast_total"],
            places=6,
        )
        # level 50 factor = 1 - 0.54 = 0.46
        self.assertAlmostEqual(
            md["dps_by_level"]["50"]["single_cast_total"],
            self.res0["single_cast"]["totals"]["single_cast_total"] * 0.46,
            places=6,
        )
        eager0 = self.res0["loops"][0]["windows"][-1]["dps"]
        self.assertAlmostEqual(md["dps_by_level"]["100"]["eager_dps"], eager0 * 0.13, places=6)

    def test_consistency_and_rules(self):
        self.assertTrue(self.res100["consistency"]["all_pass"])
        rules = {r["rule_id"]: r for r in self.res100["rule_ledger"]}
        self.assertEqual(rules["rule-target-monster-dr-table"]["status"], "confirmed")
        self.assertEqual(rules["rule-target-monster-dr-application"]["status"], "assumed")

    def test_sensitivity_view_matches_engine_table(self):
        view = present.build_view(self.res100)
        sensitivity = view["sensitivity"]
        self.assertEqual(sensitivity["current_level"], 100)
        self.assertEqual(len(sensitivity["rows"]), 101)
        current = next(row for row in sensitivity["rows"] if row["level"] == 100)
        self.assertAlmostEqual(current["single"], self.res100["single_cast"]["totals"]["single_cast_total"])
        self.assertAlmostEqual(current["eager_dps"], self.res100["monster_dr"]["dps_by_level"]["100"]["eager_dps"])
        level50 = next(row for row in sensitivity["rows"] if row["level"] == 50)
        self.assertAlmostEqual(level50["dr"], 0.54, places=12)

    def test_report_fixes_scenario_and_moves_level_table_into_drawer(self):
        html = render_html(self.res100)
        header = html.split('class="app-header"', 1)[1].split("</header>", 1)[0]
        self.assertNotIn("<select", header)
        self.assertIn("目标设置", header)
        # target editing controls moved into the target-settings drawer
        self.assertNotIn("data-scenario-level", header)
        drawer = html.split('id="drawer-scenario"', 1)[1]
        self.assertIn("data-scenario-level", drawer)
        self.assertIn("data-scenario-fire-res", drawer)
        self.assertIn("恢复默认目标", drawer)
        self.assertIn("Lv100", drawer)
        self.assertIn("Lv50", drawer)  # full level table remains available
        self.assertIn("等级影响", drawer)


if __name__ == "__main__":
    unittest.main()
