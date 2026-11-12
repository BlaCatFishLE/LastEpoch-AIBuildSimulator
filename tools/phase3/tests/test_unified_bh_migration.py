"""S5 BH 迁移定向测试：D8 纯 DoT 手算、D9 多实例/充能、D12 legacy 对照、无旧计算器 import。"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CASE = ROOT / "examples/black-hole/case.definitions-v3.json"
SCENARIO_EAGER = ROOT / "examples/black-hole/inputs/scenario-eager-v3.json"
SCENARIO_PATIENT = ROOT / "examples/black-hole/inputs/scenario-patient-v3.json"
LEGACY_RESULT = (
    ROOT / "reports/black-hole/verify-baseline-black-hole-20261110T1245/result.json"
)
BH_RUN = ROOT / "reports/black-hole/dps-step4-bh-2"
SCRATCH = ROOT / ".scratch"

sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import (  # noqa: E402
    calculation_plan as planmod,
    calculation_route,
    dps_result,
    effect_binding,
    plan_executor,
    plan_handlers,
)
from le_calc.workflow import unified  # noqa: E402



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _case() -> dict:
    case = _load(CASE)
    case["_path"] = CASE.relative_to(ROOT).as_posix()
    return case


def _scenario_inputs() -> list[dict]:
    docs = {"eager": _load(SCENARIO_EAGER), "patient": _load(SCENARIO_PATIENT)}
    target_extras = {"monster_damage_reduction": 0.87}
    return [
        {
            "evaluation_id": key,
            "scenario": {
                "states": dict(doc["controls"]),
                "target": {
                    "monster_level": doc["monster_level"],
                    "resistance": doc["resistance"],
                    "high_health": False,
                    "boss": True,
                    "blinded": False,
                    "slowed": False,
                    **target_extras,
                },
            },
        }
        for key, doc in docs.items()
    ]


def _compile_bh():
    route, compiled = calculation_route.compile_route(root=ROOT, case=_case())
    if compiled.plan is None:
        raise AssertionError(f"BH v3 编译失败：{compiled.diagnostics[:3]}")
    return compiled.plan


def _synthetic_plan_v3(
    *,
    base_damage: float = 100.0,
    duration_s: float = 3.0,
    tick_interval_s: float = 1.0,
    horizon_s: float = 10.0,
    windows_s: list[float] | None = None,
    base_max_charges: int = 2,
    base_recharge_s: float = 2.0,
    cast_time_s: float = 1.0,
    ignite: dict | None = None,
    resistance_fire: float = 0.0,
    evaluation_order: tuple[str, str] = ("eager", "patient"),
    scenario_by_policy: dict[str, dict] | None = None,
) -> tuple[dict, dict]:
    """合成 plan-v3：1 个纯 DoT 组件 + 充能调度 + 唯一具名场景，便于手算。"""
    components = [
        {
            "component_id": "synthetic:dot",
            "kind": "dot",
            "damage_type": "Cold",
            "tags": ["Spell", "DoT"],
            "scope": "body",
            "crit_capable": False,
            "base_damage": base_damage,
            "added_damage_scaling": 0.0,
            "duration_s": duration_s,
            "tick_interval_s": tick_interval_s,
            "damage_at_start": True,
            "remainder_policy": "ceil_full_ticks",
        }
    ]
    effects: list[dict] = []
    sources: list[dict] = []
    if resistance_fire:
        effect = {
            "effect_id": "rule:synthetic:fire_res",
            "contribution_id": "rule:synthetic:fire_res",
            "sequence": 0,
            "label": "synthetic fire resistance",
            "category": "effect",
            "stage": "derived_pools",
            "handler_id": "profile_effect-v1",
            "handler_version": plan_handlers.VERSION,
            "source_kind": "derived",
            "source_label": "synthetic",
            "source_path": "synthetic",
            "stat_key": "resistance",
            "stat_name": "Fire Resistance",
            "operation": "added",
            "value": float(resistance_fire),
            "unit": "percent",
            "damage_tags": [],
            "target": "fire",
            "group": None,
            "value_source": "source",
            "status": "confirmed",
            "notes": "synthetic",
            "included_static": True,
            "condition_status_static": "always",
            "exclusion_reason": None,
            "condition_ref": None,
            "condition_code": None,
            "condition_code_static": False,
            "coverage_class": None,
            "coverage_status": "mapped",
            "required": True,
            "emits_contribution": True,
            "value_mode": "static",
            "value_params": {},
            "ledger_kind": None,
            "ledger": None,
            "rule_id": None,
            "condition_style": None,
            "owner": None,
            "source_facts": {},
            "raw": {"synthetic": True},
            "evidence": [],
            "source_refs": ["rule_source:synthetic"],
            "ownership_key": {
                "source": {"kind": "derived", "path": "synthetic"},
                "condition": None,
                "stat_key": "resistance",
                "operation": "added",
                "damage_tags": [],
                "target": "fire",
                "group": None,
            },
        }
        effects.append(effect)
        sources.append(
            {
                "source_id": "rule_source:synthetic",
                "kind": "rule_source",
                "identity": {"rule": "synthetic"},
                "semantics": {"stat_keys": ["resistance"], "binding_status": "mapped"},
                "binding_status": "mapped",
                "evidence": [],
            }
        )
    ignite_spec = {
        "ailment": "Ignite",
        "damage_type": "Fire",
        "base_damage": 10.0,
        "base_duration_s": 2.0,
        "scaling_tags": ["Fire", "DoT", "Ailment"],
        "duration_stat_key": "ailment_duration",
        "rolls_per_second": 1.0,
        "first_roll_at": "spawn",
        "chance_total": 0.0,
    }
    if ignite:
        ignite_spec.update(ignite)
    schedule = {
        "schema": "le-schedule-v3",
        "driver": "charge_dot_cycle",
        "horizon_s": horizon_s,
        "windows_s": list(windows_s or [horizon_s]),
        "cast": {
            "base_rate_per_s": 1.0 / cast_time_s,
            "animation_speed": 0.0,
            "speed_multiplier": 1.0,
        },
        "charges": {
            "base_max_charges": base_max_charges,
            "extra_max_charges": 0,
            "base_recharge_s": base_recharge_s,
            "flat_adjustment_s": 0.0,
            "cooldown_reduction": 0.0,
        },
        "action_policies": [
            {"policy_id": "eager", "label": "eager"},
            {"policy_id": "patient", "label": "patient"},
        ],
        "primary_policy": "eager",
        "ignite": ignite_spec,
        "target": {
            "monster_damage_reduction_control": "monster_damage_reduction",
            "monster_damage_reduction_default": 0.0,
        },
    }
    controls = [
        {"control_id": "target.monster_level", "namespace": "target", "key": "monster_level", "type": "integer", "default": 1, "min": 1, "max": 100},
        {"control_id": "target.resistance", "namespace": "target", "key": "resistance", "type": "number", "default": 0.0, "min": -100.0, "max": 75.0},
        {"control_id": "target.high_health", "namespace": "target", "key": "high_health", "type": "boolean", "default": False},
        {"control_id": "target.boss", "namespace": "target", "key": "boss", "type": "boolean", "default": True},
        {"control_id": "target.blinded", "namespace": "target", "key": "blinded", "type": "boolean", "default": False},
        {"control_id": "target.slowed", "namespace": "target", "key": "slowed", "type": "boolean", "default": False},
        {"control_id": "target.monster_damage_reduction", "namespace": "target", "key": "monster_damage_reduction", "type": "number", "default": 0.0, "min": 0.0, "max": 1.0},
    ]
    default_scenario = {
        "states": {},
        "target": {
            "monster_level": 1,
            "resistance": 0.0,
            "high_health": False,
            "boss": True,
            "blinded": False,
            "slowed": False,
            "monster_damage_reduction": 0.0,
        },
    }
    scenarios_by_policy = {
        policy: dict(
            (scenario_by_policy or {}).get(policy) or default_scenario
        )
        for policy in ("eager", "patient")
    }
    digest_by_policy = {
        policy: planmod.scenario_sha256(scenarios_by_policy[policy])
        for policy in scenarios_by_policy
    }
    scenario = scenarios_by_policy["eager"]
    fingerprint = plan_handlers.implementation_fingerprint()
    plan = {
        "schema": planmod.SCHEMA_V3,
        "subject": {"case_id": "synthetic-bh", "build_id": "synthetic", "revision_id": "synthetic-rev-1", "main_ability_id": "synthetic"},
        "compiler": {
            "version": "3.0.0",
            "origin": "definitions_v3",
            "implementation_fingerprint": fingerprint["compiler_files"],
            "fingerprint": fingerprint["compiler_fingerprint"],
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": [
            {"role": "compiler_source", "path": "tools/phase3/src/le_calc/plan_executor_v3.py", "sha256": planmod.sha256_file(ROOT / "tools/phase3/src/le_calc/plan_executor_v3.py")},
            {"role": "scenario", "path": "fixtures/synthetic-bh.json", "sha256": planmod.sha256_text("synthetic-bh")},
        ],
        "sources": sources,
        "facts": {},
        "parameters": {
            "skill": {
                "base_damage": {"Cold": base_damage},
                "added_damage_scaling": 0.0,
                "crit_chance": 0.0,
                "crit_multiplier": 0.0,
                "cooldown_s": base_recharge_s,
                "action_s": cast_time_s,
                "skill_name": "Synthetic DoT",
            },
            "crit": {},
            "ailments": [],
            "additional_chance": {},
            "damage_to_penetration": None,
            "cooldown_reduction": 0.0,
        },
        "controls": controls,
        "conditions": [],
        "components": components,
        "effects": effects,
        "evaluation": {
            "stage_order": list(plan_handlers.STAGE_ORDER),
            "effect_stage": {effect["effect_id"]: effect["stage"] for effect in effects},
            "dependencies": plan_handlers.canonical_dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": schedule,
        "diagnostics": [],
        "assumptions": [],
        "scope": {"skill": "Synthetic DoT", "ailment_labels": ["Ignite"], "limitations": []},
        "presentation": {"skill_name": "Synthetic DoT", "skill_names": {}, "character": None, "equipment": [], "controls": []},
        "evaluations": [
            {
                "evaluation_id": policy,
                "label": policy,
                "metrics": ["horizon_dps"],
                "primary": policy == "eager",
                "action_policy": policy,
                "scenario_path": f"fixtures/{policy}.json",
                "scenario_sha256": digest_by_policy[policy],
            }
            for policy in evaluation_order
        ],
    }
    return planmod.finalize_plan(plan, schema=planmod.SCHEMA_V3), scenarios_by_policy


def _execute(plan: dict, scenario: dict) -> dict:
    if set(scenario) == {"eager", "patient"} and all(
        isinstance(value, dict) and "target" in value for value in scenario.values()
    ):
        return plan_executor.execute_plan(
            plan,
            {
                "evaluations": [
                    {
                        "evaluation_id": str(item["evaluation_id"]),
                        "scenario": copy.deepcopy(scenario[str(item["action_policy"])]),
                    }
                    for item in plan.get("evaluations") or []
                ]
            },
        )
    return plan_executor.execute_plan(plan, copy.deepcopy(scenario))


class BhD8HandOracleTest(unittest.TestCase):
    def test_pure_dot_independent_application_clock_and_expiry(self):
        plan, scenario = _synthetic_plan_v3(
            base_damage=100.0,
            duration_s=3.0,
            tick_interval_s=1.0,
            horizon_s=4.0,
            windows_s=[2.0, 4.0],
            base_max_charges=1,
            base_recharge_s=10.0,
            ignite={"chance_total": 0.01},
            resistance_fire=1.0,
        )
        result = _execute(plan, scenario)
        single = result["single_cast"]
        totals = single["totals"]
        # 本体：cast 0 → spawn 1s；逐跳在 1/2/3s 各 100；duration 3s 到期 → 观测终点 4s
        self.assertAlmostEqual(totals["body_total"], 300.0, delta=1e-9)
        self.assertAlmostEqual(totals["observation_end_s"], 5.0, delta=1e-9)
        # Ignite：每 1s 一次判定、每次 1 层、单层 10 × 2s = 20；判定于 spawn+0/1/2
        ignition = single["ignition"]
        self.assertEqual(len(ignition["cohorts"]), 3)
        self.assertAlmostEqual(ignition["cohorts"][0]["damage_total"], 10.0, delta=1e-9)
        self.assertAlmostEqual(ignition["cohorts"][0]["end_time_s"], 3.0, delta=1e-9)
        self.assertAlmostEqual(ignition["cohorts"][2]["end_time_s"], 5.0, delta=1e-9)
        self.assertAlmostEqual(totals["ignite_total"], 30.0, delta=1e-9)
        self.assertAlmostEqual(totals["ignite_damage_before_black_hole_end"], 25.0, delta=1e-9)
        self.assertAlmostEqual(totals["ignite_tail_damage"], 5.0, delta=1e-9)
        self.assertAlmostEqual(totals["single_cast_total"], 330.0, delta=1e-9)
        # 窗口：2s 内本体跳 1/2s → 200；Ignite 第一批 20 全在窗口内 → 220
        window2 = next(item for item in result["loops"][0]["windows"] if item["window_s"] == 2.0)
        self.assertAlmostEqual(window2["body_damage"], 200.0, delta=1e-9)
        self.assertAlmostEqual(window2["ignite_damage"], 5.0, delta=1e-9)
        self.assertAlmostEqual(window2["dps"], 102.5, delta=1e-9)

    def test_plan_v3_rejects_unsupported_semantics(self):
        cases: dict[str, dict] = {}
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["schedule"]["driver"] = "fixed_repeat"
        cases["driver"] = bad
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["schedule"]["ignite"]["first_roll_at"] = "cast_start"
        cases["ignite-clock"] = bad
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["components"][0]["remainder_policy"] = "merge_remainder"
        cases["remainder-policy"] = bad
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["components"][0]["damage_at_start"] = False
        cases["damage-at-start-false"] = bad
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["components"][0]["kind"] = "hit"
        cases["hit-component"] = bad
        plan, _scenario = _synthetic_plan_v3()
        bad = copy.deepcopy(plan)
        bad["schedule"]["action_policies"].append({"policy_id": "eager"})
        cases["duplicate-policy"] = bad
        for name, candidate in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(plan_executor.PlanValidationError):
                    plan_executor.execute_plan(candidate, {"states": {}, "target": {"monster_level": 1, "resistance": 0.0, "high_health": False, "boss": True, "blinded": False, "slowed": False, "monster_damage_reduction": 0.0}})


class BhD9CapacitySchedulingTest(unittest.TestCase):
    def test_charge_ledger_eager_vs_patient_and_max_concurrent(self):
        plan, scenario = _synthetic_plan_v3(
            base_damage=100.0, duration_s=3.0, tick_interval_s=1.0,
            horizon_s=10.0, windows_s=[10.0],
            base_max_charges=2, base_recharge_s=2.0, cast_time_s=1.0,
        )
        result = _execute(plan, scenario)
        loops = {item["policy_id"]: item for item in result["loops"]}
        eager_starts = [cast["cast_start_s"] for cast in loops["eager"]["casts"]]
        patient_starts = [cast["cast_start_s"] for cast in loops["patient"]["casts"]]
        # eager：起手 2 层充能连发 0/1/2s，之后每 2s 充能 → 4/6/8s；horizon=10 边界正点施放一次
        self.assertEqual(eager_starts, [0.0, 1.0, 2.0, 4.0, 6.0, 8.0, 10.0])
        # patient：必须等旧黑洞消失（spawn+3 = start+1+3）后才施放 0/4/8s
        self.assertEqual(patient_starts, [0.0, 4.0, 8.0])
        self.assertEqual(loops["eager"]["cast_count"], 7)
        self.assertEqual(loops["patient"]["cast_count"], 3)
        self.assertEqual(loops["eager"]["max_concurrent"], 3)
        self.assertEqual(loops["patient"]["max_concurrent"], 1)
        self.assertAlmostEqual(loops["eager"]["totals"]["body_damage"], 17 * 100.0, delta=1e-9)
        self.assertAlmostEqual(loops["patient"]["totals"]["body_damage"], 8 * 100.0, delta=1e-9)
        eager_window = loops["eager"]["windows"][-1]
        patient_window = loops["patient"]["windows"][-1]
        # 窗口内逐跳计数：eager spawn 1/2/3/5/7/9；patient spawn 1/5/9
        self.assertAlmostEqual(eager_window["body_damage"], 17 * 100.0, delta=1e-9)
        self.assertAlmostEqual(patient_window["body_damage"], 8 * 100.0, delta=1e-9)
        # 充能曲线：起手 2 → 施放递减 → regen 递增
        events = loops["eager"]["charge_events"]
        self.assertEqual(events[0], {"t": 0.0, "charges": 2, "kind": "initial"})
        self.assertEqual(events[1]["charges"], 1)
        self.assertEqual(events[2]["charges"], 0)
        self.assertTrue(any(event["kind"] == "regen" and event["charges"] == 1 for event in events))


class BhP1ReviewFixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = _compile_bh()
        cls.result = _execute(cls.plan, {"evaluations": _scenario_inputs()})

    def test_component_application_ledger_is_real_and_recomputable(self):
        result = self.result
        contributions = result["contributions"]
        applications = result["applications"]
        units = result["application_units"]
        self.assertEqual(len(contributions), 203)
        self.assertEqual(len(applications), len(contributions) * 3)
        self.assertEqual(
            [unit["component_id"] for unit in units],
            ["bh-binary-fire", "bh-binary-cold", "bh-ignite"],
        )
        summary = result["contribution_application_summary"]
        self.assertEqual(summary, dps_result.application_summary(applications))
        by_id: dict[str, list[dict]] = {}
        for record in applications:
            by_id.setdefault(record["contribution_id"], []).append(record)
            self.assertIn(record["status"], {
                "applied", "not_applicable", "condition_not_satisfied", "unresolved",
            })
            if record["status"] != "applied":
                self.assertTrue(record.get("reason"), record)
        self.assertEqual(set(by_id), {row["contribution_id"] for row in contributions})
        for contribution in contributions:
            records = by_id[contribution["contribution_id"]]
            self.assertEqual(
                {record["component_id"] for record in records},
                {"bh-binary-fire", "bh-binary-cold", "bh-ignite"},
            )
            self.assertEqual(contribution["application"], summary[contribution["contribution_id"]])
        component_refs = {component["component_id"] for component in result["components"]}
        component_refs |= {
            item["component_id"] for item in result["ailments"] if item.get("component_id")
        }
        self.assertTrue(
            all(record["component_id"] in component_refs for record in applications)
        )
        self.assertEqual(result["coverage"]["gap_count"], 2)
        self.assertIn("node_bindings", result["effect_ledger"])
        self.assertIn("effects", result["effect_ledger"])
        # 每个单元至少有一条 applied 且存在带原因的排除记录
        for unit in units:
            rows = [record for record in applications if record["component_id"] == unit["component_id"]]
            self.assertTrue(any(record["status"] == "applied" for record in rows), unit["component_id"])
            self.assertTrue(any(record["status"] != "applied" for record in rows), unit["component_id"])

    def test_top_level_projection_uses_primary_not_first_evaluation(self):
        plan, scenarios = _synthetic_plan_v3(
            evaluation_order=("patient", "eager"),
            scenario_by_policy={
                "eager": {
                    "states": {},
                    "target": {
                        "monster_level": 1, "resistance": 0.0, "high_health": False,
                        "boss": True, "blinded": False, "slowed": False,
                        "monster_damage_reduction": 0.0,
                    },
                },
                "patient": {
                    "states": {},
                    "target": {
                        "monster_level": 1, "resistance": 20.0, "high_health": False,
                        "boss": True, "blinded": False, "slowed": False,
                        "monster_damage_reduction": 0.0,
                    },
                },
            },
        )
        self.assertEqual(
            [item["evaluation_id"] for item in plan["evaluations"]], ["patient", "eager"]
        )
        self.assertEqual(
            [item["evaluation_id"] for item in plan["evaluations"] if item["primary"]], ["eager"]
        )
        result = _execute(plan, scenarios)
        self.assertEqual(result["primary_evaluation"], "eager")
        self.assertEqual(result["evaluations"][0]["evaluation_id"], "eager")
        self.assertEqual([loop["policy_id"] for loop in result["loops"]], ["eager", "patient"])
        # 顶层 scenario/target/value 必须来自 primary（eager resistance=0），而不是排第一的 patient(20)
        self.assertEqual(result["scenario"]["target"]["resistance"], 0.0)
        self.assertEqual(result["target"]["resistance"], 0.0)
        self.assertEqual(result["primary_scenario_sha256"], plan["evaluations"][1]["scenario_sha256"])
        self.assertAlmostEqual(result["components"][0]["target"]["resistance"], 0.0, delta=1e-12)
        self.assertAlmostEqual(result["components"][0]["per_tick_damage"], 100.0, delta=1e-9)
        self.assertAlmostEqual(result["single_cast"]["totals"]["body_total"], 300.0, delta=1e-9)
        self.assertAlmostEqual(
            result["cooldown"]["effective_recharge_s"],
            result["loops"][0]["resource"]["effective_recharge_s"],
            delta=1e-12,
        )
        # 聚合场景身份仍按 evaluation_id 排序（与签收 FR 规则一致），与评估顺序无关
        expected_aggregate = planmod.aggregate_scenario_sha256(
            sorted(
                [
                    {
                        "evaluation_id": item["evaluation_id"],
                        "scenario_sha256": item["scenario_sha256"],
                    }
                    for item in plan["evaluations"]
                ],
                key=lambda item: item["evaluation_id"],
            )
        )
        self.assertEqual(result["scenario_sha256"], expected_aggregate)
        self.assertEqual(
            [item["evaluation_id"] for item in result["evaluation_scenarios"]],
            ["eager", "patient"],
        )

    def test_result_gate_recomputes_loops_windows_tail_and_evaluation_summary(self):
        plan = self.plan
        base = self.result
        self.assertTrue(
            plan_executor_v3_validate(plan, copy.deepcopy(base))["passed"]
        )
        tampers = {
            "horizon_dps": lambda r: r["loops"][0]["totals"].__setitem__("horizon_dps", 1.0),
            "horizon_damage": lambda r: r["loops"][0]["totals"].__setitem__("horizon_damage", 1.0),
            "tail": lambda r: r["loops"][0]["windows"][0].__setitem__(
                "unsettled_tail_damage",
                r["loops"][0]["windows"][0]["unsettled_tail_damage"] + 1.0,
            ),
            "cast_count": lambda r: r["loops"][0].__setitem__(
                "cast_count", r["loops"][0]["cast_count"] + 1
            ),
            "max_concurrent": lambda r: r["loops"][0].__setitem__(
                "max_concurrent", r["loops"][0]["max_concurrent"] + 1
            ),
            "evaluation_dps": lambda r: r["evaluations"][0].__setitem__("horizon_dps", 1.0),
            "single_tail": lambda r: r["single_cast"]["totals"].__setitem__(
                "ignite_tail_damage",
                r["single_cast"]["totals"]["ignite_tail_damage"] + 1.0,
            ),
            "summary": lambda r: r["contribution_application_summary"][
                "base:attribute:strength"
            ].__setitem__("statuses", {"applied": 3}),
        }
        expected_ids = {
            "horizon_dps": "result.loop_totals",
            "horizon_damage": "result.loop_totals",
            "tail": "result.loops_windows",
            "cast_count": "result.loop_totals",
            "max_concurrent": "result.max_concurrent",
            "evaluation_dps": "result.evaluation_summary",
            "single_tail": "result.single_cast_tail",
            "summary": "result.applications_ledger",
        }
        for name, mutate in tampers.items():
            tampered = copy.deepcopy(base)
            mutate(tampered)
            validation = plan_executor_v3_validate(plan, tampered)
            failed = {item["id"] for item in validation["checks"] if item["status"] != "PASS"}
            self.assertFalse(validation["passed"], name)
            self.assertIn(expected_ids[name], failed, f"{name}: {failed}")

    def test_validate_stage_readonly_rejects_tampered_loop_totals(self):
        import shutil

        work = SCRATCH / "bh-tamper-run"
        shutil.rmtree(work, ignore_errors=True)
        shutil.copytree(BH_RUN, work)
        result = _load(work / "result.json")
        result["loops"][0]["totals"]["horizon_dps"] = 1.0
        _write(work / "result.json", result)
        validation = unified.validate_stage(_case(), root=ROOT, run_dir=work, write=False)
        self.assertFalse(validation["passed"])
        failed = {
            item["id"] for item in validation["payload"]["checks"] if item["status"] != "PASS"
        }
        self.assertIn("result.loop_totals", failed)
        shutil.rmtree(work, ignore_errors=True)


def plan_executor_v3_validate(plan: dict, result: dict) -> dict:
    from le_calc import plan_executor_v3

    return plan_executor_v3.validate_result_v5(plan, result)


class BhD12LegacyMappingTest(unittest.TestCase):
    def test_real_bh_v3_matches_legacy_semantics_and_numbers(self):
        legacy = _load(LEGACY_RESULT)
        plan = _compile_bh()
        result = _execute(plan, {"evaluations": _scenario_inputs()})
        legacy_single = legacy["single_cast"]
        single = result["single_cast"]
        for key in (
            "single_cast_total",
            "body_total",
            "ignite_total",
            "ignite_damage_before_black_hole_end",
            "ignite_tail_damage",
            "window_damage_until_black_hole_end",
            "observation_end_s",
            "last_ailment_expiry_s",
            "expected_total_stacks_applied",
        ):
            self.assertAlmostEqual(single["totals"][key], legacy_single["totals"][key], delta=1e-6, msg=key)
        legacy_components = {
            item["component_id"]: item for item in legacy_single["body"]["components"]
        }
        for component in single["components"]:
            old = legacy_components[component["component_id"]]
            self.assertAlmostEqual(component["per_tick_damage"], old["per_tick_damage"], delta=1e-6)
            self.assertAlmostEqual(component["damage_total"], old["damage_total"], delta=1e-6)
            self.assertAlmostEqual(component["increased_total"], old["increased"]["total"], delta=1e-6)
            self.assertAlmostEqual(component["more_product"], old["more_product"], delta=1e-9)
            self.assertAlmostEqual(component["penetration_total"], old["penetration"]["total"], delta=1e-9)
            self.assertAlmostEqual(
                component["target"]["multiplier"], old["target"]["multiplier"], delta=1e-9
            )
            self.assertEqual(component["tick_count"], old["tick_count"])
        old_ignite = legacy_single["ignite"]
        new_ignite = result["ignite"]
        self.assertAlmostEqual(new_ignite["duration_s"], old_ignite["effective_duration_s"], delta=1e-9)
        self.assertAlmostEqual(new_ignite["per_stack_damage"], old_ignite["per_stack_total"], delta=1e-6)
        self.assertAlmostEqual(new_ignite["more_product"], old_ignite["more_product"], delta=1e-9)
        self.assertAlmostEqual(new_ignite["penetration_total"], old_ignite["penetration"]["total"], delta=1e-9)
        self.assertAlmostEqual(
            new_ignite["application"]["uncapped_fire_resistance_percent"],
            old_ignite["application"]["uncapped_fire_resistance"],
            delta=1e-9,
        )
        self.assertAlmostEqual(
            new_ignite["application"]["stacks_per_roll"],
            old_ignite["application"]["expected_stacks_per_roll"],
            delta=1e-9,
        )
        self.assertEqual(new_ignite["application"]["roll_count"], old_ignite["application"]["roll_count"])
        legacy_loops = {item["policy_id"]: item for item in legacy["loops"]}
        new_loops = {item["policy_id"]: item for item in result["loops"]}
        for policy in ("eager", "patient"):
            old = legacy_loops[policy]
            new = new_loops[policy]
            self.assertEqual(new["cast_count"], old["totals"]["cast_count"], msg=policy)
            self.assertEqual(new["max_concurrent"], old["totals"]["max_concurrent_black_holes"], msg=policy)
            self.assertAlmostEqual(new["totals"]["horizon_dps"], old["totals"]["horizon_dps"], delta=1e-6, msg=policy)
            self.assertAlmostEqual(new["totals"]["horizon_damage"], old["totals"]["horizon_damage"], delta=1e-5, msg=policy)
            for window, old_window in zip(new["windows"], old["windows"]):
                for key in ("body_damage", "ignite_damage", "total_damage", "dps", "unsettled_tail_damage"):
                    self.assertAlmostEqual(window[key], old_window[key], delta=1e-5, msg=f"{policy}.{window['window_s']}.{key}")
                self.assertEqual(window["cast_count"], old_window["cast_count"])
        for new_key, old_key in (
            ("max_charges", "max_charges"),
            ("effective_recharge_s", "effective_recharge_s"),
            ("cooldown_reduction", "cdr_total"),
            ("icr_total", "icr_total"),
            ("flat_adjustment_s", "flat_total_s"),
        ):
            self.assertAlmostEqual(result["cooldown"][new_key], legacy["cooldown"][old_key], delta=1e-9, msg=new_key)
        self.assertAlmostEqual(result["cast"]["cast_time_s"], legacy["cast"]["cast_time_s"], delta=1e-12)

    def test_production_path_never_imports_legacy_bh_calculators(self):
        script = SCRATCH / "bh_no_legacy_import.py"
        payload = _case()
        scenario_inputs = _scenario_inputs()
        script.write_text(
            "\n".join(
                [
                    "import sys, importlib.abc",
                    "BLOCKED = {'le_calc.damage','le_calc.ignite','le_calc.cooldown','le_calc.simulate'}",
                    "class Block(importlib.abc.MetaPathFinder):",
                    "    def find_spec(self, fullname, path=None, target=None):",
                    "        if fullname in BLOCKED:",
                    "            raise RuntimeError('legacy BH calculator imported: ' + fullname)",
                    "        return None",
                    "sys.meta_path.insert(0, Block())",
                    f"sys.path.insert(0, {str(ROOT / 'tools/phase3/src')!r})",
                    "from le_calc import calculation_route, plan_executor",
                    f"case = {json.dumps(payload, ensure_ascii=False)!r}",
                    "import json as _json",
                    "case = _json.loads(case)",
                    f"scenarios = _json.loads({json.dumps(scenario_inputs)!r})",
                    f"route, compiled = calculation_route.compile_route(root=__import__('pathlib').Path({str(ROOT)!r}), case=case)",
                    "assert compiled.plan is not None, compiled.diagnostics",
                    "assert not any(name in sys.modules for name in BLOCKED), sys.modules.keys() & BLOCKED",
                    "result = plan_executor.execute_plan(compiled.plan, {'evaluations': scenarios})",
                    "assert result['schema'] == 'le-dps-result-v5'",
                    "assert not any(name in sys.modules for name in BLOCKED), sys.modules.keys() & BLOCKED",
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


class BhFormalRunTest(unittest.TestCase):
    def test_formal_run_artifacts_and_bh_binding(self):
        if not (BH_RUN / "result.json").is_file():
            self.skipTest("缺少 reports/black-hole/dps-step4-bh-1 正式 run")
        manifest = _load(BH_RUN / "run-manifest.json")
        self.assertEqual(len(manifest["files"]), 12)
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((BH_RUN / name).read_bytes()).hexdigest(), digest, name)
        validation = _load(BH_RUN / "validation-result.json")
        self.assertTrue(validation["passed"])
        required = set(unified.REQUIRED_VALIDATION_CHECKS_V5)
        self.assertEqual(set(validation["required_checks"]), required)
        checks = {item["id"]: item for item in validation["checks"]}
        self.assertTrue(required <= set(checks))
        self.assertTrue(all(checks[item]["status"] == "PASS" for item in required))
        plan = _load(BH_RUN / "calculation-plan.json")
        self.assertEqual(plan["schema"], planmod.SCHEMA_V3)
        self.assertEqual(plan["plan_sha256"], manifest["plan_sha256"])
        result = _load(BH_RUN / "result.json")
        self.assertEqual(result["schema"], "le-dps-result-v5")
        scenario = _load(BH_RUN / "scenario.json")
        primary_sha = planmod.scenario_sha256(
            {"states": scenario["states"], "target": scenario["target"]}
        )
        expected_aggregate = planmod.aggregate_scenario_sha256(
            [
                {"evaluation_id": item["evaluation_id"], "scenario_sha256": item["scenario_sha256"]}
                for item in result["evaluation_scenarios"]
            ]
        )
        self.assertEqual(result["scenario_sha256"], expected_aggregate)
        self.assertEqual(result["primary_scenario_sha256"], primary_sha)
        self.assertEqual(
            [item["evaluation_id"] for item in result["evaluation_scenarios"]],
            ["eager", "patient"],
        )
        loops = {item["policy_id"]: item for item in result["loops"]}
        self.assertEqual(set(loops), {"eager", "patient"})
        self.assertEqual(loops["eager"]["cast_count"], 35)
        self.assertEqual(loops["patient"]["cast_count"], 16)
        self.assertEqual(loops["eager"]["max_concurrent"], 3)
        self.assertEqual(loops["patient"]["max_concurrent"], 1)
        self.assertAlmostEqual(loops["eager"]["totals"]["horizon_dps"], 2919759.11554597, delta=1e-4)
        self.assertAlmostEqual(loops["patient"]["totals"]["horizon_dps"], 1337984.9192314548, delta=1e-4)
        self.assertAlmostEqual(result["single_cast"]["totals"]["single_cast_total"], 10662227.469716422, delta=1e-4)
        self.assertEqual(len(result["components"]), 2)
        self.assertEqual(len(result["contributions"]), 203)
        self.assertEqual(len(result["applications"]), 609)
        self.assertEqual(
            [unit["component_id"] for unit in result["application_units"]],
            ["bh-binary-fire", "bh-binary-cold", "bh-ignite"],
        )
        self.assertEqual(
            result["contribution_application_summary"],
            dps_result.application_summary(result["applications"]),
        )
        self.assertEqual(result["loops"][0]["policy_id"], "eager")
        self.assertEqual(result["evaluations"][0]["evaluation_id"], "eager")
        self.assertTrue(result["loops"][0]["primary"])
        self.assertIn("node_bindings", result["effect_ledger"])
        self.assertAlmostEqual(result["components"][0]["per_tick_damage"], 35660.298285715784, delta=1e-6)
        self.assertAlmostEqual(result["components"][1]["per_tick_damage"], 12020.44207104, delta=1e-6)
        binding = _load(BH_RUN / "report-binding.json")
        self.assertEqual(binding["scenario_binding"]["scenario_sha256"], result["scenario_sha256"])
        html = (BH_RUN / "dps-report.html").read_text(encoding="utf-8")
        for text in ("充能调度对比", "Ignite（独立施加时钟）", "本体 DoT", "最大并发黑洞"):
            self.assertIn(text, html)
        # 已签收 VC run 只读：其 manifest/result 未被本轮改写
        vc_manifest = _load(ROOT / "reports/void-cleave-20261110/dps-step4-vc-8/run-manifest.json")
        self.assertEqual(
            vc_manifest["plan_sha256"],
            "46ba62b4f39665d4d4f966f3d90ac90f0475a27b3c4e74d8f400d3206626f418",
        )


if __name__ == "__main__":
    unittest.main()
