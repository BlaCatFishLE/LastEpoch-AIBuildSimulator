"""装备数量条件 effect（早期属性阶段）+ effect 严格字段白名单 定向测试。

覆盖：
1. 真实 Ballista 案例注入 Urzil's Pride 规则后：Dex 228→236、attribute_scaling 9.44、
   Armed Construction per-Dex more 9.44、Mourningfrost owner added 236→shared transfer 177，
   且 horizon DPS 相对基线提升（同输入、非 canonical 估算）。
2. 条件不满足（min_items 过高）：不产出贡献行，账本 not_applicable，Dex 仍 228。
3. source selector 不唯一 → unresolved；selector 缺失/不符且 required=True → 编译拒绝。
4. 无规则声明时行为不变（基线快照：Dex 228 / attribute_scaling 9.12 / 无规则 effect）。
5. 规则结构非法（缺 requires / min_items<1 / value 非数 / 未知字段）→ 校验期拒绝。
6. 条件统计排除 idol（合成 items）。
7. effect 未知字段（如 condition 拼写错误）→ validate_definition_contract 与
   bind_profile_effects 都明确拒绝；现有模板（VC/FR/BH/Ballista）仍通过；
   per_attribute / equipment_condition_effects 相关字段在白名单内。

公开版说明：第 1/2/3/4 组与第 7 组的“真实模板经生产编译链”断言需要作者本地的
Ballista/VC/FR/BH 案例与构筑输入（不随公开版分发），因此按 ``_author_assets`` 护栏
在公开树中明确跳过；纯 binding/规则校验测试照常运行。
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from le_calc import calculation_route, effect_binding as binding, plan_v4  # noqa: E402

CASE_REL = "examples/ballista-beta5-native-20261112/case.json"
TEMPLATE_REL = "knowledge/definitions/ballista-armed-construction-v4.json"
DEFINITIONS_DIR = ROOT / "knowledge/definitions"

URZIL_RULE = {
    "id": "urzil_all_attributes_with_7_corrupted_items",
    "label": "Urzil's Pride：≥7 件 corrupted 非神像装备时 +8 全属性",
    "source": {
        "kind": "equipment_mod",
        "binding_selector": {
            "kind": "equipment_mod",
            "item_definition_entity_id": "item:letools:uniqueList.uniques:10",
            "expected_source": "affix",
            "expected_property": 98,
            "expected_tags": 649,
            "expected_special_tag": 0,
            "selection": "one",
        },
        "expected_definition_entity_id": "item:letools:uniqueList.uniques:10",
        "expected_property": 98,
        "expected_special_tag": 0,
        "expected_readable_text_contains": (
            "All Attributes with at least 7 Corrupted non-Idol Items equipped"
        ),
    },
    "requires": {"min_items": 7, "channel": "corrupted", "exclude_roles": ["idol"]},
    "stat": "all_attributes",
    "operation": "added",
    "value": 8,
    "status": "derived",
    "required": True,
    "evidence": [
        {
            "kind": "game_data",
            "detail": "equipment.chest#affix#corrupted#0.0 property 98/tags 649，value=8",
            "locator": (
                "examples/ballista-beta5-native-20261112/inputs/"
                "calculation-input.corrected.json#mods equipment.chest#affix#corrupted#0.0"
            ),
        }
    ],
}


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _author_assets_available() -> bool:
    """True only in the author's full working tree (real case + template present)."""
    return (ROOT / CASE_REL).is_file() and (ROOT / TEMPLATE_REL).is_file()


class _Compiled:
    def __init__(self, result, plan):
        self.result = result
        self.plan = plan
        self.diagnostics = [dict(item) for item in result.diagnostics or []]
        self.execution = (plan or {}).get("execution", {}).get("execution_v1", {})
        self.effects = list(self.execution.get("effects") or [])

    def effect(self, effect_id: str):
        return next((item for item in self.effects if item.get("effect_id") == effect_id), None)


