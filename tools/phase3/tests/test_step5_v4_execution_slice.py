"""S2 vertical slice: generic execution fields -> public primitives -> numbers/ledger/coverage."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc.mechanics import execution as generic  # noqa: E402


def sample() -> dict:
    return {
        "schema": "le-plan-execution-v1",
        "source_contract": "definitions_v3.real_binding",
        "components": [
            {
                "component_id": "skill:generic-hit",
                "kind": "hit",
                "unit": "damage",
                "damage_channel": "cold",
                "damage_type": "Cold",
                "resistance_key": "cold",
                "scope": "body",
                "duration_s": 0.0,
                "tick_interval_s": 0.0,
                "cast_ref": "cast",
                "base_damage": 100.0,
                "crit_capable": False,
                "tags": ["Hit", "Cold"],
            },
            {
                "component_id": "skill:generic-dot",
                "kind": "dot",
                "unit": "damage",
                "damage_channel": "cold",
                "damage_type": "Cold",
                "resistance_key": "cold",
                "scope": "ailment",
                "duration_s": 2.0,
                "tick_interval_s": 1.0,
                "cast_ref": "cast",
                "base_damage": 0.0,
                "tags": ["DoT"],
            },
        ],
        "applications": [
            {
                "application_id": "ailment:generic",
                "source_component": "skill:generic-dot",
                "damage_channel": "cold",
                "damage_type": "Cold",
                "resistance_key": "cold",
                "scope": "ailment",
                "duration_s": 2.0,
                "max_stacks": 1,
                "condition": "",
                "cohort_policy": "independent_cast",
                "window_policy": "half_open",
                "tail_policy": "include_unsettled",
                "damage_per_tick": 10.0,
            }
        ],
        "effects": [
            {
                "effect_id": "source:increased-cold-body",
                "source_id": "source:1",
                "channel_tags": ["Cold"],
                "scope": "body",
                "operation": "increased",
                "value": 0.5,
                "condition": "",
                "coverage_class": "mapped",
            }
        ],
        "sources": [{"source_id": "source:1", "kind": "equipment"}],
        "frequency": {
            "driver": "fixed_repeat",
            "horizon_s": 10.0,
            "windows_s": [5.0, 10.0],
            "cast_interval_s": 2.0,
            "policies": [],
            "rotation": {},
            "tail_policy": "include_unsettled",
        },
        "target": {"resistances": {"cold": 0.2}, "damage_reduction": 0.5, "penetration": 0.0},
        "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
    }


class GenericExecutionSliceTest(unittest.TestCase):
    def _run(self, execution):
        return generic.execute_execution_slice(execution)

    def test_execution_slice_has_no_skill_semantics(self):
        source = (ROOT / "tools/phase3/src/le_calc/mechanics/execution.py").read_text(encoding="utf-8").lower()
        for token in ("ignite", "heat wave", "eager", "patient", "ability_id", "legacy_schema", "plan_executor"):
            self.assertNotIn(token, source)

    def test_channel_scope_condition_change_result_and_ledger(self):
        baseline = self._run(sample())
        self.assertGreater(baseline["totals"]["horizon_damage"], 0)
        self.assertTrue(baseline["applications_ledger"])

        channel = copy.deepcopy(sample())
        channel["effects"][0]["channel_tags"] = ["Fire"]
        changed = self._run(channel)
        self.assertNotEqual(changed["totals"]["horizon_damage"], baseline["totals"]["horizon_damage"])
        self.assertEqual(changed["coverage"]["status"], "complete")
        self.assertEqual(changed["coverage"]["gap_count"], 0)

        scope = copy.deepcopy(sample())
        scope["effects"][0]["scope"] = "ailment"
        self.assertNotEqual(self._run(scope)["totals"]["horizon_damage"], baseline["totals"]["horizon_damage"])

        condition = copy.deepcopy(sample())
        condition["effects"][0]["condition"] = {"state": "buff", "equals": True}
        self.assertNotEqual(
            self._run(condition)["totals"]["horizon_damage"],
            self._run({**copy.deepcopy(condition), "states": {"buff": True}})["totals"]["horizon_damage"],
        )

    def test_window_tail_and_application_policy_change_result(self):
        baseline = self._run(sample())
        tail = copy.deepcopy(sample())
        tail["frequency"]["tail_policy"] = "exclude_unsettled"
        self.assertNotEqual(self._run(tail)["totals"]["tail_damage"], baseline["totals"]["tail_damage"])
        windows = copy.deepcopy(sample())
        windows["frequency"]["windows_s"] = [2.0, 4.0]
        self.assertEqual([item["window_s"] for item in self._run(windows)["windows"]], [2.0, 4.0])
        app = copy.deepcopy(sample())
        app["applications"][0]["condition"] = False
        self.assertEqual(self._run(app)["applications_ledger"][0]["status"], "condition_not_satisfied")

    def test_computed_coverage_not_declared_and_unknown_combinations_rejected(self):
        declared_complete = sample()
        declared_complete["coverage"] = {"status": "complete", "gap_count": 0, "gaps": []}
        declared_complete["effects"][0]["channel_tags"] = ["Fire"]
        declared_complete["effects"][0]["coverage_class"] = "unmapped_damage"
        self.assertEqual(declared_complete["coverage"]["status"], "complete")
        computed = self._run(declared_complete)
        self.assertEqual(computed["coverage"]["status"], "complete")
        self.assertEqual(computed["coverage"]["gap_count"], 0)

        required_verified = copy.deepcopy(sample())
        required_verified["coverage"] = {"status": "complete", "gap_count": 0, "gaps": []}
        required_verified["effects"][0]["channel_tags"] = ["Fire"]
        required_verified["effects"][0]["required"] = True
        computed_verified = self._run(required_verified)
        self.assertEqual(computed_verified["coverage"]["status"], "complete")
        self.assertEqual(computed_verified["coverage"]["gap_count"], 0)

        required_missing = copy.deepcopy(sample())
        required_missing["coverage"] = {"status": "complete", "gap_count": 0, "gaps": []}
        required_missing["effects"][0]["required"] = True
        required_missing["effects"][0]["source_id"] = "source:missing"
        computed_missing = self._run(required_missing)
        self.assertEqual(computed_missing["coverage"]["status"], "partial")
        self.assertGreaterEqual(computed_missing["coverage"]["gap_count"], 1)

        unmapped = copy.deepcopy(sample())
        unmapped["coverage"] = {"status": "complete", "gap_count": 0, "gaps": []}
        unmapped["effects"][0]["damage_related"] = True
        unmapped["effects"][0]["mapped"] = False
        computed_unmapped = self._run(unmapped)
        self.assertEqual(computed_unmapped["coverage"]["status"], "partial")
        self.assertGreaterEqual(computed_unmapped["coverage"]["gap_count"], 1)

        unresolved = copy.deepcopy(sample())
        unresolved["coverage"] = {"status": "complete", "gap_count": 0, "gaps": []}
        unresolved["effects"][0]["condition"] = "unresolved"
        computed_unresolved = self._run(unresolved)
        self.assertEqual(computed_unresolved["coverage"]["status"], "partial")
        self.assertGreaterEqual(computed_unresolved["coverage"]["gap_count"], 1)

        cases = [
            ("components", "unit", "unknown"),
            ("components", "scope", "unknown"),
            ("applications", "cohort_policy", "unknown"),
            ("applications", "window_policy", "unknown"),
            ("applications", "tail_policy", "unknown"),
            ("frequency", "tail_policy", "unknown"),
        ]
        for group, key, value in cases:
            broken = sample()
            if group == "components":
                broken["components"][0][key] = value
            elif group == "applications":
                broken["applications"][0][key] = value
            else:
                broken["frequency"][key] = value
            with self.assertRaises(generic.ExecutionContractError):
                self._run(broken)


    def test_vc_primitives_added_scaling_crit_weights_ailment_tick_tail(self):
        component = {
            "base_damage": 100.0, "added_damage": 10.0, "added_damage_scaling": 4.0,
            "crit_capable": True, "crit_chance": 0.5, "crit_multiplier": 2.0,
            "damage_weights": [1.0, 0.5, 0.5, 0.5], "hit_count": 4,
        }
        base = generic.weighted_hit_damage(component)
        self.assertAlmostEqual(base, (100.0 + 40.0) * 1.5 * 2.5, delta=1e-9)
        no_crit = copy.deepcopy(component); no_crit["crit_capable"] = False
        self.assertNotEqual(generic.weighted_hit_damage(no_crit), base)
        changed_weights = copy.deepcopy(component); changed_weights["damage_weights"] = [1.0, 1.0]
        self.assertNotEqual(generic.weighted_hit_damage(changed_weights), base)
        self.assertGreater(generic.weighted_hit_damage(component, increased=0.5), base)

        application = {"duration_s": 2.0, "tick_interval_s": 1.0, "chance": 0.5, "damage_per_tick": 10.0, "expected_stacks": 2.0}
        ailment = generic.ailment_expected_damage(application)
        self.assertAlmostEqual(ailment, 10.0 * 2.0 * 0.5 * 2.0, delta=1e-9)
        changed = copy.deepcopy(application); changed["chance"] = 1.0
        self.assertNotEqual(generic.ailment_expected_damage(changed), ailment)
        with self.assertRaises(generic.ExecutionContractError):
            generic.ailment_expected_damage({**application, "chance": -1.0})

        coverage = generic.project_computed_coverage([
            {"effect_id": "e1", "status": "not_applicable", "coverage_class": "unmapped"},
            {"effect_id": "e2", "status": "applied"},
        ], {"unmapped": ["e3"]})
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["gap_count"], 2)
        self.assertEqual(generic.window_tail_total(100.0, 20.0, "include_unsettled"), 120.0)
        self.assertEqual(generic.window_tail_total(100.0, 20.0, "exclude_unsettled"), 100.0)
        with self.assertRaises(generic.ExecutionContractError):
            generic.window_tail_total(100.0, 20.0, "unknown")

if __name__ == "__main__":
    unittest.main()
