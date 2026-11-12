"""目标侧结算（target_side）等价性测试。

判据：适配器与**冻结的改造前实现**（本文件内独立重写的 legacy 公式）在真实
案例输入与合成网格上逐位相等；``damage._target_multiplier`` /
``hit_damage.resolve_target`` / ``hit_damage.compute_single_hit`` 的生产输出
与改造前 golden 值逐位相等。

冻结来源：2026-11-07 P2 改造前的 ``damage._target_multiplier``（BH v4 口径）
与 ``hit_damage.resolve_target`` / 组件乘区（FR v2 口径），逐行抄录于本文件
的 ``legacy_*`` 函数。golden 数值来自改造前在生产输入上的实测输出。

**预期差异（不强行统一）**：``res≠0 且 pen>0`` 时 fold 公式比 separate 公式
多一个交叉项 ``res·pen·(1-dr)``；本测试显式断言该差异存在并核对解析值。
"""

from __future__ import annotations

import types
import unittest

from le_calc import damage, hit_damage, monster_dr, model
from le_calc.mechanics import target_side
from le_calc.monster_dr import dr_for_level

import test_mechanics_fixtures as fixtures

REL = 1e-12


# ---------------------------------------------------------------------------
# 冻结的改造前实现（legacy reference，独立于适配器）
# ---------------------------------------------------------------------------



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def legacy_bh_target_multiplier(
    ruleset_target_side: dict,
    component,
    penetration_total: float,
    scenario: dict,
) -> dict:
    target = scenario.get("target", {}) or {}
    base_res = float((target.get("resistances") or {}).get(component.damage_type.lower(), 0.0))
    negative_allowed = bool(ruleset_target_side.get("negative_resistance_allowed", True))
    penetration_applies = "body" in (ruleset_target_side.get("penetration_applies_to") or ["body"])
    pen = penetration_total if penetration_applies else 0.0
    effective = base_res - pen
    if not negative_allowed:
        effective = max(0.0, effective)
    monster_level = int(target.get("monster_level", 0) or 0)
    monster_dr = float(target.get("monster_damage_reduction", 0.0) or 0.0)
    monster_dr_factor = 1.0 - monster_dr
    resistance_factor = 1.0 - effective
    return {
        "target_resistance": base_res,
        "penetration": pen,
        "effective_resistance": effective,
        "resistance_multiplier": resistance_factor,
        "monster_level": monster_level,
        "monster_damage_reduction": monster_dr,
        "monster_dr_factor": monster_dr_factor,
        "multiplier": resistance_factor * monster_dr_factor,
        "negative_resistance_allowed": negative_allowed,
        "penetration_applied": penetration_applies,
    }


def legacy_fr_resolve_target(
    ruleset_document,
    scenario: dict | None,
) -> dict:
    ruleset_data = getattr(ruleset_document, "data", ruleset_document) or {}
    target = dict((ruleset_data.get("target") or {}) if isinstance(ruleset_data, dict) else {})
    for key, value in ((scenario or {}).get("target") or {}).items():
        if key in ("comment", "note", "notes"):
            continue
        target[key] = value
    monster_level = target.get("monster_level")
    if monster_level is None:
        monster_level = 100
    fire_resistance = float(target.get("fire_resistance") or 0.0)
    dr = dr_for_level(int(monster_level))
    return {
        "boss_or_rare": bool(target.get("boss_or_rare", True)),
        "monster_level": int(monster_level),
        "monster_damage_reduction": dr,
        "monster_dr_factor": 1.0 - dr,
        "fire_resistance": fire_resistance,
        "resistance_factor": 1.0 - fire_resistance,
        "source": "ruleset.target + scenario.target",
    }


def legacy_fr_component_target(
    penetration_total: float,
    resistance_factor: float,
    monster_dr_factor: float,
) -> float:
    penetration_multiplier = 1.0 + penetration_total
    return penetration_multiplier * float(resistance_factor) * float(monster_dr_factor)


