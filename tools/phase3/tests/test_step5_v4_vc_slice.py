"""task-13 VC v4 生产切片：真实装配、result-v6 映射、HTTP 重算。"""
from __future__ import annotations

import inspect
import json
import socket
import subprocess
import sys
import time
import unittest
from unittest import mock
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route, plan_executor, plan_v4  # noqa: E402

CASE_REL = "examples/void-cleave-20261110/case.json"
RUN = ROOT / "reports/void-cleave-20261110/dps-step5-v4-vc-13"
BASELINE = ROOT / "reports/void-cleave-20261110/dps-step5-p1-vc-1/result.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _case() -> dict:
    case = json.loads((ROOT / CASE_REL).read_text(encoding="utf-8"))
    case["_path"] = CASE_REL
    return case


class VcV4ProductionSliceTest(unittest.TestCase):
    def test_vc_compiles_through_real_frozen_context_and_maps_ledger(self):
        case = _case()
        route, compiled = calculation_route.compile_route(root=ROOT, case=case)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        self.assertEqual(plan["schema"], "le-calculation-plan-v4")
        self.assertTrue(plan.get("assembly"))
        self.assertEqual(plan["subject"]["build_id"], case["build"]["build_id"])
        roles = {ref.get("role") for ref in plan.get("input_refs") or []}
        self.assertTrue({"build_spec", "calculation_input", "case", "template", "scenario"} <= roles, roles)
        result = plan_executor.execute_plan(plan, None)
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(result["schema"], "le-dps-result-v6")
        self.assertAlmostEqual(result["totals"]["hit_damage"], baseline["single_hit"], delta=1e-9)
        self.assertAlmostEqual(result["totals"]["horizon_dps"], baseline["sustained_dps"], delta=1e-9)
        self.assertEqual(result["contributions"], baseline["contributions"])
        self.assertEqual(result["effect_ledger"], baseline["effect_ledger"])
        self.assertEqual(result["contribution_application_summary"], baseline["contribution_application_summary"])
        self.assertEqual(result["coverage"], baseline["coverage"])
        self.assertEqual(result["scenario"], baseline["scenario"])
        self.assertEqual(len(result["applications_ledger"]), len(baseline["applications"]))
        self.assertEqual(
            [(w["window_s"], w["hit_damage"], w["dot_damage"], w["cast_count"]) for w in result["windows"]],
            [(w["seconds"], w["hit_damage"], w["dot_damage"], w["casts"]) for w in baseline["windows"]],
        )
        component = next(iter(result["target_side"]["components"].values()))
        self.assertAlmostEqual(component["resistance"], 0.0, delta=1e-12)
        self.assertAlmostEqual(component["damage_reduction"], 0.87, delta=1e-12)
        plan_v4.validate_plan_v4(plan)
        plan_v4.validate_result_v6(plan, result)

    def test_vc_native_v4_does_not_call_legacy_execute_and_effect_drives_result(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        self.assertTrue(plan.get("execution"))
        with mock.patch.object(plan_executor, "execute_plan", side_effect=RuntimeError("legacy execute called")):
            result = plan_v4.execute_plan_v4(plan)
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
        self.assertEqual(result["contributions"], baseline["contributions"])
        self.assertEqual(result["effect_ledger"], baseline["effect_ledger"])
        self.assertAlmostEqual(result["totals"]["horizon_dps"], baseline["sustained_dps"], delta=1e-9)

        mutated = json.loads(json.dumps(plan))
        effects = mutated["execution"]["effects"]
        target = next(
            item for item in effects
            if item.get("effect_id") == "mod:equipment.head#affix#normal#1.1"
        )
        target["value"] = float(target["value"]) + 1.0
        generic_target = next(
            item for item in mutated["execution"]["execution_v1"]["effects"]
            if item.get("effect_id") == "mod:equipment.head#affix#normal#1.1"
        )
        generic_target["value"] = float(generic_target["value"]) + 1.0
        mutated["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(mutated["execution"]))
        mutated["plan_sha256"] = planmod.compute_plan_sha256(mutated)
        changed = plan_v4.execute_plan_v4(mutated)
        self.assertNotAlmostEqual(
            changed["totals"]["horizon_dps"], result["totals"]["horizon_dps"], delta=1e-6
        )
        removed = json.loads(json.dumps(plan))
        removed["execution"]["effects"] = [
            item for item in removed["execution"]["effects"]
            if item.get("effect_id") != "mod:equipment.head#affix#normal#1.1"
        ]
        removed["execution"]["execution_v1"]["effects"] = [
            item for item in removed["execution"]["execution_v1"]["effects"]
            if item.get("effect_id") != "mod:equipment.head#affix#normal#1.1"
        ]
        removed["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(removed["execution"]))
        removed["plan_sha256"] = planmod.compute_plan_sha256(removed)
        without = plan_v4.execute_plan_v4(removed)
        self.assertNotEqual(len(without["contributions"]), len(result["contributions"]))


    def test_vc_consumes_execution_v1_fields_and_mutations_change_result(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        execution_v1 = plan["execution"]["execution_v1"]
        self.assertEqual(execution_v1["schema"], "le-plan-execution-v1")
        self.assertEqual(len(execution_v1["components"]), 2)
        self.assertEqual(len(execution_v1["applications"]), 2)
        self.assertEqual(len(execution_v1["effects"]), 232)
        self.assertEqual(len(execution_v1["condition_programs"]), 10)
        self.assertTrue(execution_v1["frequency"]["windows_s"])
        base = plan_v4.execute_plan_v4(plan)
        old = json.loads((RUN / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(base["contributions"], old["contributions"])
        self.assertEqual(base["applications_ledger"], old["applications_ledger"])

        def mutate(mutator):
            data = json.loads(json.dumps(plan))
            mutator(data["execution"]["execution_v1"])
            data["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(data["execution"]))
            data["plan_sha256"] = planmod.compute_plan_sha256(data)
            return plan_v4.execute_plan_v4(data)

        effect = next(item for item in execution_v1["effects"] if item.get("emits_contribution"))
        def bump_effect(ev1):
            target = next(item for item in ev1["effects"] if item.get("effect_id") == "mod:equipment.head#affix#normal#1.1")
            target["value"] = float(target["value"]) + 1.0
        changed = mutate(bump_effect)
        self.assertNotAlmostEqual(changed["totals"]["horizon_dps"], base["totals"]["horizon_dps"], delta=1e-6)

        def bump_duration(ev1):
            ev1["applications"][0]["duration_s"] = float(ev1["applications"][0]["duration_s"]) + 1.0
        changed_app = mutate(bump_duration)
        self.assertTrue(changed_app["totals"] != base["totals"], "application mutation must change totals")

        def change_windows(ev1):
            ev1["frequency"]["windows_s"] = [1.0, 5.0, 30.0]
        changed_windows = mutate(change_windows)
        self.assertEqual([item["window_s"] for item in changed_windows["windows"]], [1.0, 5.0, 30.0])

        def change_channel(ev1):
            ev1["components"][0]["damage_channel"] = "Void"
            ev1["components"][0]["resistance_key"] = "void"
            ev1["components"][0]["damage_type"] = "Void"
            ev1["components"][0]["adaptive_damage_type"] = "Void"
        changed_channel = mutate(change_channel)
        self.assertTrue(changed_channel["totals"] != base["totals"], "channel mutation must change totals")

        broken = json.loads(json.dumps(plan))
        broken["execution"]["execution_v1"]["components"][0]["kind"] = "unknown"
        broken["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(broken["execution"]))
        broken["plan_sha256"] = planmod.compute_plan_sha256(broken)
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.execute_plan_v4(broken)

        broken_app = json.loads(json.dumps(plan))
        broken_app["execution"]["execution_v1"]["applications"][0]["cohort_policy"] = "unknown"
        broken_app["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(broken_app["execution"]))
        broken_app["plan_sha256"] = planmod.compute_plan_sha256(broken_app)
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.execute_plan_v4(broken_app)

    def test_vc_execution_has_no_skill_dispatch_or_constants(self):
        source = inspect.getsource(plan_v4._execute_native_v4) + inspect.getsource(plan_v4._build_execution_v1)
        for token in ("ability_id", "legacy_schema", "ignite", "heat wave", "eager", "patient", "execute_plan_v2", "execute_plan_v3"):
            self.assertNotIn(token, source.lower())

    def _mutate_execution_v1(self, plan, mutator):
        data = json.loads(json.dumps(plan))
        mutator(data["execution"]["execution_v1"])
        data["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(data["execution"]))
        data["plan_sha256"] = planmod.compute_plan_sha256(data)
        return plan_v4.execute_plan_v4(data)

    def test_vc_p1a_execution_v1_is_sole_runtime_authority(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        stripped = json.loads(json.dumps(plan))
        for key in ("parameters", "schedule", "controls", "components", "effects", "conditions", "facts", "scope"):
            stripped["execution"].pop(key, None)
        stripped["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(stripped["execution"]))
        stripped["plan_sha256"] = planmod.compute_plan_sha256(stripped)
        result = plan_v4.execute_plan_v4(stripped)
        for key in ("contributions", "applications_ledger", "effect_ledger", "scenario", "windows", "coverage", "issues", "totals"):
            self.assertEqual(result[key], old[key], key)

        def base(ev1):
            ev1["components"][0]["base_damage"] = 1002.0
        self.assertNotEqual(self._mutate_execution_v1(plan, base)["totals"], old["totals"])
        def interval(ev1):
            ev1["frequency"]["cast_interval_s"] = 1000.0
        changed = self._mutate_execution_v1(plan, interval)
        self.assertNotEqual(changed["totals"], old["totals"])
        self.assertNotEqual(changed["evaluations"][0]["cast_count"], old["evaluations"][0]["cast_count"])
        def dr(ev1):
            ev1["target"]["damage_reduction"] = 0.0
        self.assertNotEqual(self._mutate_execution_v1(plan, dr)["totals"], old["totals"])

    def test_vc_p1b_sources_resolve_and_verified_not_applicable(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        effect = next(item for item in plan["execution"]["execution_v1"]["effects"] if item.get("emits_contribution"))
        source_id = str(effect["source_refs"][0])

        def dropped(ev1):
            ev1["sources"] = [item for item in ev1["sources"] if item.get("source_id") != source_id]
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, dropped)

        def empty_refs(ev1):
            next(item for item in ev1["effects"] if item.get("emits_contribution"))["source_refs"] = []
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, empty_refs)

        def duplicate(ev1):
            ev1["sources"].append(json.loads(json.dumps(ev1["sources"][0])))
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, duplicate)

        def unverified(ev1):
            next(item for item in ev1["sources"] if item.get("source_id") == source_id)["binding_status"] = "unverified"
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, unverified)

        def not_applicable(ev1):
            ev1["components"][0]["damage_channel"] = "Cold"
            ev1["components"][0]["damage_type"] = "Cold"
            ev1["components"][0]["adaptive_damage_type"] = "Cold"
            ev1["components"][0]["tags"] = ["Melee", "Hit"]
        changed = self._mutate_execution_v1(plan, not_applicable)
        self.assertEqual(changed["coverage"], old["coverage"])

    def test_p1b_identity_mismatch_source_rejected(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        plan = compiled.plan
        effect = next(item for item in plan["execution"]["execution_v1"]["effects"] if item.get("emits_contribution"))
        source_id = str(effect["source_refs"][0])
        def mismatch(ev1):
            next(item for item in ev1["sources"] if item.get("source_id") == source_id)["binding_status"] = "identity_mismatch"
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, mismatch)

    def test_p1b_duplicate_ownership_key_rejected_before_execution(self):
        case = _case()
        _, compiled = calculation_route.compile_route(root=ROOT, case=case)
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        original = next(item for item in plan["execution"]["execution_v1"]["effects"] if item.get("emits_contribution"))
        def duplicate(ev1):
            copied = json.loads(json.dumps(original))
            copied["effect_id"] = "copied:ownership:duplicate"
            copied["contribution_id"] = "copied:ownership:duplicate"
            copied["value"] = float(copied.get("value") or 0.0) + 1.0
            ev1["effects"].append(copied)
        with self.assertRaises(plan_v4.PlanV4Error):
            self._mutate_execution_v1(plan, duplicate)
        self.assertEqual(len(old["contributions"]), 232)

    def test_vc_http_recompute_changes_scenario_not_plan(self):
        port = 8765  # 报告 binding 绑定端口；serve 前置检查要求一致
        process = subprocess.Popen(
            [sys.executable, "scripts/le_commands.py", "serve", "--case", CASE_REL,
             "--report", "reports/void-cleave-20261110/dps-step5-v4-vc-13", "--port", str(port)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 20
            while time.time() < deadline:
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/dps-report.html", timeout=1) as response:
                        if response.status == 200:
                            break
                except Exception:
                    time.sleep(0.2)
            else:
                self.fail("v4 serve 未在超时内启动")
            binding = json.loads((RUN / "report-binding.json").read_text(encoding="utf-8"))
            scenario = json.loads((RUN / "scenario.json").read_text(encoding="utf-8"))
            request_scenario = {
                "states": scenario.get("states") or {},
                "target": {**(scenario.get("target") or {}), "resistance": 0.5},
            }
            body = json.dumps(
                {
                    "binding": binding,
                    "scenario": request_scenario,
                    "request_id": "vc-v4-task13",
                    "client_session_id": "vc-v4-task13",
                    "client_edit_revision": 0,
                }
            ).encode()
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/recompute", data=body, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            old = json.loads((RUN / "result.json").read_text(encoding="utf-8"))
            result = payload["result"]
            self.assertEqual(response.status, 200)
            self.assertEqual(result["plan_sha256"], old["plan_sha256"])
            self.assertNotEqual(result["scenario_sha256"], old["scenario_sha256"])
            self.assertNotEqual(result["execution_fingerprint"], old["execution_fingerprint"])
            self.assertNotAlmostEqual(
                result["totals"]["horizon_dps"], old["totals"]["horizon_dps"], delta=1e-6
            )
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    unittest.main()
