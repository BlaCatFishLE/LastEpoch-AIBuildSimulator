"""task-26 S5: BH execution-v1 native slice, charge/application mutations, HTTP."""
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
from le_calc.mechanics import execution as mechanics_execution  # noqa: E402

CASE_REL = "examples/black-hole/case.json"
RUN = ROOT / "reports/black-hole/dps-step5-v4-bh-10"



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


class BhV4NativeSliceTest(unittest.TestCase):
    def test_bh_execution_v1_fields_and_stripped_authority(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        ev1 = plan["execution"]["execution_v1"]
        self.assertEqual(ev1["schema"], "le-plan-execution-v1")
        self.assertEqual(len(ev1["components"]), 2)
        self.assertTrue(all(item["kind"] == "dot" for item in ev1["components"]))
        self.assertEqual(len(ev1["applications"]), 1)
        self.assertEqual(len(ev1["effects"]), 203)
        self.assertEqual(len(ev1["sources"]), 197)
        self.assertEqual(len(ev1["evaluations"]), 2)
        self.assertEqual(ev1["frequency"]["driver"], "charge_dot_cycle")
        self.assertEqual(len(ev1["frequency"]["policies"]), 2)
        self.assertEqual(ev1["charge"]["max_charges"], 2)
        result = plan_v4.execute_plan_v4(plan)
        old = json.loads((RUN / "result.json").read_text(encoding="utf-8"))
        for key in ("contributions", "effect_ledger", "scenario", "windows", "coverage", "issues", "totals"):
            self.assertEqual(result[key], old[key], key)
        self.assertEqual(len(result["applications_ledger"]), 609)
        self.assertEqual(len(result["contributions"]), 203)
        self.assertEqual(len(result["applications_ledger"]), 609)
        evaluations = {item["evaluation_id"]: item for item in result["evaluations"]}
        self.assertAlmostEqual(evaluations["eager"]["horizon_dps"], 2919759.1155459494, delta=1e-6)
        self.assertAlmostEqual(evaluations["patient"]["horizon_dps"], 1337984.919231453, delta=1e-6)
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
        for key in ("contributions", "effect_ledger", "scenario", "windows", "coverage", "issues", "totals"):
            self.assertEqual(stripped_result[key], old[key], f"stripped {key}")
        self.assertEqual(len(stripped_result["applications_ledger"]), 609)

    def test_bh_mutations_change_result(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        def effect(ev1):
            target = next(item for item in ev1["effects"] if item.get("effect_id") == "mod:equipment.chest#unique_mod#1")
            target["value"] = float(target.get("value") or 0.0) + 1.0
        self.assertNotEqual(_mutate(plan, effect)["totals"], old["totals"])
        def channel(ev1):
            ev1["components"][0]["damage_type"] = "Cold"
            ev1["components"][0]["damage_channel"] = "cold"
            ev1["components"][0]["resistance_key"] = "cold"
            ev1["applications"][0]["damage_type"] = "Cold"
            ev1["applications"][0]["damage_channel"] = "cold"
            ev1["applications"][0]["resistance_key"] = "cold"
        self.assertNotEqual(_mutate(plan, channel)["totals"], old["totals"])
        def scope(ev1):
            ev1["components"][0]["scope"] = "ailment"
        self.assertNotEqual(_mutate(plan, scope)["totals"], old["totals"])
        def charge(ev1):
            ev1["charge"]["max_charges"] = 1
        self.assertNotEqual(_mutate(plan, charge)["totals"], old["totals"])
        def application(ev1):
            ev1["applications"][0]["chance"] = 1.0
        self.assertNotEqual(_mutate(plan, application)["totals"], old["totals"])
        def window(ev1):
            ev1["frequency"]["windows_s"] = [1.0, 5.0, 30.0]
        window_values = [item["window_s"] for item in _mutate(plan, window)["windows"]]
        self.assertTrue({1.0, 5.0, 30.0}.issubset(set(window_values)), window_values)
        def tail(ev1):
            ev1["components"][0]["duration_s"] = float(ev1["components"][0]["duration_s"]) + 3.0
        self.assertNotEqual(_mutate(plan, tail)["totals"], old["totals"])

    def test_bh_condition_program_and_constraints(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        ev1 = plan["execution"]["execution_v1"]
        effect = next(
            item for item in ev1["effects"]
            if item.get("emits_contribution") and item.get("condition_ref") and item.get("stat_key") == "damage"
        )
        def condition(value):
            value["condition_programs"][str(effect["condition_ref"])] = {
                "condition_id": str(effect["condition_ref"]),
                "op": "all", "terms": [{"kind": "control_true", "key": "__never_for_test__"}]
            }
        self.assertNotEqual(_mutate(plan, condition)["totals"], plan_v4.execute_plan_v4(plan)["totals"])
        source_id = str(effect["source_refs"][0])
        def mismatch(value):
            next(item for item in value["sources"] if item.get("source_id") == source_id)["binding_status"] = "identity_mismatch"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, mismatch)
        original = copy.deepcopy(effect)
        def duplicate(value):
            copied = copy.deepcopy(original)
            copied["effect_id"] = "copied:bh:ownership"
            copied["contribution_id"] = "copied:bh:ownership"
            copied["value"] = float(copied.get("value") or 0.0) + 1.0
            value["effects"].append(copied)
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, duplicate)

    def test_bh_application_fields_and_capability_matrix(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        ev1 = plan["execution"]["execution_v1"]
        self.assertTrue(ev1["applications"][0]["ailment_id"])
        self.assertEqual(ev1["applications"][0]["scope"], "ailment")
        def chance(value):
            value["applications"][0]["chance"] = 1.0
        self.assertNotEqual(_mutate(plan, chance)["totals"], old["totals"])
        def duration(value):
            value["applications"][0]["duration_s"] = float(value["applications"][0]["duration_s"]) + 2.0
        self.assertNotEqual(_mutate(plan, duration)["totals"], old["totals"])
        def poison(value):
            value["applications"][0]["damage_type"] = "Poison"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, poison)
        def body(value):
            value["applications"][0]["scope"] = "body"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, body)
        synthetic = {
            "application_id": "synth:application", "ailment_id": "Bleed", "damage_type": "Fire",
            "scope": "ailment", "cohort_policy": "independent_cast", "duration_s": 2.0,
            "tick_interval_s": 0.5, "chance": 0.4, "expected_stacks": 2.0,
        }
        checked = mechanics_execution.validate_application_capabilities(synthetic, {"Fire", "Cold"})
        self.assertEqual(checked["ailment_id"], "Bleed")
        self.assertEqual(checked["damage_type"], "Fire")
        with self.assertRaises(mechanics_execution.ExecutionContractError):
            mechanics_execution.validate_application_capabilities({**synthetic, "damage_type": "Poison"}, {"Fire", "Cold"})
        with self.assertRaises(mechanics_execution.ExecutionContractError):
            mechanics_execution.validate_application_capabilities({**synthetic, "scope": "body"}, {"Fire", "Cold"})

    def test_bh_application_identity_and_channel_are_declared(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        ev1 = plan["execution"]["execution_v1"]
        app = ev1["applications"][0]
        body_ids = {item["component_id"] for item in ev1["components"]}
        result = plan_v4.execute_plan_v4(plan)
        ledger = result["applications_ledger"]
        body_rows = [row for row in ledger if row.get("component_id") in body_ids]
        application_rows = [row for row in ledger if row.get("component_id") not in body_ids]
        self.assertEqual(len(ledger), 609)
        self.assertEqual(len(body_rows), 406)
        self.assertEqual(len(application_rows), 203)
        self.assertTrue(application_rows)
        for row in application_rows:
            self.assertEqual(row["application_id"], app["application_id"])
            self.assertEqual(row["ailment_id"], app["ailment_id"])
            self.assertEqual(row["damage_channel"], app["damage_channel"])
            self.assertEqual(row["scope"], app["scope"])
        self.assertNotEqual(body_rows[0]["application_id"], app["application_id"])
        self.assertNotIn("application", str(body_rows[0].get("scope")))
        app_unit = next(item for item in result["application_units"] if item.get("kind") == "application")
        self.assertEqual(app_unit["component_id"], "bh-ignite")
        self.assertEqual(app_unit["application_id"], app["application_id"])
        self.assertEqual(app_unit["ailment_id"], app["ailment_id"])
        known = {item.get("component_id") for item in result["components"]}
        known |= {item.get("component_id") for item in result["application_units"]}
        self.assertTrue({row.get("component_id") for row in ledger}.issubset(known))

        def custom(value):
            value["applications"][0]["application_id"] = "custom:bleed-app"
            value["applications"][0]["ailment_id"] = "Bleed"
        changed = _mutate(plan, custom)
        changed_body = [row for row in changed["applications_ledger"] if row.get("component_id") in body_ids]
        changed_app = [row for row in changed["applications_ledger"] if row.get("component_id") not in body_ids]
        self.assertTrue(changed_app)
        changed_unit = next(item for item in changed["application_units"] if item.get("kind") == "application")
        self.assertEqual(changed_unit["component_id"], "bh-ignite")
        self.assertEqual(changed_unit["application_id"], "custom:bleed-app")
        self.assertEqual({row["application_id"] for row in changed_app}, {"custom:bleed-app"})
        self.assertEqual({row["ailment_id"] for row in changed_app}, {"Bleed"})
        self.assertNotIn("custom:bleed-app", {row["application_id"] for row in changed_body})

        def cold(value):
            value["applications"][0]["damage_type"] = "Cold"
            value["applications"][0]["damage_channel"] = "cold"
            value["applications"][0]["resistance_key"] = "cold"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, cold)

        def bad_channel(value):
            value["applications"][0]["damage_channel"] = "poison"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, bad_channel)

        synthetic_ledger = [
            {"component_id": "body-unit", "status": "applied"},
            {"component_id": "app-unit", "status": "applied"},
        ]
        declared = [{
            "application_id": "synth:custom-app", "ailment_id": "Bleed", "source_component": "body-unit",
            "damage_type": "Fire", "damage_channel": "fire", "resistance_key": "fire", "scope": "ailment",
        }]
        projected = mechanics_execution.apply_application_identity(synthetic_ledger, declared, {"body-unit"})
        self.assertNotEqual(projected[0].get("application_id"), "synth:custom-app")
        self.assertEqual(projected[1]["application_id"], "synth:custom-app")
        self.assertEqual(projected[1]["ailment_id"], "Bleed")
        self.assertEqual(projected[1]["scope"], "ailment")

    def test_bh_result_component_reference_gate_rejects_dangling(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        result = plan_v4.execute_plan_v4(plan)
        broken = json.loads(json.dumps(result))
        broken["applications_ledger"].append({
            "application_id": "dangling", "component_id": "component:missing", "status": "applied"
        })
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_result_v6(plan, broken)

    def test_bh_charge_conditions_drive_scheduler_not_policy_id(self):
        _, compiled = calculation_route.compile_route(root=ROOT, case=_case())
        plan = compiled.plan
        old = plan_v4.execute_plan_v4(plan)
        old_eager = next(item for item in old["policy_results"] if item["policy_id"] == "eager")
        def wait(value):
            value["frequency"]["action_policies"][0]["cast_condition"] = "wait_for_all_instances_expired"
        changed = _mutate(plan, wait)
        changed_eager = next(item for item in changed["policy_results"] if item["policy_id"] == "eager")
        self.assertNotEqual(changed_eager["totals"]["cast_count"], old_eager["totals"]["cast_count"])
        self.assertNotEqual(changed_eager["totals"]["max_concurrent"], old_eager["totals"]["max_concurrent"])

        def rename(value):
            value["frequency"]["action_policies"][0]["policy_id"] = "eager_renamed"
            for evaluation in value["evaluations"]:
                if evaluation.get("action_policy") == "eager":
                    evaluation["action_policy"] = "eager_renamed"
        renamed = _mutate(plan, rename)
        renamed_row = next(item for item in renamed["policy_results"] if item["policy_id"] == "eager_renamed")
        self.assertEqual(renamed_row["totals"], old_eager["totals"])

        def unknown(value):
            value["frequency"]["action_policies"][0]["cast_condition"] = "unknown_condition"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, unknown)
        def conflict(value):
            value["charge"]["condition"] = "wait_for_all_instances_expired"
        with self.assertRaises(plan_v4.PlanV4Error):
            _mutate(plan, conflict)

    def test_bh_native_has_no_skill_dispatch_source(self):
        source = inspect.getsource(plan_v4._build_execution_v1_bh) + inspect.getsource(plan_v4._bh_runtime_plan)
        for token in ("ability_id", "legacy_schema", "heat wave", "eager", "patient", "execute_plan_v3"):
            self.assertNotIn(token, source.lower())

    def test_bh_http_recompute_changes_scenario_not_plan(self):
        port = 8765
        process = subprocess.Popen(
            [sys.executable, "scripts/le_commands.py", "serve", "--case", CASE_REL,
             "--report", "reports/black-hole/dps-step5-v4-bh-10", "--port", str(port)],
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
                self.fail("BH serve 未在超时内启动")
            binding = json.loads((RUN / "report-binding.json").read_text(encoding="utf-8"))
            scenario = json.loads((RUN / "scenario.json").read_text(encoding="utf-8"))
            request_scenario = {"states": scenario.get("states") or {}, "target": {**(scenario.get("target") or {}), "resistance": 0.5}}
            body = json.dumps({"binding": binding, "scenario": request_scenario, "request_id": "bh-v4-s5", "client_session_id": "bh-v4-s5", "client_edit_revision": 0}).encode()
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