def legacy_ignite_target(
    ruleset_target_side: dict,
    penetration_total: float,
    scenario: dict,
) -> dict:
    """冻结的改造前 ``ignite._ignite_target``（抗性读 ruleset base）。"""
    ts = ruleset_target_side
    base_res = float((ts.get("base_resistances") or {}).get("fire", 0.0))
    negative_allowed = bool(ts.get("negative_resistance_allowed", True))
    applies = "ignite" in (ts.get("penetration_applies_to") or ["ignite"])
    pen = penetration_total if applies else 0.0
    effective = base_res - pen
    if not negative_allowed:
        effective = max(0.0, effective)
    target = (scenario or {}).get("target", {}) or {}
    monster_level = int(target.get("monster_level", 0) or 0)
    monster_dr = float(target.get("monster_damage_reduction", 0.0) or 0.0)
    monster_dr_factor = 1.0 - monster_dr
    resistance_factor = 1.0 - effective
    return {
        "target_resistance": base_res,
        "penetration": pen,
        "effective_resistance": effective,
        "resistance_multiplier": resistance_factor,
        "monster_level": monster_level,
        "monster_damage_reduction": monster_dr,
        "monster_dr_factor": monster_dr_factor,
        "multiplier": resistance_factor * monster_dr_factor,
        "negative_resistance_allowed": negative_allowed,
        "penetration_applied": applies,
    }


