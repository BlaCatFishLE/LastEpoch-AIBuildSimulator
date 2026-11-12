"""第二步定向验证：统一计算计划契约（B1–B15）。

运行方式（只跑本文件）：

    cd tools/phase3/tests
    python -m unittest test_calculation_plan -v

本文件只做契约/接入相关的定向检查，不构成全仓回归。
"""

from __future__ import annotations

import builtins
import copy
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools/phase3/src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import dps  # noqa: E402
from le_calc import plan_compiler  # noqa: E402
from le_calc import plan_executor  # noqa: E402
from le_calc import plan_handlers  # noqa: E402

CASE_PATH = ROOT / "examples/void-cleave-20261110/case.profile-v2.json"
PROFILE_PATH = ROOT / "examples/void-cleave-20261110/inputs/dps-profile-v2.json"
STEP1_RESULT = ROOT / "reports/void-cleave-20261110/dps-step1-coverage-3/result.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures/calculation-plan"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def load_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_scenario(plan: dict) -> dict:
    controls = plan["controls"]
    return {
        "states": {
            control["key"]: control["default"]
            for control in controls
            if control["namespace"] == "state"
        },
        "target": {
            control["key"]: control["default"]
            for control in controls
            if control["namespace"] == "target"
        },
    }


def synthetic_context(template_path: Path | None = None, profile: dict | None = None):
    refs = []
    if template_path is not None:
        template_path = Path(template_path)
        refs.append(
            {
                "role": "template",
                "path": template_path.relative_to(ROOT).as_posix(),
                "sha256": planmod.sha256_file(template_path),
            }
        )
    if profile is not None:
        refs.append(
            {
                "role": "profile_content",
                "path": None,
                "sha256": planmod.sha256_text(planmod.canonical_json(profile)),
            }
        )
    if not refs:
        refs.append(
            {
                "role": "compiler_source",
                "path": "tools/phase3/src/le_calc/plan_compiler.py",
                "sha256": planmod.sha256_file(
                    ROOT / "tools/phase3/src/le_calc/plan_compiler.py"
                ),
            }
        )
    return plan_compiler.FrozenContext(
        root=ROOT,
        case={"case_id": "synthetic", "main_skill": {"ability_id": "synthetic"}},
        calc={},
        dataset={},
        profile=profile,
        catalog=None,
        input_refs=refs,
        synthetic=True,
    )


def native_scenario(plan: dict) -> dict:
    scenario = default_scenario(plan)
    scenario["target"]["monster_level"] = 1
    return scenario


def compile_native(path: Path):
    template = load_json(path)
    context = synthetic_context(path)
    return plan_compiler.compile_from_definitions(
        context, template, mechanisms=load_json(ROOT / "tools/phase3/rules/shared-conditional-mechanisms-v1.json")
    )


class PlanFixtureMixin:
    _compiled = None
    _result = None

    @classmethod
    def real_compiled(cls):
        if cls._compiled is None:
            case = load_json(CASE_PATH)
            profile = load_json(PROFILE_PATH)
            cls._compiled = dps.compile_case(ROOT, case, profile=profile)
        return cls._compiled

    @classmethod
    def real_result(cls):
        if cls._result is None:
            compiled = cls.real_compiled()
            scenario = default_scenario(compiled.plan)
            cls._result = plan_executor.execute_plan(compiled.plan, scenario)
        return cls._result


class B1PlanContractTest(PlanFixtureMixin, unittest.TestCase):
    def test_schema_document_matches_code_contract(self):
        schema = load_json(ROOT / "tools/phase3/schemas/calculation-plan-v1.json")
        self.assertEqual(schema["$id"], "le-calculation/calculation-plan-v1.json")
        required = set(schema["required"])
        self.assertEqual(required, set(planmod.TOP_LEVEL_FIELDS))
        self.assertEqual(schema["properties"]["schema"]["const"], planmod.SCHEMA)

    def test_compiled_plans_are_pure_json_and_structurally_valid(self):
        compiled = self.real_compiled()
        self.assertTrue(compiled.ok, compiled.diagnostics)
        plan = compiled.plan
        roundtripped = json.loads(planmod.canonical_json(plan))
        self.assertEqual(roundtripped, plan)
        self.assertEqual(planmod.validate_plan_structure(plan), [])
        native = compile_native(FIXTURES / "synthetic-melee-hit-with-bleed.json")
        self.assertTrue(native.ok, native.diagnostics)
        self.assertEqual(planmod.validate_plan_structure(native.plan), [])
        # 计划不包含机器绝对路径
        for ref in plan["input_refs"]:
            if ref["path"]:
                self.assertNotRegex(ref["path"], r"^[A-Za-z]:[\\/]|^/")
                self.assertNotIn("\\\\", ref["path"])


class B2AdapterContractTest(PlanFixtureMixin, unittest.TestCase):
    def test_profile_and_native_share_contract(self):
        profile_plan = self.real_compiled().plan
        native_plan = compile_native(FIXTURES / "synthetic-melee-hit-with-bleed.json").plan
        self.assertEqual(profile_plan["schema"], native_plan["schema"])
        self.assertNotEqual(profile_plan["compiler"]["origin"], native_plan["compiler"]["origin"])
        self.assertEqual(
            profile_plan["executor_requirements"]["handlers"],
            native_plan["executor_requirements"]["handlers"],
        )
        self.assertEqual(
            profile_plan["executor_requirements"]["executor_fingerprint"],
            native_plan["executor_requirements"]["executor_fingerprint"],
        )
        self.assertEqual(self.real_result()["plan_sha256"], profile_plan["plan_sha256"])


