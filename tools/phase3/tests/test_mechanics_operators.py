"""increased / more / crit 三个算子的等价性测试。

对真实案例输入，共享算子的输出必须与既有实现逐位相等：

* increased 加算池：BH ``damage._collect_increased``、FR 组件 ``increased`` 池；
* more 分组乘积：BH ``damage.group_more_rows``、FR ``hit_damage._more_groups``；
* crit 期望：FR ``_crit_section`` 的 chance/multiplier/avg；BH capability=False。

另加分组语义测试：``group`` 只能来自显式数据字段（``Contribution.group`` /
ruleset effect group），没有分组时以 contribution_id 为组键，不根据文本推定。
"""

from __future__ import annotations

import unittest

from le_calc import damage, hit_damage, hit_simulate, ignite, model
from le_calc.mechanics import crit_expected, increased_aggregate, more_groups

import test_mechanics_fixtures as fixtures


# ---------------------------------------------------------------------------
# 冻结的改造前实现（P3 生产切换的独立参考）
# ---------------------------------------------------------------------------



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def legacy_increased_pool(values, *, attribute_increase: float = 0.0):
    """切换前 `sum(rows) + attribute` 加算池 → ``(total, factor)``。"""
    total = sum(values) + attribute_increase
    return total, 1.0 + total


def legacy_more_groups(rows):
    """切换前 more 分组实现（BH ModifierRow / dict 行都支持）→ ``(product, groups)``。"""
    grouped: dict[str, float] = {}
    ordered_ids: dict[str, list[str]] = {}
    for row in rows:
        if isinstance(row, dict):
            value = float(row.get("value") or 0.0)
            cid = str(row.get("contribution_id"))
            key = row.get("group") or cid
        else:
            value = float(row.value)
            cid = str(row.contribution_id)
            key = row.group or cid
        grouped[key] = grouped.get(key, 0.0) + value
        ordered_ids.setdefault(key, []).append(cid)
    product = 1.0
    groups = []
    for key in sorted(grouped):
        factor = 1.0 + grouped[key]
        product *= factor
        groups.append((key, grouped[key], factor, ordered_ids[key]))
    return product, groups


def legacy_crit(chance_pools, multiplier_pools, chance_cap: float):
    """切换前 avg crit 公式：``(base+added)*(1+inc)*more`` → avg。"""
    chance = (
        (chance_pools[0] + chance_pools[1])
        * (1.0 + chance_pools[2])
        * chance_pools[3]
    )
    chance = max(0.0, min(chance, chance_cap))
    multiplier = (
        (multiplier_pools[0] + multiplier_pools[1])
        * (1.0 + multiplier_pools[2])
        * multiplier_pools[3]
    )
    return chance, multiplier, (1.0 - chance) + chance * multiplier


def _fr_states():
    return hit_damage.resolve_states(
        fixtures.fr_ruleset(),
        fixtures.fr_scenario(),
        section="single_hit",
        snapshot=fixtures.fr_snapshot(),
    )


def _fr_component_tags():
    ruleset = fixtures.fr_ruleset()
    component = ruleset.data["damage_model"]["components"][0]
    return hit_damage._component_tags(component)


def _fr_single_hit():
    return hit_damage.compute_single_hit(
        fixtures.fr_snapshot(), fixtures.fr_ruleset(), fixtures.fr_scenario()
    )


