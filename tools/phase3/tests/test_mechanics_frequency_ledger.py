"""frequency / ledger / registry 适配器的等价性与注册完整性测试。

* 频率：BH 冷却/施法时间与 ``cooldown.py`` 逐位一致；FR 攻速/冷却/窗口骨架与
  ``hit_simulate`` 输出逐位一致。BH 的逐事件窗口聚合**不**在骨架内（语义不
  合并），测试只覆盖共享的数学骨架。
* 账本：薄封装必须与既定投影实现逐行相等（不改行内容/顺序）。
* 注册表：7 个适配器声明齐全，入口符号真实存在，集成状态与任务一致。
"""

from __future__ import annotations

import importlib
import unittest

from le_calc import cooldown, ledger, model
from le_calc.hit_simulate import simulate as hit_simulate_run
from le_calc.mechanics import frequency, registry
from le_calc.mechanics import ledger as mechanics_ledger

import test_mechanics_fixtures as fixtures
from le_calc.hit_report import build_source_ledger_rows



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _legacy_cooldown(
    base_recharge_s: float,
    *,
    flat_adjustment_s: float,
    icr_total: float,
    cdr_total: float,
    cdr_applied_separately: bool,
) -> float:
    adjusted = base_recharge_s + flat_adjustment_s
    if cdr_applied_separately:
        effective = adjusted * max(0.0, 1.0 - cdr_total)
        if (1.0 + icr_total) > 0:
            effective /= 1.0 + icr_total
    else:
        effective = adjusted / (1.0 + icr_total) if (1.0 + icr_total) > 0 else adjusted
    return effective


def _legacy_cast_time(base_rate_per_s: float, increased_cast_speed: float) -> float:
    return 1.0 / (base_rate_per_s * (1.0 + increased_cast_speed))


def _legacy_final_attack_rate(base_aps: float, increased: float, more_product: float) -> float:
    return base_aps * (1.0 + increased) * more_product


def _legacy_policy_block(
    *,
    attack_interval: float,
    effective_cooldown,
    hits_per_cast: int,
    single_hit_damage: float,
    windows,
    damage_weights,
    rotation_more: float,
) -> dict:
    """冻结的改造前 ``hit_simulate._policy_block`` 实现（P3 切换参考）。"""
    casts_per_second = 1.0 / attack_interval if attack_interval > 0 else 0.0
    binding = "attack_speed"
    if effective_cooldown and effective_cooldown > 0:
        cooldown_casts = 1.0 / effective_cooldown
        if cooldown_casts < casts_per_second:
            casts_per_second = cooldown_casts
            binding = "cooldown"
        else:
            binding = "attack_speed"
    hits_per_second = casts_per_second * hits_per_cast
    weights = [float(value) for value in (damage_weights or [])]
    if len(weights) == hits_per_cast and weights:
        damage_factor_per_cast = sum(weights)
    else:
        weights = [1.0] * hits_per_cast
        damage_factor_per_cast = float(hits_per_cast)
    damage_per_cast = single_hit_damage * damage_factor_per_cast * rotation_more
    damage_per_second = casts_per_second * damage_per_cast
    window_rows = []
    for window_s in windows:
        casts = casts_per_second * window_s
        total_damage = damage_per_second * window_s
        window_rows.append(
            {
                "window_s": window_s,
                "window_id": f"{window_s:g}s",
                "casts": casts,
                "complete_casts": int(casts),
                "hits": hits_per_second * window_s,
                "single_hit_damage": single_hit_damage,
                "damage_weights": weights,
                "effective_hits_per_cast": damage_factor_per_cast,
                "rotation_more_factor": rotation_more,
                "damage_per_cast": damage_per_cast,
                "total_damage": total_damage,
                "dps": total_damage / window_s if window_s > 0 else 0.0,
                "casts_per_second": casts_per_second,
                "hits_per_second": hits_per_second,
                "damage_per_second": damage_per_second,
            }
        )
    return {
        "casts_per_second": casts_per_second,
        "hits_per_second": hits_per_second,
        "damage_per_second": damage_per_second,
        "damage_weights": weights,
        "effective_hits_per_cast": damage_factor_per_cast,
        "rotation_more_factor": rotation_more,
        "damage_per_cast": damage_per_cast,
        "binding_constraint": binding,
        "windows": window_rows,
    }


