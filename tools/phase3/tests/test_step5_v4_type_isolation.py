"""P1-A 修复的定向测试：命中伤害通道的类型隔离（``isolate_damage_channels``）。

背景：``docs/reviews/ballista-armed-construction-independent-review-20261113.zh-CN.md``
§2 指出公共 hit tags 里的 Fire 经 ``component_tags`` 拼进 Physical/Cold/... 的
标签闭包，使一条 ``[Bow, Fire]`` 的附加伤害行被记到全部七个伤害组件。本测试
文件只覆盖 ``dps_components.build_hit_components`` 的通用闭包/装配语义，不读取
案例或报告：

1. 显式带类型的 added 只进入对应类型通道；无类型 added 只进入
   ``adaptive_damage_type`` 通道；
2. increased/more/crit/penetration 行同样按隔离闭包匹配；
3. 未选中行在应用账本记为 ``not_applicable``（reason=标签不匹配该组件）；
4. 默认开启隔离；历史口径只允许显式关闭并声明 legacy added 策略，
   canonical JSON 指纹仅约束这条显式兼容调用；
5. 参数保持独立：``isolate=True`` 与 ``typed_added_policy="legacy"`` 同时出现时
   归一为 ``by_type`` 语义（隔离闭包下两者对 eligible 行等价；显式归一避免
   未定义行为）。

全部为合成输入直接调用纯函数，不写 examples/reports/knowledge。
"""
from __future__ import annotations

import hashlib
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import dps_components, model  # noqa: E402

DAMAGE_TYPES = dps_components.DAMAGE_TYPES

#: 修复前（P1-A 之前）默认参数路径的基线指纹：由同一 fixture 在改动前的
#: ``build_hit_components`` 上按 :func:`_fingerprint` 的 canonical JSON 计算。
#: 它守卫“默认路径逐字节不变”，VC/FR/BH 等既有案例依赖该口径。
LEGACY_FINGERPRINT = "d65d8238984565f1c59e89e29a69e9685fdef71c7705125c64467bea8afc9ce3"

TARGET = {"resistance": 0.2}
TARGET_INFO = {"monster_damage_reduction": 0.0}
#: 公共 hit tags：历史实现把这里的 Fire 拼进每个伤害通道的闭包（P1-A 串扰源）。
COMPONENT = {"tags": ["Bow", "Fire", "Minion"], "adaptive_damage_type": "Fire", "crit_capable": True}
SKILL = {
    "base_damage": {"Physical": 10.0, "Fire": 50.0},
    "added_damage_scaling": 2.5,
    "crit_chance": 0.05,
    "crit_multiplier": 2.0,
}


def _base_only_fire_skill() -> dict:
    return {
        "base_damage": {"Fire": 50.0},
        "added_damage_scaling": 2.5,
        "crit_chance": 0.05,
        "crit_multiplier": 2.0,
    }


def _contribution(contribution_id: str, **overrides):
    payload = {
        "contribution_id": contribution_id,
        "source_kind": "equipment",
        "source_label": contribution_id,
        "source_path": "test",
        "stat_name": model.STAT_DAMAGE,
        "stat_key": model.STAT_DAMAGE,
        "operation": "increased",
        "value": 0.0,
        "damage_tags": [],
        "group": None,
        "included": True,
        "condition_status": model.CONDITION_ALWAYS,
        "exclusion_reason": None,
    }
    payload.update(overrides)
    return model.Contribution(**payload)


def _added_damage(contribution_id: str, value: float, tags):
    return _contribution(
        contribution_id,
        stat_key=model.STAT_ADDED_DAMAGE,
        operation="added",
        value=value,
        damage_tags=list(tags),
    )


