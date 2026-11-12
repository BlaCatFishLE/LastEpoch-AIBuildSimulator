"""标签适用性（tag_match）等价性测试。

对两个真实案例的快照 tag 集 + AT 词表合成集：
* 共享算子 + BH 策略 必须与 ``damage.tags_match`` 逐条相等；
* 共享算子 + FR 策略 必须与 ``hit_damage.tags_apply`` 逐条相等；
* 显式记录两类**策略差异**（metadata 忽略 / 规则外标签忽略）与一类
  **组件定义差异**（BH 组件闭包含 Spell/DoT、FR 闭包含 Melee/Hit），
  不把差异当成错误去“统一”。
"""

from __future__ import annotations

import itertools
import unittest

from le_calc import damage, hit_damage, model
from le_calc.mechanics import tag_match

import test_mechanics_fixtures as fixtures

#: 真实数据里没有、用于验证未知标签策略的合成标签
SYNTHETIC_UNKNOWN_TAGS = ("Frostburn", "Arcane", "Elemental_Burst", "Voidfire")


# ---------------------------------------------------------------------------
# 冻结的改造前实现（P3 生产切换的独立参考，逐行抄录自切换前 damage.py /
# hit_damage.py；常量也在此冻结，避免与生产模块一起漂移）
# ---------------------------------------------------------------------------

LEGACY_DAMAGE_TYPE_TAGS = frozenset(
    {"Physical", "Lightning", "Cold", "Fire", "Void", "Necrotic", "Poison"}
)
LEGACY_ELEMENT_TYPE_TAGS = frozenset({"Fire", "Cold", "Lightning"})
LEGACY_CATEGORY_TAGS = frozenset(
    {
        "Spell", "DoT", "Hit", "Ailment", "Melee", "Throwing", "Bow",
        "Channelling", "Minion", "Totem", "Buff",
    }
)
LEGACY_IGNORED_TAGS = frozenset(
    {
        "Elemental", "LowLife", "HighLife", "FullLife", "Curse", "Transform",
        "Potion", "PetResisted", "Crit_deprecated", "Kill_deprecated",
        "Die_deprecated",
    }
)



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def legacy_bh_tags_match(contribution_tags, component) -> bool:
    for tag in contribution_tags:
        if tag in LEGACY_IGNORED_TAGS:
            continue
        if tag in LEGACY_ELEMENT_TYPE_TAGS:
            if tag not in component.types:
                return False
        elif tag in LEGACY_DAMAGE_TYPE_TAGS:
            if tag not in component.types:
                return False
        elif tag in LEGACY_CATEGORY_TAGS:
            if tag not in component.categories:
                return False
        else:
            continue
    return True


def legacy_hit_tags_apply(damage_tags, component_tags) -> bool:
    tags = set(damage_tags or [])
    component = set(component_tags or [])
    if not tags:
        return True
    if tags <= component:
        return True
    if "Elemental" in tags and (tags - {"Elemental"}) <= component:
        return True
    return False


def _bh_components():
    return (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS, damage.IGNITE_TAGS)


def _fr_component_closures():
    ruleset = fixtures.fr_ruleset()
    components = ruleset.data["damage_model"]["components"]
    return [
        (str(component.get("component_id")), hit_damage._component_tags(component))
        for component in components
    ]


class TagPolicyTest(unittest.TestCase):
    def test_bh_policy_matches_production_constants(self):
        policy = tag_match.bh_tag_policy()
        self.assertEqual(policy.ignored_tags, frozenset(damage.IGNORED_TAGS))
        self.assertEqual(
            policy.known_tags,
            frozenset(
                damage.ELEMENT_TYPE_TAGS | damage.DAMAGE_TYPE_TAGS | damage.CATEGORY_TAGS
            ),
        )
        self.assertEqual(policy.unknown_tag_policy, tag_match.UNKNOWN_IGNORE)

    def test_hit_policy_matches_production_semantics(self):
        policy = tag_match.hit_tag_policy()
        self.assertEqual(policy.ignored_tags, frozenset({"Elemental"}))
        self.assertEqual(policy.known_tags, frozenset())
        self.assertEqual(policy.unknown_tag_policy, tag_match.UNKNOWN_RESTRICT)

    def test_invalid_unknown_policy_rejected(self):
        with self.assertRaises(tag_match.TagPolicyError):
            tag_match.TagPolicy(
                ignored_tags=frozenset(), known_tags=frozenset(), unknown_tag_policy="bogus"
            )

    def test_bh_component_closure_folds_types_and_categories(self):
        self.assertEqual(
            tag_match.bh_component_tags(damage.BODY_FIRE_TAGS),
            frozenset({"Fire"}) | frozenset({"Spell", "DoT"}),
        )