class BhFrequencyEquivalenceTest(unittest.TestCase):
    def test_cooldown_matches_production(self):
        observed = cooldown.compute_cooldown(
            fixtures.bh_snapshot(), fixtures.bh_ruleset()
        ).to_dict()
        result = frequency.effective_cooldown(
            observed["base_recharge_s"],
            flat_adjustment_s=observed["flat_total_s"],
            increased_cooldown_recovery_speed=observed["icr_total"],
            cooldown_reduction=observed["cdr_total"],
            cdr_applied_separately=observed["cdr_applied_separately"],
        )
        self.assertEqual(result.adjusted_cooldown_s, observed["adjusted_cooldown_s"])
        self.assertEqual(result.effective_cooldown_s, observed["effective_recharge_s"])
        self.assertEqual(result.base_recharge_s, observed["base_recharge_s"])

    def test_cooldown_formula_branches(self):
        for cdr_separate in (False, True):
            for base, flat, icr, cdr in (
                (3.0, 0.0, 1.12, 0.6),
                (1.0, 0.5, 0.0, 0.0),
                (2.0, -0.25, 0.3, 0.15),
                (1.5, 0.0, -1.0, 0.0),  # ICR=-100% 的边界：保持既有分支行为
            ):
                result = frequency.effective_cooldown(
                    base,
                    flat_adjustment_s=flat,
                    increased_cooldown_recovery_speed=icr,
                    cooldown_reduction=cdr,
                    cdr_applied_separately=cdr_separate,
                )
                self.assertEqual(result.adjusted_cooldown_s, base + flat)
                self.assertEqual(
                    result.effective_cooldown_s,
                    _legacy_cooldown(
                        base,
                        flat_adjustment_s=flat,
                        icr_total=icr,
                        cdr_total=cdr,
                        cdr_applied_separately=cdr_separate,
                    ),
                )

    def test_cast_time_matches_production(self):
        observed = cooldown.compute_cast_time(fixtures.bh_snapshot(), fixtures.bh_ruleset())
        self.assertEqual(
            frequency.cast_time(
                observed["base_rate_per_s"],
                increased_cast_speed=observed["increased_cast_speed"],
            ),
            observed["cast_time_s"],
        )
        # 无 base rate 的退化分支
        self.assertEqual(frequency.cast_time(0.0, increased_cast_speed=0.5), 0.0)

    # -- P3 生产切换：生产调用点 vs 冻结的改造前实现 -------------------------

    def test_production_cooldown_matches_frozen_legacy(self):
        observed = cooldown.compute_cooldown(
            fixtures.bh_snapshot(), fixtures.bh_ruleset()
        ).to_dict()
        self.assertEqual(
            observed["adjusted_cooldown_s"],
            observed["base_recharge_s"] + observed["flat_total_s"],
        )
        self.assertEqual(
            observed["effective_recharge_s"],
            _legacy_cooldown(
                observed["base_recharge_s"],
                flat_adjustment_s=observed["flat_total_s"],
                icr_total=observed["icr_total"],
                cdr_total=observed["cdr_total"],
                cdr_applied_separately=observed["cdr_applied_separately"],
            ),
        )

    def test_production_cast_time_matches_frozen_legacy(self):
        observed = cooldown.compute_cast_time(fixtures.bh_snapshot(), fixtures.bh_ruleset())
        self.assertEqual(
            observed["cast_time_s"],
            _legacy_cast_time(
                observed["base_rate_per_s"], observed["increased_cast_speed"]
            ),
        )