def _fake_ruleset(target_side_config: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(target_side=target_side_config)


# ---------------------------------------------------------------------------
# 策略校验
# ---------------------------------------------------------------------------


class TargetSidePolicyTest(unittest.TestCase):
    def test_policy_enums_are_validated(self):
        with self.assertRaises(target_side.TargetSideError):
            target_side.TargetSidePolicy(penetration_mode="bogus")
        with self.assertRaises(target_side.TargetSideError):
            target_side.TargetSidePolicy(negative_resistance_clamp="bogus")
        with self.assertRaises(target_side.TargetSideError):
            target_side.TargetSidePolicy(dr_source="bogus")

    def test_undefined_combination_is_rejected(self):
        # 独立穿透乘区 + 负抗钳制没有已确认语义：明确拒绝而不是猜。
        with self.assertRaises(target_side.TargetSideError):
            target_side.TargetSidePolicy(
                penetration_mode=target_side.PENETRATION_SEPARATE,
                negative_resistance_clamp=target_side.NEGATIVE_CLAMP_AT_ZERO,
            )

    def test_resolve_monster_dr_validates_source(self):
        with self.assertRaises(target_side.TargetSideError):
            target_side.resolve_monster_dr(dr_source="bogus", monster_level=100)

    def test_registry_policies_match_model_capabilities(self):
        self.assertEqual(
            target_side.BH_TARGET_SIDE_POLICY.penetration_mode,
            target_side.PENETRATION_FOLD,
        )
        self.assertEqual(
            target_side.BH_TARGET_SIDE_POLICY.dr_source, target_side.DR_FROM_SCENARIO
        )
        self.assertEqual(
            target_side.FR_HIT_TARGET_SIDE_POLICY.penetration_mode,
            target_side.PENETRATION_SEPARATE,
        )
        self.assertEqual(
            target_side.FR_HIT_TARGET_SIDE_POLICY.dr_source, target_side.DR_FROM_TABLE
        )


# ---------------------------------------------------------------------------
# BH fold 口径
# ---------------------------------------------------------------------------


class BhTargetSideEquivalenceTest(unittest.TestCase):
    def test_real_case_matches_frozen_legacy(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        ruleset_view = _fake_ruleset(ruleset.target_side)
        comparisons = 0
        for component in (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS, damage.IGNITE_TAGS):
            penetration = sum(
                row.value for row in damage._penetration_rows(snapshot, component)
            )
            adapter = target_side.bh_target_side(
                ruleset_target_side=ruleset.target_side,
                scenario_target=scenario["target"],
                damage_type=component.damage_type,
                penetration_total=penetration,
            )
            legacy = legacy_bh_target_multiplier(
                ruleset.target_side, component, penetration, scenario
            )
            self.assertEqual(adapter, legacy, msg=component.component_id)
            # production call point is the shared operator
            self.assertEqual(
                damage._target_multiplier(ruleset_view, component, penetration, scenario),
                adapter,
            )
            comparisons += 1
        self.assertEqual(comparisons, 3)

    def test_grid_matches_frozen_legacy(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        base_side = dict(ruleset.target_side)
        component = damage.ComponentTags(
            "probe-fire", "Fire", frozenset({"Fire"}), frozenset({"Spell", "DoT"})
        )
        compared = 0
        for negative_allowed in (True, False):
            side = dict(base_side)
            side["negative_resistance_allowed"] = negative_allowed
            for resistance in (-0.3, 0.0, 0.4, 0.75):
                for penetration in (0.0, 0.15, 0.35):
                    for dr in (0.0, 0.54, 0.87):
                        scenario = {
                            "target": {
                                "resistances": {"fire": resistance},
                                "monster_level": 100,
                                "monster_damage_reduction": dr,
                            }
                        }
                        adapter = target_side.bh_target_side(
                            ruleset_target_side=side,
                            scenario_target=scenario["target"],
                            damage_type="Fire",
                            penetration_total=penetration,
                        )
                        legacy = legacy_bh_target_multiplier(
                            side, component, penetration, scenario
                        )
                        self.assertEqual(
                            adapter,
                            legacy,
                            msg=f"neg={negative_allowed} res={resistance} pen={penetration} dr={dr}",
                        )
                        compared += 1
        self.assertEqual(compared, 2 * 4 * 3 * 3)

    def test_penetration_scope_flag_is_honoured(self):
        side = dict(fixtures.bh_ruleset().target_side)
        side["penetration_applies_to"] = ["ignite"]  # body 不再吃穿透
        component = damage.BODY_FIRE_TAGS
        scenario = fixtures.bh_production_scenario()
        adapter = target_side.bh_target_side(
            ruleset_target_side=side,
            scenario_target=scenario["target"],
            damage_type=component.damage_type,
            penetration_total=0.59,
        )
        self.assertFalse(adapter["penetration_applied"])
        self.assertEqual(adapter["penetration"], 0.0)
        self.assertEqual(adapter["multiplier"], 0.13)
        self.assertEqual(
            adapter,
            legacy_bh_target_multiplier(side, component, 0.59, scenario),
        )

    def test_negative_resistance_clamp_only_when_disallowed(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        scenario["target"]["resistances"] = {"fire": -0.2, "cold": -0.2}
        component = damage.BODY_FIRE_TAGS
        allow_side = dict(ruleset.target_side)
        allow_side["negative_resistance_allowed"] = True
        clamp_side = dict(ruleset.target_side)
        clamp_side["negative_resistance_allowed"] = False
        allow = target_side.bh_target_side(
            ruleset_target_side=allow_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=0.0,
        )
        clamp = target_side.bh_target_side(
            ruleset_target_side=clamp_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=0.0,
        )
        self.assertEqual(allow["effective_resistance"], -0.2)
        self.assertEqual(allow["resistance_multiplier"], 1.2)
        self.assertEqual(clamp["effective_resistance"], 0.0)
        self.assertEqual(clamp["resistance_multiplier"], 1.0)
        self.assertTrue(allow["negative_resistance_allowed"])
        self.assertFalse(clamp["negative_resistance_allowed"])
        self.assertEqual(allow, legacy_bh_target_multiplier(allow_side, component, 0.0, scenario))
        self.assertEqual(clamp, legacy_bh_target_multiplier(clamp_side, component, 0.0, scenario))
        del snapshot  # 真实快照仅用于确认 fixture 可用

    def test_ignite_production_matches_frozen_legacy_with_explicit_source(self):
        """P3：ignite._ignite_target 走共享算子；抗性来源显式 = ruleset_base。"""
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        penetration = sum(
            row.value for row in damage._penetration_rows(fixtures.bh_snapshot(), damage.IGNITE_TAGS)
        )
        legacy = legacy_ignite_target(ruleset.target_side, penetration, scenario)
        adapter = target_side.bh_target_side(
            ruleset_target_side=ruleset.target_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=penetration,
            component_scope="ignite",
            resistance_source=target_side.RESISTANCE_FROM_RULESET_BASE,
        )
        self.assertEqual(adapter, legacy)
        from le_calc import ignite

        self.assertEqual(ignite._ignite_target(ruleset, penetration, scenario), legacy)
        # 生产场景里两种来源恰好相等（scenario 抗性 = ruleset 基础抗性）
        scenario_source = target_side.bh_target_side(
            ruleset_target_side=ruleset.target_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=penetration,
            component_scope="ignite",
            resistance_source=target_side.RESISTANCE_FROM_SCENARIO_TARGET,
        )
        self.assertEqual(scenario_source, adapter)

    def test_resistance_source_is_explicit_and_validated(self):
        with self.assertRaises(target_side.TargetSideError):
            target_side.bh_target_side(
                ruleset_target_side=fixtures.bh_ruleset().target_side,
                scenario_target={},
                damage_type="Fire",
                penetration_total=0.0,
                resistance_source="bogus",
            )
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        scenario["target"]["resistances"] = {"fire": 0.4, "cold": 0.4}
        base_source = target_side.bh_target_side(
            ruleset_target_side=ruleset.target_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=0.0,
            resistance_source=target_side.RESISTANCE_FROM_RULESET_BASE,
        )
        scenario_source = target_side.bh_target_side(
            ruleset_target_side=ruleset.target_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=0.0,
            resistance_source=target_side.RESISTANCE_FROM_SCENARIO_TARGET,
        )
        self.assertEqual(base_source["target_resistance"], 0.0)
        self.assertEqual(scenario_source["target_resistance"], 0.4)
        self.assertNotEqual(base_source["multiplier"], scenario_source["multiplier"])
        # 生产 ignite 保持历史 ruleset_base 语义（忽略 scenario 抗性覆盖）
        from le_calc import ignite

        self.assertEqual(ignite._ignite_target(ruleset, 0.0, scenario), base_source)


# ---------------------------------------------------------------------------
# FR separate 口径
# ---------------------------------------------------------------------------


class FrTargetSideEquivalenceTest(unittest.TestCase):
    def test_real_resolve_target_matches_frozen_legacy(self):
        ruleset = fixtures.fr_ruleset()
        scenario = fixtures.fr_scenario()
        adapter = target_side.resolve_hit_target(
            fixtures.merge_target_like_hit(ruleset, scenario["target"])
        )
        self.assertEqual(adapter, legacy_fr_resolve_target(ruleset, scenario))
        self.assertEqual(hit_damage.resolve_target(ruleset, scenario), adapter)

    def test_resolve_target_variants_match_frozen_legacy(self):
        ruleset = fixtures.fr_ruleset()
        base = fixtures.fr_scenario()
        variants = [
            {},
            {"monster_level": 50},
            {"fire_resistance": 0.4},
            {"monster_level": None},
            {"monster_damage_reduction": 0.5},  # 显式 DR 被 table_by_level 忽略
            {"boss_or_rare": False},
        ]
        for override in variants:
            scenario = {"target": {**base["target"], **override}}
            adapter = target_side.resolve_hit_target(
                fixtures.merge_target_like_hit(ruleset, scenario["target"])
            )
            self.assertEqual(
                adapter,
                legacy_fr_resolve_target(ruleset, scenario),
                msg=f"override={override}",
            )
            self.assertEqual(
                hit_damage.resolve_target(ruleset, scenario), adapter, msg=f"override={override}"
            )

    def test_component_target_matches_frozen_legacy(self):
        grid = (
            (0.0, 0.0, 0.87),
            (0.4, 0.35, 0.87),
            (0.0, 0.35, 0.87),
            (0.4, 0.0, 0.87),
            (-0.2, 0.1, 0.54),
            (0.75, 0.2, 0.0),
        )
        for resistance, penetration, dr in grid:
            result = target_side.hit_component_target(
                resistance=resistance,
                penetration_total=penetration,
                monster_damage_reduction=dr,
            )
            legacy = legacy_fr_component_target(
                penetration, 1.0 - resistance, 1.0 - dr
            )
            self.assertEqual(result.multiplier, legacy)
            self.assertEqual(result.penetration_multiplier, 1.0 + penetration)
            self.assertEqual(result.monster_damage_reduction, dr)
            self.assertEqual(result.monster_dr_factor, 1.0 - dr)


# ---------------------------------------------------------------------------
# 两种穿透口径的关系（严格相等 vs 预期差异）
# ---------------------------------------------------------------------------


class PenetrationModeRelationshipTest(unittest.TestCase):
    def test_fold_equals_separate_when_resistance_is_zero(self):
        for dr in (0.0, 0.54, 0.87):
            for penetration in (0.0, 0.15, 0.35, 1.0):
                fold = target_side.compute_target_multiplier(
                    policy=target_side.BH_TARGET_SIDE_POLICY,
                    resistance=0.0,
                    monster_damage_reduction=dr,
                    penetration=penetration,
                )
                separate = target_side.compute_target_multiplier(
                    policy=target_side.FR_HIT_TARGET_SIDE_POLICY,
                    resistance=0.0,
                    monster_damage_reduction=dr,
                    penetration=penetration,
                )
                self.assertEqual(fold.multiplier, separate.multiplier)
                self.assertEqual(fold.resistance_multiplier, separate.resistance_multiplier)

    def test_fold_equals_separate_when_penetration_is_zero(self):
        for dr in (0.0, 0.54, 0.87):
            for resistance in (-0.2, 0.0, 0.4, 0.75):
                fold = target_side.compute_target_multiplier(
                    policy=target_side.BH_TARGET_SIDE_POLICY,
                    resistance=resistance,
                    monster_damage_reduction=dr,
                    penetration=0.0,
                )
                separate = target_side.compute_target_multiplier(
                    policy=target_side.FR_HIT_TARGET_SIDE_POLICY,
                    resistance=resistance,
                    monster_damage_reduction=dr,
                    penetration=0.0,
                )
                self.assertEqual(fold.multiplier, separate.multiplier)

    def test_cross_term_difference_is_explicit(self):
        resistance, penetration, dr = 0.4, 0.35, 0.87
        fold = target_side.compute_target_multiplier(
            policy=target_side.BH_TARGET_SIDE_POLICY,
            resistance=resistance,
            monster_damage_reduction=dr,
            penetration=penetration,
        )
        separate = target_side.compute_target_multiplier(
            policy=target_side.FR_HIT_TARGET_SIDE_POLICY,
            resistance=resistance,
            monster_damage_reduction=dr,
            penetration=penetration,
        )
        # 交接文档 §2.1 的示例值
        self.assertEqual(fold.multiplier, 0.1235)
        self.assertEqual(separate.multiplier, 0.1053)
        self.assertNotEqual(fold.multiplier, separate.multiplier)
        relative_difference = (fold.multiplier - separate.multiplier) / fold.multiplier
        self.assertAlmostEqual(relative_difference, 0.14736842105263157, places=15)
        # 差异 = res·pen·(1-dr)（浮点最后一位以内）
        analytic = resistance * penetration * (1.0 - dr)
        self.assertAlmostEqual(
            fold.multiplier - separate.multiplier,
            analytic,
            delta=1e-15,
        )


# ---------------------------------------------------------------------------
# DR 来源：表查询 vs scenario override
# ---------------------------------------------------------------------------


class MonsterDRSourceTest(unittest.TestCase):
    def test_table_matches_describe_for_levels_1_to_100(self):
        for level in range(1, 101):
            info = monster_dr.describe(level)
            resolved = target_side.resolve_monster_dr(
                dr_source=target_side.DR_FROM_TABLE, monster_level=level
            )
            self.assertEqual(
                resolved.monster_damage_reduction, info["monster_damage_reduction"]
            )
            self.assertEqual(resolved.monster_dr_factor, info["monster_dr_factor"])
            self.assertEqual(resolved.monster_level, info["monster_level"])
            self.assertEqual(resolved.monster_damage_reduction, dr_for_level(level))

    def test_table_ignores_scenario_override_and_handles_disabled_levels(self):
        resolved = target_side.resolve_monster_dr(
            dr_source=target_side.DR_FROM_TABLE,
            monster_level=50,
            scenario_monster_damage_reduction=0.5,
        )
        self.assertEqual(resolved.monster_damage_reduction, dr_for_level(50))
        self.assertEqual(resolved.monster_damage_reduction, 0.54)
        self.assertEqual(
            target_side.resolve_monster_dr(
                dr_source=target_side.DR_FROM_TABLE, monster_level=0
            ).monster_damage_reduction,
            0.0,
        )
        self.assertEqual(
            target_side.resolve_monster_dr(
                dr_source=target_side.DR_FROM_TABLE, monster_level=None
            ).monster_damage_reduction,
            0.0,
        )

    def test_scenario_override_uses_explicit_value_and_defaults_to_zero(self):
        resolved = target_side.resolve_monster_dr(
            dr_source=target_side.DR_FROM_SCENARIO,
            monster_level=100,
            scenario_monster_damage_reduction=0.5,
        )
        self.assertEqual(resolved.monster_damage_reduction, 0.5)
        self.assertEqual(
            target_side.resolve_monster_dr(
                dr_source=target_side.DR_FROM_SCENARIO, monster_level=100
            ).monster_damage_reduction,
            0.0,
        )
        self.assertEqual(
            target_side.resolve_monster_dr(
                dr_source=target_side.DR_FROM_SCENARIO,
                monster_level=100,
                scenario_monster_damage_reduction=0.0,
            ).monster_damage_reduction,
            0.0,
        )

    def test_bh_adapter_uses_scenario_dr_even_with_different_level(self):
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario(monster_level=50)
        scenario["target"]["monster_damage_reduction"] = 0.5
        adapter = target_side.bh_target_side(
            ruleset_target_side=ruleset.target_side,
            scenario_target=scenario["target"],
            damage_type="Fire",
            penetration_total=0.0,
        )
        self.assertEqual(adapter["monster_damage_reduction"], 0.5)
        self.assertEqual(adapter["monster_dr_factor"], 0.5)
        self.assertEqual(adapter["monster_level"], 50)
        self.assertEqual(adapter["multiplier"], 0.5)


# ---------------------------------------------------------------------------
# 生产接入 golden（改造前实测值，逐位）
# ---------------------------------------------------------------------------

#: 2026-11-07 P2 改造前在生产输入（BH v4 快照/场景）上的实测值。
GOLDEN_BH_BODY_TOTAL = 1144337.768562139
GOLDEN_BH_COMPONENTS = {
    "bh-binary-fire": {
        "damage_total": 855847.1588571789,
        "per_tick_damage": 35660.298285715784,
        "per_tick_base": 223.2,
        "more_product": 39.80288,
        "increased_total": 18.419400000000003,
        "penetration": 0.59,
        "target": {
            "target_resistance": 0.0,
            "penetration": 0.59,
            "effective_resistance": -0.59,
            "resistance_multiplier": 1.5899999999999999,
            "monster_level": 100,
            "monster_damage_reduction": 0.87,
            "monster_dr_factor": 0.13,
            "multiplier": 0.2067,
            "negative_resistance_allowed": True,
            "penetration_applied": True,
        },
    },
    "bh-binary-cold": {
        "damage_total": 288490.60970496,
        "per_tick_damage": 12020.44207104,
        "per_tick_base": 223.2,
        "more_product": 26.624000000000002,
        "increased_total": 14.56,
        "penetration": 0,
        "target": {
            "target_resistance": 0.0,
            "penetration": 0,
            "effective_resistance": 0.0,
            "resistance_multiplier": 1.0,
            "monster_level": 100,
            "monster_damage_reduction": 0.87,
            "monster_dr_factor": 0.13,
            "multiplier": 0.13,
            "negative_resistance_allowed": True,
            "penetration_applied": True,
        },
    },
}

GOLDEN_FR_COMPONENT = {
    "component_id": "fr11mv:fire",
    "damage": 51831.5589095769,
    "target_multiplier": 0.13,
    "penetration": 1.0,
    "penetration_total": 0.0,
    "avg_crit": 2.4796725,
    "more_product": 6.9295584959999985,
    "increased_multiplier": 15.129999999999999,
    "target_detail": {
        "resistance_factor": 1.0,
        "monster_dr_factor": 0.13,
        "fire_resistance": 0.0,
        "monster_level": 100,
    },
}
GOLDEN_FR_TARGET = {
    "boss_or_rare": True,
    "monster_level": 100,
    "monster_damage_reduction": 0.87,
    "monster_dr_factor": 0.13,
    "fire_resistance": 0.0,
    "resistance_factor": 1.0,
    "source": "ruleset.target + scenario.target",
}


class ProductionIntegrationGoldenTest(unittest.TestCase):
    """生产调用点（已改调共享算子）必须复现改造前 golden。"""

    def test_bh_body_golden_unchanged(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        body = damage.compute_body(snapshot, ruleset, scenario)
        self.assertEqual(body["totals"]["damage"], GOLDEN_BH_BODY_TOTAL)
        self.assertEqual(len(body["components"]), len(GOLDEN_BH_COMPONENTS))
        for component in body["components"]:
            golden = GOLDEN_BH_COMPONENTS[component["component_id"]]
            self.assertEqual(component["damage_total"], golden["damage_total"])
            self.assertEqual(component["per_tick_damage"], golden["per_tick_damage"])
            self.assertEqual(component["per_tick_base"], golden["per_tick_base"])
            self.assertEqual(component["more_product"], golden["more_product"])
            self.assertEqual(component["increased"]["total"], golden["increased_total"])
            self.assertEqual(component["penetration"]["total"], golden["penetration"])
            self.assertEqual(component["target"], golden["target"])
            # 逐步重算：目标乘区来自共享算子，乘法顺序不变
            reconstructed = (
                component["per_tick_base"]
                * (1.0 + component["increased"]["total"])
                * component["more_product"]
                * component["target"]["multiplier"]
            )
            self.assertEqual(component["per_tick_damage"], reconstructed)

    def test_fr_single_hit_golden_unchanged(self):
        snapshot = fixtures.fr_snapshot()
        ruleset = fixtures.fr_ruleset()
        scenario = fixtures.fr_scenario()
        single = hit_damage.compute_single_hit(snapshot, ruleset, scenario)
        self.assertEqual(len(single["components"]), 1)
        component = single["components"][0]
        for key, expected in GOLDEN_FR_COMPONENT.items():
            self.assertEqual(component[key], expected, msg=key)
        self.assertEqual(single["totals"]["target"], GOLDEN_FR_TARGET)
        self.assertEqual(single["totals"]["damage"], GOLDEN_FR_COMPONENT["damage"])
        self.assertEqual(
            single["totals"]["target_multiplier"], GOLDEN_FR_COMPONENT["target_multiplier"]
        )
        # 生产 target 解析 = 冻结 legacy
        self.assertEqual(
            hit_damage.resolve_target(ruleset, scenario), GOLDEN_FR_TARGET
        )
        self.assertEqual(
            hit_damage.resolve_target(ruleset, scenario),
            legacy_fr_resolve_target(ruleset, scenario),
        )
        # 组件乘区 = 冻结 legacy 公式
        self.assertEqual(
            component["target_multiplier"],
            legacy_fr_component_target(
                component["penetration_total"],
                component["target_detail"]["resistance_factor"],
                component["target_detail"]["monster_dr_factor"],
            ),
        )

    def test_bh_body_component_count_and_no_crit(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        scenario = fixtures.bh_production_scenario()
        body = damage.compute_body(snapshot, ruleset, scenario)
        self.assertEqual(len(body["components"]), 2)
        for component in body["components"]:
            self.assertNotIn("avg_crit", component)
            self.assertNotIn("crit_chance", component)
            reconstructed = (
                component["per_tick_base"]
                * (1.0 + component["increased"]["total"])
                * component["more_product"]
                * component["target"]["multiplier"]
            )
            self.assertEqual(component["per_tick_damage"], reconstructed)


if __name__ == "__main__":
    unittest.main()
