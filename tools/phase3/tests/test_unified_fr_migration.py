"""第四步 S4：FR definitions_v3 迁移、plan-v2/result-v4、D7 合成多命中与 D12 legacy 对照。"""
from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools/phase3/src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from le_calc import calculation_plan as planmod
from le_calc import calculation_route, definitions_v3, plan_executor
from le_calc.workflow import unified

CASE = ROOT / "examples/flame-reave/case.definitions-v3.json"
TEMPLATE = ROOT / "knowledge/definitions/fr-flame-reave-v3.json"
SINGLE_SCENARIO = ROOT / "examples/flame-reave/inputs/scenario-single-hit-v3.json"
DPS_SCENARIO = ROOT / "examples/flame-reave/inputs/scenario-dps-v3.json"
LEGACY_RUN = ROOT / "reports/flame-reave/verify-baseline-flame-reave-20261110T1245"
FR_RUN = ROOT / "reports/flame-reave/dps-step4-fr-2"
SCRATCH = ROOT / ".scratch/unified-dps-step4/fr-tests"

LEGACY_SINGLE_HIT = 51831.5589095769
LEGACY_DPS = 121329.04081416794
LEGACY_PER_CAST = 171692.0388879735
LEGACY_CASTS_PER_SECOND = 0.7066666666666667
LEGACY_HITS_PER_SECOND = 2.8266666666666667
LEGACY_EFFECTIVE_HITS = 2.5
LEGACY_POLICY = "cooldown_limited"
LEGACY_ROTATION = 1.325
LEGACY_ALT_DPS = 696427.4028630381



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, payload) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _case() -> dict:
    case = _load(CASE)
    case["_path"] = CASE.relative_to(ROOT).as_posix()
    return case


def _compile(case: dict):
    route = calculation_route.resolve_calculation_route(case, root=ROOT)
    _, compiled = calculation_route.compile_route(root=ROOT, case=case, route=route)
    return route, compiled


def _evaluation_scenarios() -> list[dict]:
    entries = []
    for evaluation_id, path in (("single_hit", SINGLE_SCENARIO), ("dps", DPS_SCENARIO)):
        document = _load(path)
        states = document["controls"]
        from le_calc import plan_compiler  # noqa: PLC0415

        target = dict(plan_compiler.DEFAULT_TARGET)
        target["monster_level"] = document["monster_level"]
        target["resistance"] = document["resistance"]
        entries.append(
            {
                "evaluation_id": evaluation_id,
                "scenario": {"states": states, "target": target},
                "scenario_path": path.relative_to(ROOT).as_posix(),
            }
        )
    return entries


def _execute(plan: dict) -> dict:
    return plan_executor.execute_plan(plan, {"evaluations": _evaluation_scenarios()})


def _evaluation(result: dict, evaluation_id: str) -> dict:
    return next(
        item for item in result["evaluations"] if item["evaluation_id"] == evaluation_id
    )


def _synthetic_plan_v2(
    *,
    hits_per_cast: int = 2,
    damage_weights: list[float] | None = None,
    base_damage: float = 100.0,
    casts_per_second: float = 2.0,
    rotation: float = 1.0,
) -> tuple[dict, dict]:
    """独立合成多命中计划（纯手算 oracle 用；不代表任何游戏技能）。"""
    weights = list(damage_weights if damage_weights is not None else [1.0] * hits_per_cast)
    from le_calc import plan_compiler, plan_handlers  # noqa: PLC0415

    target = dict(plan_compiler.DEFAULT_TARGET)
    target["monster_level"] = 1
    target["resistance"] = 0
    scenario = {"states": {}, "target": target}
    digest = planmod.scenario_sha256(scenario)
    cooldown = 1.0 / casts_per_second
    source_sha = planmod.sha256_file(ROOT / "tools/phase3/src/le_calc/plan_executor_v2.py")
    fingerprint = plan_handlers.implementation_fingerprint()
    plan = {
        "subject": {
            "case_id": "synthetic-multi-hit",
            "build_id": "synthetic",
            "revision_id": "synthetic-rev-1",
            "main_ability_id": "synthetic",
        },
        "compiler": {
            "version": "2.0.0",
            "origin": "definitions_v3",
            "implementation_fingerprint": {
                "tools/phase3/src/le_calc/plan_executor_v2.py": source_sha
            },
            "fingerprint": planmod.sha256_text("synthetic-multi-hit"),
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": [
            {
                "role": "compiler_source",
                "path": "tools/phase3/src/le_calc/plan_executor_v2.py",
                "sha256": source_sha,
            },
            {
                "role": "scenario",
                "path": "fixtures/synthetic-multi-hit-single.json",
                "sha256": planmod.sha256_text("synthetic-single"),
            },
            {
                "role": "scenario",
                "path": "fixtures/synthetic-multi-hit-dps.json",
                "sha256": planmod.sha256_text("synthetic-dps"),
            },
        ],
        "sources": [],
        "facts": {},
        "parameters": {
            "skill": {
                "base_damage": {"Fire": base_damage},
                "added_damage_scaling": 0.0,
                "crit_chance": 0.0,
                "crit_multiplier": 2.0,
                "cooldown_s": cooldown,
                "action_s": 1.0,
                "base_attack_rate": 2.0,
                "skill_name": "Synthetic Multi Hit",
            },
            "crit": {},
            "ailments": [],
            "additional_chance": {},
            "damage_to_penetration": None,
            "cooldown_reduction": 0.0,
            "attribute_ratios": {},
            "attribute_conversion": None,
        },
        "controls": [
            {
                "control_id": "target.monster_level",
                "namespace": "target",
                "key": "monster_level",
                "type": "integer",
                "default": 100,
                "min": 1,
                "max": 100,
            },
            {
                "control_id": "target.resistance",
                "namespace": "target",
                "key": "resistance",
                "type": "number",
                "default": 0,
                "min": -100,
                "max": 75,
            },
            {
                "control_id": "target.high_health",
                "namespace": "target",
                "key": "high_health",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.boss",
                "namespace": "target",
                "key": "boss",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.blinded",
                "namespace": "target",
                "key": "blinded",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.slowed",
                "namespace": "target",
                "key": "slowed",
                "type": "boolean",
                "default": False,
            },
        ],
        "conditions": [],
        "components": [
            {
                "component_id": "synthetic:fire",
                "kind": "hit",
                "damage_type": "Fire",
                "tags": ["Fire"],
                "crit_capable": False,
                "adaptive_damage_type": "Fire",
                "hits_per_cast": hits_per_cast,
                "damage_weights": weights,
            }
        ],
        "effects": [],
        "evaluation": {
            "stage_order": list(plan_handlers_stage_order()),
            "effect_stage": {},
            "dependencies": _dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": {
            "schema": "le-schedule-v2",
            "driver": "steady_state",
            "first_cast_s": 0.0,
            "windows_s": [5],
            "window_semantics": "expected_casts",
            "policies": [
                {
                    "policy_id": "cooldown_limited",
                    "binding_constraint": "cooldown",
                    "interval_policy": "max(action_time, cooldown)",
                },
                {
                    "policy_id": "attack_speed_limited",
                    "binding_constraint": "attack_speed",
                    "interval_policy": "action_time",
                },
            ],
            "primary_policy": "cooldown_limited",
            "rotation": {
                "kind": "fixed_more_factor",
                "factor": rotation,
                "applies_to": ["direct_hit_dps"],
                "status": "confirmed",
                "label": "synthetic rotation",
            },
            "every_nth_hit": None,
        },
        "evaluations": [
            {
                "evaluation_id": "single_hit",
                "label": "synthetic single hit",
                "metrics": ["single_hit"],
                "primary": False,
                "scenario_path": "fixtures/synthetic-multi-hit-single.json",
                "scenario_sha256": digest,
            },
            {
                "evaluation_id": "dps",
                "label": "synthetic dps",
                "metrics": ["windows"],
                "primary": True,
                "scenario_path": "fixtures/synthetic-multi-hit-dps.json",
                "scenario_sha256": digest,
            },
        ],
        "diagnostics": [],
        "assumptions": [],
        "scope": {"skill": "Synthetic Multi Hit", "ailment_labels": [], "limitations": []},
        "presentation": {
            "skill_name": "Synthetic Multi Hit",
            "skill_names": {},
            "character": None,
            "equipment": [],
            "controls": [],
        },
    }
    return planmod.finalize_plan(plan, schema=planmod.SCHEMA_V2), scenario