class FrFrequencyEquivalenceTest(unittest.TestCase):
    def setUp(self):
        self.simulation = hit_simulate_run(
            fixtures.fr_snapshot(), fixtures.fr_ruleset(), fixtures.fr_scenario()
        )

    def test_attack_rate_matches_production(self):
        observed = self.simulation["attack_rate"]
        final_aps = frequency.final_attack_rate(
            observed["base_aps"],
            increased_attack_speed=observed["increased_attack_speed"],
            more_product=observed["more_attack_speed"],
        )
        self.assertEqual(final_aps, observed["final_aps"])
        interval = frequency.attack_interval(final_aps)
        self.assertEqual(interval, observed["attack_interval_s"])

    def test_primary_cast_policy_matches_production(self):
        observed = self.simulation["policy"]
        rate = self.simulation["attack_rate"]
        interval = frequency.attack_interval(
            frequency.final_attack_rate(
                rate["base_aps"],
                increased_attack_speed=rate["increased_attack_speed"],
                more_product=rate["more_attack_speed"],
            )
        )
        result = frequency.cast_policy(
            attack_interval_s=interval,
            effective_cooldown_s=observed["effective_cooldown_s"],
        )
        self.assertEqual(result.casts_per_second, observed["casts_per_second"])
        self.assertEqual(result.binding_constraint, observed["binding_constraint"])
        self.assertEqual(result.binding_constraint, "cooldown")
        self.assertEqual(result.effective_cooldown_s, 1.4150943396226414)
        self.assertEqual(result.casts_per_second, 0.7066666666666667)

    def test_alternate_policy_without_cooldown(self):
        alternate = self.simulation["alternate_policy"]
        rate = self.simulation["attack_rate"]
        interval = frequency.attack_interval(
            frequency.final_attack_rate(
                rate["base_aps"],
                increased_attack_speed=rate["increased_attack_speed"],
                more_product=rate["more_attack_speed"],
            )
        )
        result = frequency.cast_policy(attack_interval_s=interval)
        self.assertEqual(result.casts_per_second, alternate["casts_per_second"])
        self.assertEqual(result.binding_constraint, "attack_speed")

    def test_window_skeleton_matches_production(self):
        observed = self.simulation["policy"]
        hits_per_cast = self.simulation["hits_per_cast"]["per_cast"]
        single_hit_damage = self.simulation["single_hit"]["damage"]
        self.assertEqual(hits_per_cast, 4)
        self.assertEqual(len(self.simulation["windows"]), 3)
        for window in self.simulation["windows"]:
            skeleton = frequency.window_skeleton(
                window_s=window["window_s"],
                casts_per_second=observed["casts_per_second"],
                hits_per_cast=hits_per_cast,
                single_hit_damage=single_hit_damage,
                damage_weights=window["damage_weights"],
                rotation_more_factor=window["rotation_more_factor"],
            )
            for key in (
                "window_s",
                "window_id",
                "casts",
                "complete_casts",
                "hits",
                "damage_weights",
                "effective_hits_per_cast",
                "rotation_more_factor",
                "damage_per_cast",
                "total_damage",
                "dps",
                "casts_per_second",
                "hits_per_second",
                "damage_per_second",
            ):
                self.assertEqual(skeleton[key], window[key], msg=f"{window['window_id']}/{key}")

    def test_weights_fallback_keeps_full_damage_per_hit(self):
        weights, effective = frequency.damage_weights_factor([], 3)
        self.assertEqual(weights, [1.0, 1.0, 1.0])
        self.assertEqual(effective, 3.0)
        weights, effective = frequency.damage_weights_factor([1.0, 0.5, 0.5, 0.5], 4)
        self.assertEqual(effective, 2.5)
        # 长度不匹配时回退（与既有实现一致）
        _, effective = frequency.damage_weights_factor([1.0, 0.5], 4)
        self.assertEqual(effective, 4.0)

    # -- P3 生产切换：生产调用点 vs 冻结的改造前实现 -------------------------

    def test_production_rate_matches_frozen_legacy(self):
        rate = self.simulation["attack_rate"]
        expected_aps = _legacy_final_attack_rate(
            rate["base_aps"],
            rate["increased_attack_speed"],
            rate["more_attack_speed"],
        )
        self.assertEqual(rate["final_aps"], expected_aps)
        expected_interval = 1.0 / expected_aps if expected_aps > 0 else 0.0
        self.assertEqual(rate["attack_interval_s"], expected_interval)

    def test_production_policy_and_windows_match_frozen_legacy(self):
        simulation = self.simulation
        rate = simulation["attack_rate"]
        policy = simulation["policy"]
        interval = _legacy_final_attack_rate(
            rate["base_aps"],
            rate["increased_attack_speed"],
            rate["more_attack_speed"],
        )
        interval = 1.0 / interval if interval > 0 else 0.0
        # FR 冷却守卫：cooldown<=0 才无约束（与 BH 的 ICR 守卫不同）
        expected_cooldown = (
            policy["cooldown_s"] / (1.0 + policy["increased_cooldown_recovery_speed"])
            if policy["cooldown_s"] > 0
            else None
        )
        self.assertEqual(policy["effective_cooldown_s"], expected_cooldown)
        expected = _legacy_policy_block(
            attack_interval=interval,
            effective_cooldown=expected_cooldown,
            hits_per_cast=simulation["hits_per_cast"]["per_cast"],
            single_hit_damage=simulation["single_hit"]["damage"],
            windows=[window["window_s"] for window in simulation["windows"]],
            damage_weights=policy["damage_weights"],
            rotation_more=policy["rotation_more_factor"],
        )
        for key in (
            "casts_per_second",
            "hits_per_second",
            "damage_per_second",
            "damage_weights",
            "effective_hits_per_cast",
            "rotation_more_factor",
            "damage_per_cast",
            "binding_constraint",
        ):
            self.assertEqual(policy[key], expected[key], msg=key)
        for window, expected_window in zip(simulation["windows"], expected["windows"]):
            for key in expected_window:
                self.assertEqual(window[key], expected_window[key], msg=f"{window['window_id']}/{key}")
        # 备选策略（忽略冷却）同样等价
        alternate = simulation["alternate_policy"]
        expected_alternate = _legacy_policy_block(
            attack_interval=interval,
            effective_cooldown=None,
            hits_per_cast=simulation["hits_per_cast"]["per_cast"],
            single_hit_damage=simulation["single_hit"]["damage"],
            windows=[window["window_s"] for window in alternate["windows"]],
            damage_weights=alternate["damage_weights"],
            rotation_more=alternate["rotation_more_factor"],
        )
        self.assertEqual(alternate["casts_per_second"], expected_alternate["casts_per_second"])
        self.assertEqual(alternate["binding_constraint"], "attack_speed")
        for window, expected_window in zip(alternate["windows"], expected_alternate["windows"]):
            for key in expected_window:
                self.assertEqual(window[key], expected_window[key], msg=f"alt/{window['window_id']}/{key}")