def _rich_contributions() -> list[model.Contribution]:
    """覆盖七种伤害类型闭包差异的合成贡献池（也用于 legacy 指纹守卫）。"""
    return [
        _added_damage("add:fire", 10.5, ["Bow", "Fire"]),
        _added_damage("add:cold", 7.0, ["Bow", "Cold"]),
        _added_damage("add:untyped", 3.0, ["Bow"]),
        _added_damage("add:physical", 4.0, ["Bow", "Physical"]),
        _contribution("inc:fire", value=0.5, damage_tags=["Fire"]),
        _contribution("inc:cold", value=0.25, damage_tags=["Cold"]),
        _contribution("inc:untyped", value=0.1),
        _contribution("more:fire", operation="more", value=0.2, damage_tags=["Fire"], group="more:fire"),
        _contribution("more:untyped", operation="more", value=0.3, group="more:untyped"),
        _contribution(
            "crit:chance:fire",
            stat_key=model.STAT_CRIT_CHANCE,
            operation="added",
            value=0.1,
            damage_tags=["Fire"],
        ),
        _contribution(
            "crit:multi:bow",
            stat_key=model.STAT_CRIT_MULTIPLIER,
            operation="added",
            value=0.5,
            damage_tags=["Bow"],
        ),
        _contribution(
            "pen:fire",
            stat_key=model.STAT_PENETRATION,
            operation="added",
            value=0.1,
            damage_tags=["Fire"],
        ),
        _contribution(
            "excluded:fire",
            value=0.9,
            damage_tags=["Fire"],
            included=False,
            exclusion_reason="输入已排除",
        ),
        _contribution(
            "cond:fire",
            value=0.4,
            damage_tags=["Fire"],
            condition_status=model.CONDITION_NOT_SATISFIED,
            exclusion_reason="条件未满足",
        ),
    ]


def _build(
    contributions,
    *,
    skill: dict | None = None,
    component: dict | None = None,
    isolate=None,
    typed_added_policy=None,
    damage_types=None,
    unit_scope=None,
):
    kwargs = {
        "contributions": list(contributions),
        "skill": dict(skill or SKILL),
        "component": dict(component or COMPONENT),
        "crit_rules": {},
        "target": dict(TARGET),
        "states": {},
        "target_info": dict(TARGET_INFO),
        "damage_types": damage_types,
        "unit_scope": unit_scope,
    }
    if isolate is not None:
        kwargs["isolate_damage_channels"] = isolate
    if typed_added_policy is not None:
        kwargs["typed_added_policy"] = typed_added_policy
    return dps_components.build_hit_components(**kwargs)