class IncreasedAggregateEquivalenceTest(unittest.TestCase):
    def test_bh_increased_pool_matches_production(self):
        snapshot = fixtures.bh_snapshot()
        for attribute_increase in (0.0, 0.31, 1.0):
            for component in (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS):
                observed = damage._collect_increased(snapshot, component, attribute_increase)
                rows = damage.collect_modifier_rows(
                    snapshot, model.STAT_DAMAGE, component, ("increased",)
                )
                aggregate = increased_aggregate.aggregate(
                    [row.value for row in rows],
                    attribute_increase=attribute_increase,
                )
                self.assertEqual(aggregate.total, observed["total"])
                self.assertEqual(aggregate.factor, observed["factor"])
                self.assertEqual(aggregate.row_count, len(rows))
                self.assertEqual(aggregate.rows_total, sum(row.value for row in rows))

    def test_fr_increased_pool_matches_production(self):
        single = _fr_single_hit()
        component = single["components"][0]
        states = _fr_states()
        tags = _fr_component_tags()
        collected = hit_damage.collect_rows(fixtures.fr_snapshot(), fixtures.fr_ruleset(), states)
        rows = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_DAMAGE
            and row["operation"] == "increased"
            and hit_damage.tags_apply(row["damage_tags"], tags)
        ]
        aggregate = increased_aggregate.aggregate([row["value"] for row in rows])
        self.assertEqual(aggregate.total, component["increased"])
        self.assertEqual(aggregate.factor, component["increased_multiplier"])
        self.assertEqual(aggregate.attribute_increase, 0.0)

    def test_empty_pool(self):
        empty = increased_aggregate.aggregate([])
        self.assertEqual(empty.total, 0.0)
        self.assertEqual(empty.factor, 1.0)
        self.assertEqual(empty.row_count, 0)

    def test_rows_helper_accepts_objects_and_dicts(self):
        class Row:
            value = 0.25

        self.assertEqual(
            increased_aggregate.aggregate_rows([Row(), {"value": 0.75}]).total, 1.0
        )

    # -- P3 生产切换：生产调用点 vs 冻结的改造前实现 -------------------------

    def test_production_bh_increased_matches_frozen_legacy(self):
        snapshot = fixtures.bh_snapshot()
        for attribute_increase in (0.0, 0.31, 1.0):
            for component in (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS):
                rows = damage.collect_modifier_rows(
                    snapshot, model.STAT_DAMAGE, component, ("increased",)
                )
                expected = legacy_increased_pool(
                    [row.value for row in rows], attribute_increase=attribute_increase
                )
                observed = damage._collect_increased(snapshot, component, attribute_increase)
                self.assertEqual((observed["total"], observed["factor"]), expected)

    def test_production_ignite_increased_matches_frozen_legacy(self):
        snapshot = fixtures.bh_snapshot()
        ruleset = fixtures.bh_ruleset()
        attribute = ruleset.black_hole.get("attribute_scaling", {})
        attribute_increase = float(attribute.get("increased_per_point", 0.0)) * snapshot.attribute(
            attribute.get("attribute", "intelligence")
        )
        rows = damage.collect_modifier_rows(
            snapshot, model.STAT_DAMAGE, damage.IGNITE_TAGS, ("increased",)
        )
        expected = legacy_increased_pool(
            [row.value for row in rows], attribute_increase=attribute_increase
        )
        observed = ignite._ignite_scaling(snapshot, ruleset)
        self.assertEqual(
            (observed["increased_total"], observed["increased_factor"]), expected
        )

    def test_production_fr_increased_matches_frozen_legacy(self):
        single = _fr_single_hit()
        component = single["components"][0]
        states = _fr_states()
        tags = _fr_component_tags()
        collected = hit_damage.collect_rows(fixtures.fr_snapshot(), fixtures.fr_ruleset(), states)
        rows = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_DAMAGE
            and row["operation"] == "increased"
            and hit_damage.tags_apply(row["damage_tags"], tags)
        ]
        expected = legacy_increased_pool([row["value"] for row in rows])
        self.assertEqual((component["increased"], component["increased_multiplier"]), expected)