class LedgerEquivalenceTest(unittest.TestCase):
    def test_bh_ledger_projection_is_unchanged(self):
        snapshot = fixtures.bh_snapshot()
        expected = ledger.build_source_ledger(snapshot)
        actual = mechanics_ledger.project_source_ledger(snapshot)
        self.assertEqual(actual, expected)
        self.assertGreater(len(actual), 0)
        # 单行投影：字段与取值完全不变
        for contribution, row in zip(
            sorted(snapshot.contributions, key=lambda item: (
                item.source_kind, item.source_path, item.contribution_id
            )),
            expected,
        ):
            self.assertEqual(
                mechanics_ledger.project_contribution_row(
                    contribution, int(row["row_id"].split("-")[1])
                ),
                row,
            )

    def test_bh_ledger_summary_matches_production_counts(self):
        snapshot = fixtures.bh_snapshot()
        rows = ledger.build_source_ledger(snapshot)
        summary = ledger.summarize(rows)
        shared = mechanics_ledger.ledger_summary(rows)
        self.assertEqual(shared["total_rows"], summary["total_rows"])
        self.assertEqual(shared["included_rows"], summary["included_rows"])
        self.assertEqual(shared["active_rows"], summary["active_rows"])
        self.assertEqual(shared["excluded_rows"], summary["excluded_rows"])

    def test_hit_report_ledger_projection_is_unchanged(self):
        snapshot_document = fixtures.read_json(fixtures.FR_SNAPSHOT_PATH)
        expected = build_source_ledger_rows(snapshot_document)
        actual = mechanics_ledger.project_hit_source_ledger(snapshot_document)
        self.assertEqual(actual, expected)
        self.assertGreater(len(actual), 0)
        # 行内容确实来自既有 renderer 投影（字段存在性抽查）
        first = actual[0]
        self.assertIn("source_id", first)
        self.assertIn("evidence_refs", first)
        self.assertIn("stat_key", first)

    def test_field_sets_are_recorded(self):
        fields = mechanics_ledger.field_sets()
        self.assertIn("bh_ledger", fields)
        self.assertIn("hit_report", fields)
        self.assertIn("player_label", fields["bh_ledger"])
        self.assertIn("stat_key", fields["hit_report"])


