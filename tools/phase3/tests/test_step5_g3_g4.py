"""G3/G4：plan-v4 target_side、通用 DoT/application/charge 与 result-v6 门禁。"""
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import plan_v4  # noqa: E402

FIXTURES = ROOT / "tools/phase3/tests/fixtures/step5"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def load(name: str):
    return (
        json.loads((FIXTURES / f"{name}.template.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.case.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.scenario.json").read_text(encoding="utf-8")),
    )


def compile_execute(name: str):
    template, case, scenario = load(name)
    plan = plan_v4.compile_plan_v4(template, case, scenario)
    return plan, plan_v4.execute_plan_v4(plan)


class G3TargetSideTest(unittest.TestCase):
    def test_o1_separate_and_fold_multiplier(self):
        template, case, scenario = load("syn-hit-cold")
        separate_plan = plan_v4.compile_plan_v4(template, case, scenario)
        separate = plan_v4.execute_plan_v4(separate_plan)
        self.assertAlmostEqual(separate["components"][0]["damage_total"], 44.0, delta=1e-9)
        entry = separate["target_side"]["components"]["syn-cold-hit"]
        self.assertEqual(entry["unit"], "fraction")
        self.assertEqual(entry["damage_channel"], "cold")
        self.assertEqual(entry["strategy"], "separate_multiplier")
        self.assertEqual(entry["resistance"], 0.2)
        self.assertEqual(entry["penetration"], 0.1)
        self.assertEqual(entry["damage_reduction"], 0.5)
        fold_template = copy.deepcopy(template)
        fold_template["definition"]["target_side"]["strategy"] = "fold_into_resistance"
        fold_plan = plan_v4.compile_plan_v4(fold_template, case, scenario)
        fold = plan_v4.execute_plan_v4(fold_plan)
        self.assertAlmostEqual(fold["components"][0]["damage_total"], 45.0, delta=1e-9)

    def test_target_side_rejects_bad_values_and_conflicts(self):
        template, case, scenario = load("syn-hit-cold")
        bad = copy.deepcopy(template)
        bad["definition"]["target_side"]["strategy"] = "magic_strategy"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(bad, case, scenario)
        for value in (float("nan"), float("inf"), float("-inf")):
            bad_scenario = copy.deepcopy(scenario)
            bad_scenario["target"]["resistance"] = value
            with self.assertRaises(plan_v4.PlanV4Error):
                plan_v4.compile_plan_v4(template, case, bad_scenario)
        bad_unit = copy.deepcopy(template)
        bad_unit["definition"]["components"][0]["damage_type"] = "Unknown"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(bad_unit, case, scenario)
        duplicate = copy.deepcopy(template)
        duplicate["definition"]["components"].append(
            copy.deepcopy(duplicate["definition"]["components"][0])
        )
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(duplicate, case, scenario)
        mismatch = copy.deepcopy(template)
        mismatch["definition"]["plan_version"] = "le-calculation-plan-v3"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(mismatch, case, scenario)

    def test_compatibility_target_policy_mapping(self):
        self.assertEqual(plan_v4.compatibility_target_policy("le-calculation-plan-v3", "bh2"), "bh_fold_scenario_dr")
        self.assertEqual(
            plan_v4.compatibility_target_policy("le-calculation-plan-v2", "fr11mv"),
            "fr_separate_multiplier_table_dr",
        )
        self.assertEqual(
            plan_v4.compatibility_target_policy("le-calculation-plan-v1", "v01cv"),
            "legacy_profile_fold_negative_allow",
        )
        self.assertIsNone(plan_v4.compatibility_target_policy("le-calculation-plan-v4", "bh2"))