class MoreGroupsEquivalenceTest(unittest.TestCase):
    def _bh_rows(self, component):
        ruleset = fixtures.bh_ruleset()
        snapshot = fixtures.bh_snapshot()
        scenario = fixtures.bh_production_scenario()
        player_more = list(
            damage.collect_modifier_rows(snapshot, model.STAT_DAMAGE, component, ("more",))
        )
        keys = {"body", "body_fire" if component is damage.BODY_FIRE_TAGS else "body_cold"}
        tree_rows = []
        for effect in ruleset.tree_more_effects(keys):
            if not damage.tags_match(effect.get("damage_tags", []), component):
                continue
            if not damage.condition_met(effect.get("condition_code"), scenario):
                continue
            tree_rows.append(
                damage.ModifierRow(
                    contribution_id=f"ruleset:node:{effect['node_id']}",
                    label=effect.get("name") or "",
                    value=float(effect["value"]),
                    source_kind="skill_node",
                    source_path=f"bh2.node.{effect['node_id']}",
                    status="confirmed",
                    damage_tags=list(effect.get("damage_tags", [])),
                    group=effect.get("group"),
                    rule_id=effect.get("rule_id"),
                )
            )
        return player_more, tree_rows

    def test_bh_more_groups_match_production(self):
        for component in (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS):
            player_more, tree_rows = self._bh_rows(component)
            rows = player_more + tree_rows
            aggregate = more_groups.aggregate(more_groups.rows_from_modifier_rows(rows))
            legacy_groups = damage.group_more_rows(rows)
            legacy_product = 1.0
            for group in legacy_groups:
                legacy_product *= group["factor"]
            self.assertEqual(aggregate.product, legacy_product)
            self.assertEqual(
                [(group.group, group.value, group.factor) for group in aggregate.groups],
                [(group["group"], group["value"], group["factor"]) for group in legacy_groups],
            )
            # 生产 compute_body 的 more_product 也来自同一组行
            body = damage.compute_body(
                fixtures.bh_snapshot(), fixtures.bh_ruleset(), fixtures.bh_production_scenario()
            )
            component_payload = next(
                item for item in body["components"] if item["component_id"].endswith(
                    "fire" if component is damage.BODY_FIRE_TAGS else "cold"
                )
            )
            self.assertEqual(aggregate.product, component_payload["more_product"])

    def test_fr_more_groups_match_production(self):
        single = _fr_single_hit()
        component = single["components"][0]
        states = _fr_states()
        tags = _fr_component_tags()
        collected = hit_damage.collect_rows(fixtures.fr_snapshot(), fixtures.fr_ruleset(), states)
        rows = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_DAMAGE
            and row["operation"] == "more"
            and hit_damage.tags_apply(row["damage_tags"], tags)
        ]
        aggregate = more_groups.aggregate(more_groups.rows_from_hit_rows(rows))
        legacy_product, legacy_groups = hit_damage._more_groups(collected["rows"], tags)
        self.assertEqual(aggregate.product, legacy_product)
        self.assertEqual(aggregate.product, component["more_product"])
        self.assertEqual(
            [(group.group, group.value, group.factor) for group in aggregate.groups],
            [(group["group"], group["value"], group["multiplier"]) for group in legacy_groups],
        )

    def test_group_key_comes_from_explicit_field_only(self):
        rows = [
            more_groups.MoreRow("row-a", 0.30, group="node:one"),
            more_groups.MoreRow("row-b", 0.20, group="node:one"),
            more_groups.MoreRow("row-c", 0.50),
            more_groups.MoreRow("row-d", 0.50),
        ]
        aggregate = more_groups.aggregate(rows)
        self.assertEqual([group.group for group in aggregate.groups], ["node:one", "row-c", "row-d"])
        self.assertEqual(aggregate.groups[0].value, 0.5)
        self.assertEqual(aggregate.groups[0].factor, 1.5)
        # (1+0.5) * (1+0.5) * (1+0.5): 组内加算、组间乘算
        self.assertEqual(aggregate.product, 1.5 * 1.5 * 1.5)

    def test_missing_group_falls_back_to_contribution_id(self):
        # 两行文本/来源相同但 contribution_id 不同且无 group -> 仍是两个独立乘区
        rows = [
            more_groups.MoreRow("same:1", 0.2),
            more_groups.MoreRow("same:2", 0.2),
        ]
        aggregate = more_groups.aggregate(rows)
        self.assertEqual(len(aggregate.groups), 2)
        self.assertAlmostEqual(aggregate.product, 1.44, places=15)

    def test_group_order_is_sorted_by_name(self):
        rows = [
            more_groups.MoreRow("z", 0.1, group="z-group"),
            more_groups.MoreRow("a", 0.2, group="a-group"),
        ]
        aggregate = more_groups.aggregate(rows)
        self.assertEqual([group.group for group in aggregate.groups], ["a-group", "z-group"])
        self.assertEqual(aggregate.product, 1.2 * 1.1)

    def test_row_indices_preserve_input_order(self):
        rows = [
            more_groups.MoreRow("a", 0.1, group="g"),
            more_groups.MoreRow("b", 0.2),
            more_groups.MoreRow("c", 0.3, group="g"),
        ]
        aggregate = more_groups.aggregate(rows)
        by_group = {group.group: group for group in aggregate.groups}
        self.assertEqual(by_group["g"].row_indices, (0, 2))
        self.assertEqual(by_group["b"].row_indices, (1,))

    # -- P3 生产切换：生产调用点 vs 冻结的改造前实现 -------------------------

    def test_production_bh_more_matches_frozen_legacy(self):
        for component in (damage.BODY_FIRE_TAGS, damage.BODY_COLD_TAGS):
            player_more, tree_rows = self._bh_rows(component)
            rows = list(player_more) + list(tree_rows)
            expected_product, expected_groups = legacy_more_groups(rows)
            observed = damage.group_more_rows(rows)
            observed_product = 1.0
            for group in observed:
                observed_product *= group["factor"]
            self.assertEqual(observed_product, expected_product)
            self.assertEqual(
                [(group["group"], group["value"], group["factor"]) for group in observed],
                [(group[0], group[1], group[2]) for group in expected_groups],
            )
            for group, expected in zip(observed, expected_groups):
                self.assertEqual(
                    [row["contribution_id"] for row in group["rows"]], expected[3]
                )

    def test_production_fr_more_matches_frozen_legacy(self):
        single = _fr_single_hit()
        component = single["components"][0]
        states = _fr_states()
        tags = _fr_component_tags()
        collected = hit_damage.collect_rows(fixtures.fr_snapshot(), fixtures.fr_ruleset(), states)
        selected = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_DAMAGE
            and row["operation"] == "more"
            and hit_damage.tags_apply(row["damage_tags"], tags)
        ]
        expected_product, expected_groups = legacy_more_groups(selected)
        product, ordered = hit_damage._more_groups(collected["rows"], tags)
        self.assertEqual(product, expected_product)
        self.assertEqual(product, component["more_product"])
        self.assertEqual(
            [(group["group"], group["value"], group["multiplier"]) for group in ordered],
            [(group[0], group[1], group[2]) for group in expected_groups],
        )
        for group, expected in zip(ordered, expected_groups):
            self.assertEqual(
                [row["contribution_id"] for row in group["rows"]], expected[3]
            )

    def test_production_attack_speed_more_matches_frozen_legacy(self):
        snapshot = fixtures.fr_snapshot()
        ruleset = fixtures.fr_ruleset()
        scenario = fixtures.fr_scenario()
        states = hit_damage.resolve_states(ruleset, scenario, section="simulate", snapshot=snapshot)
        collected = hit_damage.collect_rows(snapshot, ruleset, states)
        hit_tags = hit_simulate._skill_hit_tags(snapshot)
        more_rows = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_ATTACK_SPEED
            and row["operation"] == "more"
            and hit_damage.tags_apply(row["damage_tags"], hit_tags)
        ]
        expected_product, expected_groups = legacy_more_groups(more_rows)
        pools = hit_simulate._attack_speed_pools(collected["rows"], hit_tags)
        self.assertEqual(pools["more_attack_speed"], expected_product)
        self.assertEqual(
            [(group["group"], group["value"], group["multiplier"]) for group in pools["more_groups"]],
            [(group[0], group[1], group[2]) for group in expected_groups],
        )
        for group, expected in zip(pools["more_groups"], expected_groups):
            self.assertEqual(group["rows"], expected[3])
        increased_rows = [
            row
            for row in collected["rows"]
            if row["stat_key"] == model.STAT_ATTACK_SPEED
            and row["operation"] == "increased"
            and hit_damage.tags_apply(row["damage_tags"], hit_tags)
        ]
        self.assertEqual(
            pools["increased_attack_speed"],
            legacy_increased_pool([row["value"] for row in increased_rows])[0],
        )


