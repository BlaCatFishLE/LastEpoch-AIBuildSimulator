"""task-21: generic condition evaluator + real VC effects settlement consumers."""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import plan_handlers  # noqa: E402
from le_calc.mechanics import execution as generic  # noqa: E402

VC_RUN = ROOT / "reports/void-cleave-20261110/dps-step5-p1-vc-1"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _plan():
    return json.loads((VC_RUN / "calculation-plan.json").read_text(encoding="utf-8"))


def _scenario():
    result = json.loads((VC_RUN / "result.json").read_text(encoding="utf-8"))
    return result["scenario"]["states"], result["scenario"]["target"]


class ConditionSettlementTest(unittest.TestCase):
    def test_real_vc_condition_and_effects_settlement(self):
        plan = _plan()
        conditions = {str(item["condition_id"]): item for item in plan.get("conditions") or []}
        effects = [item for item in plan["effects"] if item.get("emits_contribution")]
        states, target = _scenario()
        runtime = plan_handlers.PlanRuntime()
        components = [
            {
                "component_id": str(item.get("component_id")),
                "kind": item.get("kind"),
                "tags": list(item.get("tags") or []),
                "damage_channel": item.get("adaptive_damage_type") or "physical",
                "damage_type": item.get("adaptive_damage_type") or "Physical",
            }
            for item in plan.get("components") or []
        ]
        settled = generic.settle_execution_effects(
            effects, conditions=conditions, states=states, target=target, runtime=runtime,
            components=components,
        )
        self.assertEqual(settled["row_count"], 232)
        self.assertTrue(settled["component_totals"])
        for row in settled["rows"]:
            self.assertIn(row["status"], {"applied", "condition_not_satisfied", "unresolved", "excluded_static"})
            self.assertIn("ownership", row)
            self.assertTrue(row["ledger_ref"])
        included_before = sum(1 for row in settled["rows"] if row["included"])

        challenged = copy.deepcopy(states)
        challenged["high_health"] = True
        challenged_target = {**target, "high_health": True}
        changed = generic.settle_execution_effects(
            effects, conditions=conditions, states=challenged, target=challenged_target, runtime=runtime
        )
        included_after = sum(1 for row in changed["rows"] if row["included"])
        self.assertNotEqual(included_after, included_before)

        mutated = copy.deepcopy(effects)
        mutated[0]["value"] = float(mutated[0].get("value") or 0.0) + 1.0
        settled_mutated = generic.settle_execution_effects(
            mutated[:1], conditions=conditions, states=states, target=target, runtime=runtime
        )
        self.assertNotEqual(settled_mutated["rows"][0]["value"], settled["rows"][0]["value"])

        excluded = copy.deepcopy(effects[:1])
        excluded[0]["included_static"] = False
        excluded[0]["exclusion_reason"] = "static_tag_mismatch"
        excluded_rows = generic.settle_execution_effects(
            excluded, conditions=conditions, states=states, target=target, runtime=runtime
        )
        self.assertFalse(excluded_rows["rows"][0]["included"])

        unknown = copy.deepcopy(effects[:1])
        unknown[0]["condition_ref"] = "cond:does-not-exist"
        with self.assertRaises(generic.ExecutionContractError):
            generic.settle_execution_effects(
                unknown, conditions=conditions, states=states, target=target, runtime=runtime
            )
        bad_program = {"op": "all", "terms": [{"kind": "unknown-kind"}]}
        with self.assertRaises(generic.ExecutionContractError):
            generic.evaluate_execution_condition_program(
                bad_program, states={}, target={}, runtime=runtime
            )

    def test_real_vc_effect_condition_program_consumes_static_flags(self):
        plan = _plan()
        conditions = {str(item["condition_id"]): item for item in plan.get("conditions") or []}
        effects = [item for item in plan["effects"] if item.get("emits_contribution") and item.get("condition_ref")]
        self.assertTrue(effects)
        states, target = _scenario()
        runtime = plan_handlers.PlanRuntime()
        program = conditions[str(effects[0]["condition_ref"])]
        evaluated = generic.evaluate_execution_condition_program(
            program, states=states, target=target, runtime=runtime
        )
        self.assertIsInstance(evaluated, tuple)
        self.assertIn(evaluated[0], (True, False, None))
        source = (ROOT / "tools/phase3/src/le_calc/mechanics/execution.py").read_text(encoding="utf-8").lower()
        for token in ("ignite", "bleed", "heat wave", "eager", "patient", "plan_executor"):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
