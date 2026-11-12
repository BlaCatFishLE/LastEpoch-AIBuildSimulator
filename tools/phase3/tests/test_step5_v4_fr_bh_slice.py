"""task-14：FR/BH v4 assembly 切片（真实 v2/v3 装配，多评估/DoT 映射）。"""
from __future__ import annotations

import json
import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route, plan_executor, plan_executor_v2, plan_executor_v3, plan_v4  # noqa: E402

CASES = {
    "fr": ("examples/flame-reave/case.json", "reports/flame-reave/dps-step5-v4-fr-5/result.json",
           "reports/flame-reave/dps-step5-p1c-fr-1/result.json"),
    "bh": ("examples/black-hole/case.json", "reports/black-hole/dps-step5-v4-bh-5/result.json",
           "reports/black-hole/dps-step5-p1c-bh-1/result.json"),
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class FrBhV4AssemblyTest(unittest.TestCase):
    def test_fr_bh_real_assembly_and_ledger_mapping(self):
        for name, (case_rel, run_rel, baseline_rel) in CASES.items():
            with self.subTest(case=name):
                case = json.loads((ROOT / case_rel).read_text(encoding="utf-8"))
                case["_path"] = case_rel
                _, compiled = calculation_route.compile_route(root=ROOT, case=case)
                self.assertTrue(compiled.ok, compiled.diagnostics)
                plan = compiled.plan
                self.assertEqual(plan["schema"], "le-calculation-plan-v4")
                self.assertTrue(plan.get("assembly"))
                self.assertIn(plan["assembly"]["legacy_schema"], (
                    "le-calculation-plan-v2", "le-calculation-plan-v3"
                ))
                result = plan_executor.execute_plan(plan, None)
                baseline = json.loads((ROOT / baseline_rel).read_text(encoding="utf-8"))
                self.assertEqual(result["contributions"], baseline["contributions"])
                self.assertEqual(result["effect_ledger"], baseline["effect_ledger"])
                self.assertEqual(result["contribution_application_summary"], baseline["contribution_application_summary"])
                self.assertEqual(result["coverage"], baseline["coverage"])
                self.assertEqual(result["scenario"], baseline["scenario"])
                self.assertEqual(len(result["applications_ledger"]), len(baseline["applications"]))
                if name == "fr":
                    evaluations = {item["evaluation_id"]: item for item in result["evaluations"]}
                    self.assertAlmostEqual(evaluations["single_hit"]["horizon_damage"], 51831.5589095769, delta=1e-9)
                    self.assertAlmostEqual(evaluations["dps"]["horizon_dps"], 121329.04081416794, delta=1e-9)
                    self.assertEqual(len(result["policy_results"]), 2)
                else:
                    evaluations = {item["evaluation_id"]: item for item in result["evaluations"]}
                    self.assertAlmostEqual(evaluations["eager"]["horizon_dps"], 2919759.1155459494, delta=1e-6)
                    self.assertAlmostEqual(evaluations["patient"]["horizon_dps"], 1337984.919231453, delta=1e-6)
                    self.assertEqual(len(result["policy_results"]), 2)


    def test_v4_modules_do_not_import_or_call_legacy_executors(self):
        for rel in ("tools/phase3/src/le_calc/plan_v4.py", "tools/phase3/src/le_calc/v4_execution.py"):
            source = (ROOT / rel).read_text(encoding="utf-8")
            for forbidden in (
                "plan_executor_v2", "plan_executor_v3", "_execute_v2_native",
                "_execute_v3_native", "execute_plan_v2(", "execute_plan_v3(",
                "plan_executor.execute_plan",
            ):
                self.assertNotIn(forbidden, source, f"{rel} contains {forbidden}")

    def test_fr_bh_native_does_not_call_legacy_executors_and_effect_drives_result(self):
        for name, (case_rel, _run_rel, baseline_rel) in CASES.items():
            with self.subTest(case=name):
                case = json.loads((ROOT / case_rel).read_text(encoding="utf-8"))
                case["_path"] = case_rel
                _, compiled = calculation_route.compile_route(root=ROOT, case=case)
                self.assertTrue(compiled.ok, compiled.diagnostics)
                plan = compiled.plan
                self.assertTrue(plan.get("execution"))
                baseline = json.loads((ROOT / baseline_rel).read_text(encoding="utf-8"))
                with mock.patch.object(plan_executor, "execute_plan", side_effect=RuntimeError("legacy execute")), \
                     mock.patch.object(plan_executor_v2, "execute_plan_v2", side_effect=RuntimeError("legacy v2")), \
                     mock.patch.object(plan_executor_v3, "execute_plan_v3", side_effect=RuntimeError("legacy v3")):
                    result = plan_v4.execute_plan_v4(plan)
                self.assertEqual(result["contributions"], baseline["contributions"])
                self.assertEqual(result["effect_ledger"], baseline["effect_ledger"])
                self.assertEqual(result["coverage"], baseline["coverage"])
                mutated = json.loads(json.dumps(plan))
                generic = mutated["execution"].get("execution_v1")
                if generic:
                    wanted = (
                        "mod:equipment.chest#affix#normal#0.1"
                        if name == "fr"
                        else "mod:equipment.chest#unique_mod#1"
                    )
                    target = next(
                        item for item in generic["effects"]
                        if item.get("effect_id") == wanted
                    )
                    target["value"] = float(target["value"]) + 1.0
                else:
                    effects = mutated["execution"]["effects"]
                    target = next(
                        item for item in effects
                        if item.get("emits_contribution")
                        and item.get("operation") in ("increased", "more")
                        and abs(float(item.get("value") or 0.0)) > 0.5
                    )
                    target["value"] = float(target["value"]) + 1.0
                    legacy_execution = {
                        key: value for key, value in mutated["execution"].items()
                        if key not in ("engine", "source_contract", "strategies")
                    }
                    legacy_sha = planmod.compute_plan_sha256(legacy_execution)
                    mutated["execution"]["plan_sha256"] = legacy_sha
                    mutated["execution"]["plan_id"] = planmod.PLAN_ID_PREFIX + legacy_sha
                mutated["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(mutated["execution"]))
                mutated["plan_sha256"] = planmod.compute_plan_sha256(mutated)
                changed = plan_v4.execute_plan_v4(mutated)
                self.assertNotAlmostEqual(
                    changed["totals"]["horizon_dps"], result["totals"]["horizon_dps"], delta=1e-6
                )

if __name__ == "__main__":
    unittest.main()
