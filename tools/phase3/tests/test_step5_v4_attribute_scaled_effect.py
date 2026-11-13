"""task-7：通用 ``per_attribute`` 缩放 effect + ``unique_description`` 转移来源。

覆盖边界：

1. ``per_attribute`` 声明契约：属性枚举 / ``per``（缺省 1、>0）/ 未知字段 / 缺 attribute；
2. ``bind_profile_effects`` 只保留声明与来源事实，不提前乘属性值；
3. ``plan_compiler`` 在属性池就绪后换算 value = 声明值 × 属性值 / per，并写审计事实；
4. 属性缺失或为 0 时不静默按 0 折算：effect 转为 unresolved，reason 明确；
5. ``actor_pool.DEFAULT_TRANSFER_SOURCE_KINDS`` 纳入 ``unique_description``（玩家来源），
   显式 ``include_source_kinds`` 仍可排除它，非玩家来源（skill_node）仍被排除；
6. unique_description 来源沿用既有稳定锚点校验（不放宽）；
7. 真实 Ballista 案例端到端：玩家行 1×Dex=228，Shared Enhancements 0.75 转移=171，
   Cold 通道 added 效率 250% → 该行等效 427.5（合成定义路径与真实案例路径都断言）。

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
    actor_pool,
    calculation_route,
    dps_result,
    effect_binding as binding,
    model,
    plan_compiler,
    plan_v4,
)

CASE_PATH = "examples/ballista-beta5-native-20261112/case.json"
UNIQUE_SOURCE = {
    "kind": "unique_description",
    "item_instance_id": "equipment.feet",
    "description_index": 0,
    "expected_text_contains": "cold damage to attacks and spells per point of dexterity",
    "expected_definition_entity_id": "item:letools:uniqueList.uniques:19",
}
EFFECT_ID = "synthetic_mourningfrost_cold_per_dexterity"


def _synthetic_effect(per_attribute=None) -> dict:
    effect = {
        "id": EFFECT_ID,
        "label": "合成 Mourningfrost：每点敏捷 +1 冰霜伤害（Bow 分支）",
        "source": copy.deepcopy(UNIQUE_SOURCE),
        "stat": "added_damage",
        "operation": "added",
        "value": 1.0,
        "value_source": "declared",
        "tags": ["Bow", "Cold"],
        "scope": "owner",
        "source_kind": "unique_description",
        "status": "derived",
        "required": False,
    }
    if per_attribute is not None:
        effect["per_attribute"] = per_attribute
    return effect


def _definition_payload(effect: dict) -> dict:
    """最小合法 plan-v1 definition，只用于触发 effects 契约校验。"""
    return {
        "plan_version": "le-calculation-plan-v1",
        "controls": [{"key": "dummy", "type": "boolean", "default": 0}],
        "skill": {
            "components": {
                "hit": {"tags": ["Bow"]},
                "dot": {"tags": ["Fire"]},
            }
        },
        "effects": [effect],
        "rules": {},
    }


def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()


class PerAttributeContractTest(unittest.TestCase):
    """声明契约：结构非法/未知属性/per<=0 在定义校验与绑定两处都明确拒绝。"""

    def test_valid_declaration_normalizes_per_default(self):
        self.assertEqual(
            binding.validate_per_attribute(
                {"attribute": "dexterity"}, effect_id="x"
            ),
            {"attribute": "dexterity", "per": 1.0},
        )
        self.assertEqual(
            binding.validate_per_attribute(
                {"attribute": "strength", "per": 2}, effect_id="x"
            ),
            {"attribute": "strength", "per": 2.0},
        )
        self.assertIsNone(binding.validate_per_attribute(None, effect_id="x"))

    def test_definition_contract_rejects_nonpositive_per(self):
        for per in (0, 0.0, -1, -0.5):
            effect = _synthetic_effect({"attribute": "dexterity", "per": per})
            with self.assertRaises(binding.BindingError) as caught:
                binding.validate_definition_contract(_definition_payload(effect))
            message = str(caught.exception)
            self.assertIn(EFFECT_ID, message)
            self.assertIn("per_attribute.per", message)
            self.assertIn("大于 0", message)

    def test_definition_contract_rejects_unknown_attribute(self):
        effect = _synthetic_effect({"attribute": "luck", "per": 1.0})
        with self.assertRaises(binding.BindingError) as caught:
            binding.validate_definition_contract(_definition_payload(effect))
        message = str(caught.exception)
        self.assertIn(EFFECT_ID, message)
        self.assertIn("per_attribute.attribute", message)
        self.assertIn("不受支持", message)

    def test_definition_contract_rejects_missing_attribute(self):
        effect = _synthetic_effect({"per": 1.0})
        with self.assertRaises(binding.BindingError) as caught:
            binding.validate_definition_contract(_definition_payload(effect))
        message = str(caught.exception)
        self.assertIn(EFFECT_ID, message)
        self.assertIn("缺少 attribute", message)

    def test_definition_contract_rejects_unknown_per_attribute_field(self):
        effect = _synthetic_effect({"attribute": "dexterity", "per": 1.0, "scale": 2})
        with self.assertRaises(binding.BindingError) as caught:
            binding.validate_definition_contract(_definition_payload(effect))
        self.assertIn("未知字段", str(caught.exception))

    def test_unique_description_anchors_not_relaxed(self):
        """per_attribute 不放宽 stable 锚点：缺 expected_definition_entity_id 仍拒绝。"""
        effect = _synthetic_effect({"attribute": "dexterity", "per": 1.0})
        effect["source"].pop("expected_definition_entity_id")
        with self.assertRaises(binding.BindingError) as caught:
            binding.validate_definition_contract(_definition_payload(effect))
        self.assertIn("expected_definition_entity_id", str(caught.exception))


class PerAttributeBindingAndPlanTest(unittest.TestCase):
    """真实当前输入（只读）：bind 保留声明，plan 换算为最终值。"""

    @classmethod
    def setUpClass(cls):
        case = json.loads((ROOT / CASE_PATH).read_text(encoding="utf-8"))
        case["_path"] = CASE_PATH
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        context, inputs, diagnostics = calculation_route.load_compilation_inputs(
            route, root=ROOT, case=case
        )
        assert context is not None, diagnostics
        cls.case = case
        cls.context = context
        cls.inputs = inputs
        cls.compile_result = plan_compiler.compile_from_resolved_model(
            context, copy.deepcopy(inputs["model"])
        )
        assert cls.compile_result.plan is not None, cls.compile_result.diagnostics
        cls.plan = cls.compile_result.plan
        cls.attributes = dict(
            (cls.plan["execution"]["execution_v1"].get("facts") or {}).get("attributes") or {}
        )

    @classmethod
    def tearDownClass(cls):
        cls.context.close()

    # -- helpers ---------------------------------------------------------
    def _bind_synthetic(self, per_attribute=None):
        effect = _synthetic_effect(per_attribute)
        profile = {
            "schema": "le-dps-profile-v2",
            "ability_id": "ba1574",
            "controls": [],
            "controls_by_key": {},
            "effects": [effect],
        }
        results = binding.bind_profile_effects(
            self.context.calc,
            self.context.catalog,
            profile,
            states={},
            target={},
            pool_lookup=lambda *_args: 0.0,
        )
        self.assertEqual(len(results), 1)
        return results[0]

    # -- bind 阶段：只保留声明，不提前乘属性 ------------------------------
    def test_bind_keeps_declaration_without_multiplying(self):
        result = self._bind_synthetic({"attribute": "dexterity", "per": 1.0})
        self.assertEqual(result.status, binding.BINDING_APPLIED)
        self.assertIsNotNone(result.contribution)
        # bind 阶段没有属性池：数值仍是声明值 1.0
        self.assertAlmostEqual(float(result.contribution.value), 1.0, delta=1e-12)
        self.assertEqual(
            (result.contribution.raw or {}).get("per_attribute"),
            {"attribute": "dexterity", "per": 1.0},
        )
        self.assertEqual(
            (result.payload or {}).get("per_attribute"),
            {"attribute": "dexterity", "per": 1.0},
        )
        # 来源事实仍在 payload/raw，可供 plan_compiler 审计
        self.assertEqual(result.source.get("item_instance_id"), "equipment.feet")
        self.assertEqual(result.source.get("description_index"), 0)

    def test_bind_rejects_nonpositive_per(self):
        with self.assertRaises(binding.BindingError) as caught:
            self._bind_synthetic({"attribute": "dexterity", "per": 0})
        self.assertIn(EFFECT_ID, str(caught.exception))
        self.assertIn("per", str(caught.exception))

    # -- plan_compiler：属性池就绪后换算 ----------------------------------
    def test_per_attribute_scales_with_real_dexterity(self):
        self.assertAlmostEqual(float(self.attributes.get("dexterity")), 228.0, delta=1e-12)
        result = self._bind_synthetic({"attribute": "dexterity", "per": 1.0})
        facts = plan_compiler._apply_per_attribute_scaling(result, self.attributes)
        self.assertAlmostEqual(float(result.contribution.value), 228.0, delta=1e-12)
        self.assertEqual(facts["attribute"], "dexterity")
        self.assertAlmostEqual(float(facts["per"]), 1.0, delta=1e-12)
        self.assertAlmostEqual(float(facts["attribute_value"]), 228.0, delta=1e-12)
        self.assertAlmostEqual(float(facts["declared_value"]), 1.0, delta=1e-12)
        self.assertAlmostEqual(float(facts["scaled_value"]), 228.0, delta=1e-12)
        # 审计事实同时进入 payload 与 raw，并保留可读 note
        self.assertAlmostEqual(
            float((result.payload or {})["per_attribute"]["scaled_value"]), 228.0, delta=1e-12
        )
        self.assertAlmostEqual(
            float((result.contribution.raw or {})["per_attribute"]["attribute_value"]),
            228.0,
            delta=1e-12,
        )
        self.assertIn("按 dexterity 缩放", str(result.contribution.notes or ""))

    def test_per_two_halves_the_scaled_value(self):
        result = self._bind_synthetic({"attribute": "dexterity", "per": 2.0})
        facts = plan_compiler._apply_per_attribute_scaling(result, self.attributes)
        self.assertAlmostEqual(float(result.contribution.value), 114.0, delta=1e-12)
        self.assertAlmostEqual(float(facts["scaled_value"]), 114.0, delta=1e-12)

    def test_missing_attribute_is_unresolved_not_silent_zero(self):
        for attributes in ({}, {"dexterity": 0.0}):
            result = self._bind_synthetic({"attribute": "dexterity", "per": 1.0})
            facts = plan_compiler._apply_per_attribute_scaling(result, attributes)
            self.assertIsNone(result.contribution, "属性缺失时不能产出静默 0 的贡献")
            self.assertEqual(result.status, binding.BINDING_UNRESOLVED)
            self.assertIn("dexterity", str(result.reason or ""))
            self.assertIn("缺失", str(result.reason or ""))
            self.assertIsNone(facts["attribute_value"])
            self.assertIsNone(facts["scaled_value"])
            # 覆盖缺口按 effect_statuses 生成，reason 必须透传
            gaps = dps_result.static_coverage_gaps(
                contributions=[],
                claimed_mod_ids=set(),
                effect_statuses=[
                    {
                        "effect_id": result.effect_id,
                        "label": result.label,
                        "status": result.status,
                        "reason": result.reason,
                    }
                ],
                declared_inactive_checks=[],
                skill_constants={},
                per_attribute_problems=[],
            )
            self.assertTrue(
                any(
                    gap.get("code") == "effect:unresolved"
                    and "dexterity" in str(gap.get("detail") or "")
                    for gap in gaps
                ),
                gaps,
            )


class UniqueDescriptionTransferTest(unittest.TestCase):
    """``unique_description`` 玩家来源进入默认 transfer 白名单。"""

    def _row(self, contribution_id, source_kind, value, tags, scope="owner"):
        return model.Contribution(
            contribution_id=contribution_id,
            source_kind=source_kind,
            source_label=contribution_id,
            source_path="test",
            stat_name="added_damage",
            stat_key="added_damage",
            operation="added",
            value=value,
            damage_tags=list(tags),
            included=True,
            raw={"scope": scope},
        )

    def _rule(self, **input_pool_overrides):
        input_pool = {
            "stat_key": "added_damage",
            "operations": ["added"],
            "tag_policy": "none",
        }
        input_pool.update(input_pool_overrides)
        return {
            "schema": "le-stat-transfer-v1",
            "target_actor": "minion:test:100053",
            "transfers": [
                {
                    "id": "shared_enhancements_added",
                    "input_pool": input_pool,
                    "ratio": 0.75,
                    "output": {
                        "stat": "added_damage",
                        "operation": "added",
                        "scope": "actor",
                    },
                }
            ],
        }

    def test_default_whitelist_includes_unique_description(self):
        self.assertIn("unique_description", actor_pool.DEFAULT_TRANSFER_SOURCE_KINDS)

        rows = [
            self._row("equipment.feet#unique_description#0:x", "unique_description", 228.0, ["Bow", "Cold"]),
            self._row("mod:equipment.weapon1#implicit#0", "equipment", 32.0, ["Bow", "Cold"]),
            self._row("skill:ba1574:23", "skill_node", 100.0, ["Bow", "Cold"]),
        ]
        produced = actor_pool.apply_stat_transfers(rows, self._rule(), states={})
        ids = {row.contribution_id for row in produced}
        self.assertEqual(len(produced), 2, ids)
        mourningfrost = [
            row
            for row in produced
            if row.raw.get("input_contribution_id") == "equipment.feet#unique_description#0:x"
        ]
        self.assertEqual(len(mourningfrost), 1)
        row = mourningfrost[0]
        self.assertAlmostEqual(float(row.value), 171.0, delta=1e-12)
        self.assertEqual(set(row.damage_tags), {"Bow", "Cold"})
        self.assertEqual(row.source_kind, "derived")
        self.assertEqual((row.raw or {}).get("scope"), "actor")
        # skill_node（actor 自身模板效果）仍被默认白名单排除
        self.assertFalse(
            any(row.raw.get("input_contribution_id") == "skill:ba1574:23" for row in produced)
        )

    def test_explicit_include_source_kinds_still_excludes_unique_description(self):
        rows = [
            self._row("equipment.feet#unique_description#0:x", "unique_description", 228.0, ["Bow", "Cold"]),
        ]
        produced = actor_pool.apply_stat_transfers(
            rows, self._rule(include_source_kinds=["equipment"]), states={}
        )
        self.assertEqual(produced, [])


class RealCaseMourningfrostEndToEndTest(unittest.TestCase):
    """真实案例（模板已声明 effect）：228 → 171 → 冷通道等效 427.5。"""

    @classmethod
    def setUpClass(cls):
        case = json.loads((ROOT / CASE_PATH).read_text(encoding="utf-8"))
        case["_path"] = CASE_PATH
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        context, inputs, diagnostics = calculation_route.load_compilation_inputs(
            route, root=ROOT, case=case
        )
        assert context is not None, diagnostics
        cls.context = context
        cls.inputs = inputs
        cls.compile_result = plan_compiler.compile_from_resolved_model(
            context, copy.deepcopy(inputs["model"])
        )
        assert cls.compile_result.plan is not None, cls.compile_result.diagnostics
        cls.plan = cls.compile_result.plan
        cls.execution_effects = cls.plan["execution"]["execution_v1"]["effects"]
        cls.player_effect = next(
            effect
            for effect in cls.execution_effects
            if (effect.get("raw") or {}).get("effect_id") == "mourningfrost_cold_per_dexterity"
        )
        cls.transfer_effects = [
            effect
            for effect in cls.execution_effects
            if str(effect.get("effect_id") or "").startswith(
                "actor:100053:transfer:shared_enhancements_added:equipment_feet_unique_descriptio"
            )
        ]

    @classmethod
    def tearDownClass(cls):
        cls.context.close()

    def test_player_side_value_is_dexterity(self):
        attributes = (
            self.plan["execution"]["execution_v1"].get("facts") or {}
        ).get("attributes") or {}
        self.assertAlmostEqual(float(attributes.get("dexterity")), 228.0, delta=1e-12)
        self.assertEqual(self.player_effect["source_kind"], "unique_description")
        self.assertAlmostEqual(float(self.player_effect["value"]), 228.0, delta=1e-12)
        raw_facts = (self.player_effect.get("raw") or {}).get("per_attribute") or {}
        self.assertEqual(raw_facts.get("attribute"), "dexterity")
        self.assertAlmostEqual(float(raw_facts.get("per")), 1.0, delta=1e-12)
        self.assertAlmostEqual(float(raw_facts.get("attribute_value")), 228.0, delta=1e-12)
        self.assertAlmostEqual(float(raw_facts.get("scaled_value")), 228.0, delta=1e-12)
        ledger_facts = ((self.player_effect.get("ledger") or {}).get("payload") or {}).get(
            "per_attribute"
        ) or {}
        self.assertAlmostEqual(float(ledger_facts.get("attribute_value")), 228.0, delta=1e-12)
        # value_mode 仍为 static（不在运行期重算）
        self.assertEqual(self.player_effect.get("value_mode"), "static")

    def test_shared_enhancements_transfer_is_075_dexterity(self):
        self.assertEqual(len(self.transfer_effects), 1, self.transfer_effects)
        row = self.transfer_effects[0]
        self.assertAlmostEqual(float(row["value"]), 171.0, delta=1e-12)
        self.assertEqual(set(row["damage_tags"]), {"Cold", "Bow"})
        self.assertEqual((row.get("raw") or {}).get("scope"), "actor")

    def test_cold_component_uses_transferred_row_with_250pct_effectiveness(self):
        execution = plan_v4.execute_plan_v4(copy.deepcopy(self.plan))
        components = (execution.get("report_details") or {}).get("components") or []
        cold = next(item for item in components if item.get("damage_type") == "Cold")
        transfer_id = self.transfer_effects[0]["effect_id"]
        self.assertIn(transfer_id, cold["source_ids"])
        self.assertAlmostEqual(float(cold["added_effectiveness"]), 2.5, delta=1e-12)
        # 该转移行经 250% added 效率后的等效值 = 0.75 × Dex × 2.5 = 427.5
        self.assertAlmostEqual(171.0 * 2.5, 427.5, delta=1e-12)
        self.assertGreaterEqual(float(cold["added"]), 171.0 - 1e-12)


if __name__ == "__main__":
    unittest.main()