class BhTagMatchEquivalenceTest(unittest.TestCase):
    def test_real_snapshot_tags_match_production(self):
        policy = tag_match.bh_tag_policy()
        snapshot = fixtures.bh_snapshot()
        compared = 0
        for component in _bh_components():
            closure = tag_match.bh_component_tags(component)
            for contribution in snapshot.contributions:
                self.assertEqual(
                    tag_match.tags_match(contribution.damage_tags, closure, policy),
                    damage.tags_match(contribution.damage_tags, component),
                    msg=f"{component.component_id}/{contribution.contribution_id}",
                )
                compared += 1
        self.assertGreater(compared, 100)

    def test_synthetic_tag_sets_match_production(self):
        policy = tag_match.bh_tag_policy()
        all_tags = sorted(model.AT_BITS) + list(SYNTHETIC_UNKNOWN_TAGS)
        tag_sets = [[tag] for tag in all_tags]
        tag_sets += [list(pair) for pair in itertools.combinations(sorted(model.AT_BITS), 2)]
        tag_sets += [
            ["Elemental", "DoT"],
            ["Elemental", "Spell"],
            ["Fire", "Melee"],
            ["Fire", "Hit"],
            ["Frostburn", "Spell"],
            [],
        ]
        for component in _bh_components():
            closure = tag_match.bh_component_tags(component)
            for tags in tag_sets:
                self.assertEqual(
                    tag_match.tags_match(tags, closure, policy),
                    damage.tags_match(tags, component),
                    msg=f"{component.component_id}/{tags}",
                )


class HitTagMatchEquivalenceTest(unittest.TestCase):
    def test_real_snapshot_tags_match_production(self):
        policy = tag_match.hit_tag_policy()
        snapshot = fixtures.fr_snapshot()
        compared = 0
        for component_id, closure in _fr_component_closures():
            for contribution in snapshot.contributions:
                self.assertEqual(
                    tag_match.tags_match(contribution.damage_tags, closure, policy),
                    hit_damage.tags_apply(contribution.damage_tags, closure),
                    msg=f"{component_id}/{contribution.contribution_id}",
                )
                compared += 1
        self.assertGreater(compared, 100)

    def test_synthetic_tag_sets_match_production(self):
        policy = tag_match.hit_tag_policy()
        all_tags = sorted(model.AT_BITS) + list(SYNTHETIC_UNKNOWN_TAGS)
        tag_sets = [[tag] for tag in all_tags]
        tag_sets += [list(pair) for pair in itertools.combinations(sorted(model.AT_BITS), 2)]
        tag_sets += [
            ["Elemental"],
            ["Elemental", "Melee"],
            ["Elemental", "Spell"],
            ["Frostburn", "Melee"],
            ["Fire", "Hit", "Spell"],
            [],
        ]
        for component_id, closure in _fr_component_closures():
            for tags in tag_sets:
                self.assertEqual(
                    tag_match.tags_match(tags, closure, policy),
                    hit_damage.tags_apply(tags, closure),
                    msg=f"{component_id}/{tags}",
                )


class ProductionSwitchVsFrozenLegacyTest(unittest.TestCase):
    """P3 切换后：生产调用点必须与冻结的改造前 matcher 逐条相等。"""

    @staticmethod
    def _tag_sets():
        all_tags = sorted(model.AT_BITS) + list(SYNTHETIC_UNKNOWN_TAGS)
        tag_sets = [[tag] for tag in all_tags]
        tag_sets += [list(pair) for pair in itertools.combinations(sorted(model.AT_BITS), 2)]
        tag_sets += [
            ["Elemental", "DoT"],
            ["Elemental", "Spell"],
            ["Fire", "Melee"],
            ["Fire", "Hit"],
            ["Frostburn", "Spell"],
            [],
        ]
        return tag_sets

    def test_engine_components_have_disjoint_type_category_sets(self):
        # 闭包折叠 `types | categories` 与旧实现按桶检查等价的前提
        known_tags = LEGACY_DAMAGE_TYPE_TAGS | LEGACY_ELEMENT_TYPE_TAGS | LEGACY_CATEGORY_TAGS
        for component in _bh_components():
            self.assertEqual(frozenset(component.types) & frozenset(component.categories), frozenset())
            closure = frozenset(component.types) | frozenset(component.categories)
            for tag in known_tags:
                self.assertEqual(tag in closure, legacy_bh_tags_match([tag], component))

    def test_production_bh_matcher_matches_frozen_legacy(self):
        snapshot = fixtures.bh_snapshot()
        compared = 0
        for component in _bh_components():
            for contribution in snapshot.contributions:
                self.assertEqual(
                    damage.tags_match(contribution.damage_tags, component),
                    legacy_bh_tags_match(contribution.damage_tags, component),
                    msg=f"{component.component_id}/{contribution.contribution_id}",
                )
                compared += 1
            for tags in self._tag_sets():
                self.assertEqual(
                    damage.tags_match(tags, component),
                    legacy_bh_tags_match(tags, component),
                    msg=f"{component.component_id}/{tags}",
                )
                compared += 1
        self.assertGreater(compared, 500)

    def test_production_hit_matcher_matches_frozen_legacy(self):
        snapshot = fixtures.fr_snapshot()
        compared = 0
        for component_id, closure in _fr_component_closures():
            for contribution in snapshot.contributions:
                self.assertEqual(
                    hit_damage.tags_apply(contribution.damage_tags, closure),
                    legacy_hit_tags_apply(contribution.damage_tags, closure),
                    msg=f"{component_id}/{contribution.contribution_id}",
                )
                compared += 1
            for tags in self._tag_sets():
                self.assertEqual(
                    hit_damage.tags_apply(tags, closure),
                    legacy_hit_tags_apply(tags, closure),
                    msg=f"{component_id}/{tags}",
                )
                compared += 1
        self.assertGreater(compared, 500)