def _fingerprint(components, applications) -> str:
    payload = json.dumps(
        {"components": components, "applications": applications},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _by_type(components) -> dict[str, dict]:
    return {str(component["damage_type"]): component for component in components}


class ChannelIsolationTest(unittest.TestCase):
    """显式类型 added / 无类型 added 只进入正确的伤害通道。"""

    def test_fire_only_added_enters_only_fire_channel(self):
        rows = [_added_damage("add:fire", 10.5, ["Bow", "Fire"])]
        components, applications = _build(rows, skill=_base_only_fire_skill(), isolate=True)

        self.assertEqual([component["component_id"] for component in components], ["hit:Fire"])
        fire = components[0]
        self.assertEqual(fire["tags"], ["Fire", "Bow", "Minion", "Elemental"])
        self.assertAlmostEqual(fire["added"], 10.5, delta=1e-12)
        self.assertAlmostEqual(fire["target_multiplier"], 0.8, delta=1e-12)
        # (50 + 10.5×250%) × 暴击期望 1.05 × 目标乘区 0.8
        self.assertAlmostEqual(fire["damage"], 64.05, delta=1e-9)
        for other in DAMAGE_TYPES:
            if other != "Fire":
                self.assertNotIn(f"hit:{other}", {row["component_id"] for row in applications})
        self.assertEqual({row["component_id"] for row in applications}, {"hit:Fire"})

    def test_untyped_added_enters_only_adaptive_channel(self):
        skill = {
            "base_damage": {"Fire": 50.0, "Cold": 40.0},
            "added_damage_scaling": 2.5,
            "crit_chance": 0.05,
            "crit_multiplier": 2.0,
        }
        rows = [_added_damage("add:untyped", 3.0, ["Bow"])]
        components, _applications = _build(rows, skill=skill, isolate=True)

        by_type = _by_type(components)
        self.assertEqual(set(by_type), {"Fire", "Cold"})
        # adaptive_damage_type=Fire 是唯一接收无类型 added 的通道
        self.assertAlmostEqual(by_type["Fire"]["added"], 3.0, delta=1e-12)
        self.assertEqual(by_type["Cold"]["added"], 0.0)
        self.assertNotIn("add:untyped", by_type["Cold"]["source_ids"])

    def test_cold_typed_added_enters_only_cold_channel(self):
        cold_added = _added_damage("add:cold", 7.0, ["Bow", "Cold"])
        skill = {
            "base_damage": {"Cold": 40.0},
            "added_damage_scaling": 2.5,
            "crit_chance": 0.05,
            "crit_multiplier": 2.0,
        }
        components, _applications = _build([cold_added], skill=skill, isolate=True)
        self.assertEqual([component["component_id"] for component in components], ["hit:Cold"])
        self.assertAlmostEqual(components[0]["added"], 7.0, delta=1e-12)
        # (40 + 7×250%) × 1.05 × 0.8
        self.assertAlmostEqual(components[0]["damage"], 48.3, delta=1e-9)

        # 有 Fire 基伤时 Cold added 仍只进 Cold：既不改写 Fire 的 added，
        # 也不会因为公共 tags 含 Fire 而进入其它通道。
        components, _applications = _build(
            [cold_added], skill=_base_only_fire_skill(), isolate=True
        )
        by_type = _by_type(components)
        self.assertEqual(set(by_type), {"Fire", "Cold"})
        self.assertEqual(by_type["Fire"]["added"], 0.0)
        self.assertAlmostEqual(by_type["Cold"]["added"], 7.0, delta=1e-12)


class ActorScopeIsolationTest(unittest.TestCase):
    """actor 路径组合：unit_scope 隔离 + 伤害类型隔离（真实调用侧口径）。"""

    def test_actor_scope_filter_combined_with_type_isolation(self):
        actor_row = _added_damage("actor:added:fire", 10.5, ["Bow", "Fire"])
        actor_row.raw = {"scope": "actor"}
        owner_row = _contribution(
            "owner:inc:fire",
            value=0.9,
            damage_tags=["Fire"],
            raw={"scope": "owner"},
        )
        components, applications = _build(
            [actor_row, owner_row],
            skill=_base_only_fire_skill(),
            isolate=True,
            typed_added_policy="by_type",
            unit_scope="actor",
        )
        self.assertEqual([component["component_id"] for component in components], ["hit:Fire"])
        self.assertAlmostEqual(components[0]["added"], 10.5, delta=1e-12)
        # owner 行不进 actor 组件，账本记 scope 原因（优先于标签原因）。
        ledger = {
            (row["component_id"], row["contribution_id"]): row for row in applications
        }
        owner = ledger[("hit:Fire", "owner:inc:fire")]
        self.assertEqual(owner["status"], dps_components.NOT_APPLICABLE)
        self.assertIn("scope", owner["reason"])
        self.assertEqual(
            ledger[("hit:Fire", "actor:added:fire")]["status"], dps_components.APPLIED
        )


class IsolatedClosureTest(unittest.TestCase):
    """increased/more/crit/penetration 行按隔离闭包匹配。"""

    def test_increased_more_crit_penetration_follow_isolated_closure(self):
        components, _applications = _build(_rich_contributions(), isolate=True)
        by_type = _by_type(components)
        self.assertEqual(set(by_type), {"Physical", "Fire", "Cold"})

        physical, fire, cold = by_type["Physical"], by_type["Fire"], by_type["Cold"]
        # Fire 标签的 inc/more/crit/pen 不得进入 Physical（isolate 前的串扰点）。
        self.assertAlmostEqual(physical["increased_multiplier"], 1.1, delta=1e-12)  # 只有无标签 inc 0.1
        self.assertAlmostEqual(physical["more_multiplier"], 1.3, delta=1e-12)  # 只有无标签 more 0.3
        self.assertAlmostEqual(physical["crit_chance"], 0.05, delta=1e-12)  # 基础 5%，Fire crit 不适用
        self.assertEqual(physical["penetration"], 0.0)
        for excluded in ("inc:fire", "more:fire", "crit:chance:fire", "pen:fire"):
            self.assertNotIn(excluded, physical["source_ids"])
        # Bow 是无类型行为标签，对每个通道都有效。
        self.assertIn("crit:multi:bow", physical["source_ids"])
        self.assertIn("add:physical", physical["source_ids"])

        self.assertAlmostEqual(fire["increased_multiplier"], 1.6, delta=1e-12)  # 0.5 + 0.1
        self.assertAlmostEqual(fire["more_multiplier"], 1.56, delta=1e-12)  # (1+0.2)(1+0.3)
        self.assertAlmostEqual(fire["crit_chance"], 0.15, delta=1e-9)
        self.assertAlmostEqual(fire["penetration"], 0.1, delta=1e-12)
        self.assertAlmostEqual(fire["added"], 13.5, delta=1e-12)  # Fire 10.5 + 无类型 3.0

        # Cold 通道只保留 Cold/无标签/行为标签行，Fire 标签行被隔离。
        self.assertAlmostEqual(cold["increased_multiplier"], 1.35, delta=1e-12)  # 0.25 + 0.1
        self.assertAlmostEqual(cold["more_multiplier"], 1.3, delta=1e-12)
        self.assertAlmostEqual(cold["crit_chance"], 0.05, delta=1e-12)
        self.assertEqual(cold["penetration"], 0.0)
        self.assertNotIn("inc:fire", cold["source_ids"])
        self.assertIn("inc:cold", cold["source_ids"])

        # 组件伤害与自身乘区自洽（隔离不改变命中公式）。
        for component in components:
            expected = (
                (component["base"] + component["added"] * component["added_effectiveness"])
                * component["increased_multiplier"]
                * component["more_multiplier"]
                * component["average_crit"]
                * component["target_multiplier"]
            )
            self.assertAlmostEqual(component["damage"], expected, delta=1e-9)

    def test_unselected_rows_recorded_not_applicable(self):
        components, applications = _build(_rich_contributions(), isolate=True)
        self.assertIn("hit:Physical", {component["component_id"] for component in components})
        ledger = {
            (row["component_id"], row["contribution_id"]): row for row in applications
        }
        # 显式 Fire 类型的 added/increased 行在 Physical 通道不适用。
        for contribution_id in ("add:fire", "inc:fire", "more:fire", "crit:chance:fire", "pen:fire"):
            record = ledger[("hit:Physical", contribution_id)]
            self.assertEqual(record["status"], dps_components.NOT_APPLICABLE, contribution_id)
            self.assertEqual(record["reason"], "标签不匹配该组件", contribution_id)
        # 在 Fire 通道它们是 applied。
        for contribution_id in ("add:fire", "inc:fire", "more:fire", "crit:chance:fire", "pen:fire"):
            self.assertEqual(
                ledger[("hit:Fire", contribution_id)]["status"], dps_components.APPLIED
            )
        # 无标签 increased 对所有存在的通道适用。
        for component_id in ("hit:Physical", "hit:Fire", "hit:Cold"):
            self.assertEqual(
                ledger[(component_id, "inc:untyped")]["status"], dps_components.APPLIED
            )
        # 未包含/条件未满足的行保留原状态，不因隔离被误记为 applied。
        for contribution_id in ("excluded:fire", "cond:fire"):
            for component_id in ("hit:Physical", "hit:Fire", "hit:Cold"):
                self.assertEqual(
                    ledger[(component_id, contribution_id)]["status"],
                    dps_components.CONDITION_NOT_SATISFIED,
                )


class LegacyCompatibilityTest(unittest.TestCase):
    """默认使用正确隔离；历史指纹仅用于显式兼容调用。"""

    def test_default_path_matches_safe_isolated_path(self):
        components, applications = _build(_rich_contributions())
        isolated, apps = _build(_rich_contributions(), isolate=True, typed_added_policy="by_type")
        self.assertEqual(_fingerprint(components, applications), _fingerprint(isolated, apps))
        explicit, explicit_apps = _build(_rich_contributions(), isolate=False, typed_added_policy="legacy")
        self.assertEqual(_fingerprint(explicit, explicit_apps), LEGACY_FINGERPRINT)

    def test_legacy_path_keeps_historical_type_leak(self):
        components, _applications = _build(_rich_contributions(), isolate=False, typed_added_policy="legacy")
        by_type = _by_type(components)
        # 修复前的口径：公共 tags 含 Fire，于是 Fire added 行串入其余六种组件。
        self.assertEqual(set(by_type), set(DAMAGE_TYPES))
        for damage_type in ("Lightning", "Void", "Necrotic", "Poison"):
            self.assertAlmostEqual(by_type[damage_type]["added"], 10.5, delta=1e-12)
        # Fire 标签的 increased 也作用于 Physical 组件（历史串扰）。
        self.assertAlmostEqual(by_type["Physical"]["increased_multiplier"], 1.6, delta=1e-12)
        self.assertIn("add:fire", by_type["Physical"]["source_ids"])

        # 开启隔离后同一 fixture 不再产生这四类纯串扰组件。
        isolated, _apps = _build(_rich_contributions(), isolate=True)
        isolated_ids = {component["component_id"] for component in isolated}
        for damage_type in ("Lightning", "Void", "Necrotic", "Poison"):
            self.assertNotIn(f"hit:{damage_type}", isolated_ids)


class ComponentTagsTest(unittest.TestCase):
    """``component_tags`` 闭包：默认口径与隔离口径。"""

    def test_component_tags_isolates_by_default(self):
        self.assertEqual(
            dps_components.component_tags(COMPONENT, "Physical"),
            ["Physical", "Bow", "Minion"],
        )
        self.assertEqual(
            dps_components.component_tags(COMPONENT, "Fire"),
            ["Fire", "Bow", "Minion", "Elemental"],
        )

    def test_component_tags_isolates_other_damage_types(self):
        self.assertEqual(
            dps_components.component_tags(COMPONENT, "Physical", isolate_damage_channels=True),
            ["Physical", "Bow", "Minion"],
        )
        # 当前通道的 damage_type 始终保留在闭包里（即使组件 tags 也声明了它）。
        self.assertEqual(
            dps_components.component_tags(COMPONENT, "Fire", isolate_damage_channels=True),
            ["Fire", "Bow", "Minion", "Elemental"],
        )
        self.assertEqual(
            dps_components.component_tags(COMPONENT, "Cold", isolate_damage_channels=True),
            ["Cold", "Bow", "Minion", "Elemental"],
        )

    def test_tag_ok_follows_isolation_flag(self):
        fire_row = _contribution("inc:fire", value=0.5, damage_tags=["Fire"])
        untyped_row = _contribution("inc:untyped", value=0.1)
        self.assertFalse(dps_components._tag_ok(fire_row, COMPONENT, "Physical"))
        self.assertTrue(dps_components._tag_ok(fire_row, COMPONENT, "Physical", isolate_damage_channels=False))
        self.assertFalse(
            dps_components._tag_ok(fire_row, COMPONENT, "Physical", isolate_damage_channels=True)
        )
        self.assertTrue(
            dps_components._tag_ok(fire_row, COMPONENT, "Fire", isolate_damage_channels=True)
        )
        self.assertTrue(dps_components._tag_ok(untyped_row, COMPONENT, "Physical", isolate_damage_channels=True))


class ParameterIndependenceTest(unittest.TestCase):
    """``isolate_damage_channels`` 与 ``typed_added_policy`` 保持显式独立。"""

    def test_isolate_with_legacy_policy_normalizes_to_by_type(self):
        legacy_components, legacy_applications = _build(
            _rich_contributions(), isolate=True, typed_added_policy="legacy"
        )
        by_type_components, by_type_applications = _build(
            _rich_contributions(), isolate=True, typed_added_policy="by_type"
        )
        self.assertEqual(
            _fingerprint(legacy_components, legacy_applications),
            _fingerprint(by_type_components, by_type_applications),
        )

    def test_added_policy_independent_when_isolation_off(self):
        legacy_components, _apps = _build(_rich_contributions(), isolate=False, typed_added_policy="legacy")
        by_type_components, _apps = _build(
            _rich_contributions(), isolate=False, typed_added_policy="by_type"
        )
        legacy_ids = {component["component_id"] for component in legacy_components}
        by_type_ids = {component["component_id"] for component in by_type_components}
        self.assertIn("hit:Void", legacy_ids)  # legacy：Fire added 串入 Void
        self.assertNotIn("hit:Void", by_type_ids)  # by_type：带类型 added 只进对应通道
        # 但 by_type 仍保留公共 tags 造成的 inc/crit 串扰（隔离参数独立控制）。
        by_type = _by_type(by_type_components)
        self.assertIn("inc:fire", by_type["Physical"]["source_ids"])
        self.assertAlmostEqual(by_type["Physical"]["increased_multiplier"], 1.6, delta=1e-12)


if __name__ == "__main__":
    unittest.main()