class RegistryTest(unittest.TestCase):
    EXPECTED_ADAPTERS = {
        "target_side",
        "tag_match",
        "increased_aggregate",
        "more_groups",
        "crit_expected",
        "frequency",
        "ledger",
    }

    def test_all_adapters_declared(self):
        described = registry.describe()
        self.assertEqual({item["name"] for item in described}, self.EXPECTED_ADAPTERS)
        module_paths = {
            "le_calc.mechanics.target_side",
            "le_calc.mechanics.tag_match",
            "le_calc.mechanics.increased_aggregate",
            "le_calc.mechanics.more_groups",
            "le_calc.mechanics.crit_expected",
            "le_calc.mechanics.frequency",
            "le_calc.mechanics.ledger",
        }
        for item in described:
            self.assertTrue(item["purpose"])
            self.assertTrue(item["entry_points"])
            self.assertIn(item["integration_status"], ("integrated", "adapter_only"))
            self.assertIn(item["module"], module_paths)

    #: P3 生产切换进度：每切换一个算子，把名字加进来（与 registry 同步维护）。
    EXPECTED_INTEGRATED = {"target_side", "tag_match", "increased_aggregate", "more_groups", "crit_expected", "frequency"}

    def test_integration_report_matches_p3_scope(self):
        report = registry.integration_report()
        self.assertEqual(report["integrated"], sorted(self.EXPECTED_INTEGRATED))
        self.assertEqual(
            report["adapter_only"],
            sorted(self.EXPECTED_ADAPTERS - self.EXPECTED_INTEGRATED),
        )
        capabilities = report["model_capabilities"]
        self.assertFalse(capabilities["black-hole"]["crit_enabled"])
        self.assertTrue(capabilities["flame-reave-hit"]["crit_enabled"])
        self.assertEqual(
            capabilities["black-hole"]["target_side_dr_source"], "scenario_override"
        )
        self.assertEqual(
            capabilities["flame-reave-hit"]["target_side_dr_source"], "table_by_level"
        )

    def test_entry_points_exist_on_modules(self):
        modules = registry.modules()
        for item in registry.describe():
            module_name = item["module"].rsplit(".", 1)[-1]
            module = modules[module_name]
            for entry in item["entry_points"]:
                self.assertTrue(hasattr(module, entry), msg=f"{item['module']}.{entry}")
            self.assertIsNotNone(importlib.import_module(item["module"]))

    def test_unknown_adapter_raises(self):
        with self.assertRaises(KeyError):
            registry.get("does-not-exist")

    def test_integrated_adapter_has_targets(self):
        spec = registry.get("target_side")
        self.assertEqual(spec.integration_status, "integrated")
        self.assertGreaterEqual(len(spec.integration_targets), 3)
        for target in spec.integration_targets:
            self.assertIn(".", target)


if __name__ == "__main__":
    unittest.main()