class B3InputClosureTest(PlanFixtureMixin, unittest.TestCase):
    def test_input_refs_block_roles_and_hashes(self):
        plan = self.real_compiled().plan
        roles = {ref["role"] for ref in plan["input_refs"]}
        for role in (
            "calculation_input",
            "build_spec",
            "dataset_pointer",
            "dataset_content",
            "profile",
            "profile_content",
            "attribute_conversion_rules",
            "compiler_source",
        ):
            self.assertIn(role, roles)
        for ref in plan["input_refs"]:
            if ref["path"] and ref["role"] != "profile_content":
                self.assertEqual(planmod.sha256_file(ROOT / ref["path"]), ref["sha256"])

    def test_changed_profile_rule_or_points_recompile_and_old_plan_kept(self):
        case = load_json(CASE_PATH)
        profile = load_json(PROFILE_PATH)
        baseline = self.real_compiled()
        baseline_plan = baseline.plan
        baseline_result = self.real_result()
        scratch = ROOT / ".scratch/unified-dps-step2"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="b3-") as temp_dir:
            temp = Path(temp_dir)
            temp_profile = temp / "dps-profile-v2.json"
            modified = copy.deepcopy(profile)
            modified["skill"]["base_damage"] = {"Fire": 999.0}
            temp_profile.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
            temp_case = copy.deepcopy(case)
            temp_case["dps_profile"] = temp_profile.relative_to(ROOT).as_posix()
            temp_case_path = temp / "case.json"
            temp_case_path.write_text(json.dumps(temp_case, ensure_ascii=False), encoding="utf-8")
            fresh = load_json(temp_profile)
            changed = dps.compile_case(ROOT, temp_case_path, profile=fresh)
            self.assertTrue(changed.ok, changed.diagnostics)
            self.assertNotEqual(changed.plan["plan_sha256"], baseline_plan["plan_sha256"])
            # 修改磁盘上的本次输入后，旧计划仍按原内容执行（不会静默读新文件）
            after = plan_executor.execute_plan(baseline_plan, default_scenario(baseline_plan))
            self.assertEqual(after["sustained_dps"], baseline_result["sustained_dps"])
            self.assertEqual(after["plan_sha256"], baseline_plan["plan_sha256"])
            # 规则变化同样改变计划
            modified_rule = copy.deepcopy(profile)
            modified_rule["rules"]["state_scalings"][0]["per_state"] = 0.31
            temp_profile.write_text(json.dumps(modified_rule, ensure_ascii=False), encoding="utf-8")
            changed_rule = dps.compile_case(ROOT, temp_case_path, profile=load_json(temp_profile))
            self.assertTrue(changed_rule.ok)
            self.assertNotEqual(changed_rule.plan["plan_sha256"], baseline_plan["plan_sha256"])
            # 点数变化：复制 calculation-input 并改节点点数
            calc_rel = case["inputs"]["calculation_input"]
            calc = load_json(ROOT / calc_rel)
            node = calc["skills"]["specializations"][0]["nodes"][0]
            node["points"] = int(node.get("points") or 0) + 1
            temp_calc = temp / "calculation-input.json"
            temp_calc.write_text(json.dumps(calc, ensure_ascii=False), encoding="utf-8")
            changed_points_case = copy.deepcopy(case)
            changed_points_case["inputs"]["calculation_input"] = temp_calc.relative_to(ROOT).as_posix()
            changed_points_case["input_sha256"]["calculation_input"] = planmod.sha256_file(temp_calc)
            changed_points_case["dps_profile"] = PROFILE_PATH.relative_to(ROOT).as_posix()
            points_path = temp / "case-points.json"
            points_path.write_text(
                json.dumps(changed_points_case, ensure_ascii=False), encoding="utf-8"
            )
            points_compiled = dps.compile_case(ROOT, points_path, profile=profile)
            self.assertTrue(points_compiled.ok, points_compiled.diagnostics)
            self.assertNotEqual(points_compiled.plan["plan_sha256"], baseline_plan["plan_sha256"])
            # dataset 内容闭包：计划引用了实际 database sha256
            dataset_ref = next(
                ref for ref in baseline_plan["input_refs"] if ref["role"] == "dataset_content"
            )
            self.assertEqual(dataset_ref["sha256"], load_json(ROOT / case["dataset"])["file_sha256"])


class B4ValidationTest(PlanFixtureMixin, unittest.TestCase):
    def test_missing_field_tampered_hash_and_unknown_handler_rejected(self):
        plan = self.real_compiled().plan
        tampered = copy.deepcopy(plan)
        tampered["effects"][0]["value"] = 1.0
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(tampered, default_scenario(tampered))
        missing = copy.deepcopy(plan)
        missing.pop("schedule")
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(missing, default_scenario(plan))
        unknown = copy.deepcopy(plan)
        unknown["effects"][0]["handler_id"] = "future-handler-v9"
        unknown = planmod.finalize_plan(unknown)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(unknown, default_scenario(plan))
        bad_operation = copy.deepcopy(plan)
        bad_operation["effects"][0]["operation"] = "future_operation"
        bad_operation = planmod.finalize_plan(bad_operation)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(bad_operation, default_scenario(plan))
        bad_condition = copy.deepcopy(plan)
        bad_condition["effects"][0]["condition_ref"] = "cond:does-not-exist"
        bad_condition = planmod.finalize_plan(bad_condition)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(bad_condition, default_scenario(plan))

    def test_missing_runtime_handler_rejected(self):
        plan = self.real_compiled().plan
        runtime = plan_handlers.PlanRuntime({})
        with self.assertRaises(plan_executor.PlanValidationError) as caught:
            plan_executor.execute_plan(plan, default_scenario(plan), runtime=runtime)
        codes = {issue["code"] for issue in caught.exception.issues}
        self.assertIn("missing_handler", codes)

    def test_unknown_condition_is_not_silently_false(self):
        plan = self.real_compiled().plan
        tampered = copy.deepcopy(plan)
        tampered["conditions"].append(
            {
                "condition_id": "cond:unknown-code",
                "op": "all",
                "terms": [{"kind": "code", "code": "no_such_condition_code"}],
            }
        )
        tampered["effects"][0]["condition_ref"] = "cond:unknown-code"
        tampered = planmod.finalize_plan(tampered)
        result = plan_executor.execute_plan(tampered, default_scenario(tampered))
        row = next(
            item
            for item in result["contributions"]
            if item["contribution_id"] == tampered["effects"][0].get("contribution_id")
        )
        self.assertFalse(row["included"])
        self.assertIn("无法判定", row.get("exclusion_reason") or "")

    def test_invalid_scenario_rejected(self):
        plan = self.real_compiled().plan
        scenario = default_scenario(plan)
        bad = copy.deepcopy(scenario)
        bad["states"]["frenzy"] = 2
        with self.assertRaises(ValueError):
            plan_executor.execute_plan(plan, bad)
        extra = copy.deepcopy(scenario)
        extra["states"]["not_a_control"] = True
        with self.assertRaises(ValueError):
            plan_executor.execute_plan(plan, extra)


