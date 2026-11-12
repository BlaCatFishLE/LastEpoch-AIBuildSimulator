"""第五步独立复验 P1 反例回归：只读验证、v4 生产边界、运行时场景、结果/计划门禁。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route, definitions_v3, plan_v4  # noqa: E402
from le_calc.workflow import unified  # noqa: E402
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "scripts/le_pipeline"), str(ROOT / "tools/save_import/scripts/pipeline")]
from le_pipeline import onboarding  # noqa: E402

FIXTURES = ROOT / "tools/phase3/tests/fixtures/step5"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(name: str):
    return (
        json.loads((FIXTURES / f"{name}.template.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.case.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.scenario.json").read_text(encoding="utf-8")),
    )


def _plan_result(name: str = "syn-hit-cold"):
    template, case, scenario = _load(name)
    plan = plan_v4.compile_plan_v4(template, case, scenario)
    result = plan_v4.execute_plan_v4(plan)
    return plan, result, case


def _write_run(run_dir: Path, plan, result):
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "calculation-plan.json").write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (run_dir / "scenario.json").write_text(json.dumps(result["scenario"], ensure_ascii=False), encoding="utf-8")


class P1ReviewRegressionTest(unittest.TestCase):
    def test_validate_write_false_never_writes_for_valid_v4_or_load_failure(self):
        plan, result, case = _plan_result()
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch", prefix="p1-readonly-") as tmp:
            run_dir = Path(tmp)
            _write_run(run_dir, plan, result)
            out = unified.validate_stage(case, root=ROOT, run_dir=run_dir, write=False)
            self.assertTrue(out["passed"])
            self.assertFalse((run_dir / "validation-result.json").exists())
            empty = run_dir / "empty"
            empty.mkdir()
            failed = unified.validate_stage(case, root=ROOT, run_dir=empty, write=False)
            self.assertFalse(failed["passed"])
            self.assertFalse((empty / "validation-result.json").exists())

    def test_plan_v4_production_route_is_prototype_only_not_bypass(self):
        _, case, _ = _load("syn-hit-cold")
        case = dict(case)
        case["calculation"] = {
            "engine": "unified_dps",
            "source": {
                "kind": "definitions_v3",
                "template": "tools/phase3/tests/fixtures/step5/syn-hit-cold.template.json",
                "scenario": "tools/phase3/tests/fixtures/step5/syn-hit-cold.scenario.json",
            },
        }
        case["_path"] = "tools/phase3/tests/fixtures/step5/syn-hit-cold.case.json"
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        resolved, compiled = calculation_route.compile_route(root=ROOT, case=case, route=route)
        self.assertFalse(compiled.ok)
        codes = {item.get("code") for item in compiled.diagnostics}
        self.assertTrue(codes & {"input_load_failed", "definitions_v3_binding_failed"}, codes)

    def test_unknown_v4_rules_and_definition_fields_rejected(self):
        template, case, scenario = _load("syn-hit-cold")
        broken = copy.deepcopy(template)
        broken["definition"]["rules"] = {"unknown_damage_handler": {"value": 1}}
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(broken, case, scenario)
        broken2 = copy.deepcopy(template)
        broken2["definition"]["unknown_field"] = {"value": 1}
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.compile_plan_v4(broken2, case, scenario)

    def test_v4_route_uses_real_context_then_reports_capability_gap(self):
        vc_case = json.loads((ROOT / "examples/void-cleave-20261110/case.json").read_text(encoding="utf-8"))
        vc_case["_path"] = "examples/void-cleave-20261110/case.json"
        vc_case = copy.deepcopy(vc_case)
        vc_case.setdefault("main_skill", {})["ability_id"] = "syn-hit-cold"
        vc_case["calculation"] = {
            "engine": "unified_dps",
            "source": {
                "kind": "definitions_v3",
                "template": "tools/phase3/tests/fixtures/step5/syn-hit-cold.template.json",
                "scenario": "tools/phase3/tests/fixtures/step5/syn-hit-cold.scenario.json",
            },
        }
        route = calculation_route.resolve_calculation_route(vc_case, root=ROOT)
        _, compiled = calculation_route.compile_route(root=ROOT, case=vc_case, route=route)
        self.assertFalse(compiled.ok)
        codes = {item.get("code") for item in compiled.diagnostics}
        self.assertTrue(codes & {"definitions_v3_binding_failed", "input_load_failed", "capability_gap"}, codes)
        _, bad = calculation_route.compile_route(
            root=ROOT,
            case=vc_case,
            route=route,
            input_overrides={
                "calculation_input": ".scratch/does-not-exist.json",
                "calculation_input_sha256": "0" * 64,
            },
        )
        self.assertFalse(bad.ok)
        self.assertTrue({item.get("code") for item in bad.diagnostics} & {"input_load_failed", "definitions_v3_binding_failed"})

    def test_runtime_scenario_recomputes_o1_and_identity(self):
        plan, result, _ = _plan_result()
        base = plan_v4.execute_plan_v4(plan)
        self.assertAlmostEqual(base["totals"]["hit_damage"], 44.0, delta=1e-9)
        updated = plan_v4.execute_plan_v4(plan, {"target": {"resistance": 0.6}})
        self.assertAlmostEqual(updated["totals"]["hit_damage"], 22.0, delta=1e-9)
        self.assertEqual(updated["scenario"]["target"]["resistance"], 0.6)
        self.assertNotEqual(updated["scenario_sha256"], base["scenario_sha256"])
        self.assertNotEqual(updated["execution_fingerprint"], base["execution_fingerprint"])
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.execute_plan_v4(plan, {"target": {"unknown": 1}})
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.execute_plan_v4(plan, {"primary_evaluation": "other"})

    def test_stage_rejects_reviewer_tamper_cases(self):
        plan, result, case = _plan_result()
        tampered = []
        coverage = copy.deepcopy(result)
        coverage["coverage"] = {"status": "partial", "gap_count": 999, "gaps": []}
        tampered.append(coverage)
        subject = copy.deepcopy(result)
        subject["subject"]["build_id"] = "other-build"
        tampered.append(subject)
        component = copy.deepcopy(result)
        component["applications_ledger"][0]["component_id"] = "missing-component"
        component["contribution_application_summary"] = plan_v4.dps_result.application_summary(
            component["applications_ledger"]
        )
        tampered.append(component)
        evaluations = copy.deepcopy(result)
        evaluations["evaluations"] = []
        tampered.append(evaluations)
        for item in tampered:
            checks = plan_v4.stage_validation_checks(plan, item, root=ROOT, case=case)
            self.assertFalse(all(ok for ok, _ in checks.values()), item.get("schema"))
        empty_caps = copy.deepcopy(plan)
        empty_caps["capabilities"] = []
        empty_caps["plan_sha256"] = planmod.compute_plan_sha256(empty_caps)
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_plan_v4(empty_caps)



    def test_fr_vc_applicability_uses_real_facts_and_conversion_premises(self):
        roots = {
            "v01cv": ("examples/void-cleave-20261110", "examples/void-cleave-20261110/inputs/calculation-input.json"),
            "fr11mv": ("examples/flame-reave", "examples/flame-reave/inputs/calculation-input.affix-db.corrected.json"),
        }
        for ability, (case_rel, calc_rel) in roots.items():
            case = json.loads((ROOT / case_rel / "case.json").read_text(encoding="utf-8"))
            case["_path"] = f"{case_rel}/case.json"
            calc = json.loads((ROOT / calc_rel).read_text(encoding="utf-8"))
            facts = onboarding._derive_main_skill_facts(ROOT, case, calc, ability)
            self.assertIn("damage_tags", facts)
            skill = case.setdefault("main_skill", {})
            skill["ability_id"] = ability
            skill.update(facts)
            character = calc.get("character") or {}
            skill["class"] = (character.get("class") or {}).get("name")
            candidates = calculation_route.discover_definition_candidates(case, root=ROOT)
            self.assertEqual(candidates.get("status"), "applicable_single", candidates)
        vc = json.loads((ROOT / "examples/void-cleave-20261110/case.json").read_text(encoding="utf-8"))
        vc["_path"] = "examples/void-cleave-20261110/case.json"
        vc_calc = json.loads((ROOT / "examples/void-cleave-20261110/inputs/calculation-input.json").read_text(encoding="utf-8"))
        vc["main_skill"] = {"ability_id": "v01cv", "class": "Sentinel"}
        vc["main_skill"].update(onboarding._derive_main_skill_facts(ROOT, vc, vc_calc, "v01cv"))
        vc["main_skill"]["conversion_premises"] = []
        self.assertEqual(
            calculation_route.discover_definition_candidates(vc, root=ROOT).get("status"),
            "not_applicable",
        )

    def test_onboarding_partial_not_blocked(self):
        projection = onboarding._project_onboarding_status(
            {"engine": "unified_dps", "status": "ready", "diagnostics": []},
            {"readiness": "partial", "stage_results": [{"stage_id": name, "execution_status": "completed"} for name in ("resolve_skills", "compile_plan", "min_validate")]},
            3,
            {"status": "applicable_single"},
        )
        self.assertEqual(projection["status"], "partial")
        self.assertEqual(projection["blocked_stages"], [])
        self.assertEqual(projection["next_actions"], ["run"])


if __name__ == "__main__":
    unittest.main()