class CritExpectedEquivalenceTest(unittest.TestCase):
    def _fr_pools(self) -> crit_expected.CritPools:
        component = _fr_single_hit()["components"][0]
        detail = component["crit_detail"]
        return crit_expected.CritPools(
            base_chance=detail["base_chance"],
            chance_added=detail["added_chance"],
            chance_increased=detail["increased_chance"],
            chance_more_product=detail["more_chance_product"],
            base_multiplier=detail["base_multiplier"],
            multiplier_added=detail["added_multiplier"],
            multiplier_increased=detail["increased_multiplier"],
            multiplier_more_product=detail["more_multiplier_product"],
            chance_cap=detail["chance_cap"],
        )

    def test_fr_crit_matches_production(self):
        detail = _fr_single_hit()["components"][0]["crit_detail"]
        result = crit_expected.expected_crit(
            capability=crit_expected.FR_HIT_CRIT_CAPABILITY, pools=self._fr_pools()
        )
        self.assertEqual(result.crit_chance, detail["crit_chance"])
        self.assertEqual(result.crit_multiplier, detail["crit_multiplier"])
        self.assertEqual(result.avg_crit, detail["avg_crit"])
        self.assertEqual(
            crit_expected.avg_crit(detail["crit_chance"], detail["crit_multiplier"]),
            detail["avg_crit"],
        )
        self.assertEqual(result.crit_chance, 0.2375)
        self.assertEqual(result.crit_multiplier, 7.2302)
        self.assertEqual(result.avg_crit, 2.4796725)

    def test_crit_chance_cap_is_applied(self):
        pools = crit_expected.CritPools(
            base_chance=5.0,
            chance_cap=1.0,
            base_multiplier=2.0,
        )
        result = crit_expected.expected_crit(
            capability=crit_expected.FR_HIT_CRIT_CAPABILITY, pools=pools
        )
        self.assertEqual(result.crit_chance, 1.0)
        self.assertEqual(result.avg_crit, 2.0)

    def test_bh_has_no_crit_capability(self):
        result = crit_expected.expected_crit(capability=crit_expected.BH_CRIT_CAPABILITY)
        self.assertFalse(result.crit_enabled)
        self.assertEqual(result.crit_chance, 0.0)
        self.assertEqual(result.avg_crit, 1.0)
        # BH 生产组件里没有 crit 字段（capability 关闭不得默认开启）
        body = damage.compute_body(
            fixtures.bh_snapshot(), fixtures.bh_ruleset(), fixtures.bh_production_scenario()
        )
        for component in body["components"]:
            self.assertNotIn("avg_crit", component)
        # FR capability 开启
        self.assertTrue(crit_expected.FR_HIT_CRIT_CAPABILITY.crit_enabled)

    def test_enabled_without_pools_is_rejected(self):
        with self.assertRaises(ValueError):
            crit_expected.expected_crit(capability=crit_expected.FR_HIT_CRIT_CAPABILITY)

    # -- P3 生产切换：生产调用点 vs 冻结的改造前实现 -------------------------

    def test_production_fr_crit_matches_frozen_legacy(self):
        detail = _fr_single_hit()["components"][0]["crit_detail"]
        chance_pools = (
            detail["base_chance"],
            detail["added_chance"],
            detail["increased_chance"],
            detail["more_chance_product"],
        )
        multiplier_pools = (
            detail["base_multiplier"],
            detail["added_multiplier"],
            detail["increased_multiplier"],
            detail["more_multiplier_product"],
        )
        expected = legacy_crit(chance_pools, multiplier_pools, detail["chance_cap"])
        self.assertEqual(
            (detail["crit_chance"], detail["crit_multiplier"], detail["avg_crit"]),
            expected,
        )
        # 生产组件字段 = crit_detail（切换后仍逐位一致）
        component = _fr_single_hit()["components"][0]
        self.assertEqual(
            (component["crit_chance"], component["crit_multiplier"], component["avg_crit"]),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