class B5ExecutionIsolationTest(PlanFixtureMixin, unittest.TestCase):
    def test_execution_does_not_read_inputs_or_call_old_path(self):
        plan = self.real_compiled().plan
        scenario = default_scenario(plan)
        runtime = plan_handlers.PlanRuntime()
        baseline = plan_executor.execute_plan(plan, scenario, runtime=runtime)  # warm imports

        def forbidden(*args, **kwargs):
            raise AssertionError("计划执行期不得读取输入文件或调用旧计算路径")

        with mock.patch.object(builtins, "open", forbidden), mock.patch.object(io, "open", forbidden), mock.patch.object(
            Path, "read_text", forbidden
        ), mock.patch.object(
            Path, "read_bytes", forbidden
        ), mock.patch.object(
            plan_compiler, "prepare_context", forbidden
        ), mock.patch.object(
            dps, "calculate", forbidden
        ):
            again = plan_executor.execute_plan(plan, scenario, runtime=runtime)
        self.assertEqual(again["sustained_dps"], baseline["sustained_dps"])
        self.assertEqual(again["contributions"], baseline["contributions"])
        self.assertEqual(again["plan_sha256"], baseline["plan_sha256"])


class B6ScenarioRuntimeTest(PlanFixtureMixin, unittest.TestCase):
    def test_plan_stable_and_runtime_conditions_flip(self):
        plan = self.real_compiled().plan
        base = default_scenario(plan)
        base_result = self.real_result()
        # 打开易伤/冰冻状态与 nether_coating、ambition 层数
        activated = copy.deepcopy(base)
        activated["states"]["nether_coating"] = True
        activated["states"]["ambition"] = 20
        activated["target"]["blinded"] = True
        result = plan_executor.execute_plan(plan, activated)
        self.assertEqual(result["plan_sha256"], base_result["plan_sha256"])
        node_rows = [
            row
            for row in result["contributions"]
            if row["contribution_id"] == "skill:ab0lh:14:Embrace the Darkness"
        ]
        self.assertTrue(node_rows)
        self.assertTrue(any(row["included"] for row in node_rows))
        ambition = next(
            row for row in result["contributions"] if row["contribution_id"].endswith("Throne of Ambition")
        )
        self.assertTrue(ambition["included"])
        self.assertGreater(ambition["value"], 0.0)
        self.assertGreater(result["sustained_dps"], base_result["sustained_dps"])
        # 目标关闭 boss 时 ambition 不生效，但计划仍相同
        no_boss = copy.deepcopy(activated)
        no_boss["target"]["boss"] = False
        no_boss_result = plan_executor.execute_plan(plan, no_boss)
        self.assertEqual(no_boss_result["plan_sha256"], base_result["plan_sha256"])
        no_boss_ambition = next(
            row for row in no_boss_result["contributions"] if row["contribution_id"].endswith("Throne of Ambition")
        )
        self.assertFalse(no_boss_ambition["included"])
        # 状态缩放（incisive）随场景变化，但计划不变
        incisive_off = copy.deepcopy(base)
        incisive_off["states"]["incisive"] = False
        incisive_result = plan_executor.execute_plan(plan, incisive_off)
        self.assertLess(
            incisive_result["attack_speed_increased"], base_result["attack_speed_increased"]
        )


class B7NativeDefinitionsTest(unittest.TestCase):
    def test_two_synthetic_definitions_same_executor(self):
        spell = compile_native(FIXTURES / "synthetic-spell-hit-no-crit.json")
        melee = compile_native(FIXTURES / "synthetic-melee-hit-with-bleed.json")
        self.assertTrue(spell.ok, spell.diagnostics)
        self.assertTrue(melee.ok, melee.diagnostics)
        spell_result = plan_executor.execute_plan(spell.plan, native_scenario(spell.plan))
        melee_result = plan_executor.execute_plan(melee.plan, native_scenario(melee.plan))
        # 合成 A：不可暴击组件不因暴击率变化而改变
        self.assertEqual(spell_result["single_hit"], 100.0)
        self.assertEqual(spell_result["components"][0]["damage"], 100.0)
        self.assertEqual(spell_result["components"][0]["average_crit"], 1.0)
        self.assertEqual(spell_result["components"][0]["crit_chance"], 0.0)
        # 合成 B：DoT 100×1.2=120；Hit 50×1.3=65；标签互不串
        self.assertEqual(melee_result["single_hit"], 65.0)
        self.assertEqual(melee_result["components"][0]["damage"], 65.0)
        bleed = next(item for item in melee_result["ailments"] if item["name"] == "Bleed")
        self.assertAlmostEqual(bleed["stack_damage"], 120.0)
        self.assertNotEqual(bleed["stack_damage"], 156.0)  # 不叠加 Melee-only 30%
        self.assertNotEqual(melee_result["components"][0]["damage"], 78.0)  # 不叠加 DoT-only 20%
        # 机制库合并：target_stack_chance 进入计划规则
        self.assertIn("target_stack_chance", melee.plan["parameters"]["crit"])
        self.assertTrue(all(row["emits_contribution"] for row in melee.plan["effects"]))
        self.assertEqual(spell.plan["compiler"]["origin"], "native_template")