def plan_handlers_stage_order() -> tuple[str, ...]:
    from le_calc import plan_handlers  # noqa: PLC0415

    return plan_handlers.STAGE_ORDER


def _dependencies() -> list[dict[str, str]]:
    from le_calc import plan_handlers  # noqa: PLC0415

    return plan_handlers.canonical_dependencies()


def _multi_component_plan() -> tuple[dict, dict]:
    """两个命中组件（Fire/Cold）+ 各自增伤行：验证应用记录不串组件。"""
    from le_calc import plan_compiler, plan_handlers  # noqa: PLC0415

    target = dict(plan_compiler.DEFAULT_TARGET)
    target["monster_level"] = 1
    target["resistance"] = 0
    scenario = {"states": {}, "target": target}
    digest = planmod.scenario_sha256(scenario)
    source_sha = planmod.sha256_file(ROOT / "tools/phase3/src/le_calc/plan_executor_v2.py")
    fingerprint = plan_handlers.implementation_fingerprint()

    def effect(effect_id: str, tag: str) -> dict:
        source_id = f"base_character:synthetic:{tag.lower()}"
        return {
            "effect_id": effect_id,
            "sequence": 0,
            "label": effect_id,
            "category": "base",
            "stage": "baseline",
            "handler_id": "base_contribution-v1",
            "handler_version": plan_handlers.VERSION,
            "source_kind": "base_character",
            "source_label": effect_id,
            "source_path": "synthetic",
            "stat_key": "damage",
            "stat_name": "damage",
            "operation": "increased",
            "value": 0.1,
            "unit": "percent",
            "damage_tags": [tag],
            "target": None,
            "group": effect_id,
            "value_source": "source",
            "status": "confirmed",
            "notes": None,
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
            "raw": {"synthetic": effect_id},
            "evidence": [],
            "source_refs": [source_id],
            "ownership_key": {
                "source": {"kind": "base_character", "path": "synthetic"},
                "condition": None,
                "stat_key": "damage",
                "operation": "increased",
                "damage_tags": [tag],
                "target": None,
                "group": effect_id,
            },
            "support_status": "mapped",
        }

    plan = {
        "subject": {
            "case_id": "synthetic-multi-component",
            "build_id": "synthetic",
            "revision_id": "synthetic-rev-1",
            "main_ability_id": "synthetic",
        },
        "compiler": {
            "version": "2.0.0",
            "origin": "definitions_v3",
            "implementation_fingerprint": {
                "tools/phase3/src/le_calc/plan_executor_v2.py": source_sha
            },
            "fingerprint": planmod.sha256_text("synthetic-multi-component"),
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": [
            {
                "role": "compiler_source",
                "path": "tools/phase3/src/le_calc/plan_executor_v2.py",
                "sha256": source_sha,
            },
            {
                "role": "scenario",
                "path": "fixtures/multi-component-single.json",
                "sha256": planmod.sha256_text("synthetic-single"),
            },
            {
                "role": "scenario",
                "path": "fixtures/multi-component-dps.json",
                "sha256": planmod.sha256_text("synthetic-dps"),
            },
        ],
        "sources": [
            {
                "source_id": "base_character:synthetic:fire",
                "kind": "base_character",
                "identity": {"source_path": "synthetic"},
                "semantics": {"stat_keys": ["damage"], "binding_status": "applied"},
                "binding_status": "applied",
                "evidence": [],
            },
            {
                "source_id": "base_character:synthetic:cold",
                "kind": "base_character",
                "identity": {"source_path": "synthetic"},
                "semantics": {"stat_keys": ["damage"], "binding_status": "applied"},
                "binding_status": "applied",
                "evidence": [],
            },
        ],
        "facts": {},
        "parameters": {
            "skill": {
                "base_damage": {"Fire": 100.0, "Cold": 50.0},
                "added_damage_scaling": 0.0,
                "crit_chance": 0.0,
                "crit_multiplier": 2.0,
                "cooldown_s": 0.5,
                "action_s": 1.0,
                "base_attack_rate": 2.0,
                "skill_name": "Synthetic Multi Component",
            },
            "crit": {},
            "ailments": [],
            "additional_chance": {},
            "damage_to_penetration": None,
            "cooldown_reduction": 0.0,
            "attribute_ratios": {},
            "attribute_conversion": None,
        },
        "controls": [
            {
                "control_id": "target.monster_level",
                "namespace": "target",
                "key": "monster_level",
                "type": "integer",
                "default": 1,
                "min": 1,
                "max": 100,
            },
            {
                "control_id": "target.resistance",
                "namespace": "target",
                "key": "resistance",
                "type": "number",
                "default": 0,
                "min": -100,
                "max": 75,
            },
            {
                "control_id": "target.high_health",
                "namespace": "target",
                "key": "high_health",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.boss",
                "namespace": "target",
                "key": "boss",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.blinded",
                "namespace": "target",
                "key": "blinded",
                "type": "boolean",
                "default": False,
            },
            {
                "control_id": "target.slowed",
                "namespace": "target",
                "key": "slowed",
                "type": "boolean",
                "default": False,
            },
        ],
        "conditions": [],
        "components": [
            {
                "component_id": "synthetic:fire",
                "kind": "hit",
                "damage_type": "Fire",
                "tags": ["Fire"],
                "crit_capable": False,
                "adaptive_damage_type": "Fire",
                "hits_per_cast": 1,
                "damage_weights": [1.0],
            },
            {
                "component_id": "synthetic:cold",
                "kind": "hit",
                "damage_type": "Cold",
                "tags": ["Cold"],
                "crit_capable": False,
                "adaptive_damage_type": "Cold",
                "hits_per_cast": 1,
                "damage_weights": [1.0],
            },
        ],
        "effects": [
            effect("synthetic:fire_mod", "Fire"),
            effect("synthetic:cold_mod", "Cold"),
        ],
        "evaluation": {
            "stage_order": list(plan_handlers.STAGE_ORDER),
            "effect_stage": {
                "synthetic:fire_mod": "baseline",
                "synthetic:cold_mod": "baseline",
            },
            "dependencies": plan_handlers.canonical_dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": {
            "schema": "le-schedule-v2",
            "driver": "steady_state",
            "first_cast_s": 0,
            "windows_s": [5],
            "window_semantics": "expected_casts",
            "policies": [
                {
                    "policy_id": "cooldown_limited",
                    "binding_constraint": "cooldown",
                    "interval_policy": "max(action_time, cooldown)",
                },
                {
                    "policy_id": "attack_speed_limited",
                    "binding_constraint": "attack_speed",
                    "interval_policy": "action_time",
                },
            ],
            "primary_policy": "cooldown_limited",
            "rotation": {
                "kind": "fixed_more_factor",
                "factor": 1.0,
                "applies_to": ["direct_hit_dps"],
                "status": "confirmed",
                "label": "synthetic rotation",
            },
            "every_nth_hit": None,
        },
        "evaluations": [
            {
                "evaluation_id": "single_hit",
                "label": "synthetic single hit",
                "metrics": ["single_hit"],
                "primary": False,
                "scenario_path": "fixtures/multi-component-single.json",
                "scenario_sha256": digest,
            },
            {
                "evaluation_id": "dps",
                "label": "synthetic dps",
                "metrics": ["windows"],
                "primary": True,
                "scenario_path": "fixtures/multi-component-dps.json",
                "scenario_sha256": digest,
            },
        ],
        "diagnostics": [],
        "assumptions": [],
        "scope": {"skill": "Synthetic Multi Component", "ailment_labels": [], "limitations": []},
        "presentation": {
            "skill_name": "Synthetic Multi Component",
            "skill_names": {},
            "character": None,
            "equipment": [],
            "controls": [],
        },
    }
    return planmod.finalize_plan(plan, schema=planmod.SCHEMA_V2), scenario