class G4GenericMechanicsTest(unittest.TestCase):
    def test_o2_dot_and_application_windows(self):
        plan, result = compile_execute("syn-dot-poison")
        windows = {item["window_s"]: item for item in result["windows"]}
        self.assertAlmostEqual(windows[2.0]["dot_damage"], 40.0, delta=1e-9)
        self.assertAlmostEqual(windows[2.0]["application_damage"], 12.5, delta=1e-9)
        self.assertAlmostEqual(windows[2.0]["unsettled_tail_damage"], 7.5, delta=1e-9)
        self.assertAlmostEqual(windows[3.0]["application_damage"], 18.75, delta=1e-9)
        self.assertAlmostEqual(windows[3.0]["unsettled_tail_damage"], 1.25, delta=1e-9)
        application = result["applications"][0]
        self.assertAlmostEqual(application["damage_total"], 20.0, delta=1e-9)
        self.assertEqual(
            sorted(round(item["expiry_s"], 6) for item in application["cohorts"]),
            [2.0, 2.5, 3.0, 3.5],
        )
        self.assertEqual(result["schema"], plan_v4.SCHEMA_RESULT)
        self.assertEqual(result["applications_ledger"][1]["component_id"], "syn-poison")
        self.assertEqual(
            result["contribution_application_summary"],
            __import__("le_calc.dps_result", fromlist=["application_summary"]).application_summary(
                result["applications_ledger"]
            ),
        )
        # 目标抗性通道：Fire 抗性不影响 Poison application
        template, case, scenario = load("syn-dot-poison")
        scenario = copy.deepcopy(scenario)
        scenario["target"]["resistances"] = {"cold": 0.0, "poison": 0.0, "fire": 0.9}
        other = plan_v4.execute_plan_v4(plan_v4.compile_plan_v4(template, case, scenario))
        self.assertAlmostEqual(other["applications"][0]["damage_total"], 20.0, delta=1e-9)
        self.assertAlmostEqual(
            other["applications"][0]["target_side"]["resistance"], 0.0
        )

    def test_o3_charge_conditions_and_policy_id_are_labels(self):
        plan, result = compile_execute("syn-charge")
        burst = next(item for item in result["policy_results"] if item["policy_id"] == "burst")
        hold = next(item for item in result["policy_results"] if item["policy_id"] == "hold")
        self.assertEqual([item["cast_start_s"] for item in burst["casts"]], [0, 1, 2, 4, 6, 8, 10])
        self.assertEqual(burst["totals"]["max_concurrent"], 3)
        self.assertEqual([item["cast_start_s"] for item in hold["casts"]], [0, 4, 8])
        self.assertEqual(hold["totals"]["max_concurrent"], 1)
        template, case, scenario = load("syn-charge")
        renamed = copy.deepcopy(template)
        renamed["definition"]["schedule"]["action_policies"] = [
            {"policy_id": "alpha", "condition": "charge_available_and_action_free"},
            {"policy_id": "beta", "condition": "wait_for_all_instances_expired"},
        ]
        for evaluation, policy in zip(
            renamed["definition"]["evaluations"], ["alpha", "beta"], strict=True
        ):
            evaluation["action_policy"] = policy
        renamed_result = plan_v4.execute_plan_v4(
            plan_v4.compile_plan_v4(renamed, case, scenario)
        )
        renamed_burst = next(
            item for item in renamed_result["policy_results"] if item["policy_id"] == "alpha"
        )
        self.assertEqual(
            [item["cast_start_s"] for item in renamed_burst["casts"]], [0, 1, 2, 4, 6, 8, 10]
        )
        bad = copy.deepcopy(template)
        bad["definition"]["schedule"]["action_policies"][0]["condition"] = "never_condition"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(bad, case, scenario)

    def test_capability_matrix_and_data_only_fixtures(self):
        for name in ("syn-hit-cold", "syn-dot-poison", "syn-charge"):
            with self.subTest(fixture=name):
                plan, result = compile_execute(name)
                self.assertEqual(plan["schema"], plan_v4.SCHEMA_PLAN)
                self.assertTrue(plan["capabilities"])
                self.assertTrue(result["presentation"]["title"])
        template, case, scenario = load("syn-charge")
        missing = copy.deepcopy(template)
        missing["definition"]["capabilities"] = ["hit_components", "presentation"]
        with self.assertRaisesRegex(plan_v4.PlanV4Error, "capability_gap"):
            plan_v4.compile_plan_v4(missing, case, scenario)
        unknown = copy.deepcopy(template)
        unknown["definition"]["capabilities"].append("arbitrary_script")
        with self.assertRaisesRegex(plan_v4.PlanV4Error, "capability_gap"):
            plan_v4.compile_plan_v4(unknown, case, scenario)

    def test_result_v6_recompute_gate_rejects_tamper(self):
        charge_plan, charge_result = compile_execute("syn-charge")
        dot_plan, dot_result = compile_execute("syn-dot-poison")
        plan_v4.validate_result_v6(charge_plan, copy.deepcopy(charge_result))
        plan_v4.validate_result_v6(dot_plan, copy.deepcopy(dot_result))
        charge_tampers = {
            "window_dps": lambda r: r["windows"][0].__setitem__("dps", 1.0),
            "window_total": lambda r: r["windows"][0].__setitem__("total_damage", 0.0),
            "totals_dps": lambda r: r["totals"].__setitem__("horizon_dps", 1.0),
            "cast_count": lambda r: r["totals"].__setitem__("cast_count", 0),
            "max_concurrent": lambda r: r["totals"].__setitem__("max_concurrent", 1),
            "evaluation_dps": lambda r: r["evaluations"][0].__setitem__("horizon_dps", 1.0),
            "presentation": lambda r: r["presentation"].__setitem__("title", "tampered"),
        }
        for label, mutate in charge_tampers.items():
            tampered = copy.deepcopy(charge_result)
            mutate(tampered)
            with self.assertRaises(plan_v4.PlanV4Error, msg=label):
                plan_v4.validate_result_v6(charge_plan, tampered)
        dot_tampers = {
            "window_application": lambda r: r["windows"][0].__setitem__("application_damage", 0.0),
            "window_tail": lambda r: r["windows"][0].__setitem__("unsettled_tail_damage", 0.0),
            "application_total": lambda r: r["applications"][0].__setitem__("damage_total", 1.0),
        }
        for label, mutate in dot_tampers.items():
            tampered = copy.deepcopy(dot_result)
            mutate(tampered)
            with self.assertRaises(plan_v4.PlanV4Error, msg=label):
                plan_v4.validate_result_v6(dot_plan, tampered)

    def test_report_is_data_driven(self):
        _, result = compile_execute("syn-dot-poison")
        html = plan_v4.render_report_v6(result)
        self.assertIn("Synthetic Poison DoT", html)
        self.assertIn("Synthetic Poison Application", html)
        self.assertIn("Synthetic Poison Application", html)
        self.assertNotIn("Black Hole", html)
        self.assertNotIn("Ignite", html)
        self.assertNotIn("bh-ignite", html)


if __name__ == "__main__":
    unittest.main()
