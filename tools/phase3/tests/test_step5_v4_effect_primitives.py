"""task-20: real VC effects consumers for generic contribution/ledger/application/coverage primitives."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc.mechanics import execution as generic  # noqa: E402

VC_PLAN = ROOT / "reports/void-cleave-20261110/dps-step5-p1-vc-1/calculation-plan.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def real_effects() -> list[dict]:
    plan = json.loads(VC_PLAN.read_text(encoding="utf-8"))
    return [dict(item) for item in plan["effects"] if item.get("emits_contribution")]


class EffectPrimitivesRealVcTest(unittest.TestCase):
    def test_real_vc_effect_fields_consumed(self):
        effects = real_effects()
        self.assertEqual(len(effects), 232)
        projection = generic.project_effect_contributions(effects)
        self.assertEqual(projection["row_count"], 232)
        for row in projection["rows"]:
            self.assertTrue(row["handler_id"])
            self.assertTrue(row["ledger_ref"])
            self.assertIn(row["coverage_status"], generic.COVERAGE_STATUSES)
            self.assertIn("ownership", row)
        ownership = generic.project_ownership(projection["rows"])
        self.assertGreaterEqual(ownership["owner_count"], 1)
        self.assertTrue(projection["refs"])

    def test_real_effect_mutation_and_deletion_change_projection(self):
        effects = real_effects()
        base = generic.project_effect_contributions(effects)
        mutated = copy.deepcopy(effects)
        mutated[0]["value"] = float(mutated[0].get("value") or 0.0) + 1.0
        changed = generic.project_effect_contributions(mutated)
        self.assertNotEqual(changed["rows"][0]["value"], base["rows"][0]["value"])
        removed = effects[1:]
        self.assertEqual(generic.project_effect_contributions(removed)["row_count"], 231)
        normal = copy.deepcopy(effects[:1])
        normal[0]["included_static"] = False
        normal[0]["coverage_status"] = "mapped"
        normal[0]["exclusion_reason"] = "tag_mismatch"
        coverage = generic.project_effect_coverage(generic.project_effect_contributions(normal)["rows"])
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(len(coverage["normal_exclusions"]), 1)
        gap = copy.deepcopy(effects[:1])
        gap[0]["included_static"] = False
        gap[0]["coverage_status"] = "unresolved"
        gap_cov = generic.project_effect_coverage(generic.project_effect_contributions(gap)["rows"])
        self.assertEqual(gap_cov["status"], "partial")
        self.assertEqual(gap_cov["gap_count"], 1)
        broken = copy.deepcopy(effects[:1])
        broken[0]["operation"] = "unknown"
        with self.assertRaises(generic.ExecutionContractError):
            generic.project_effect_contributions(broken)

    def test_generic_application_window_primitive(self):
        application = {
            "application_id": "app:1",
            "duration_s": 2.0,
            "tick_interval_s": 0.5,
            "chance": 0.5,
            "expected_stacks": 2.0,
            "damage_per_tick": 10.0,
            "cohort_policy": "independent_cast",
            "tail_policy": "include_unsettled",
        }
        base = generic.application_window_damage(application, casts=3, windows_s=[5.0], cast_interval_s=2.0)
        self.assertGreater(base["total_damage"], 0)
        changed = copy.deepcopy(application)
        changed["chance"] = 1.0
        self.assertNotEqual(
            generic.application_window_damage(changed, casts=3, windows_s=[5.0], cast_interval_s=2.0)["total_damage"],
            base["total_damage"],
        )
        stacks = copy.deepcopy(application)
        stacks["expected_stacks"] = 3.0
        self.assertNotEqual(
            generic.application_window_damage(stacks, casts=3, windows_s=[5.0], cast_interval_s=2.0)["total_damage"],
            base["total_damage"],
        )
        tail = copy.deepcopy(application)
        tail["tail_policy"] = "exclude_unsettled"
        self.assertNotEqual(
            generic.application_window_damage(tail, casts=3, windows_s=[5.0], cast_interval_s=2.0)["tail_damage"],
            base["tail_damage"],
        )
        with self.assertRaises(generic.ExecutionContractError):
            generic.application_window_damage({**application, "cohort_policy": "unknown"}, casts=3, windows_s=[5.0], cast_interval_s=2.0)
        source = (ROOT / "tools/phase3/src/le_calc/mechanics/execution.py").read_text(encoding="utf-8").lower()
        for token in ("ignite", "bleed", "heat wave", "eager", "patient"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