class FrV3TemplateTest(unittest.TestCase):
    def test_template_is_plan_v2_definition_with_stable_selectors(self):
        template = _load(TEMPLATE)
        self.assertEqual(template["schema"], "le-skill-template-v3")
        self.assertEqual(template["ability_id"], "fr11mv")
        definition = template["definition"]
        self.assertEqual(definition["plan_version"], "le-calculation-plan-v2")
        self.assertEqual([item["evaluation_id"] for item in definition["evaluations"]], ["single_hit", "dps"])
        self.assertEqual(sum(1 for item in definition["evaluations"] if item.get("primary")), 1)
        self.assertNotIn("monster_level", definition)
        self.assertNotIn("resistance", definition)
        text = json.dumps(template, ensure_ascii=False)
        for token in ('"item_instance_id"', '"mod_id"', '"dps_profile"', '"profile_version"'):
            self.assertNotIn(token, text)
        # 当前实例数值（49% / 97% / 节点总值）只在证据 locator 中出现，不作为定义参数
        self.assertEqual(definition["skill"]["base_damage"], {"Fire": 2})
        self.assertEqual(definition["rules"]["hit_events"]["return_sources"][0]["node"], "16")
        self.assertIn("selector", json.dumps(definition["rules"]["item_effectiveness"], ensure_ascii=False))
        for row in definition["rules"]["row_normalizations"]:
            self.assertIn("evidence", row)
            self.assertIn("match", row)

    def test_schema_documents_match_plan_v2_and_template_extension(self):
        plan_schema = _load(ROOT / "tools/phase3/schemas/calculation-plan-v2.json")
        self.assertEqual(plan_schema["$id"], "le-calculation/calculation-plan-v2.json")
        self.assertEqual(set(plan_schema["required"]), set(planmod.TOP_LEVEL_FIELDS_V2))
        self.assertEqual(plan_schema["properties"]["schema"]["const"], planmod.SCHEMA_V2)
        self.assertIn("evaluations", plan_schema["properties"])
        schedule = plan_schema["properties"]["schedule"]["properties"]
        self.assertEqual(schedule["driver"], {"const": "steady_state"})
        self.assertEqual(schedule["first_cast_s"], {"const": 0})
        self.assertEqual(schedule["every_nth_hit"], {"type": "null"})
        components = plan_schema["properties"]["components"]["items"]
        self.assertEqual(components["properties"]["kind"], {"const": "hit"})
        self.assertIn("hits_per_cast", components["required"])
        self.assertIn("damage_weights", components["required"])
        self.assertEqual(
            plan_schema["properties"]["parameters"]["properties"]["ailments"]["maxItems"], 0
        )
        template_schema = _load(ROOT / "tools/phase3/schemas/skill-template-v3.json")
        definition = template_schema["properties"]["definition"]["properties"]
        self.assertIn("plan_version", definition)
        self.assertIn("evaluations", definition)
        self.assertEqual(
            definition["plan_version"]["enum"],
            [
                "le-calculation-plan-v1",
                "le-calculation-plan-v2",
                "le-calculation-plan-v3",
            ],
        )

    def test_plan_v2_requires_evaluations_and_rejects_unknown_rules(self):
        case = _case()
        work = SCRATCH / "template-negatives"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        for name, mutate, expect_code in (
            (
                "missing-evaluations",
                lambda t: t["definition"].pop("evaluations"),
                "evaluations",
            ),
            (
                "unknown-rule",
                lambda t: t["definition"]["rules"].__setitem__("unknown_plan_rule", {"x": 1}),
                "未知规则处理器",
            ),
            (
                "plan-v1-with-evaluations",
                lambda t: (
                    t["definition"].__setitem__("plan_version", "le-calculation-plan-v1"),
                ),
                "plan-v1",
            ),
            (
                "two-primaries",
                lambda t: t["definition"]["evaluations"][0].__setitem__("primary", True),
                "primary",
            ),
            (
                "bad-evaluation-metrics",
                lambda t: t["definition"]["evaluations"][0].__setitem__("metrics", []),
                "metrics",
            ),
        ):
            template = _load(TEMPLATE)
            mutate(template)
            path = work / f"{name}.json"
            _write(path, template)
            bad_case = copy.deepcopy(case)
            bad_case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
            _, compiled = _compile(bad_case)
            self.assertEqual(compiled.execution_status, "rejected", name)
            self.assertIsNone(compiled.plan, name)
            self.assertTrue(
                any(expect_code in str(item.get("message")) for item in compiled.diagnostics),
                (name, compiled.diagnostics[:3]),
            )