def _compile_with_rule(rule, *, template_path: Path | None = None):
    case = _load_json(ROOT / CASE_REL)
    case["_path"] = CASE_REL
    template = _load_json(ROOT / TEMPLATE_REL)
    if rule is not None:
        template["definition"].setdefault("rules", {})["equipment_condition_effects"] = [
            copy.deepcopy(rule)
        ]
    owns_scratch = template_path is None
    if template_path is None:
        scratch = Path(tempfile.mkdtemp(dir=str(ROOT / ".scratch"), prefix="equip-cond-"))
        template_path = scratch / "template.json"
    else:
        template_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        template_path.write_text(
            json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        source = case.setdefault("calculation", {}).setdefault("source", {})
        source["template"] = template_path.relative_to(ROOT).as_posix()
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        _resolved, result = calculation_route.compile_route(root=ROOT, case=case, route=route)
        return result
    finally:
        if owns_scratch:
            shutil.rmtree(template_path.parent, ignore_errors=True)


def _dex_from_effects(effects: list[dict]) -> float:
    total = 0.0
    for effect in effects:
        scope = str(effect.get("scope") or "owner")
        if scope not in ("owner", "all"):
            continue
        if effect.get("operation") not in ("added", "base"):
            continue
        if effect.get("stat_key") == "attribute" and str(
            effect.get("target") or ""
        ).lower() == "dexterity":
            total += float(effect.get("value") or 0)
        elif effect.get("stat_key") == "all_attributes":
            total += float(effect.get("value") or 0)
    return total


def _pair(result):
    return result, result.plan


class RealBuildEquipmentConditionTests(unittest.TestCase):
    """断言来自作者本地真实 Ballista 构筑（公开版整类跳过）。"""

    @classmethod
    def setUpClass(cls):
        if not _author_assets_available():
            raise unittest.SkipTest(
                "作者本地 Ballista 案例/构筑输入不随公开版分发（缺少 "
                f"{CASE_REL} 或 {TEMPLATE_REL}）：真实数值断言只在作者完整工作树运行"
            )
        cls.baseline = _Compiled(*_pair(_compile_with_rule(None)))
        cls.applied = _Compiled(*_pair(_compile_with_rule(URZIL_RULE)))

    def test_real_case_condition_applied_affects_dex_chain(self):
        applied, baseline = self.applied, self.baseline
        self.assertEqual(applied.result.execution_status, "completed", applied.diagnostics)
        rule_effect = applied.effect(
            "rule:equipment_condition:urzil_all_attributes_with_7_corrupted_items"
        )
        self.assertIsNotNone(rule_effect)
        self.assertEqual(rule_effect["stat_key"], "all_attributes")
        self.assertEqual(rule_effect["operation"], "added")
        self.assertAlmostEqual(float(rule_effect["value"]), 8.0, places=6)
        # 账本：条件事实（计数/实例列表/min_items）+ 来源唯一命中
        ledger = (
            (applied.plan.get("assembly", {}).get("legacy_plan", {}).get("facts") or {})
            .get("ledger_static", {})
            .get("equipment_condition_effects")
        )
        self.assertTrue(ledger)
        self.assertEqual(ledger[0]["status"], "applied")
        condition = ledger[0]["condition"]
        self.assertIs(condition["satisfied"], True)
        self.assertEqual(condition["min_items"], 7)
        self.assertGreaterEqual(condition["count"], 7)
        self.assertIn("equipment.chest", condition["instance_ids"])
        self.assertTrue(all(not iid.startswith("idols.") for iid in condition["instance_ids"]))
        self.assertTrue(ledger[0]["source"]["detail"])
        # 属性链：Dex 228→236，attribute_scaling 9.12→9.44，armed per-Dex more 9.44
        self.assertAlmostEqual(_dex_from_effects(applied.effects), 236.0, places=6)
        self.assertAlmostEqual(_dex_from_effects(baseline.effects), 228.0, places=6)
        scaling = applied.effect("rule:attribute_scaling")
        self.assertIsNotNone(scaling)
        self.assertAlmostEqual(float(scaling["value"]), 9.44, places=6)
        baseline_scaling = baseline.effect("rule:attribute_scaling")
        self.assertIsNotNone(baseline_scaling)
        self.assertAlmostEqual(float(baseline_scaling["value"]), 9.12, places=6)
        armed = next(
            item
            for item in applied.effects
            if item.get("operation") == "more" and item.get("scope") == "actor"
            and "armed_construction" in str(item.get("effect_id"))
        )
        self.assertAlmostEqual(float(armed["value"]), 9.44, places=6)
        # Mourningfrost：owner added damage = Dex（228→236），经 shared 0.75 → 171→177
        owner_added = next(
            item
            for item in applied.effects
            if item.get("stat_key") == "added_damage"
            and item.get("scope") in (None, "owner")
            and item.get("damage_tags") == ["Cold", "Bow"]
        )
        self.assertAlmostEqual(float(owner_added["value"]), 236.0, places=6)
        transfer = next(
            item
            for item in applied.effects
            if item.get("stat_key") == "added_damage" and item.get("scope") == "actor"
            and item.get("damage_tags") == ["Cold", "Bow"]
        )
        self.assertAlmostEqual(float(transfer["value"]), 236.0 * 0.75, places=6)

        # DPS：同一输入下条件生效应提升（非 canonical，仅方向/量级检查）
        baseline_dps = float(plan_v4.execute_plan_v4(baseline.plan)["totals"]["horizon_dps"])
        applied_dps = float(plan_v4.execute_plan_v4(applied.plan)["totals"]["horizon_dps"])
        self.assertGreater(applied_dps, baseline_dps)
        self.assertTrue(1.03 <= applied_dps / baseline_dps <= 1.12)

    def test_condition_unsatisfied_records_not_applicable(self):
        unsatisfied = copy.deepcopy(URZIL_RULE)
        unsatisfied["requires"]["min_items"] = 99
        compiled = _Compiled(*_pair(_compile_with_rule(unsatisfied)))
        self.assertEqual(compiled.result.execution_status, "completed", compiled.diagnostics)
        rule_effect = compiled.effect(
            "rule:equipment_condition:urzil_all_attributes_with_7_corrupted_items"
        )
        # 只有审计记录（emits_contribution=False、无 contribution_id），不伪造 0 值贡献行
        self.assertIsNotNone(rule_effect)
        self.assertIs(rule_effect.get("emits_contribution"), False)
        self.assertIsNone(rule_effect.get("contribution_id"))
        self.assertIs(rule_effect.get("included_static"), False)
        ledger = (
            (compiled.plan.get("assembly", {}).get("legacy_plan", {}).get("facts") or {})
            .get("ledger_static", {})
            .get("equipment_condition_effects")
        )
        self.assertTrue(ledger)
        self.assertEqual(ledger[0]["status"], "not_applicable")
        self.assertIsNone(ledger[0]["contribution_id"])
        self.assertIn("99", str(ledger[0]["reason"]))
        # 不伪造 0 行：Dex/attribute_scaling 保持基线
        self.assertAlmostEqual(_dex_from_effects(compiled.effects), 228.0, places=6)
        scaling = compiled.effect("rule:attribute_scaling")
        self.assertAlmostEqual(float(scaling["value"]), 9.12, places=6)

    def test_source_selector_missing_rejected(self):
        broken = copy.deepcopy(URZIL_RULE)
        broken["source"]["binding_selector"]["item_definition_entity_id"] = (
            "item:letools:uniqueList.uniques:9999"
        )
        broken["source"]["expected_definition_entity_id"] = "item:letools:uniqueList.uniques:9999"
        result = _compile_with_rule(broken)
        self.assertEqual(result.execution_status, "rejected")
        messages = " ".join(str(item.get("message") or "") for item in result.diagnostics or [])
        self.assertIn("urzil_all_attributes_with_7_corrupted_items", messages)

    def test_no_rule_declaration_is_unchanged(self):
        baseline = self.baseline
        self.assertEqual(baseline.result.execution_status, "completed", baseline.diagnostics)
        self.assertFalse(
            any(
                str(item.get("effect_id") or "").startswith("rule:equipment_condition")
                for item in baseline.effects
            )
        )
        scaling = baseline.effect("rule:attribute_scaling")
        self.assertAlmostEqual(float(scaling["value"]), 9.12, places=6)
        self.assertAlmostEqual(_dex_from_effects(baseline.effects), 228.0, places=6)
        # 同输入重复编译应稳定（固定临时模板路径，避免 input_refs 路径不同影响 plan_sha）
        fixed = ROOT / ".scratch" / "equip-cond-deterministic" / "template.json"
        first = _Compiled(*_pair(_compile_with_rule(None, template_path=fixed)))
        second = _Compiled(*_pair(_compile_with_rule(None, template_path=fixed)))
        self.assertEqual(first.plan.get("plan_sha256"), second.plan.get("plan_sha256"))
        self.assertAlmostEqual(
            float(first.effect("rule:attribute_scaling")["value"]), 9.12, places=6
        )

    def test_effect_whitelist_covers_real_templates_via_route(self):
        # 现有真实模板经生产编译链（bind_template 解析 selector + 严格 effect 白名单）仍全部通过
        cases = [
            "examples/void-cleave-20261110/case.json",
            "examples/flame-reave/case.json",
            "examples/black-hole/case.json",
            CASE_REL,
        ]
        for case_rel in cases:
            with self.subTest(case=case_rel):
                case = _load_json(ROOT / case_rel)
                case["_path"] = case_rel
                route = calculation_route.resolve_calculation_route(case, root=ROOT)
                _resolved, result = calculation_route.compile_route(root=ROOT, case=case, route=route)
                self.assertEqual(result.execution_status, "completed", (case_rel, result.diagnostics))


class PureBindingTests(unittest.TestCase):
    """不依赖作者案例的 binding / 规则校验测试（公开版照常运行）。"""

    def test_source_selector_ambiguous_is_unresolved(self):
        calc = {
            "items": [
                {"instance_id": "equipment.a", "role": "equipment",
                 "definition_entity_id": "item:test:x", "base_entity_id": "base:test:x"},
                {"instance_id": "equipment.b", "role": "equipment",
                 "definition_entity_id": "item:test:x", "base_entity_id": "base:test:x"},
            ],
            "mods": [
                {"mod_id": "equipment.a#affix#corrupted#0.0", "item_instance_id": "equipment.a",
                 "source": "affix", "channel": "corrupted", "property": 98, "special_tag": 0,
                 "tags": 649, "value_numeric": 8,
                 "readable_text": "All Attributes with at least 7 Corrupted non-Idol Items equipped"},
                {"mod_id": "equipment.b#affix#corrupted#0.0", "item_instance_id": "equipment.b",
                 "source": "affix", "channel": "corrupted", "property": 98, "special_tag": 0,
                 "tags": 649, "value_numeric": 8,
                 "readable_text": "All Attributes with at least 7 Corrupted non-Idol Items equipped"},
            ],
        }
        source = {
            "kind": "equipment_mod",
            "binding_selector": {
                "kind": "equipment_mod",
                "item_definition_entity_id": "item:test:x",
                "expected_source": "affix",
                "expected_property": 98,
                "expected_tags": 649,
                "expected_special_tag": 0,
                "selection": "one",
            },
            "expected_definition_entity_id": "item:test:x",
            "expected_property": 98,
            "expected_special_tag": 0,
            "expected_readable_text_contains": "All Attributes with at least 7 Corrupted non-Idol Items equipped",
        }
        check = binding.resolve_equipment_condition_source(calc, source)
        self.assertEqual(check.status, binding.BINDING_UNRESOLVED)
        self.assertFalse(check.ok)

    def test_invalid_rule_structure_rejected(self):
        mutations = [
            (lambda rule: rule.pop("requires"), "requires"),
            (lambda rule: rule["requires"].__setitem__("min_items", 0), "min_items"),
            (lambda rule: rule.__setitem__("value", "8"), "value"),
            (lambda rule: rule.__setitem__("condition", {}), "未支持字段"),
            (lambda rule: rule["requires"].__setitem__("channel", ""), "channel"),
        ]
        for mutate, needle in mutations:
            with self.subTest(needle=needle):
                rule = copy.deepcopy(URZIL_RULE)
                mutate(rule)
                with self.assertRaises(binding.BindingError) as caught:
                    binding.validate_plan_v2_rules({"equipment_condition_effects": [rule]})
                self.assertIn(needle, str(caught.exception))

    def test_plan_v3_rejects_early_condition_rule(self):
        with self.assertRaises(binding.BindingError):
            binding.validate_plan_v3_rules({"equipment_condition_effects": [copy.deepcopy(URZIL_RULE)]})

    def test_condition_count_excludes_idol(self):
        calc = {
            "items": [
                *[
                    {"instance_id": f"equipment.slot{i}", "role": "equipment"}
                    for i in range(7)
                ],
                *[{"instance_id": f"idols.{i}", "role": "idol"} for i in range(3)],
            ],
            "mods": [
                *[
                    {"mod_id": f"equipment.slot{i}#affix#corrupted#0.0",
                     "item_instance_id": f"equipment.slot{i}", "channel": "corrupted"}
                    for i in range(7)
                ],
                *[
                    {"mod_id": f"idols.{i}#affix#corrupted#0.0",
                     "item_instance_id": f"idols.{i}", "channel": "corrupted"}
                    for i in range(3)
                ],
            ],
        }
        facts = binding.count_equipment_condition_items(
            calc, {"min_items": 8, "channel": "corrupted", "exclude_roles": ["idol"]}
        )
        self.assertEqual(facts["count"], 7)
        self.assertIs(facts["satisfied"], False)
        facts = binding.count_equipment_condition_items(
            calc, {"min_items": 7, "channel": "corrupted", "exclude_roles": ["idol"]}
        )
        self.assertIs(facts["satisfied"], True)
        facts = binding.count_equipment_condition_items(
            calc, {"min_items": 10, "channel": "corrupted", "include_roles": ["idol"]}
        )
        self.assertEqual(facts["count"], 3)
        self.assertIs(facts["satisfied"], False)

    def test_effect_unknown_field_rejected_by_contract(self):
        payload = {
            "plan_version": "le-calculation-plan-v1",
            "controls": [{"key": "state", "type": "boolean"}],
            "skill": {"components": {"hit": {"tags": ["Melee"]}, "dot": {"tags": ["DoT"]}}},
            "effects": [
                {
                    "id": "urzil_typo",
                    "label": "typo condition",
                    "source": {
                        "kind": "state_source",
                        "control": "state",
                        "expected_definition_entity_id": "item:x",
                    },
                    "stat": "all_attributes",
                    "operation": "added",
                    "value": 8,
                    "condition": {"min_items": 7},
                }
            ],
            "rules": {},
            "state_sources": {},
            "attribute_conversion": {},
            "ailments": [],
        }
        with self.assertRaises(binding.BindingError) as caught:
            binding.validate_definition_contract(payload, allow_unresolved_sources=True)
        message = str(caught.exception)
        self.assertIn("urzil_typo", message)
        self.assertIn("condition", message)

    def test_bind_profile_effects_rejects_unknown_field_first(self):
        profile = {
            "effects": [
                {
                    "id": "unknown_field_effect",
                    "label": "bad",
                    "condition": {"min_items": 7},
                }
            ],
            "controls_by_key": {},
        }
        with self.assertRaises(binding.BindingError) as caught:
            binding.bind_profile_effects(
                {}, None, profile, states={}, target={}, pool_lookup=lambda *_args: 0.0
            )
        self.assertIn("unknown_field_effect", str(caught.exception))
        self.assertIn("condition", str(caught.exception))

    def test_effect_whitelist_covers_definition_templates(self):
        used_fields: set[str] = set()
        for path in sorted(DEFINITIONS_DIR.glob("*.json")):
            document = _load_json(path)
            definition = document.get("definition") or {}
            for effect in definition.get("effects") or []:
                used_fields.update(effect)
        # 白名单覆盖随公开版分发的定义模板全部字段（未知字段会被拒绝）
        self.assertLessEqual(used_fields, binding.EFFECT_ALLOWED_FIELDS)
        self.assertIn("per_attribute", binding.EFFECT_ALLOWED_FIELDS)
        self.assertIn("per_attribute", used_fields)  # Ballista 的 Phantom Grip 行
        # 新规则字段自身也通过严格校验
        binding.validate_plan_v2_rules({"equipment_condition_effects": [copy.deepcopy(URZIL_RULE)]})
        self.assertIn("requires", binding.EQUIPMENT_CONDITION_EFFECT_FIELDS)
        self.assertIn("min_items", binding.EQUIPMENT_CONDITION_REQUIRES_FIELDS)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
