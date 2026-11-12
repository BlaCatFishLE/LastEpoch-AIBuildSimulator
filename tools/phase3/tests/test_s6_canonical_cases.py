"""S6 入口收敛验证：三个 canonical case 走 definitions_v3，无 legacy/profile 静默回退。

只做 compile/execute smoke 与路由断言；不生成/覆盖正式 run，不跑网页矩阵。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"

sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route, plan_executor, plan_v4  # noqa: E402

CASES = {
    "vc": {
        "case": "examples/void-cleave-20261110/case.json",
        "legacy": "examples/void-cleave-20261110/case.profile-v2.json",
        "alias": "examples/void-cleave-20261110/case.definitions-v3.json",
        "plan_schema": "le-calculation-plan-v4",
        "result_schema": "le-dps-result-v6",
        "signed": "reports/void-cleave-20261110/dps-step4-vc-8/result.json",
        "canonical_run": "reports/void-cleave-20261110/dps-step5-v4-vc-13",
    },
    "fr": {
        "case": "examples/flame-reave/case.json",
        "legacy": "examples/flame-reave/case.legacy-hit.json",
        "alias": "examples/flame-reave/case.definitions-v3.json",
        "plan_schema": "le-calculation-plan-v4",
        "result_schema": "le-dps-result-v6",
        "signed": "reports/flame-reave/dps-step5-p1c-fr-1/result.json",
        "canonical_run": "reports/flame-reave/dps-step5-v4-fr-18",
    },
    "bh": {
        "case": "examples/black-hole/case.json",
        "legacy": "examples/black-hole/case.legacy.json",
        "alias": "examples/black-hole/case.definitions-v3.json",
        "plan_schema": "le-calculation-plan-v4",
        "result_schema": "le-dps-result-v6",
        "signed": "reports/black-hole/dps-step5-p1c-bh-1/result.json",
        "canonical_run": "reports/black-hole/dps-step5-v4-bh-10",
    },
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def _case(rel: str) -> dict:
    case = _load(rel)
    case["_path"] = rel
    return case


def _target_from_plan(plan: dict, document: dict) -> dict:
    target = {
        control["key"]: control.get("default")
        for control in plan.get("controls") or []
        if control.get("namespace") == "target"
    }
    if document.get("monster_level") is not None:
        target["monster_level"] = document["monster_level"]
    if document.get("resistance") is not None:
        target["resistance"] = document["resistance"]
    for key in list(target):
        if key in document and key not in ("monster_level", "resistance"):
            target[key] = document[key]
    return target


def _scenario_inputs(rel: str, plan: dict) -> dict:
    case = _case(rel)
    source = ((case.get("calculation") or {}).get("source") or {})
    entries = source.get("scenarios")
    if entries:
        evaluations = []
        for evaluation in plan.get("evaluations") or []:
            path = evaluation.get("scenario_path")
            document = _load(str(path))
            evaluations.append(
                {
                    "evaluation_id": str(evaluation["evaluation_id"]),
                    "scenario": {
                        "states": dict(document.get("controls") or {}),
                        "target": _target_from_plan(plan, document),
                    },
                }
            )
        return {"evaluations": evaluations}
    document = _load(str(source.get("scenario")))
    return {
        "states": dict(document.get("controls") or {}),
        "target": _target_from_plan(plan, document),
    }


def _compile(rel: str):
    case = _case(rel)
    route, compiled = calculation_route.compile_route(root=ROOT, case=case)
    if compiled.plan is None:
        raise AssertionError(f"{rel} 编译失败：{(compiled.diagnostics or [])[:3]}")
    return route, compiled.plan


class S6CanonicalCaseTest(unittest.TestCase):
    def test_canonical_cases_route_to_definitions_v3_without_fallback(self):
        for name, spec in CASES.items():
            with self.subTest(case=name):
                case = _case(spec["case"])
                self.assertEqual(case.get("schema"), "le-case-v1")
                self.assertEqual(
                    ((case.get("calculation") or {}).get("source") or {}).get("kind"),
                    "definitions_v3",
                )
                route = calculation_route.resolve_calculation_route(copy.deepcopy(case), root=ROOT)
                self.assertEqual(route.get("engine"), "unified_dps")
                self.assertEqual(route.get("status"), "ready", route.get("diagnostics"))
                self.assertEqual(route.get("route_id"), "unified:definitions_v3")
                self.assertEqual((route.get("source") or {}).get("source_kind"), "definitions_v3")
                self.assertIsNone(route.get("fallback"))
                self.assertFalse(
                    [
                        item
                        for item in route.get("diagnostics") or []
                        if "fallback" in str(item).lower() or "synthetic" in str(item).lower()
                    ]
                )

    def test_canonical_default_reports_pass_serve_precheck(self):
        for name, spec in CASES.items():
            with self.subTest(case=name):
                case = _case(spec["case"])
                run = ROOT / spec["canonical_run"]
                self.assertTrue(case["output"].get("canonical_run_id"))
                self.assertTrue((run / "report-binding.json").is_file())
                binding = _load(str((run / "report-binding.json").relative_to(ROOT)))
                route = calculation_route.resolve_calculation_route(copy.deepcopy(case), root=ROOT)
                expected = calculation_route.dependency_fingerprints(
                    root=ROOT, route=route, plan=None
                )
                self.assertEqual(binding.get("input_fingerprints"), expected)
                plan = planmod.load_plan(run / "calculation-plan.json")
                if plan.get("schema") == "le-calculation-plan-v4":
                    plan_v4.validate_plan_v4(plan)
                else:
                    self.assertFalse(planmod.validate_plan_structure(plan))
                for ref in plan.get("input_refs") or []:
                    path = ref.get("path")
                    if not path:
                        continue
                    file_path = ROOT / path
                    self.assertTrue(file_path.is_file(), path)
                    self.assertEqual(planmod.sha256_file(file_path), ref["sha256"], path)
                self.assertTrue(str(binding.get("recompute_endpoint") or "").endswith("/recompute"))

    def test_canonical_plan_versions_and_signed_numbers_match(self):
        expectations = {
            "vc": ("single_hit", "sustained_dps"),
            "fr": ("single_hit", "sustained_dps"),
        }
        for name, spec in CASES.items():
            with self.subTest(case=name):
                route, plan = _compile(spec["case"])
                self.assertEqual(plan["schema"], spec["plan_schema"])
                if plan.get("schema") == "le-calculation-plan-v4":
                    self.assertTrue(plan.get("assembly"))
                    self.assertNotIn("synthetic", json.dumps(plan, ensure_ascii=False).lower())
                    result = plan_executor.execute_plan(plan, None)
                else:
                    self.assertEqual(plan["compiler"]["origin"], "definitions_v3")
                    self.assertNotIn("synthetic", json.dumps(plan, ensure_ascii=False).lower())
                    result = plan_executor.execute_plan(plan, _scenario_inputs(spec["case"], plan))
                self.assertEqual(result["schema"], spec["result_schema"])
                if plan.get("schema") == "le-calculation-plan-v4":
                    if name == "vc":
                        baseline = _load("reports/void-cleave-20261110/dps-step5-p1-vc-1/result.json")
                        self.assertAlmostEqual(result["totals"]["hit_damage"], baseline["single_hit"], delta=1e-9)
                        self.assertAlmostEqual(result["totals"]["horizon_dps"], baseline["sustained_dps"], delta=1e-9)
                    elif name == "fr":
                        evals = {item["evaluation_id"]: item for item in result["evaluations"]}
                        self.assertAlmostEqual(evals["single_hit"]["horizon_damage"], 51831.5589095769, delta=1e-9)
                        self.assertAlmostEqual(evals["dps"]["horizon_dps"], 121329.04081416794, delta=1e-9)
                    elif name == "bh":
                        evals = {item["evaluation_id"]: item for item in result["evaluations"]}
                        self.assertAlmostEqual(evals["eager"]["horizon_dps"], 2919759.1155459494, delta=1e-6)
                        self.assertAlmostEqual(evals["patient"]["horizon_dps"], 1337984.919231453, delta=1e-6)
                    continue
                signed = _load(spec["signed"])
                if name in expectations:
                    for key in expectations[name]:
                        self.assertAlmostEqual(result[key], signed[key], delta=1e-9, msg=f"{name}.{key}")
                if name == "bh":
                    self.assertEqual(len(result["applications"]), 609)
                    self.assertAlmostEqual(
                        result["single_cast"]["totals"]["single_cast_total"],
                        signed["single_cast"]["totals"]["single_cast_total"],
                        delta=1e-6,
                    )
                    result_loops = {loop["policy_id"]: loop for loop in result["loops"]}
                    signed_loops = {loop["policy_id"]: loop for loop in signed["loops"]}
                    for policy in ("eager", "patient"):
                        self.assertAlmostEqual(
                            result_loops[policy]["totals"]["horizon_dps"],
                            signed_loops[policy]["totals"]["horizon_dps"],
                            delta=1e-6,
                        )

    def test_canonical_alias_and_legacy_migration_files_are_preserved(self):
        for name, spec in CASES.items():
            with self.subTest(case=name):
                canonical = _load(spec["case"])
                alias = _load(spec["alias"])
                self.assertEqual(canonical, alias)
                self.assertTrue((ROOT / spec["legacy"]).is_file())
                self.assertNotEqual(Path(spec["case"]).name, Path(spec["legacy"]).name)
                legacy = _case(spec["legacy"])
                legacy_route = calculation_route.resolve_calculation_route(
                    copy.deepcopy(legacy), root=ROOT
                )
                self.assertNotEqual(legacy_route.get("route_id"), "unified:definitions_v3")
                if name == "vc":
                    self.assertEqual(
                        ((legacy_route.get("source") or {}).get("source_kind")), "profile_v2"
                    )

    def test_canonical_chain_does_not_import_legacy_calculators(self):
        script = SCRATCH / "s6_no_legacy_import.py"
        script.write_text(
            "\n".join(
                [
                    "import sys, importlib.abc",
                    "BLOCKED = {'le_calc.damage','le_calc.ignite','le_calc.cooldown','le_calc.simulate',",
                    "           'le_calc.hit_inputs','le_calc.hit_damage','le_calc.hit_simulate','le_calc.dps'}",
                    "class Block(importlib.abc.MetaPathFinder):",
                    "    def find_spec(self, fullname, path=None, target=None):",
                    "        if fullname in BLOCKED:",
                    "            raise RuntimeError('legacy calculator imported: ' + fullname)",
                    "        return None",
                    "sys.meta_path.insert(0, Block())",
                    f"sys.path.insert(0, {str(ROOT / 'tools/phase3/src')!r})",
                    "import json, copy",
                    "from pathlib import Path",
                    f"ROOT = Path({str(ROOT)!r})",
                    "from le_calc import calculation_route, plan_executor",
                    f"CASES = {json.dumps({k: v['case'] for k, v in CASES.items()})}",
                    "for name, rel in CASES.items():",
                    "    case = json.loads((ROOT / rel).read_text(encoding='utf-8'))",
                    "    case['_path'] = rel",
                    "    route, compiled = calculation_route.compile_route(root=ROOT, case=case)",
                    "    assert compiled.plan is not None, compiled.diagnostics",
                    "    if compiled.plan.get('schema') == 'le-calculation-plan-v4':\n        assert compiled.plan.get('assembly'), compiled.diagnostics\n    else:\n        assert compiled.plan['compiler']['origin'] == 'definitions_v3'",
                    "    assert not (set(sys.modules) & BLOCKED), sys.modules.keys() & BLOCKED",
                    "print('OK')",
                ]
            ),
            encoding="utf-8",
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT / "tools/phase3/src"))
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertIn("OK", proc.stdout)


if __name__ == "__main__":
    unittest.main()