class TagDifferenceClassTest(unittest.TestCase):
    """差异必须只落在登记过的类别内（同一组件闭包下比较两个策略）。"""

    def test_policy_differences_are_classified(self):
        bh_policy = tag_match.bh_tag_policy()
        hit_policy = tag_match.hit_tag_policy()
        closure = frozenset({"Fire", "Spell", "DoT"})
        all_tags = sorted(model.AT_BITS) + list(SYNTHETIC_UNKNOWN_TAGS)
        observed_classes: dict[str, list[str]] = {}
        for tag in all_tags:
            cls = tag_match.classify_difference(
                tag, closure, bh_policy=bh_policy, hit_policy=hit_policy
            )
            self.assertIn(cls, tag_match.DIFFERENCE_CLASSES)
            observed_classes.setdefault(cls, []).append(tag)
        # metadata/ignored 标签：BH 忽略、FR 限制
        metadata_tags = [tag for tag in observed_classes.get(tag_match.DIFF_BH_METADATA_IGNORED, [])]
        self.assertIn("LowLife", metadata_tags)
        self.assertIn("Curse", metadata_tags)
        self.assertIn("PetResisted", metadata_tags)
        for tag in metadata_tags:
            self.assertIn(tag, bh_policy.ignored_tags)
        # 规则外标签：BH 忽略、FR 限制
        unknown_tags = observed_classes.get(tag_match.DIFF_BH_UNKNOWN_IGNORED, [])
        self.assertEqual(sorted(unknown_tags), sorted(SYNTHETIC_UNKNOWN_TAGS))
        # Elemental 不是差异类（两边都跳过/展开等价）
        self.assertEqual(
            tag_match.classify_difference(
                "Elemental", closure, bh_policy=bh_policy, hit_policy=hit_policy
            ),
            tag_match.DIFF_NONE,
        )

    def test_classification_reports_every_tag(self):
        rows = tag_match.classify_tag_set_differences(
            ["LowLife", "Fire", "Frostburn"],
            frozenset({"Fire"}),
            bh_policy=tag_match.bh_tag_policy(),
            hit_policy=tag_match.hit_tag_policy(),
        )
        self.assertEqual([row["tag"] for row in rows], ["LowLife", "Fire", "Frostburn"])
        self.assertEqual(
            [row["difference_class"] for row in rows],
            [
                tag_match.DIFF_BH_METADATA_IGNORED,
                tag_match.DIFF_NONE,
                tag_match.DIFF_BH_UNKNOWN_IGNORED,
            ],
        )

    def test_component_definition_difference_is_recorded(self):
        """同一标签在两模型组件定义不同时结果不同——这是输入差异，不是 matcher 差异。"""
        bh_fire = tag_match.bh_component_tags(damage.BODY_FIRE_TAGS)
        _, fr_closure = _fr_component_closures()[0]
        bh_only = bh_fire - set(fr_closure)
        fr_only = set(fr_closure) - bh_fire
        self.assertIn("Spell", bh_only)
        self.assertIn("DoT", bh_only)
        self.assertIn("Melee", fr_only)
        self.assertIn("Hit", fr_only)
        # 记录差异形态：["Spell"] 在 BH 本体组件适用、在 FR 组件不适用
        self.assertTrue(damage.tags_match(["Spell"], damage.BODY_FIRE_TAGS))
        self.assertFalse(hit_damage.tags_apply(["Spell"], fr_closure))


if __name__ == "__main__":
    unittest.main()
