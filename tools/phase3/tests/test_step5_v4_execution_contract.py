"""S1: v4 generic execution contract (structure only; no behavior migration yet)."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import plan_v4  # noqa: E402


def contract() -> dict:
    return {
        "schema": "le-plan-execution-v1",
        "source_contract": "definitions_v3.real_binding",
        "capabilities": ["hit_components", "applications", "charge_schedule"],
        "components": [
            {
                "component_id": "skill:generic",
                "kind": "hit",
                "unit": "damage",
                "damage_channel": "cold",
                "damage_type": "Cold",
                "resistance_key": "cold",
                "scope": "body",
                "duration_s": 0.0,
                "tick_interval_s": 0.0,
                "cast_ref": "cast",
            }
        ],
        "applications": [
            {
                "application_id": "ailment:generic",
                "source_component": "skill:generic",
                "damage_channel": "cold",
                "damage_type": "Cold",
                "resistance_key": "cold",
                "scope": "ailment",
                "duration_s": 2.0,
                "max_stacks": 3,
                "condition": "",
                "cohort_policy": "independent_cast",
                "window_policy": "half_open",
                "tail_policy": "include_unsettled",
            }
        ],
        "effects": [{"effect_id": "source:1", "source_refs": ["source:1"], "ownership_key": {"stat": "damage"}}],
        "sources": [{"source_id": "source:1", "kind": "equipment", "binding_status": "mapped"}],
        "frequency": {
            "driver": "fixed_repeat",
            "horizon_s": 10.0,
            "windows_s": [5.0, 10.0],
            "policies": [{"policy_id": "default", "condition": "", "interval_policy": "max(action_time, cooldown)"}],
            "rotation": {},
            "tail_policy": "include_unsettled",
        },
        "charge": {
            "enabled": False,
            "max_charges": 1,
            "recharge_s": 0.0,
            "cast_time_s": 0.0,
            "instance_duration_s": 0.0,
            "condition": "",
        },
        "coverage": {"status": "from_execution", "gap_count": 0, "gaps": []},
    }


class ExecutionContractS1Test(unittest.TestCase):
    def test_contract_accepts_generic_execution(self):
        plan_v4.validate_execution_contract(contract())

    def test_contract_rejects_non_generic_or_missing_fields(self):
        broken = copy.deepcopy(contract())
        del broken["frequency"]["tail_policy"]
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(broken)
        broken = copy.deepcopy(contract())
        broken["components"][0]["kind"] = "ignite"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(broken)
        broken = copy.deepcopy(contract())
        broken["schema"] = "le-plan-execution-v0"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(broken)


if __name__ == "__main__":
    unittest.main()
