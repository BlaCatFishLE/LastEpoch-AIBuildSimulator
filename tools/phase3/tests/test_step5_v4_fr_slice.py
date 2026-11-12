"""task-25 S4: FR execution-v1 native slice, P1-B constraints, HTTP recompute."""
from __future__ import annotations

import copy
import inspect
import json
import subprocess
import sys
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route, plan_v4  # noqa: E402

CASE_REL = "examples/flame-reave/case.json"
RUN = ROOT / "reports/flame-reave/dps-step5-v4-fr-18"
BASELINE = ROOT / "reports/flame-reave/dps-step5-p1c-fr-1/result.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _case() -> dict:
    case = json.loads((ROOT / CASE_REL).read_text(encoding="utf-8"))
    case["_path"] = CASE_REL
    return case


def _mutate(plan, mutator):
    data = copy.deepcopy(plan)
    mutator(data["execution"]["execution_v1"])
    data["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(data["execution"]))
    data["plan_sha256"] = planmod.compute_plan_sha256(data)
    return plan_v4.execute_plan_v4(data)


class FrV4NativeSliceTest(unittest.TestCase):
    def test_fr_execution_v1_fields_and_stripped_authority(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        ev1 = plan["execution"]["execution_v1"]
        self.assertEqual(ev1["schema"], "le-plan-execution-v1")
        self.assertEqual(len(ev1["components"]), 1)
        self.assertEqual(ev1["components"][0]["damage_weights"], [1.0, 0.5, 0.5, 0.5])
        self.assertEqual(len(ev1["effects"]), 229)
        self.assertEqual(len(ev1["condition_programs"]), 5)
        self.assertEqual(len(ev1["evaluations"]), 2)
        self.assertEqual(ev1["frequency"]["driver"], "steady_state")
        self.assertEqual(len(ev1["frequency"]["policies"]), 2)
        self.assertEqual(len(ev1["sources"]), 224)
        result = plan_v4.execute_plan_v4(plan)
        old = json.loads((RUN / "result.json").read_text(encoding="utf-8"))
        for key in ("contributions", "applications_ledger", "effect_ledger", "scenario", "windows", "coverage", "issues", "totals"):
            self.assertEqual(result[key], old[key], key)
        self.assertEqual(len(result["contributions"]), 229)
        self.assertEqual(len(result["applications_ledger"]), 69)
        evaluations = {item["evaluation_id"]: item for item in result["evaluations"]}
        self.assertAlmostEqual(evaluations["single_hit"]["horizon_damage"], 51831.5589095769, delta=1e-9)
        self.assertAlmostEqual(evaluations["dps"]["horizon_dps"], 121329.04081416794, delta=1e-9)
        self.assertEqual(len(result["policy_results"]), 2)

        stripped = copy.deepcopy(plan)
        for key in (
            "parameters", "schedule", "controls", "components", "effects", "conditions",
            "facts", "scope", "evaluations", "target_side", "presentation", "assumptions",
            "limitations", "diagnostics", "sources", "coverage",
        ):
            stripped["execution"].pop(key, None)
        stripped["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(stripped["execution"]))
        stripped["plan_sha256"] = planmod.compute_plan_sha256(stripped)
        stripped_result = plan_v4.execute_plan_v4(stripped)
        for key in ("contributions", "applications_ledger", "effect_ledger", "scenario", "windows", "coverage", "issues", "totals"):
            self.assertEqual(stripped_result[key], old[key], f"stripped {key}")

    def test_fr_mutations_change_result(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        def effect(ev1):
            target = next(item for item in ev1["effects"] if item.get("effect_id") == "mod:equipment.chest#affix#normal#0.1")
            target["value"] = float(target.get("value") or 0.0) + 1.0
        self.assertNotEqual(_mutate(plan, effect)["totals"], old["totals"])
        def weights(ev1):
            ev1["components"][0]["damage_weights"] = [2.0, 1.0, 1.0, 1.0]
        self.assertNotEqual(_mutate(plan, weights)["totals"], old["totals"])
        def windows(ev1):
            ev1["frequency"]["windows_s"] = [1.0, 5.0, 30.0]
        self.assertEqual([item["window_s"] for item in _mutate(plan, windows)["windows"]], [1.0, 5.0, 30.0])
        def channel(ev1):
            ev1["components"][0]["damage_channel"] = "Cold"
            ev1["components"][0]["damage_type"] = "Cold"
            ev1["components"][0]["adaptive_damage_type"] = "Cold"
        self.assertNotEqual(_mutate(plan, channel)["totals"], old["totals"])

    def test_fr_component_base_damage_is_authoritative(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        def base(ev1):
            ev1["components"][0]["base_damage"] = 1002.0
        changed = _mutate(plan, base)
        self.assertNotEqual(changed["totals"], old["totals"])
        self.assertNotAlmostEqual(changed["totals"]["horizon_dps"], old["totals"]["horizon_dps"], delta=1e-6)
        conflict = copy.deepcopy(plan)
        conflict["execution"]["execution_v1"]["parameters"]["skill"]["base_damage"] = {"Fire": 999.0}
        conflict["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(conflict["execution"]))
        conflict["plan_sha256"] = planmod.compute_plan_sha256(conflict)
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.execute_plan_v4(conflict)

    def test_fr_sources_and_ownership_constraints(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        ev1 = plan["execution"]["execution_v1"]
        effect = next(item for item in ev1["effects"] if item.get("emits_contribution"))
        source_id = str(effect["source_refs"][0])
        def mismatch(value):
            next(item for item in value["sources"] if item.get("source_id") == source_id)["binding_status"] = "identity_mismatch"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, mismatch)
        original = copy.deepcopy(effect)
        def duplicate(value):
            copied = copy.deepcopy(original)
            copied["effect_id"] = "copied:fr:ownership"
            copied["contribution_id"] = "copied:fr:ownership"
            copied["value"] = float(copied.get("value") or 0.0) + 1.0
            value["effects"].append(copied)
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, duplicate)

    def test_fr_native_has_no_skill_dispatch_source(self):
        source = inspect.getsource(plan_v4._build_execution_v1_fr) + inspect.getsource(plan_v4._fr_runtime_plan)
        for token in ("ability_id", "legacy_schema", "ignite", "heat wave", "eager", "patient", "execute_plan_v2", "execute_plan_v3"):
            self.assertNotIn(token, source.lower())

    def test_fr_http_recompute_changes_scenario_not_plan(self):
        port = 8765
        process = subprocess.Popen(
            [sys.executable, "scripts/le_commands.py", "serve", "--case", CASE_REL,
             "--report", "reports/flame-reave/dps-step5-v4-fr-18", "--port", str(port)],
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
                self.fail("FR serve 未在超时内启动")
            binding = json.loads((RUN / "report-binding.json").read_text(encoding="utf-8"))
            scenario = json.loads((RUN / "scenario.json").read_text(encoding="utf-8"))
            request_scenario = {"states": scenario.get("states") or {}, "target": {**(scenario.get("target") or {}), "resistance": 0.5}}
            body = json.dumps({"binding": binding, "scenario": request_scenario, "request_id": "fr-v4-s4", "client_session_id": "fr-v4-s4", "client_edit_revision": 0}).encode()
            request = urllib.request.Request(f"http://127.0.0.1:{port}/recompute", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read())
            old = json.loads((RUN / "result.json").read_text(encoding="utf-8"))
            result = payload["result"]
            self.assertEqual(response.status, 200)
            self.assertEqual(result["plan_sha256"], old["plan_sha256"])
            self.assertNotEqual(result["scenario_sha256"], old["scenario_sha256"])
            self.assertNotEqual(result["execution_fingerprint"], old["execution_fingerprint"])
            self.assertNotAlmostEqual(result["totals"]["horizon_dps"], old["totals"]["horizon_dps"], delta=1e-6)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


if __name__ == "__main__":
    unittest.main()
