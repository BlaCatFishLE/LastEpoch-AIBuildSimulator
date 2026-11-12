"""task-5（P1-B1 / P1-B2 / P1-D / P2）反例闭合测试。

覆盖：
1. 真实案例编译计划不再有编译期 crit 吸收/覆盖（crit 基础值 5% / 200%）；
2. 当前输入把 ex4tp 节点 30 points 改成 0 → 编译拒绝（不得仍 0.8/s，不得用场景点数补回）；
3. 当前输入移除 ex4tp 专精 → 编译拒绝；
4. actor.source_entity 指向不存在的 minion → 编译拒绝（ActorCompileError）；
5. dataset 来源 base_effect 值改为 0.9（dataset 无此项）→ 编译拒绝；
6. 条件不满足/标签不匹配/未 included 的合成 actor crit 行不影响执行期 crit；
7. 场景 control 越界（trap_detonation_rate_per_s=0 < template min=1）→ 绑定期明确拒绝；
8. 运行期只用编译后的 gate probability，场景 points 不能补回真实节点。

全部变异只在内存中发生；不写 reports/examples/knowledge。
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import (  # noqa: E402
    calculation_route,
    definitions_v3,
    dps_components,
    model,
    plan_compiler,
    plan_compiler_actor,
    plan_v4,
)

CASE_PATH = "examples/ballista-beta5-native-20261112/case.json"
SCENARIO_PATH = "examples/ballista-beta5-native-20261112/inputs/scenario-armed-construction-v4.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_case() -> dict:
    path = ROOT / CASE_PATH
    case = json.loads(path.read_text(encoding="utf-8"))
    case["_path"] = CASE_PATH
    return case


class RealCaseActorBindingTest(unittest.TestCase):
    """基于当前真实案例（只读文件 + 内存 FrozenContext/ResolvedModel）。"""

    @classmethod
    def setUpClass(cls):
        case = _load_case()
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        cls.case = case
        cls.context, inputs, diagnostics = calculation_route.load_compilation_inputs(
            route, root=ROOT, case=case
        )
        assert cls.context is not None, diagnostics
        cls.inputs = inputs
        cls.template = inputs["template"]
        cls.scenario = json.loads((ROOT / SCENARIO_PATH).read_text(encoding="utf-8"))
        cls.base_result = plan_compiler.compile_from_resolved_model(
            cls.context, copy.deepcopy(inputs["model"])
        )
        assert cls.base_result.plan is not None, cls.base_result.diagnostics

    @classmethod
    def tearDownClass(cls):
        cls.context.close()

    # -- helpers ---------------------------------------------------------
    def _compile_with_calc(self, calc: dict):
        context = plan_compiler.FrozenContext(
            root=self.context.root,
            case=self.context.case,
            calc=calc,
            dataset=self.context.dataset,
            profile=self.context.profile,
            catalog=self.context.catalog,
            input_refs=self.context.input_refs,
            synthetic=self.context.synthetic,
        )
        return plan_compiler.compile_from_resolved_model(
            context, copy.deepcopy(self.inputs["model"])
        )

    def _compile_with_actor(self, actor_mutator):
        model_copy = copy.deepcopy(self.inputs["model"])
        actor_mutator(model_copy.definition["actor"])
        return plan_compiler.compile_from_resolved_model(self.context, model_copy)

    def _calc_with_node30_points(self, points: int) -> dict:
        calc = copy.deepcopy(self.context.calc)
        changed = False
        for spec in (calc.get("skills") or {}).get("specializations") or []:
            tree = str((spec.get("tree") or {}).get("entity_id") or "")
            if not tree.endswith(":ex4tp"):
                continue
            for node in spec.get("nodes") or []:
                if str(node.get("node_id")) == "30":
                    node["points"] = points
                    changed = True
        self.assertTrue(changed, "真实输入应包含 ex4tp 节点 30")
        return calc

    # -- P1-D ------------------------------------------------------------
    def test_plan_has_no_compile_time_crit_absorption(self):
        plan = self.base_result.plan
        serialized = json.dumps(plan, ensure_ascii=False)
        self.assertNotIn("crit_absorbed", serialized)
        self.assertNotIn("crit_override", serialized)
        execution_v1 = (plan.get("execution") or {}).get("execution_v1") or {}
        actor_model = (execution_v1.get("facts") or {}).get("actor_model") or {}
        self.assertNotIn("crit_absorbed", actor_model)
        self.assertEqual(actor_model.get("crit_policy"), "execution_time_pool")
        components = execution_v1.get("components") or []
        self.assertTrue(components)
        for component in components:
            self.assertAlmostEqual(float(component.get("crit_chance") or 0.0), 0.05, delta=1e-12)
            self.assertAlmostEqual(
                float(component.get("crit_multiplier") or 0.0), 2.0, delta=1e-12
            )
        # crit 行仍是普通 effect（执行期 crit 池消费），不带编译期吸收标记
        crit_effects = [
            effect
            for effect in execution_v1.get("effects") or []
            if effect.get("stat_key") in ("crit_chance", "crit_multiplier")
        ]
        self.assertTrue(crit_effects)
        self.assertFalse(any(effect.get("crit_absorbed") for effect in crit_effects))
        result = plan_v4.execute_plan_v4(copy.deepcopy(plan))
        self.assertAlmostEqual(
            float(result["frequency"]["event_rate_per_s"]), 0.8, delta=1e-12
        )
        # 执行期 crit 池按行结算，真实结果仍使用聚合后的暴击期望（不是 0.05/2.0 裸值）
        detail_components = (result.get("report_details") or {}).get("components") or []
        self.assertTrue(detail_components)
        self.assertGreater(float(detail_components[0]["crit_chance"]), 0.05)

    def test_tampered_actor_crit_effect_changes_numbers(self):
        """复核 §5 反例 2：改 crit effect 值不能再只改账本、不改数值（无编译期镜像）。"""
        from le_calc import calculation_plan as planmod

        plan = copy.deepcopy(self.base_result.plan)
        baseline = plan_v4.execute_plan_v4(copy.deepcopy(plan))
        execution_v1 = plan["execution"]["execution_v1"]
        target = next(
            effect
            for effect in execution_v1["effects"]
            if effect.get("stat_key") in ("crit_chance", "crit_multiplier")
            and effect.get("emits_contribution")
            and str(effect.get("effect_id", "")).startswith("actor:")
            and str((effect.get("raw") or {}).get("scope") or "") == "actor"
            and effect.get("included_static")
        )
        old_value = float(target["value"])
        target["value"] = old_value + 0.5
        # 按 plan_v4.compile_from_resolved_model 的同一顺序重算哈希绑定
        plan["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(plan["execution"]))
        plan["scenario_sha256"] = planmod.scenario_sha256(plan["scenario"])
        plan["execution_fingerprint"] = plan_v4._sha256_text(
            plan_v4._canonical({"plan": plan, "scenario_sha256": plan["scenario_sha256"]})
        )
        plan["plan_sha256"] = plan_v4._sha256_text(
            plan_v4._canonical({key: value for key, value in plan.items() if key != "plan_sha256"})
        )
        plan_v4.validate_plan_v4(plan)
        tampered = plan_v4.execute_plan_v4(plan)
        self.assertNotEqual(
            float(tampered["totals"]["hit_damage"]),
            float(baseline["totals"]["hit_damage"]),
            "actor crit effect 值必须参与执行期结算，不能留下只改账本的镜像",
        )

    # -- P1-B1 -----------------------------------------------------------
    def test_zero_node_points_rejected_not_restored_by_scenario(self):
        calc = self._calc_with_node30_points(0)
        # 最高层编译链必须拒绝（ActorCompileError 被 PlanCompileError 包装）
        with self.assertRaises(plan_compiler.PlanCompileError) as caught:
            self._compile_with_calc(calc)
        message = str(caught.exception)
        self.assertIn("ex4tp", message)
        self.assertIn("点数=0", message)
        self.assertIn("拒绝以场景点数补回", message)
        # actor 层直接抛 ActorCompileError
        definition = copy.deepcopy(self.inputs["model"].definition)
        with self.assertRaises(plan_compiler_actor.ActorCompileError):
            plan_compiler_actor.compile_actor_model(
                rows=[],
                resolved_model=object(),
                definition=definition,
                catalog=self.context.catalog,
                calc=calc,
            )

    def test_frequency_gate_requires_calc_and_catalog(self):
        frequency = copy.deepcopy(self.inputs["model"].definition["frequency"])
        with self.assertRaises(plan_compiler_actor.ActorCompileError) as caught:
            plan_compiler_actor.resolve_derived_frequency(frequency, catalog=None, calc=None)
        message = str(caught.exception)
        self.assertIn("calc/catalog", message)
        self.assertIn("不能留给运行期", message)
        # 非 skill_node 来源不要求 calc/catalog（不扩展到其它 gate 语义）
        static_frequency = copy.deepcopy(frequency)
        static_frequency["derivation"]["gate"]["source"] = {"kind": "static", "probability": 0.3}
        resolved = plan_compiler_actor.resolve_derived_frequency(
            static_frequency, catalog=None, calc=None
        )
        self.assertEqual(
            resolved["derivation"]["gate"]["source"]["kind"], "static"
        )

    def test_missing_specialization_rejected(self):
        calc = copy.deepcopy(self.context.calc)
        specs = (calc.get("skills") or {}).get("specializations") or []
        before = len(specs)
        calc["skills"]["specializations"] = [
            spec
            for spec in specs
            if not str((spec.get("tree") or {}).get("entity_id") or "").endswith(":ex4tp")
        ]
        self.assertLess(len(calc["skills"]["specializations"]), before)
        with self.assertRaises(plan_compiler.PlanCompileError) as caught:
            self._compile_with_calc(calc)
        message = str(caught.exception)
        self.assertIn("ex4tp", message)
        self.assertIn("没有", message)
        self.assertIn("专精", message)

    def test_runtime_gate_uses_compiled_probability_only(self):
        plan = self.base_result.plan
        gate = (
            ((plan["execution"]["execution_v1"]["frequency"].get("derivation") or {}).get("gate"))
            or {}
        )
        self.assertAlmostEqual(float(gate.get("probability")), 0.2, delta=1e-12)
        self.assertEqual(int(gate.get("points")), 2)
        self.assertNotIn("points_control", gate)
        self.assertNotIn("per_point", gate)
        self.assertEqual(str(gate.get("source", {}).get("kind")), "skill_node")

        # 上游场景控件影响事件率；场景 points 不能改变 gate（编译后无 control 口径）
        low = plan_v4.execute_plan_v4(
            copy.deepcopy(plan),
            {"states": {"trap_detonation_rate_per_s": 4.0}, "target": {}},
        )
        high = plan_v4.execute_plan_v4(
            copy.deepcopy(plan),
            {"states": {"trap_detonation_rate_per_s": 10.0}, "target": {}},
        )
        self.assertAlmostEqual(float(low["frequency"]["event_rate_per_s"]), 0.8, delta=1e-12)
        self.assertAlmostEqual(float(high["frequency"]["event_rate_per_s"]), 2.0, delta=1e-12)
        with_points = plan_v4.execute_plan_v4(
            copy.deepcopy(plan),
            {
                "states": {
                    "trap_detonation_rate_per_s": 4.0,
                    "points_siege_engineering": 99,
                },
                "target": {},
            },
        )
        self.assertAlmostEqual(
            float(with_points["frequency"]["event_rate_per_s"]), 0.8, delta=1e-12
        )

    # -- P1-B2 -----------------------------------------------------------
    def test_actor_source_entity_must_exist(self):
        def mutate(actor):
            actor["source_entity"] = "minion:nonexistent:999999"

        with self.assertRaises(plan_compiler.PlanCompileError) as caught:
            self._compile_with_actor(mutate)
        self.assertIn("minion:nonexistent:999999", str(caught.exception))
        self.assertIn("不存在", str(caught.exception))
        # actor 层直接抛 ActorCompileError
        definition = copy.deepcopy(self.inputs["model"].definition)
        mutate(definition["actor"])
        with self.assertRaises(plan_compiler_actor.ActorCompileError):
            plan_compiler_actor.compile_actor_model(
                rows=[],
                resolved_model=object(),
                definition=definition,
                catalog=self.context.catalog,
                calc=copy.deepcopy(self.context.calc),
            )

    def test_actor_base_effect_must_match_dataset(self):
        def mutate(actor):
            actor["base_effects"][0]["value"] = 0.9

        with self.assertRaises(plan_compiler.PlanCompileError) as caught:
            self._compile_with_actor(mutate)
        message = str(caught.exception)
        self.assertIn("base_effects", message)
        self.assertIn("base stats", message)
        definition = copy.deepcopy(self.inputs["model"].definition)
        mutate(definition["actor"])
        with self.assertRaises(plan_compiler_actor.ActorCompileError):
            plan_compiler_actor.compile_actor_model(
                rows=[],
                resolved_model=object(),
                definition=definition,
                catalog=self.context.catalog,
                calc=copy.deepcopy(self.context.calc),
            )

    def test_actor_id_and_source_entity_must_agree(self):
        def mutate(actor):
            actor["actor_id"] = "minion:letools:LEAbilities.entityList:100054"

        with self.assertRaises(plan_compiler.PlanCompileError) as caught:
            self._compile_with_actor(mutate)
        self.assertIn("actor_id", str(caught.exception))

    # -- P1-D 执行期 crit 池反例 ------------------------------------------
    def test_unsatisfied_synthetic_crit_row_does_not_affect_execution(self):
        skill = {
            "base_damage": {"Fire": 50.0},
            "added_damage_scaling": 2.5,
            "crit_chance": 0.05,
            "crit_multiplier": 2.0,
        }
        component = {"tags": ["Bow", "Fire"], "adaptive_damage_type": "Fire", "crit_capable": True}

        def assemble(contributions):
            components, _applications = dps_components.build_hit_components(
                contributions=contributions,
                skill=skill,
                component=component,
                crit_rules={},
                target={"resistance": 0.0},
                states={},
                target_info={"monster_damage_reduction": 0.0},
                damage_types=["Fire"],
                unit_scope="actor",
            )
            self.assertEqual(len(components), 1)
            return components[0]

        baseline = assemble([])
        self.assertEqual(float(baseline["crit_chance"]), 0.05)
        self.assertEqual(float(baseline["crit_multiplier"]), 2.0)

        # 1) included=False、条件不满足、tags=[Spell]、crit_multiplier added=5
        excluded_row = model.Contribution(
            contribution_id="actor:test:unsatisfied-crit",
            source_kind="derived",
            source_label="synthetic unsatisfied crit",
            source_path="test",
            stat_name="crit_multiplier",
            stat_key="crit_multiplier",
            operation="added",
            value=5.0,
            damage_tags=["Spell"],
            group="actor:test:unsatisfied",
            included=False,
            condition_status=model.CONDITION_NOT_SATISFIED,
            raw={"scope": "actor"},
        )
        with_excluded = assemble([excluded_row])
        self.assertEqual(float(with_excluded["crit_multiplier"]), 2.0)
        self.assertEqual(float(with_excluded["crit_chance"]), 0.05)
        self.assertAlmostEqual(
            float(with_excluded["damage"]), float(baseline["damage"]), delta=1e-12
        )

        # 2) included/条件满足但标签不匹配（Spell 不进入 Fire 组件闭包）→ 仍不参与
        tag_mismatch = model.Contribution(
            contribution_id="actor:test:spell-crit",
            source_kind="derived",
            source_label="synthetic spell crit",
            source_path="test",
            stat_name="crit_multiplier",
            stat_key="crit_multiplier",
            operation="added",
            value=5.0,
            damage_tags=["Spell"],
            group="actor:test:spell",
            included=True,
            condition_status=model.CONDITION_ALWAYS,
            raw={"scope": "actor"},
        )
        with_tag_mismatch = assemble([tag_mismatch])
        self.assertEqual(float(with_tag_mismatch["crit_multiplier"]), 2.0)
        self.assertAlmostEqual(
            float(with_tag_mismatch["damage"]), float(baseline["damage"]), delta=1e-12
        )

        # 3) 对照组：满足 included/条件/标签的 crit 行必须参与（证明 crit 池仍生效）
        applied_row = model.Contribution(
            contribution_id="actor:test:applied-crit",
            source_kind="derived",
            source_label="synthetic applied crit",
            source_path="test",
            stat_name="crit_multiplier",
            stat_key="crit_multiplier",
            operation="added",
            value=1.0,
            damage_tags=["Fire"],
            group="actor:test:applied",
            included=True,
            condition_status=model.CONDITION_ALWAYS,
            raw={"scope": "actor"},
        )
        with_applied = assemble([applied_row])
        self.assertEqual(float(with_applied["crit_multiplier"]), 3.0)
        self.assertGreater(float(with_applied["damage"]), float(baseline["damage"]))


class ScenarioControlRangeTest(unittest.TestCase):
    """P2：场景 control 数值按模板 control min/max 在绑定期拒绝。"""

    @classmethod
    def setUpClass(cls):
        case = _load_case()
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        cls.context, inputs, diagnostics = calculation_route.load_compilation_inputs(
            route, root=ROOT, case=case
        )
        assert cls.context is not None, diagnostics
        cls.template = inputs["template"]
        cls.scenario = json.loads((ROOT / SCENARIO_PATH).read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.context.close()

    def _bind(self, scenario):
        return definitions_v3.bind_template(
            self.template, context=self.context, scenario=scenario
        )

    def test_out_of_range_integer_control_rejected_at_binding(self):
        scenario = copy.deepcopy(self.scenario)
        scenario["controls"]["trap_detonation_rate_per_s"] = 0  # 模板 min=1
        with self.assertRaises(definitions_v3.DefinitionBindingError) as caught:
            self._bind(scenario)
        message = str(caught.exception)
        self.assertIn("trap_detonation_rate_per_s", message)
        self.assertIn("越界", message)
        self.assertIn("[1, 30]", message)

    def test_out_of_range_integer_control_above_max_rejected(self):
        scenario = copy.deepcopy(self.scenario)
        scenario["controls"]["trap_detonation_rate_per_s"] = 31  # 模板 max=30
        with self.assertRaises(definitions_v3.DefinitionBindingError) as caught:
            self._bind(scenario)
        message = str(caught.exception)
        self.assertIn("trap_detonation_rate_per_s", message)
        self.assertIn("[1, 30]", message)

    def test_wrong_control_type_rejected_at_binding(self):
        scenario = copy.deepcopy(self.scenario)
        scenario["controls"]["trap_detonation_rate_per_s"] = "4"
        with self.assertRaises(definitions_v3.DefinitionBindingError) as caught:
            self._bind(scenario)
        self.assertIn("trap_detonation_rate_per_s", str(caught.exception))
        scenario = copy.deepcopy(self.scenario)
        scenario["controls"]["elixir"] = 1  # boolean control 必须是 bool
        with self.assertRaises(definitions_v3.DefinitionBindingError) as caught:
            self._bind(scenario)
        self.assertIn("elixir", str(caught.exception))

    def test_valid_scenario_still_binds(self):
        resolved = self._bind(copy.deepcopy(self.scenario))
        probability = float(
            resolved.definition["frequency"]["derivation"]["gate"]["probability"]
        )
        self.assertAlmostEqual(probability, 0.2, delta=1e-12)


if __name__ == "__main__":
    unittest.main()