class FrV3RealBindingTest(unittest.TestCase):
    def test_real_binding_plan_v2_structure_and_evaluations(self):
        route, compiled = _compile(_case())
        self.assertEqual(route["route_id"], "unified:definitions_v3")
        self.assertEqual(compiled.execution_status, "completed")
        plan = compiled.plan
        self.assertEqual(plan["schema"], planmod.SCHEMA_V2)
        self.assertEqual(planmod.validate_plan_structure(plan), [])
        hit = next(item for item in plan["components"] if item["kind"] == "hit")
        self.assertEqual(hit["damage_type"], "Fire")
        self.assertEqual(hit["hits_per_cast"], 4)
        self.assertEqual(hit["damage_weights"], [1.0, 0.5, 0.5, 0.5])
        self.assertEqual(plan["parameters"]["skill"]["base_attack_rate"], 1.3750031)
        schedule = plan["schedule"]
        self.assertEqual(schedule["driver"], "steady_state")
        self.assertEqual(schedule["primary_policy"], "cooldown_limited")
        self.assertEqual(
            [row["policy_id"] for row in schedule["policies"]],
            ["cooldown_limited", "attack_speed_limited"],
        )
        self.assertEqual(schedule["rotation"]["factor"], 1.325)
        self.assertEqual(schedule["rotation"]["status"], "assumed")
        evaluations = {item["evaluation_id"]: item for item in plan["evaluations"]}
        self.assertEqual(set(evaluations), {"single_hit", "dps"})
        self.assertEqual(evaluations["dps"]["primary"], True)
        self.assertNotEqual(
            evaluations["single_hit"]["scenario_path"], evaluations["dps"]["scenario_path"]
        )
        for evaluation in plan["evaluations"]:
            self.assertEqual(len(evaluation["scenario_sha256"]), 64)
        refs = {ref["role"]: ref for ref in plan["input_refs"]}
        self.assertIn("template", refs)
        self.assertNotIn("profile", refs)
        scenario_paths = {
            ref["path"] for ref in plan["input_refs"] if ref["role"] == "scenario"
        }
        self.assertEqual(
            scenario_paths,
            {
                "examples/flame-reave/inputs/scenario-single-hit-v3.json",
                "examples/flame-reave/inputs/scenario-dps-v3.json",
            },
        )

    def test_numeric_mapping_matches_legacy_fr_baseline(self):
        _, compiled = _compile(_case())
        result = _execute(compiled.plan)
        self.assertEqual(result["schema"], "le-dps-result-v4")
        self.assertAlmostEqual(result["single_hit"], LEGACY_SINGLE_HIT, delta=1e-9)
        self.assertAlmostEqual(result["sustained_dps"], LEGACY_DPS, delta=1e-9)
        self.assertAlmostEqual(result["damage_per_cast"], LEGACY_PER_CAST, delta=1e-9)
        self.assertAlmostEqual(result["casts_per_second"], LEGACY_CASTS_PER_SECOND, delta=1e-12)
        self.assertAlmostEqual(result["hits_per_second"], LEGACY_HITS_PER_SECOND, delta=1e-12)
        self.assertAlmostEqual(
            result["hits_per_cast"]["effective_hits_per_cast"], LEGACY_EFFECTIVE_HITS, delta=1e-12
        )
        self.assertEqual(result["hits_per_cast"]["per_cast"], 4)
        self.assertEqual(result["hits_per_cast"]["damage_weights"], [1.0, 0.5, 0.5, 0.5])
        self.assertEqual(result["policy"], LEGACY_POLICY)
        self.assertAlmostEqual(result["rotation_more_factor"], LEGACY_ROTATION, delta=1e-12)
        self.assertAlmostEqual(
            result["policies"]["attack_speed_limited"]["damage_per_second"], LEGACY_ALT_DPS, delta=1e-6
        )
        single = _evaluation(result, "single_hit")
        self.assertAlmostEqual(single["single_hit"], LEGACY_SINGLE_HIT, delta=1e-9)
        self.assertAlmostEqual(single["per_cast_hit"], LEGACY_SINGLE_HIT * 2.5, delta=1e-6)
        self.assertNotIn("damage_per_second", single)
        dps = _evaluation(result, "dps")
        self.assertAlmostEqual(dps["damage_per_second"], LEGACY_DPS, delta=1e-9)
        self.assertAlmostEqual(dps["hits_per_second"], LEGACY_HITS_PER_SECOND, delta=1e-12)
        self.assertAlmostEqual(dps["casts_per_second"], LEGACY_CASTS_PER_SECOND, delta=1e-12)
        # legacy 窗口口径：期望施放数（非整数），逐窗口总伤害 = casts × 每次伤害
        legacy = _load(LEGACY_RUN / "result.json")
        for new_window, old_window in zip(result["windows"], legacy["dps"]["windows"]):
            self.assertAlmostEqual(new_window["casts"], old_window["casts"], delta=1e-9)
            self.assertAlmostEqual(new_window["hits"], old_window["hits"], delta=1e-9)
            self.assertAlmostEqual(new_window["hit_damage"], old_window["total_damage"], delta=1e-6)
            self.assertAlmostEqual(new_window["dps"], old_window["dps"], delta=1e-6)
        # 首击/权重与 legacy hits_per_cast 结构一一对应
        self.assertEqual(result["hits_per_cast"]["per_cast"], legacy["hits_per_cast"]["per_cast"])
        self.assertEqual(
            result["hits_per_cast"]["effective_hits_per_cast"],
            legacy["hits_per_cast"]["effective_hits_per_cast"],
        )
        self.assertEqual(result["hits_per_cast"]["damage_weights"], legacy["hits_per_cast"]["damage_weights"])

    def test_production_binding_and_execution_never_import_legacy_fr_calculators(self):
        import builtins
        from unittest import mock

        forbidden = ("le_calc.hit_inputs", "le_calc.hit_damage", "le_calc.hit_simulate")
        original_import = builtins.__import__

        def guarded(name, *args, **kwargs):
            if name in forbidden:
                raise AssertionError(f"FR definitions_v3 生产路径不得导入旧计算器：{name}")
            return original_import(name, *args, **kwargs)

        with mock.patch("builtins.__import__", guarded):
            for module in forbidden:
                sys.modules.pop(module, None)
            _, compiled = _compile(_case())
            result = _execute(compiled.plan)
        self.assertEqual(compiled.execution_status, "completed")
        self.assertAlmostEqual(result["single_hit"], LEGACY_SINGLE_HIT, delta=1e-9)

    def test_current_instance_variation_rebinds_points_and_rarity(self):
        work = SCRATCH / "variant"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        base_case = _case()
        for name, mutate, check in (
            (
                "node-points",
                lambda calc: _set_node_points(calc, "9", 0),
                lambda result: self.assertNotAlmostEqual(result["single_hit"], LEGACY_SINGLE_HIT, delta=1.0),
            ),
            (
                "offhand-rarity",
                lambda calc: _downgrade_offhand_tiers(calc),
                lambda result: self.assertNotAlmostEqual(result["single_hit"], LEGACY_SINGLE_HIT, delta=1.0),
            ),
        ):
            calc = _load(ROOT / base_case["inputs"]["calculation_input"])
            mutate(calc)
            calc_path = work / f"{name}-calculation-input.json"
            _write(calc_path, calc)
            case = copy.deepcopy(base_case)
            case["inputs"]["calculation_input"] = calc_path.relative_to(ROOT).as_posix()
            case["input_sha256"]["calculation_input"] = hashlib.sha256(calc_path.read_bytes()).hexdigest()
            case_path = work / f"{name}-case.json"
            _write(case_path, case)
            case["_path"] = case_path.relative_to(ROOT).as_posix()
            _, compiled = _compile(case)
            self.assertEqual(compiled.execution_status, "completed", name)
            result = _execute(compiled.plan)
            check(result)
            if name == "node-points":
                rows = {
                    item["contribution_id"]: item for item in result["contributions"]
                }
                row = rows.get("skill:fr11mv:9:Heat Wave")
                self.assertTrue(row is None or row["included"] is False)
            if name == "offhand-rarity":
                fact = compiled.plan["facts"]["item_effectiveness"]
                self.assertEqual(fact["status"], "condition_not_satisfied", fact)
                self.assertNotEqual(fact["display_rarity"], "exalted")

    def test_named_evaluations_use_independent_scenarios(self):
        work = SCRATCH / "scenarios"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        base_case = _case()
        single = _load(SINGLE_SCENARIO)
        single = copy.deepcopy(single)
        single["controls"]["ignited"] = True
        single_path = work / "scenario-single-hit-ignited.json"
        _write(single_path, single)
        case = copy.deepcopy(base_case)
        case["calculation"]["source"]["scenarios"] = [
            {
                "evaluation_id": "single_hit",
                "path": single_path.relative_to(ROOT).as_posix(),
            },
            {
                "evaluation_id": "dps",
                "path": DPS_SCENARIO.relative_to(ROOT).as_posix(),
            },
        ]
        case_path = work / "case.json"
        _write(case_path, case)
        case["_path"] = case_path.relative_to(ROOT).as_posix()
        _, compiled = _compile(case)
        self.assertEqual(compiled.execution_status, "completed")
        plan = compiled.plan
        scenarios = []
        for evaluation_id, path in (
            ("single_hit", single_path),
            ("dps", DPS_SCENARIO),
        ):
            document = _load(path)
            from le_calc import plan_compiler  # noqa: PLC0415

            target = dict(plan_compiler.DEFAULT_TARGET)
            target["monster_level"] = document["monster_level"]
            target["resistance"] = document["resistance"]
            scenarios.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario": {"states": document["controls"], "target": target},
                }
            )
        result = plan_executor.execute_plan(plan, {"evaluations": scenarios})
        single_result = _evaluation(result, "single_hit")
        dps_result = _evaluation(result, "dps")
        self.assertGreater(single_result["single_hit"], LEGACY_SINGLE_HIT * 1.1)
        self.assertAlmostEqual(dps_result["damage_per_second"], LEGACY_DPS, delta=1e-9)
        plan_evaluations = {item["evaluation_id"]: item for item in plan["evaluations"]}
        self.assertNotEqual(
            plan_evaluations["single_hit"]["scenario_sha256"],
            plan_evaluations["dps"]["scenario_sha256"],
        )

    def test_applications_reference_declared_component_ids(self):
        _, compiled = _compile(_case())
        result = _execute(compiled.plan)
        component_ids = {item["component_id"] for item in result["components"]}
        self.assertEqual(component_ids, {"fr11mv:fire"})
        self.assertTrue(result["applications"])
        self.assertEqual({item["component_id"] for item in result["applications"]}, {"fr11mv:fire"})
        self.assertNotIn("hit:Fire", json.dumps(result["applications"]))

    def test_non_primary_scenario_change_changes_execution_identity(self):
        work = SCRATCH / "aggregate-identity"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        _, baseline_compiled = _compile(_case())
        baseline = _execute(baseline_compiled.plan)
        # 只改非主评估 single_hit 的场景（ignited=true），dps 场景保持不变
        single = _load(SINGLE_SCENARIO)
        single = copy.deepcopy(single)
        single["controls"]["ignited"] = True
        single_path = work / "scenario-single-hit-ignited.json"
        _write(single_path, single)
        case = _case()
        case["calculation"]["source"]["scenarios"] = [
            {
                "evaluation_id": "single_hit",
                "path": single_path.relative_to(ROOT).as_posix(),
            },
            {
                "evaluation_id": "dps",
                "path": DPS_SCENARIO.relative_to(ROOT).as_posix(),
            },
        ]
        case_path = work / "case.json"
        _write(case_path, case)
        case["_path"] = case_path.relative_to(ROOT).as_posix()
        _, compiled = _compile(case)
        self.assertEqual(compiled.execution_status, "completed")
        plan = compiled.plan
        scenarios = []
        for evaluation_id, path in (("single_hit", single_path), ("dps", DPS_SCENARIO)):
            document = _load(path)
            from le_calc import plan_compiler  # noqa: PLC0415

            target = dict(plan_compiler.DEFAULT_TARGET)
            target["monster_level"] = document["monster_level"]
            target["resistance"] = document["resistance"]
            scenarios.append(
                {
                    "evaluation_id": evaluation_id,
                    "scenario": {"states": document["controls"], "target": target},
                }
            )
        variant = plan_executor.execute_plan(plan, {"evaluations": scenarios})
        single_result = _evaluation(variant, "single_hit")
        dps_result = _evaluation(variant, "dps")
        self.assertAlmostEqual(single_result["single_hit"], 92827.76671860272, delta=1e-9)
        self.assertAlmostEqual(dps_result["damage_per_second"], LEGACY_DPS, delta=1e-9)
        # 非主评估场景变化必须改变执行身份（聚合场景哈希 + execution_fingerprint）
        self.assertNotEqual(variant["scenario_sha256"], baseline["scenario_sha256"])
        self.assertNotEqual(variant["execution_fingerprint"], baseline["execution_fingerprint"])
        # 主场景未变时 primary_scenario_sha256 保持
        self.assertEqual(variant["primary_scenario_sha256"], baseline["primary_scenario_sha256"])
        variant_evaluations = {
            item["evaluation_id"]: item["scenario_sha256"] for item in variant["evaluation_scenarios"]
        }
        baseline_evaluations = {
            item["evaluation_id"]: item["scenario_sha256"] for item in baseline["evaluation_scenarios"]
        }
        self.assertEqual(variant_evaluations["dps"], baseline_evaluations["dps"])
        self.assertNotEqual(variant_evaluations["single_hit"], baseline_evaluations["single_hit"])
        # 完整 staged 前置链（compile/simulate/validate）接受聚合身份并写回新身份
        run_dir = work / "run"
        run_dir.mkdir()
        unified.compile_stage(case, root=ROOT, run_dir=run_dir)
        unified.simulate_stage(case, root=ROOT, run_dir=run_dir)
        validation = unified.validate_stage(case, root=ROOT, run_dir=run_dir, write=True)
        self.assertTrue(validation["passed"], validation["payload"]["errors"][:3])
        stored = _load(run_dir / "result.json")
        self.assertEqual(stored["scenario_sha256"], variant["scenario_sha256"])
        # 篡改顶层为旧主场景哈希 -> validate 必须拒绝
        tampered = copy.deepcopy(stored)
        tampered["scenario_sha256"] = tampered["primary_scenario_sha256"]
        _write(run_dir / "result.json", tampered)
        failed = unified.validate_stage(case, root=ROOT, run_dir=run_dir, write=False)
        failed_ids = {item["id"] for item in failed["payload"]["checks"] if item["status"] != "PASS"}
        self.assertFalse(failed["passed"])
        self.assertIn("result.scenario_sha256", failed_ids)