class NoSkillBranchTest(unittest.TestCase):
    """补充证据：编译器/执行器无技能 ID 分支（不属于 B9 的替代）。"""

    def test_compiler_and_executor_have_no_skill_id_branches(self):
        for relative in (
            "tools/phase3/src/le_calc/plan_compiler.py",
            "tools/phase3/src/le_calc/plan_executor.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            for skill_id in ("v01cv", "ab0lh", "fr11mv", "bh2", "void-cleave"):
                self.assertNotIn(skill_id, text, f"{relative} 不应出现技能专用分支 {skill_id}")
        template = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
        template["ability_id"] = "synthetic:renamed"
        context = synthetic_context()
        compiled = plan_compiler.compile_from_definitions(context, template)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        renamed = plan_executor.execute_plan(compiled.plan, native_scenario(compiled.plan))
        self.assertEqual(renamed["single_hit"], 100.0)


def _spell_template_with_effects(effects: list[dict]) -> dict:
    template = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
    template["effects"] = effects
    return template


def _damage_effect(effect_id: str, path: str, value: float, group: str | None = None) -> dict:
    return {
        "effect_id": effect_id,
        "label": effect_id,
        "stat_key": "damage",
        "operation": "increased",
        "value": value,
        "unit": "percent",
        "damage_tags": [],
        "group": group or effect_id,
        "status": "assumed",
        "source": {"kind": "template_constant", "label": effect_id, "path": path},
    }


class B8OwnershipConflictTest(PlanFixtureMixin, unittest.TestCase):
    def test_plan_ownership_unique_and_every_emitting_effect_has_source(self):
        import json as _json
        plan = self.real_compiled().plan
        emitting = [effect for effect in plan["effects"] if effect["emits_contribution"]]
        keys = [_json.dumps(effect["ownership_key"], sort_keys=True) for effect in emitting]
        self.assertEqual(len(keys), len(set(keys)), "同一所有权键不能重复注入")
        self.assertTrue(all(effect["source_refs"] for effect in emitting))
        self.assertEqual(plan["evaluation"]["ownership"], "unique_by_ownership_key")
        result = self.real_result()
        self.assertEqual(result["sustained_dps"], load_json(STEP1_RESULT)["sustained_dps"]) if STEP1_RESULT.is_file() else None

    def test_distinct_sources_same_mechanism_are_kept(self):
        template = _spell_template_with_effects(
            [
                _damage_effect("synthetic-a", "fixture#a", 0.1),
                _damage_effect("synthetic-b", "fixture#b", 0.2),
            ]
        )
        compiled = plan_compiler.compile_from_definitions(synthetic_context(), template)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        result = plan_executor.execute_plan(compiled.plan, native_scenario(compiled.plan))
        self.assertAlmostEqual(result["single_hit"], 130.0)  # 100 × (1 + 0.1 + 0.2)
        self.assertEqual(len(compiled.plan["effects"]), 2)

    def test_same_value_duplicate_merges_sources(self):
        template = _spell_template_with_effects(
            [
                _damage_effect("synthetic-first", "fixture#same", 0.1, group="shared"),
                _damage_effect("synthetic-second", "fixture#same", 0.1, group="shared"),
            ]
        )
        compiled = plan_compiler.compile_from_definitions(synthetic_context(), template)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        self.assertEqual(len(compiled.plan["effects"]), 1)
        effect = compiled.plan["effects"][0]
        self.assertEqual(len(effect["source_refs"]), 2, "同语义重复应合并来源引用")
        self.assertTrue(
            any(item["code"] == "duplicate_merged" for item in compiled.diagnostics)
        )
        self.assertEqual(
            {source["source_id"] for source in compiled.plan["sources"]},
            set(effect["source_refs"]),
        )
        result = plan_executor.execute_plan(compiled.plan, native_scenario(compiled.plan))
        self.assertAlmostEqual(result["single_hit"], 110.0)  # 只计一次

    def test_conflicting_definitions_rejected_both_orders(self):
        for first, second in ((0.1, 0.5), (0.5, 0.1)):
            template = _spell_template_with_effects(
                [
                    _damage_effect("synthetic-a", "fixture#same", first, group="shared"),
                    _damage_effect("synthetic-b", "fixture#same", second, group="shared"),
                ]
            )
            compiled = plan_compiler.compile_from_definitions(synthetic_context(), template)
            self.assertFalse(compiled.ok, f"{first}->{second} 同所有权不同定义必须拒绝")
            self.assertEqual(compiled.execution_status, "rejected")
            self.assertEqual(compiled.readiness, "blocked")
            self.assertIsNone(compiled.plan)
            self.assertEqual(
                [item["code"] for item in compiled.diagnostics].count("ownership_conflict"), 1
            )

    def test_profile_duplicate_same_value_merges_and_different_definition_conflicts(self):
        profile = load_json(PROFILE_PATH)
        baseline = self.real_compiled()

        same_value = copy.deepcopy(profile)
        first = copy.deepcopy(same_value["node_bindings"][0])
        duplicate = copy.deepcopy(first)
        same_value["node_bindings"] = [first, duplicate] + same_value["node_bindings"][1:]
        context = plan_compiler.prepare_context(CASE_PATH, ROOT)
        try:
            merged = plan_compiler.compile_from_profile_v2(context, same_value)
        finally:
            context.close()
        self.assertTrue(merged.ok, merged.diagnostics)
        self.assertEqual(len(merged.plan["effects"]), len(baseline.plan["effects"]))
        self.assertTrue(any(item["code"] == "duplicate_merged" for item in merged.diagnostics))
        merged_result = plan_executor.execute_plan(
            merged.plan, default_scenario(merged.plan)
        )
        self.assertEqual(merged_result["sustained_dps"], self.real_result()["sustained_dps"])

        for order in (0, 1):
            conflict_profile = copy.deepcopy(profile)
            base_rule = copy.deepcopy(conflict_profile["node_bindings"][0])
            variant = copy.deepcopy(base_rule)
            original_condition = base_rule.get("condition")
            variant["condition"] = (
                "nether_coating" if original_condition != "nether_coating" else "frenzy"
            )
            pair = [base_rule, variant]
            if order == 1:
                pair.reverse()
            conflict_profile["node_bindings"] = pair + conflict_profile["node_bindings"][1:]
            context = plan_compiler.prepare_context(CASE_PATH, ROOT)
            try:
                conflict = plan_compiler.compile_from_profile_v2(context, conflict_profile)
            finally:
                context.close()
            self.assertFalse(conflict.ok, f"node_bindings order {order} 应冲突")
            self.assertEqual(conflict.execution_status, "rejected")
            self.assertIsNone(conflict.plan)
            self.assertTrue(
                any(item["code"] == "ownership_conflict" for item in conflict.diagnostics)
            )


class B9AttributeDependencyTest(PlanFixtureMixin, unittest.TestCase):
    def test_attribute_views_and_fixed_stage_dependencies(self):
        plan = self.real_compiled().plan
        self.assertEqual(list(plan["evaluation"]["stage_order"]), list(plan_handlers.STAGE_ORDER))
        self.assertEqual(plan["evaluation"]["dependencies"], plan_handlers.canonical_dependencies())
        views = plan["facts"].get("attribute_views") or {}
        self.assertEqual(set(views), {"original", "converted", "effective"})
        conversion = next(
            effect for effect in plan["effects"] if effect["category"] == "attribute_conversion"
        )
        scalings = [
            effect for effect in plan["effects"] if effect["category"] == "attribute_scaling"
        ]
        self.assertTrue(scalings)
        stage_index = {stage: index for index, stage in enumerate(plan_handlers.STAGE_ORDER)}
        self.assertLess(stage_index[conversion["stage"]], stage_index[scalings[0]["stage"]])
        self.assertTrue(views["converted"], "转换已应用时应保留 converted 视图")

    def test_cycle_and_future_stage_rejected(self):
        plan = copy.deepcopy(self.real_compiled().plan)
        plan["evaluation"]["dependencies"] = [
            {"from": "baseline", "to": "projection"},
            {"from": "projection", "to": "baseline"},
        ]
        plan = planmod.finalize_plan(plan)
        self.assertTrue(
            any(issue["code"] == "invalid_dependencies" for issue in planmod.validate_plan_structure(plan))
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(plan, default_scenario(plan))

        plan = copy.deepcopy(self.real_compiled().plan)
        conversion_id = next(
            effect["effect_id"]
            for effect in plan["effects"]
            if effect["category"] == "attribute_conversion"
        )
        plan["evaluation"]["effect_stage"][conversion_id] = "projection"
        plan = planmod.finalize_plan(plan)
        self.assertTrue(
            any(issue["code"] == "invalid_effect_stage" for issue in planmod.validate_plan_structure(plan))
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(plan, default_scenario(plan))


class B11ParameterValidationTest(unittest.TestCase):
    def test_serialized_plan_authority_and_negative_parameters(self):
        compiled = compile_native(FIXTURES / "synthetic-spell-hit-no-crit.json")
        plan = json.loads(json.dumps(compiled.plan))  # 序列化往返，避免共享引用掩盖问题
        self.assertNotIn("components", plan["parameters"]["skill"])
        self.assertNotIn("every_nth_hit", plan["parameters"])
        self.assertEqual(len(plan["components"]), 2)
        self.assertEqual(planmod.validate_plan_structure(plan), [])

        # schedule.every_nth_hit 是唯一权威：修改后合法且被实际执行
        changed = json.loads(json.dumps(plan))
        changed["schedule"]["windows_s"] = [2.0]
        changed["schedule"]["every_nth_hit"] = {
            "n": 2,
            "extra_more": 0.5,
            "counting": "hit",
            "phase": "from_first_hit",
        }
        changed = planmod.finalize_plan(changed)
        result = plan_executor.execute_plan(changed, native_scenario(changed))
        # 2 秒 2 次命中：100 × (2 + 1 × 0.5) = 250
        self.assertAlmostEqual(result["windows"][0]["hit_damage"], 250.0)

        # 小数/布尔 N 被拒绝
        for bad_n in (2.5, True):
            invalid = json.loads(json.dumps(changed))
            invalid["schedule"]["every_nth_hit"]["n"] = bad_n
            invalid = planmod.finalize_plan(invalid)
            self.assertTrue(
                any(issue["code"] == "invalid_schedule" for issue in planmod.validate_plan_structure(invalid)),
                f"n={bad_n!r} 应被拒绝",
            )

        # 非法单位/未知条件/未知字段在编译期拒绝
        bad_unit = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
        bad_unit["effects"][0]["unit"] = "banana"
        rejected = plan_compiler.compile_from_definitions(synthetic_context(), bad_unit)
        self.assertFalse(rejected.ok)
        bad_condition = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
        bad_condition["effects"][0]["condition"] = {"op": "all", "terms": [{"kind": "future"}]}
        rejected = plan_compiler.compile_from_definitions(synthetic_context(), bad_condition)
        self.assertFalse(rejected.ok)
        bad_field = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
        bad_field["effects"][0]["unknown_field"] = 1
        rejected = plan_compiler.compile_from_definitions(synthetic_context(), bad_field)
        self.assertFalse(rejected.ok)

        # 必需组件基础参数缺失时不得返回 DPS
        missing_base = json.loads(json.dumps(plan))
        missing_base["parameters"]["skill"]["base_damage"] = {}
        missing_base = planmod.finalize_plan(missing_base)
        self.assertTrue(
            any(issue["code"] == "missing_parameter" for issue in planmod.validate_plan_structure(missing_base))
        )

    def test_windows_and_every_nth_drive_execution(self):
        plan = copy.deepcopy(
            plan_compiler.compile_from_definitions(
                synthetic_context(), load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
            ).plan
        )
        plan["schedule"]["windows_s"] = [1.0, 2.0]
        plan["schedule"]["every_nth_hit"] = {
            "n": 2,
            "extra_more": 0.5,
            "counting": "hit",
            "phase": "from_first_hit",
        }
        plan = planmod.finalize_plan(plan)
        result = plan_executor.execute_plan(plan, native_scenario(plan))
        self.assertEqual([window["seconds"] for window in result["windows"]], [1.0, 2.0])
        self.assertEqual(result["windows"][0]["hit_damage"], 100.0)


class NativeDataOnlyTest(unittest.TestCase):
    def test_adding_definition_needs_no_executor_branch(self):
        base = load_json(FIXTURES / "synthetic-spell-hit-no-crit.json")
        variant = copy.deepcopy(base)
        variant["template_id"] = "synthetic-spell-hit-fire-150"
        variant["skill"]["base_damage"] = {"Fire": 150.0}
        compiled = plan_compiler.compile_from_definitions(synthetic_context(), variant)
        self.assertTrue(compiled.ok, compiled.diagnostics)
        result = plan_executor.execute_plan(compiled.plan, native_scenario(compiled.plan))
        self.assertEqual(result["single_hit"], 150.0)
        spell_plan = compile_native(FIXTURES / "synthetic-spell-hit-no-crit.json").plan
        base_result = plan_executor.execute_plan(spell_plan, native_scenario(spell_plan))
        self.assertEqual(base_result["single_hit"], 100.0)


class ContractGapRegressionTest(PlanFixtureMixin, unittest.TestCase):
    """复验四条反例的回归：序列化权威、来源约束、覆盖裁决依据、依赖/阶段。"""

    def test_serialized_plan_single_authority(self):
        plan = json.loads(json.dumps(self.real_compiled().plan))
        self.assertNotIn("components", plan["parameters"]["skill"])
        self.assertNotIn("every_nth_hit", plan["parameters"])
        baseline = plan_executor.execute_plan(plan, default_scenario(plan))

        # 顶层 components 是执行权威：改变暴击能力必须改变命中结果
        changed = json.loads(json.dumps(plan))
        hit_entry = next(item for item in changed["components"] if item["kind"] == "hit")
        self.assertTrue(hit_entry.get("crit_capable"))
        hit_entry["crit_capable"] = False
        changed = planmod.finalize_plan(changed)
        self.assertEqual(planmod.validate_plan_structure(changed), [])
        result = plan_executor.execute_plan(changed, default_scenario(changed))
        self.assertFalse(result["components"][0]["crit_capable"])
        self.assertNotEqual(result["single_hit"], baseline["single_hit"])

        # 顶层组件标签同样是执行权威：改空标签后命中乘区筛选必须变化
        changed = json.loads(json.dumps(plan))
        hit_entry = next(item for item in changed["components"] if item["kind"] == "hit")
        hit_entry["tags"] = []
        changed = planmod.finalize_plan(changed)
        self.assertEqual(planmod.validate_plan_structure(changed), [])
        tagged_result = plan_executor.execute_plan(changed, default_scenario(changed))
        self.assertNotEqual(tagged_result["single_hit"], baseline["single_hit"])

        # schedule.every_nth_hit 是唯一权威：改变 N 应改变窗口伤害
        changed = json.loads(json.dumps(plan))
        changed["schedule"]["windows_s"] = [30.0]
        changed["schedule"]["every_nth_hit"] = {
            "n": 2,
            "extra_more": 0.5,
            "counting": "hit",
            "phase": "from_first_hit",
        }
        changed = planmod.finalize_plan(changed)
        baseline_plan = json.loads(json.dumps(plan))
        baseline_plan["schedule"]["windows_s"] = [30.0]
        baseline_plan = planmod.finalize_plan(baseline_plan)
        baseline30 = plan_executor.execute_plan(baseline_plan, default_scenario(baseline_plan))
        result = plan_executor.execute_plan(changed, default_scenario(changed))
        count = result["windows"][0]["casts"]
        expected = result["single_hit"] * (count + (count // 2) * 0.5)
        self.assertAlmostEqual(result["windows"][0]["hit_damage"], expected)
        self.assertNotEqual(result["windows"][0]["hit_damage"], baseline30["windows"][0]["hit_damage"])

        # 注入镜像字段（序列化后）必须被拒绝，而不是被忽略
        mirrored = json.loads(json.dumps(plan))
        mirrored["parameters"]["every_nth_hit"] = {"n": 2, "extra_more": 0.5}
        mirrored = planmod.finalize_plan(mirrored)
        codes = {issue["code"] for issue in planmod.validate_plan_structure(mirrored)}
        self.assertIn("authority_conflict", codes)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(mirrored, default_scenario(plan))

    def test_sources_are_execution_constraint(self):
        plan = json.loads(json.dumps(self.real_compiled().plan))
        cleared = json.loads(json.dumps(plan))
        cleared["sources"] = []
        cleared = planmod.finalize_plan(cleared)
        codes = {issue["code"] for issue in planmod.validate_plan_structure(cleared)}
        self.assertIn("missing_sources", codes)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(cleared, default_scenario(plan))

        effect = next(item for item in plan["effects"] if item["emits_contribution"])
        ref = effect["source_refs"][0]
        removed_ref = json.loads(json.dumps(plan))
        removed_ref["effects"][0]["source_refs"] = []
        removed_ref = planmod.finalize_plan(removed_ref)
        self.assertTrue(
            any(
                issue["code"] == "missing_source_ref"
                for issue in planmod.validate_plan_structure(removed_ref)
            )
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(removed_ref, default_scenario(plan))

        removed_source = json.loads(json.dumps(plan))
        removed_source["sources"] = [
            source for source in removed_source["sources"] if source["source_id"] != ref
        ]
        removed_source = planmod.finalize_plan(removed_source)
        self.assertTrue(
            any(
                issue["code"] == "unknown_source_ref"
                for issue in planmod.validate_plan_structure(removed_source)
            )
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(removed_source, default_scenario(plan))

    def test_coverage_declarations_need_identity_evidence_premise(self):
        normal = self.real_compiled().plan
        decisions = normal["facts"]["coverage_decisions"]
        self.assertTrue(decisions)
        for decision in decisions:
            if not decision["applied"]:
                continue
            identity = decision["identity"]
            self.assertTrue(identity.get("definition_entity_id") or identity.get("base_entity_id"))
            self.assertTrue(decision["evidence"])
            self.assertEqual(decision["premise"]["satisfied"], True)
            self.assertIn(decision["status"], ("verified", "assumed"))

        profile = load_json(PROFILE_PATH)
        mutated = copy.deepcopy(profile)
        mutated["coverage"]["not_applicable_mods"] = [
            {"mod_id": "equipment.waist#unique_mod#1"}
        ]
        context = plan_compiler.prepare_context(CASE_PATH, ROOT)
        try:
            compiled = plan_compiler.compile_from_profile_v2(context, mutated)
        finally:
            context.close()
        self.assertTrue(compiled.ok, compiled.diagnostics)
        gaps = compiled.plan["facts"]["coverage_base_gaps"]
        self.assertEqual(len(gaps), 7, "仅凭 mod_id 的声明不得消除缺口")
        self.assertTrue(
            any("Melee Damage while you have Frenzy" in gap["detail"] for gap in gaps)
        )
        rejected = [
            decision
            for decision in compiled.plan["facts"]["coverage_decisions"]
            if decision["mod_id"] == "equipment.waist#unique_mod#1"
        ]
        self.assertTrue(rejected)
        self.assertFalse(rejected[0]["applied"])
        self.assertEqual(rejected[0]["status"], "rejected")
        self.assertTrue(
            any(item["code"] == "coverage_decision_rejected" for item in compiled.diagnostics)
        )

    def test_decision_evidence_tamper_rejected(self):
        plan = json.loads(json.dumps(self.real_compiled().plan))
        decision = next(item for item in plan["facts"]["coverage_decisions"] if item["applied"])
        decision["evidence"] = []
        plan = planmod.finalize_plan(plan)
        self.assertTrue(
            any(
                issue["code"] == "missing_decision_evidence"
                for issue in planmod.validate_plan_structure(plan)
            )
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(plan, default_scenario(plan))

    def test_dependency_cycle_after_roundtrip_rejected(self):
        plan = json.loads(json.dumps(self.real_compiled().plan))
        plan["evaluation"]["dependencies"] = [
            {"from": "projection", "to": "baseline"},
            {"from": "baseline", "to": "projection"},
        ]
        plan = planmod.finalize_plan(plan)
        self.assertIn(
            "invalid_dependencies",
            {issue["code"] for issue in planmod.validate_plan_structure(plan)},
        )
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(plan, default_scenario(plan))


class _SyntheticCatalog:
    """最小目录假体：只覆盖 profile-v2 编译路径用到的读取。"""

    def dataset_id(self):
        return "synthetic"

    def one(self, sql, params=()):
        if "FROM classes" in sql:
            return {
                "extensions": json.dumps(
                    {
                        "characterClass": {
                            "baseStrength": 10,
                            "baseVitality": 10,
                            "baseIntelligence": 10,
                            "baseDexterity": 10,
                            "baseAttunement": 10,
                        }
                    }
                )
            }
        return None

    def source_record(self, *args, **kwargs):
        return None

    def passive_bonus_payload(self, tree_id):
        return {"stats": {}}

    def nodes_for_tree(self, tree_id):
        return []

    def trees(self):
        return [
            {
                "source_tree_id": "passive",
                "entity_id": "tree:synthetic:passive",
                "name": "Synthetic Passive",
            }
        ]

    def ailment_by_name(self, name):
        if name == "Bleed":
            return {"damage_type": "Physical", "base_damage": 100.0, "duration_ms": 3000}
        raise KeyError(name)

    def skill_by_name(self, name):
        return {
            "entity_id": "skill:synthetic",
            "name": name,
            "cast_time_ms": 1000,
            "mana_cost": 10,
            "extensions": {"attributeScaling": {}},
        }

    def skill_components(self, entity_id):
        return [
            {
                "component_type": "DamageEnemyOnHit",
                "base_damage": 50.0,
                "damage_type": "Physical",
                "damageTags": 0,
                "config": {
                    "baseDamageStats": {
                        "addedDamageScaling": 0.0,
                        "critChance": 0.0,
                        "critMultiplier": 1.0,
                    }
                },
            }
        ]

    def entity(self, kind, entity_id):
        return None


def _synthetic_profile() -> dict:
    return {
        "schema": "le-dps-profile-v2",
        "ability_id": "synthetic:melee-bleed",
        "skill": {
            "base_damage": {"Physical": 50.0},
            "added_damage_scaling": 0.0,
            "crit_chance": 0.0,
            "crit_multiplier": 1.0,
            "cooldown_s": 1.0,
            "action_s": 1.0,
            "components": {
                "hit": {"tags": ["Melee", "Hit"], "crit_capable": False},
                "dot": {"tags": ["DoT"]},
            },
        },
        "controls": [
            {
                "key": "synthetic_toggle",
                "label": "合成观察开关",
                "source": "manual",
                "type": "boolean",
                "default": False,
                "readonly": False,
                "effect": "合成夹具",
            }
        ],
        "effects": [
            {
                "id": "synthetic-dot-increased",
                "label": "合成 DoT 增加 20%",
                "stat": "damage",
                "operation": "increased",
                "value": 0.2,
                "unit": "percent",
                "tags": ["DoT"],
                "source": {"kind": "scenario_assumption"},
                "states": [],
                "status": "assumed",
                "required": True,
            },
            {
                "id": "synthetic-melee-increased",
                "label": "合成近战增加 30%",
                "stat": "damage",
                "operation": "increased",
                "value": 0.3,
                "unit": "percent",
                "tags": ["Melee"],
                "source": {"kind": "scenario_assumption"},
                "states": [],
                "status": "assumed",
                "required": True,
            },
        ],
        "ailments": [
            {
                "name": "Bleed",
                "label_zh": "流血",
                "ailment_id": 2,
                "damage_type": "Physical",
                "duration_s": 3.0,
                "base_damage": 100.0,
                "chance": {
                    "source_kinds": ["equipment"],
                    "excluded_tags": [],
                    "additional": [],
                },
            }
        ],
        "rules": {"crit": {}},
        "limitations": [],
    }


def _synthetic_calc() -> dict:
    return {
        "dataset_id": "synthetic",
        "character": {
            "name": "Synthetic",
            "level": 1,
            "class": {"entity_id": "synthetic:class", "name": "Synthetic"},
            "mastery": {"name": "Synthetic"},
        },
        "items": [],
        "mods": [],
        "skills": {
            "specializations": [
                {
                    "tree": {
                        "entity_id": "tree:synthetic:synthetic:melee-bleed",
                        "name": "Synthetic Melee",
                        "source_tree_id": "synthetic-skill",
                    },
                    "owner_skill": {"name": "Synthetic Melee"},
                    "nodes": [],
                }
            ]
        },
        "passive_tree": {
            "tree": {"entity_id": "tree:synthetic:passive", "name": "Synthetic Passive"},
            "nodes": [],
        },
    }


class B12ProfileVsNativeTest(unittest.TestCase):
    def test_profile_and_native_adapters_share_core_result(self):
        profile = _synthetic_profile()
        calc = _synthetic_calc()
        context = plan_compiler.FrozenContext(
            root=ROOT,
            case={
                "case_id": "synthetic-profile",
                "main_skill": {"ability_id": "synthetic:melee-bleed"},
                "inputs": {"calculation_input": "synthetic"},
            },
            calc=calc,
            dataset={},
            profile=profile,
            catalog=_SyntheticCatalog(),
            input_refs=[
                {
                    "role": "profile_content",
                    "path": None,
                    "sha256": planmod.sha256_text(planmod.canonical_json(profile)),
                }
            ],
            synthetic=True,
        )
        profile_plan = plan_compiler.compile_from_profile_v2(context, profile)
        self.assertTrue(profile_plan.ok, profile_plan.diagnostics)
        profile_result = plan_executor.execute_plan(
            profile_plan.plan, native_scenario(profile_plan.plan)
        )
        native = compile_native(FIXTURES / "synthetic-melee-hit-with-bleed.json")
        native_result = plan_executor.execute_plan(native.plan, native_scenario(native.plan))
        self.assertEqual(profile_result["single_hit"], native_result["single_hit"])
        profile_bleed = next(item for item in profile_result["ailments"] if item["name"] == "Bleed")
        native_bleed = next(item for item in native_result["ailments"] if item["name"] == "Bleed")
        self.assertAlmostEqual(profile_bleed["stack_damage"], native_bleed["stack_damage"])
        self.assertEqual(
            profile_plan.plan["executor_requirements"]["handlers"],
            native.plan["executor_requirements"]["handlers"],
        )
        # 语义核心一致；来源/provenance 不同导致 plan_id 不同
        self.assertEqual(profile_plan.plan["schema"], native.plan["schema"])
        self.assertNotEqual(profile_plan.plan["plan_id"], native.plan["plan_id"])


class B13Step1ParityTest(PlanFixtureMixin, unittest.TestCase):
    def test_numbers_ledger_and_coverage_identical_to_step1_run(self):
        if not STEP1_RESULT.is_file():
            self.skipTest("缺少第一步 run-3 结果")
        old = load_json(STEP1_RESULT)
        new = self.real_result()
        for key in (
            "sustained_dps",
            "single_hit",
            "hit_dps",
            "dot_dps",
            "interval_s",
            "cooldown_reduction",
            "attack_speed_increased",
            "single_cast_with_tail",
        ):
            self.assertEqual(new[key], old[key], key)
        self.assertEqual(new["applications"], old["applications"])
        self.assertEqual(new["coverage"]["gaps"], old["coverage"]["gaps"])
        self.assertEqual(new["coverage"]["status"], old["coverage"]["status"])
        self.assertEqual(len(new["contributions"]), len(old["contributions"]))
        self.assertEqual(new["contributions"], old["contributions"])
        self.assertEqual(new["issues"], old["issues"])
        self.assertEqual(new["effect_ledger"], old["effect_ledger"])
        self.assertEqual(new["components"], old["components"])
        self.assertEqual(new["ailments"], old["ailments"])


class B14RunAndRecomputeTest(PlanFixtureMixin, unittest.TestCase):
    def test_plan_bound_run_and_same_entry_recompute(self):
        case = load_json(CASE_PATH)
        profile = load_json(PROFILE_PATH)
        scratch = ROOT / ".scratch/unified-dps-step2"
        scratch.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=scratch, prefix="run-") as temp_dir:
            out = Path(temp_dir) / "run"
            result = dps.run(ROOT, case, out, port=8799)
            manifest = load_json(out / "run-manifest.json")
            plan = load_json(out / "calculation-plan.json")
            self.assertEqual(manifest["plan_sha256"], plan["plan_sha256"])
            self.assertEqual(manifest["scenario_sha256"], result["scenario_sha256"])
            self.assertEqual(result["plan_ref"]["plan_sha256"], plan["plan_sha256"])
            self.assertEqual(manifest["coverage_status"], "partial")
            self.assertTrue((out / "plan-diagnostics.json").is_file())
            binding = load_json(out / "report-binding.json")
            self.assertEqual(binding["input_fingerprints"], dps.dependency_fingerprints(ROOT, case, profile))
            recomputed = dps.calculate(
                ROOT, case, profile, dps.defaults(profile)["states"], dps.defaults(profile)["target"]
            )
            for key in ("sustained_dps", "single_hit", "dot_dps"):
                self.assertEqual(recomputed[key], result[key])
            self.assertEqual(recomputed["plan_sha256"], result["plan_sha256"])
            self.assertEqual(recomputed["execution_fingerprint"], result["execution_fingerprint"])
            self.assertEqual(recomputed["coverage"]["gaps"], result["coverage"]["gaps"])
            self.assertEqual(
                load_json(out / "result.json")["plan_sha256"], plan["plan_sha256"]
            )


class B15TamperTest(PlanFixtureMixin, unittest.TestCase):
    def test_tampered_plan_content_rejected_or_coverage_explicit(self):
        plan = self.real_compiled().plan
        tampered = copy.deepcopy(plan)
        tampered["facts"]["coverage_base_gaps"] = []
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(tampered, default_scenario(plan))
        # 重算哈希后，非法结构仍被拒绝
        tampered = planmod.finalize_plan(tampered)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(tampered, default_scenario(plan))
        broken = copy.deepcopy(plan)
        broken["effects"][0]["value_mode"] = "future_mode"
        broken = planmod.finalize_plan(broken)
        with self.assertRaises(plan_executor.PlanValidationError):
            plan_executor.execute_plan(broken, default_scenario(plan))


if __name__ == "__main__":
    unittest.main(verbosity=2)