def _set_node_points(calc: dict, node_id: str, points: int) -> None:
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        if not str((spec.get("tree") or {}).get("entity_id") or "").endswith(":fr11mv"):
            continue
        for node in spec.get("nodes") or []:
            if str(node.get("node_id")) == node_id:
                node["points"] = points


def _downgrade_offhand_tiers(calc: dict) -> None:
    for item in calc.get("items") or []:
        if item.get("instance_id") != "equipment.weapon2":
            continue
        for mod in item.get("mods") or []:
            affix = mod.get("affix") or {}
            if mod.get("source") == "affix" and affix:
                affix["tier_index"] = 1
                affix["source_tier"] = 2


class FrD7SyntheticMultiHitTest(unittest.TestCase):
    """D7 合成 oracle：每击 100、每次 2 击、每秒 2 次 → 直接命中 400/s。"""

    def test_multi_hit_frequency_and_hit_cast_counting(self):
        plan, scenario = _synthetic_plan_v2(hits_per_cast=2, damage_weights=[1.0, 1.0])
        self.assertEqual(planmod.validate_plan_structure(plan), [])
        result = plan_executor.execute_plan(plan, scenario)
        self.assertEqual(result["schema"], "le-dps-result-v4")
        self.assertAlmostEqual(result["single_hit"], 100.0, delta=1e-9)
        self.assertAlmostEqual(result["damage_per_cast"], 200.0, delta=1e-9)
        self.assertAlmostEqual(result["casts_per_second"], 2.0, delta=1e-9)
        self.assertAlmostEqual(result["hits_per_second"], 4.0, delta=1e-9)
        self.assertAlmostEqual(result["damage_per_second"], 400.0, delta=1e-9)
        self.assertAlmostEqual(result["sustained_dps"], 400.0, delta=1e-9)
        self.assertEqual(result["hits_per_cast"]["per_cast"], 2)
        self.assertAlmostEqual(result["hits_per_cast"]["effective_hits_per_cast"], 2.0, delta=1e-9)
        single = _evaluation(result, "single_hit")
        dps = _evaluation(result, "dps")
        self.assertAlmostEqual(single["single_hit"], 100.0, delta=1e-9)
        self.assertAlmostEqual(single["per_cast_hit"], 200.0, delta=1e-9)
        self.assertAlmostEqual(dps["damage_per_second"], 400.0, delta=1e-9)
        self.assertAlmostEqual(dps["casts_per_second"], 2.0, delta=1e-9)
        self.assertAlmostEqual(dps["hits_per_second"], 4.0, delta=1e-9)
        window = result["windows"][0]
        self.assertAlmostEqual(window["casts"], 10.0, delta=1e-9)
        self.assertAlmostEqual(window["hits"], 20.0, delta=1e-9)
        self.assertAlmostEqual(window["hit_damage"], 2000.0, delta=1e-9)
        self.assertAlmostEqual(window["dps"], 400.0, delta=1e-9)
        # 权重不同则命中当量与每秒命中数分开：每击 100/50 → 每次 150，命中数仍是 2/次
        plan2, scenario2 = _synthetic_plan_v2(hits_per_cast=2, damage_weights=[1.0, 0.5])
        result2 = plan_executor.execute_plan(plan2, scenario2)
        self.assertAlmostEqual(result2["damage_per_cast"], 150.0, delta=1e-9)
        self.assertAlmostEqual(result2["damage_per_second"], 300.0, delta=1e-9)
        self.assertAlmostEqual(result2["hits_per_second"], 4.0, delta=1e-9)
        self.assertAlmostEqual(result2["hits_per_cast"]["effective_hits_per_cast"], 1.5, delta=1e-9)

    def test_multi_component_applications_do_not_cross_components(self):
        plan, scenario = _multi_component_plan()
        self.assertEqual(planmod.validate_plan_structure(plan), [])
        result = plan_executor.execute_plan(
            plan,
            {"evaluations": [
                {"evaluation_id": "single_hit", "scenario": scenario},
                {"evaluation_id": "dps", "scenario": scenario},
            ]},
        )
        component_ids = {item["component_id"] for item in result["components"]}
        self.assertEqual(component_ids, {"synthetic:fire", "synthetic:cold"})
        app_component_ids = {item["component_id"] for item in result["applications"]}
        self.assertEqual(app_component_ids, {"synthetic:fire", "synthetic:cold"})
        self.assertNotIn("hit:Fire", json.dumps(result["applications"]))
        self.assertNotIn("hit:Cold", json.dumps(result["applications"]))
        # 每个应用记录必须引用结果中实际存在的组件
        self.assertTrue(
            all(
                item["component_id"] in component_ids for item in result["applications"]
            )
        )
        damage = {item["component_id"]: item["damage"] for item in result["components"]}
        self.assertAlmostEqual(damage["synthetic:fire"], 110.0, delta=1e-9)
        self.assertAlmostEqual(damage["synthetic:cold"], 55.0, delta=1e-9)
        self.assertAlmostEqual(result["single_hit"], 165.0, delta=1e-9)
        self.assertAlmostEqual(result["damage_per_second"], 330.0, delta=1e-9)


class FrPlanV2ContractTest(unittest.TestCase):
    def _base(self):
        plan, scenario = _synthetic_plan_v2()
        return plan, scenario

    def test_v2_contract_negatives_are_rejected(self):
        cases = {}
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["components"][0]["damage_weights"] = [1.0]
        cases["weights-length"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["components"].append({**copy.deepcopy(bad["components"][0]), "component_id": "dot:declared", "kind": "dot"})
        cases["dot-hits"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["policies"] = [
            {"policy_id": "unsupported_policy", "binding_constraint": "cooldown", "interval_policy": "action_time"}
        ]
        cases["unsupported-policy"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["rotation"]["applies_to"] = ["dot_dps"]
        cases["rotation-applies-to"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["evaluations"][0]["primary"] = True
        bad["evaluations"][1]["primary"] = True
        cases["two-primaries"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["evaluations"][0]["scenario_path"] = "fixtures/not-in-refs.json"
        cases["unknown-scenario-ref"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["parameters"]["skill"].pop("base_attack_rate")
        cases["missing-base-rate"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schema"] = planmod.SCHEMA
        bad["evaluations"] = plan["evaluations"]
        cases["v1-with-evaluations"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["every_nth_hit"] = {"n": 2, "extra_more": 1.0}
        cases["every-nth-hit"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["first_cast_s"] = 100
        cases["first-cast-nonzero"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["driver"] = "hit_windows"
        cases["driver-hit-windows"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["schedule"]["policies"][0]["interval_policy"] = "action_time"
        cases["policy-shape-mismatch"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["evaluations"][1]["metrics"] = ["single_hit"]
        cases["primary-single-hit-only"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["components"].append(
            {
                "component_id": "synthetic:dot",
                "kind": "dot",
                "tags": ["DoT"],
                "crit_capable": False,
            }
        )
        cases["dot-component"] = bad
        plan, _ = self._base()
        bad = copy.deepcopy(plan)
        bad["parameters"]["ailments"] = [
            {"name": "Synthetic Ailment", "base_damage": 100.0, "duration_s": 2.0}
        ]
        cases["ailments-declared"] = bad
        for name, candidate in cases.items():
            finalized = planmod.finalize_plan(candidate, schema=candidate.get("schema") or planmod.SCHEMA_V2)
            issues = planmod.validate_plan_structure(finalized)
            self.assertTrue(issues, name)
            with self.assertRaises(plan_executor.PlanValidationError, msg=name):
                plan_executor.execute_plan(finalized, {"evaluations": _evaluation_scenarios()})

    def test_unsupported_rotation_and_policy_shapes_rejected_at_binding(self):
        template = _load(TEMPLATE)
        work = SCRATCH / "rule-negatives"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, exist_ok=True)
        for name, mutate in (
            (
                "unknown-policy",
                lambda t: t["definition"]["rules"]["rotation"].__setitem__(
                    "policies", ["cooldown_limited", "greedy_charges"]
                ),
            ),
            (
                "bad-window-semantics",
                lambda t: t["definition"]["rules"]["rotation"].__setitem__(
                    "window_semantics", "wall_clock"
                ),
            ),
            (
                "hit-return-node-missing",
                lambda t: t["definition"]["rules"]["hit_events"]["return_sources"][0].__setitem__(
                    "node", "999"
                ),
            ),
            (
                "bad-row-normalization",
                lambda t: t["definition"]["rules"]["row_normalizations"].append(
                    {"id": "bad", "match": {"node_id": "999"}, "set": {"value": 1}}
                ),
            ),
        ):
            variant = copy.deepcopy(template)
            mutate(variant)
            path = work / f"{name}.json"
            _write(path, variant)
            case = _case()
            case["calculation"]["source"]["template"] = path.relative_to(ROOT).as_posix()
            _, compiled = _compile(case)
            self.assertEqual(compiled.execution_status, "rejected", name)
            self.assertIsNone(compiled.plan, name)


class FrD12LegacyMappingTest(unittest.TestCase):
    def test_legacy_mapping_categories(self):
        _, compiled = _compile(_case())
        result = _execute(compiled.plan)
        legacy = _load(LEGACY_RUN / "result.json")
        mapping = {
            "legacy.single_hit.totals.damage": legacy["single_hit"]["totals"]["damage"],
            "legacy.dps.damage_per_second": legacy["dps"]["damage_per_second"],
            "legacy.dps.damage_per_cast": legacy["dps"]["damage_per_cast"],
            "legacy.dps.casts_per_second": legacy["dps"]["casts_per_second"],
            "legacy.dps.hits_per_second": legacy["dps"]["hits_per_second"],
            "legacy.dps.alternate_policy.damage_per_second": legacy["dps"]["alternate_policy"]["damage_per_second"],
        }
        mapped = {
            "legacy.single_hit.totals.damage": result["single_hit"],
            "legacy.dps.damage_per_second": result["sustained_dps"],
            "legacy.dps.damage_per_cast": result["damage_per_cast"],
            "legacy.dps.casts_per_second": result["casts_per_second"],
            "legacy.dps.hits_per_second": result["hits_per_second"],
            "legacy.dps.alternate_policy.damage_per_second": result["policies"]["attack_speed_limited"]["damage_per_second"],
        }
        for key, expected in mapping.items():
            self.assertAlmostEqual(mapped[key], expected, delta=1e-6, msg=key)
        # 表示/覆盖差异（不是数值差异）：legacy 没有 plan/scenario/coverage 契约
        self.assertNotIn("calculation-plan", json.dumps(legacy))
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertGreater(result["coverage"]["gap_count"], 0)
        self.assertIsNone(result.get("profile_sha256"))
        self.assertNotIn("profile", {ref["role"] for ref in compiled.plan["input_refs"]})
        # scope/limitations 显式保留；legacy 的 7 条 engine assumptions 不再逐字复制，
        # 但对应口径进入 definition limitations / plan assumptions。
        self.assertIn("Flame Reave", result["scope"])
        self.assertTrue(result["issues"])
        self.assertTrue(compiled.plan["assumptions"])
        legacy_assumption_text = json.dumps(legacy.get("dps", {}).get("assumptions") or [], ensure_ascii=False)
        self.assertIn("Rhythm", legacy_assumption_text)
        self.assertIn("Rhythm", json.dumps(compiled.plan["assumptions"], ensure_ascii=False))
        # 组件口径一致：基础/附加/乘区/暴击/目标/伤害逐字段映射
        new_component = result["components"][0]
        old_component = legacy["single_hit"]["components"][0]
        for key in (
            "base",
            "added",
            "subtotal",
            "increased_multiplier",
            "crit_chance",
            "crit_multiplier",
            "target_multiplier",
            "damage",
        ):
            self.assertAlmostEqual(new_component[key], old_component[key], delta=1e-6, msg=key)
        self.assertAlmostEqual(
            new_component["average_crit"],
            old_component["crit_detail"]["avg_crit"],
            delta=1e-12,
        )
        legacy_more = 1.0
        for group in old_component["more_groups"]:
            legacy_more *= float(group["multiplier"])
        self.assertAlmostEqual(new_component["more_multiplier"], legacy_more, delta=1e-12)


class FrFormalRunTest(unittest.TestCase):
    def test_formal_run_artifacts_and_binding(self):
        if not (FR_RUN / "result.json").is_file():
            self.skipTest("缺少 reports/flame-reave/dps-step4-fr-2 正式 run")
        manifest = _load(FR_RUN / "run-manifest.json")
        self.assertEqual(len(manifest["files"]), 12)
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((FR_RUN / name).read_bytes()).hexdigest(), digest, name)
        validation = _load(FR_RUN / "validation-result.json")
        self.assertTrue(validation["passed"])
        required = set(unified.REQUIRED_VALIDATION_CHECKS)
        self.assertEqual(set(validation["required_checks"]), required)
        checks = {item["id"]: item for item in validation["checks"]}
        self.assertTrue(required <= set(checks))
        self.assertTrue(all(checks[item]["status"] == "PASS" for item in required))
        plan = _load(FR_RUN / "calculation-plan.json")
        self.assertEqual(plan["schema"], planmod.SCHEMA_V2)
        self.assertEqual(plan["plan_sha256"], manifest["plan_sha256"])
        result = _load(FR_RUN / "result.json")
        self.assertEqual(result["schema"], "le-dps-result-v4")
        self.assertAlmostEqual(result["single_hit"], LEGACY_SINGLE_HIT, delta=1e-9)
        self.assertAlmostEqual(result["sustained_dps"], LEGACY_DPS, delta=1e-9)
        # P1-1：顶层执行身份为全部评估聚合哈希，主场景哈希单独保存
        scenario = _load(FR_RUN / "scenario.json")
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
        self.assertNotEqual(result["scenario_sha256"], result["primary_scenario_sha256"])
        self.assertEqual(result["primary_scenario_sha256"], primary_sha)
        self.assertEqual(
            [item["evaluation_id"] for item in result["evaluation_scenarios"]],
            ["dps", "single_hit"],
        )
        self.assertEqual(manifest["scenario_sha256"], result["scenario_sha256"])
        self.assertEqual(manifest["primary_scenario_sha256"], primary_sha)
        self.assertEqual(
            [item["evaluation_id"] for item in manifest["evaluation_scenarios"]],
            ["dps", "single_hit"],
        )
        self.assertEqual(scenario["primary_evaluation"], "dps")
        self.assertEqual(
            {item["evaluation_id"] for item in scenario["evaluations"]}, {"single_hit", "dps"}
        )
        # P1-2：应用记录引用最终组件 ID
        component_ids = {item["component_id"] for item in result["components"]}
        self.assertEqual(component_ids, {"fr11mv:fire"})
        self.assertTrue(
            all(
                item["component_id"] in component_ids
                for item in result["applications"]
            )
        )
        for check_id in (
            "result.scenario_sha256",
            "result.primary_scenario_sha256",
            "result.evaluation_scenarios",
            "result.application_component_refs",
        ):
            self.assertIn(check_id, checks, check_id)
            self.assertEqual(checks[check_id]["status"], "PASS", check_id)
        # 只读复验（含 render 之后的 report-binding 场景身份检查）必须通过
        fresh = unified.validate_stage(_case(), root=ROOT, run_dir=FR_RUN, write=False)
        self.assertTrue(fresh["passed"], fresh["payload"]["errors"][:3])
        fresh_checks = {item["id"]: item["status"] for item in fresh["payload"]["checks"]}
        self.assertEqual(fresh_checks.get("result.report_scenario_binding"), "PASS")
        self.assertEqual(fresh_checks.get("result.application_component_refs"), "PASS")
        binding = _load(FR_RUN / "report-binding.json")
        self.assertEqual(binding["scenario_binding"]["scenario_sha256"], result["scenario_sha256"])
        self.assertEqual(
            binding["scenario_binding"]["primary_scenario_sha256"],
            result["primary_scenario_sha256"],
        )
        self.assertEqual(binding["report_ref"]["run_dir"], FR_RUN.relative_to(ROOT).as_posix())
        html = (FR_RUN / "dps-report.html").read_text(encoding="utf-8")
        self.assertIn("多命中与频率", html)
        # 旧 VC 已签收 run 只读：其 manifest/result 未被本次 run 改写
        vc_manifest = _load(ROOT / "reports/void-cleave-20261110/dps-step4-vc-8/run-manifest.json")
        self.assertEqual(
            vc_manifest["plan_sha256"],
            "46ba62b4f39665d4d4f966f3d90ac90f0475a27b3c4e74d8f400d3206626f418",
        )
        # 上一轮 FR-1 正式 run 保留为历史产物，不删除/不改写
        self.assertTrue((ROOT / "reports/flame-reave/dps-step4-fr-1/run-manifest.json").is_file())

    def test_formal_scenario_file_change_is_rejected(self):
        work = SCRATCH / "run-tamper"
        shutil.rmtree(work, ignore_errors=True)
        run_dir = work / "run"
        run_dir.mkdir(parents=True)
        case = _case()
        unified.compile_stage(case, root=ROOT, run_dir=run_dir)
        # 复制 scenario 文件引用后篡改文件内容：simulate 必须拒绝（哈希/计划绑定）
        scenario_path = ROOT / "examples/flame-reave/inputs/scenario-dps-v3.json"
        tampered = _load(scenario_path)
        tampered["controls"]["ignited"] = True
        case_source = copy.deepcopy(case)
        case_source["_path"] = case["_path"]
        # 直接篡改仓库内 scenario 是禁止的；用临时 case 指向临时 scenario 来验证拒绝逻辑
        temp_scenario = work / "scenario-dps-v3.json"
        _write(temp_scenario, tampered)
        plan = _load(run_dir / "calculation-plan.json")
        # 把 plan 的 scenario 文件路径换成临时文件，但保留旧哈希 -> simulate 必须失败
        for evaluation in plan["evaluations"]:
            if evaluation["evaluation_id"] == "dps":
                evaluation["scenario_path"] = temp_scenario.relative_to(ROOT).as_posix()
        _write(run_dir / "calculation-plan.json", planmod.finalize_plan(plan, schema=planmod.SCHEMA_V2))
        with self.assertRaises((ValueError, plan_executor.PlanValidationError)):
            unified.simulate_stage(case_source, root=ROOT, run_dir=run_dir)


if __name__ == "__main__":
    unittest.main(verbosity=2)
