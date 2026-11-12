"""通用 actor / 派生事件频率 / effect scope 的定向测试（task-2 实现切片）。

只覆盖本轮新增/修复的通用能力，不复制历史案例基线：
1. ``mechanics/execution.settle_execution_effects`` 的 increased/more 不再静默丢失；
2. ``mechanics/frequency.derived_event_rate`` 纯函数与 ``frequency_derived_events-v1``；
3. ``actor_pool`` 的 actor 基础行 / 显式转移 / scope 过滤；
4. execution-v1 契约（derivation/actors/stat_transfers）与结果门禁；
5. ``plan_v4._build_execution_v1`` 的 derived driver / actor 透传；
6. capability 白名单与模板声明一致性。

全部为数据驱动断言，不按技能名/ability_id 分派。
"""
from __future__ import annotations

import copy
import json
import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import actor_pool, definitions_v3, effect_binding, model, plan_handlers, plan_v4  # noqa: E402
from le_calc import dps_components  # noqa: E402
from le_calc.mechanics import execution as mechanics_execution  # noqa: E402
from le_calc.mechanics import frequency as frequency_module  # noqa: E402
from le_calc.mechanics import frequency  # noqa: E402


def derived_derivation(**overrides):
    derivation = {
        "kind": "summon_lifecycle",
        "upstream": {"kind": "scenario_control", "control": "trap_rate", "default": 4.0},
        "gate": {
            "kind": "chance",
            "per_point": 0.1,
            "points_control": "points_siege_engineering",
            "aggregate": "per_point_add",
        },
        "lifecycle": {
            "limit": 2,
            "duration_s": 10.0,
            "destroy_policy": "replace_or_expire",
            "events_per_summon": 1,
        },
        "rate_formula": "upstream_rate * gate_probability",
        "assertions": {"max_rate": "lifecycle.limit / lifecycle.duration_s", "on_violation": "warn"},
    }
    derivation.update(overrides)
    return derivation


def actor_component(**overrides):
    component = {
        "component_id": "hit:actor-fire",
        "kind": "hit",
        "unit": "damage",
        "damage_channel": "fire",
        "damage_type": "Fire",
        "resistance_key": "fire",
        "scope": "actor",
        "tags": ["Fire", "Bow", "Hit"],
        "base_damage": 50.0,
        "added_damage_scaling": 2.5,
        "crit_capable": True,
        "crit_chance": 0.5,
        "crit_multiplier": 2.6,
        "duration_s": 0.0,
        "tick_interval_s": 0.0,
        "cast_ref": "event",
    }
    component.update(overrides)
    return component


class SettleExecutionEffectsTest(unittest.TestCase):
    def _effects(self):
        return [
            {
                "effect_id": "transfer:increased",
                "emits_contribution": True,
                "scope": "all",
                "channel_tags": ["Fire", "Bow"],
                "operation": "increased",
                "value": 1.125,
                "damage_tags": ["Fire"],
                "ownership_key": {"owner": "actor", "transfer": "increased"},
            },
            {
                "effect_id": "transfer:another-increased",
                "emits_contribution": True,
                "scope": "all",
                "channel_tags": ["Fire"],
                "operation": "increased",
                "value": 1.84,
                "damage_tags": ["Fire"],
                "ownership_key": {"owner": "actor", "transfer": "increased-2"},
            },
            {
                "effect_id": "transfer:added",
                "emits_contribution": True,
                "scope": "all",
                "channel_tags": ["Fire", "Bow"],
                "operation": "added",
                "value": 18.0,
                "damage_tags": ["Fire", "Bow"],
                "ownership_key": {"owner": "actor", "transfer": "added"},
            },
            {
                "effect_id": "transfer:more",
                "emits_contribution": True,
                "scope": "all",
                "channel_tags": ["Fire"],
                "operation": "more",
                "value": 0.5,
                "damage_tags": ["Fire"],
                "ownership_key": {"owner": "actor", "transfer": "more"},
            },
        ]

    def test_increased_and_more_reach_component_damage(self):
        component = actor_component()
        settled = mechanics_execution.settle_execution_effects(
            self._effects(),
            states={},
            target={"resistances": {"fire": 0.0}, "damage_reduction": 0.0},
            runtime=plan_handlers.PlanRuntime(),
            components=[component],
        )
        totals = settled["component_totals"][component["component_id"]]
        self.assertAlmostEqual(totals["increased"], 2.965, delta=1e-12)
        self.assertAlmostEqual(totals["more"], 1.5, delta=1e-12)
        # (50 + 18×2.5) × (1+2.965) × 1.5 × (1 + 0.5×(2.6-1)) = 1017.0225
        self.assertAlmostEqual(totals["damage"], 1017.0225, delta=1e-9)
        self.assertTrue(all(row["included"] for row in settled["rows"]))

    def test_settle_matches_execute_slice(self):
        component = actor_component()
        effects = self._effects()
        settled = mechanics_execution.settle_execution_effects(
            effects,
            states={},
            target={"resistances": {"fire": 0.0}, "damage_reduction": 0.0},
            runtime=plan_handlers.PlanRuntime(),
            components=[component],
        )
        execution = {
            "schema": "le-plan-execution-v1",
            "source_contract": "test",
            "capabilities": ["hit_components"],
            "components": [component],
            "applications": [],
            "effects": effects,
            "sources": [{"source_id": "actor", "binding_status": "mapped"}],
            "frequency": {
                "driver": "fixed_repeat",
                "horizon_s": 30.0,
                "windows_s": [5.0, 30.0],
                "cast_interval_s": 1.25,
                "policies": [],
                "rotation": {},
                "tail_policy": "include_unsettled",
            },
            "charge": {"enabled": False, "max_charges": 1, "recharge_s": 0.0,
                       "cast_time_s": 0.0, "instance_duration_s": 0.0, "condition": ""},
            "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
            "target": {"resistances": {"fire": 0.0}, "damage_reduction": 0.0},
            "states": {},
        }
        slice_result = mechanics_execution.execute_execution_slice(execution, {"states": {}})
        self.assertAlmostEqual(
            settled["component_totals"][component["component_id"]]["damage"],
            slice_result["totals"]["hit_damage"],
            delta=1e-9,
        )
        self.assertAlmostEqual(
            slice_result["totals"]["horizon_damage"],
            1017.0225 * (int(30.0 / 1.25) + 1),
            delta=1e-9,
        )

    def test_no_effects_is_base_times_crit(self):
        component = actor_component()
        settled = mechanics_execution.settle_execution_effects(
            [],
            states={},
            target={"resistances": {"fire": 0.0}, "damage_reduction": 0.0},
            runtime=plan_handlers.PlanRuntime(),
            components=[component],
        )
        totals = settled["component_totals"][component["component_id"]]
        self.assertEqual(totals["increased"], 0.0)
        self.assertEqual(totals["more"], 1.0)
        self.assertAlmostEqual(totals["damage"], 50.0 * (1.0 + 0.5 * (2.6 - 1.0)), delta=1e-12)

    def test_condition_not_satisfied_is_excluded(self):
        effects = self._effects()
        effects[0] = dict(effects[0], condition_ref="cond:missing")
        component = actor_component()
        settled = mechanics_execution.settle_execution_effects(
            effects,
            conditions={
                "cond:missing": {
                    "op": "all",
                    "terms": [{"kind": "control_true", "key": "missing_state"}],
                }
            },
            states={},
            target={"resistances": {"fire": 0.0}, "damage_reduction": 0.0},
            runtime=plan_handlers.PlanRuntime(),
            components=[component],
        )
        self.assertAlmostEqual(
            settled["component_totals"][component["component_id"]]["increased"], 1.84, delta=1e-12
        )
        self.assertEqual(settled["rows"][0]["status"], "condition_not_satisfied")


class DerivedEventRateTest(unittest.TestCase):
    def test_summon_lifecycle_math(self):
        out = frequency.derived_event_rate(derived_derivation(), states={"points_siege_engineering": 2})
        self.assertAlmostEqual(out["upstream_rate_per_s"], 4.0, delta=1e-12)
        self.assertAlmostEqual(out["gate_probability"], 0.2, delta=1e-12)
        self.assertAlmostEqual(out["spawn_rate_per_s"], 0.8, delta=1e-12)
        self.assertAlmostEqual(out["event_rate_per_s"], 0.8, delta=1e-12)
        self.assertAlmostEqual(out["event_interval_s"], 1.25, delta=1e-12)
        self.assertEqual(out["destroy_binding"], "limit")
        self.assertAlmostEqual(out["steady_state_concurrent"], 2.0, delta=1e-12)
        self.assertTrue(all(item["ok"] for item in out["checks"]), out["checks"])
        self.assertEqual(out["derivation"]["kind"], "summon_lifecycle")

    def test_scenario_control_overrides_default(self):
        out = frequency.derived_event_rate(
            derived_derivation(), states={"points_siege_engineering": 2, "trap_rate": 5.0}
        )
        self.assertAlmostEqual(out["event_rate_per_s"], 1.0, delta=1e-12)
        self.assertAlmostEqual(out["event_interval_s"], 1.0, delta=1e-12)

    def test_chance_caps_at_one(self):
        derivation = derived_derivation()
        derivation["gate"] = dict(derivation["gate"], per_point=0.5)
        out = frequency.derived_event_rate(derivation, states={"points_siege_engineering": 4})
        self.assertAlmostEqual(out["gate_probability"], 1.0, delta=1e-12)

    def test_zero_points_rejects_zero_rate(self):
        with self.assertRaises(frequency.FrequencyError):
            frequency.derived_event_rate(derived_derivation(), states={"points_siege_engineering": 0})

    def test_expire_only_rate_cap_error(self):
        derivation = derived_derivation(
            lifecycle={
                "limit": 2,
                "duration_s": 10.0,
                "destroy_policy": "expire_only",
                "events_per_summon": 1,
            },
            assertions={"max_rate": "lifecycle.limit / lifecycle.duration_s", "on_violation": "error"},
        )
        with self.assertRaises(frequency.FrequencyError):
            frequency.derived_event_rate(derivation, states={"points_siege_engineering": 2})

    def test_unknown_fields_and_enums_rejected(self):
        with self.assertRaises(frequency.FrequencyError):
            frequency.derived_event_rate(derived_derivation(kind="proc_chain"), states={})
        bad = derived_derivation()
        bad["extra"] = 1
        with self.assertRaises(frequency.FrequencyError):
            frequency.validate_derivation(bad)
        bad = derived_derivation()
        bad["rate_formula"] = "anything"
        with self.assertRaises(frequency.FrequencyError):
            frequency.validate_derivation(bad)
        bad = derived_derivation()
        bad["lifecycle"] = dict(bad["lifecycle"], destroy_policy="never")
        with self.assertRaises(frequency.FrequencyError):
            frequency.validate_derivation(bad)
        with self.assertRaises(frequency.FrequencyError):
            frequency.derived_event_rate(
                derived_derivation(), states={"points_siege_engineering": "2"}
            )

    def test_handler_is_registered_and_runs(self):
        self.assertIn("frequency_derived_events-v1", plan_handlers.EXECUTOR_HANDLERS)
        self.assertIn("frequency_derived_events-v1", plan_handlers.EXECUTOR_IMPLEMENTATIONS)
        self.assertIn("derived_events-v1", plan_handlers.COMPILE_HANDLERS)
        runtime = plan_handlers.PlanRuntime()
        result = runtime.handler("frequency_derived_events-v1")(
            derivation=derived_derivation(), states={"points_siege_engineering": 2}
        )
        self.assertAlmostEqual(result["event_interval_s"], 1.25, delta=1e-12)


class ActorPoolTest(unittest.TestCase):
    def _actor(self):
        return {
            "actor_id": "minion:test:42",
            "kind": "summon",
            "source_entity": "minion:test:42",
            "damage_source": {"kind": "dataset_skill", "ability_entity": "skill:test:explode"},
            "inherits": "none",
            "base_effects": [
                {"id": "base-more", "stat": "damage", "operation": "more", "value": 0.5,
                 "tags": ["Fire"], "source": {"kind": "dataset_minion"}},
            ],
        }

    def _transfer_rule(self):
        return {
            "label": "explicit transfer",
            "target_actor": "minion:test:42",
            "transfers": [
                {
                    "id": "damage-share",
                    "input_pool": {
                        "stat_key": "damage",
                        "operations": ["increased"],
                        "tag_closure": ["Fire"],
                        "tag_policy": "component-closure",
                    },
                    "ratio_per_point": 0.25,
                    "points_control": "points_share",
                    "output": {"stat": "damage", "operation": "increased", "tags": ["Fire"]},
                },
                {
                    "id": "added-share",
                    "input_pool": {"stat_key": "added_damage", "operations": ["added"],
                                   "tag_closure": ["Bow", "Fire"], "tag_policy": "component-closure"},
                    "ratio": 0.5,
                    "output": {"stat": "added_damage", "operation": "added", "tags": ["Bow"]},
                },
            ],
        }

    def _owner_rows(self):
        return [
            model.Contribution(
                contribution_id="owner:inc-fire", source_kind="passive_node",
                source_label="owner fire", source_path="passive", stat_name="damage",
                stat_key="damage", operation="increased", value=0.8,
                damage_tags=["Fire"], raw={"scope": "owner"},
            ),
            model.Contribution(
                contribution_id="owner:inc-melee", source_kind="passive_node",
                source_label="owner melee", source_path="passive", stat_name="damage",
                stat_key="damage", operation="increased", value=0.4,
                damage_tags=["Melee"], raw={"scope": "owner"},
            ),
            model.Contribution(
                contribution_id="owner:added-bow", source_kind="equipment",
                source_label="owner bow", source_path="equip", stat_name="added",
                stat_key="added_damage", operation="added", value=12.0,
                damage_tags=["Bow", "Fire"], raw={"scope": "owner"},
            ),
        ]

    def test_actor_declaration_validation(self):
        validated = actor_pool.validate_actor_declaration(self._actor())
        self.assertEqual(validated["inherits"], "none")
        bad = self._actor()
        bad["inherits"] = "all"
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_actor_declaration(bad)
        bad = self._actor()
        bad["base_effects"][0]["operation"] = "override"
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_actor_declaration(bad)

    def test_actor_base_rows(self):
        rows = actor_pool.build_actor_base_rows([self._actor()])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.stat_key, "damage")
        self.assertEqual(row.operation, "more")
        self.assertEqual(row.raw["scope"], "actor")
        self.assertAlmostEqual(row.value, 0.5, delta=1e-12)

    def test_stat_transfer_rows(self):
        rows = self._owner_rows()
        produced = actor_pool.apply_stat_transfers(
            rows, self._transfer_rule(), states={"points_share": 2}
        )
        by_id = {row.raw["transfer_ref"]: row for row in produced}
        self.assertAlmostEqual(by_id["damage-share"].value, 0.8 * 0.5, delta=1e-12)  # 0.25×2 点
        self.assertAlmostEqual(by_id["added-share"].value, 12.0 * 0.5, delta=1e-12)
        self.assertEqual(by_id["damage-share"].raw["scope"], "actor")
        self.assertEqual(by_id["damage-share"].raw["input_contribution_id"], "owner:inc-fire")
        self.assertEqual(by_id["added-share"].raw["input_contribution_id"], "owner:added-bow")
        # 逐行复制：damage_tags 保持输入行 tags
        self.assertEqual(by_id["damage-share"].damage_tags, ["Fire"])
        # 显式 output.tags 非空时覆盖；否则保持输入行 tags
        self.assertEqual(by_id["added-share"].damage_tags, ["Bow"])
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.apply_stat_transfers(rows, self._transfer_rule(), states={"points_share": "2"})

    def test_transfer_rule_validation(self):
        rule = self._transfer_rule()
        rule.pop("target_actor")
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_stat_transfer_rule(rule)
        bad = self._transfer_rule()
        bad["transfers"][0]["ratio"] = 0.1
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_stat_transfer_rule(bad)
        bad = self._transfer_rule()
        bad["transfers"][0]["output"]["scope"] = "owner"
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_stat_transfer_rule(bad)

    def test_mark_owner_scope(self):
        rows = self._owner_rows()
        actor_pool.mark_owner_scope(rows, actor_ids=["minion:test:42"])
        self.assertTrue(all(row.raw["scope"] == "owner" for row in rows))


class HitScopeFilterTest(unittest.TestCase):
    def _contributions(self):
        actor_row = model.Contribution(
            contribution_id="actor:inc", source_kind="derived", source_label="actor inc",
            source_path="rules.stat_transfer", stat_name="damage", stat_key="damage",
            operation="increased", value=1.5, damage_tags=["Fire"], group="actor:test:42",
            raw={"scope": "actor", "owner": "minion:test:42"},
        )
        owner_row = model.Contribution(
            contribution_id="owner:inc", source_kind="passive_node", source_label="owner inc",
            source_path="passive", stat_name="damage", stat_key="damage",
            operation="increased", value=0.9, damage_tags=["Fire"], raw={"scope": "owner"},
        )
        return [actor_row, owner_row]

    def test_actor_scope_excludes_owner_rows_and_records_reason(self):
        components, applications = dps_components.build_hit_components(
            contributions=self._contributions(),
            skill={"base_damage": {"Fire": 50.0}, "added_damage_scaling": 2.5,
                   "crit_chance": 0.05, "crit_multiplier": 2.0},
            component={"tags": ["Fire", "Bow"], "adaptive_damage_type": "Fire", "crit_capable": True},
            crit_rules={},
            target={"resistance": 0.0},
            states={},
            target_info={"monster_damage_reduction": 0.0},
            damage_types=["Fire"],
            unit_scope="actor",
        )
        self.assertEqual(len(components), 1)
        self.assertAlmostEqual(components[0]["increased_multiplier"], 2.5, delta=1e-12)
        reasons = {
            row["contribution_id"]: (row["status"], row["reason"]) for row in applications
        }
        self.assertEqual(reasons["owner:inc"][0], dps_components.NOT_APPLICABLE)
        self.assertIn("scope", reasons["owner:inc"][1])
        self.assertEqual(reasons["actor:inc"][0], dps_components.APPLIED)

    def test_without_unit_scope_behaviour_unchanged(self):
        components, _applications = dps_components.build_hit_components(
            contributions=self._contributions(),
            skill={"base_damage": {"Fire": 50.0}, "added_damage_scaling": 2.5,
                   "crit_chance": 0.05, "crit_multiplier": 2.0},
            component={"tags": ["Fire", "Bow"], "adaptive_damage_type": "Fire", "crit_capable": True},
            crit_rules={},
            target={"resistance": 0.0},
            states={},
            target_info={"monster_damage_reduction": 0.0},
            damage_types=["Fire"],
        )
        self.assertAlmostEqual(components[0]["increased_multiplier"], 3.4, delta=1e-12)


class ExecutionContractTest(unittest.TestCase):
    def _execution(self):
        derivation = derived_derivation()
        return {
            "schema": "le-plan-execution-v1",
            "source_contract": "test",
            "capabilities": ["hit_components", "derived_event_rate"],
            "components": [actor_component()],
            "applications": [],
            "effects": [
                {
                    "effect_id": "e1",
                    "emits_contribution": True,
                    "source_refs": ["actor"],
                    "ownership_key": {"owner": "actor", "k": "e1"},
                    "scope": "actor",
                }
            ],
            "sources": [{"source_id": "actor", "binding_status": "mapped"}],
            "frequency": {
                "driver": "derived_event_rate",
                "derivation": derivation,
                "horizon_s": 30.0,
                "windows_s": [5.0, 30.0],
                "policies": [{"policy_id": "steady", "condition": "", "interval_policy": "derived_event_rate"}],
                "rotation": {},
                "tail_policy": "include_unsettled",
            },
            "charge": {"enabled": False, "max_charges": 1, "recharge_s": 0.0,
                       "cast_time_s": 0.0, "instance_duration_s": 0.0, "condition": ""},
            "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
            "actors": [
                {
                    "actor_id": "minion:test:42",
                    "kind": "summon",
                    "source_entity": "minion:test:42",
                    "inherits": "none",
                    "base_effects": [],
                }
            ],
            "stat_transfers": [
                {
                    "target_actor": "minion:test:42",
                    "transfers": [
                        {
                            "id": "share",
                            "input_pool": {"stat_key": "damage", "operations": ["increased"]},
                            "ratio": 0.5,
                            "output": {"stat": "damage", "operation": "increased"},
                        }
                    ],
                }
            ],
        }

    def test_accepts_derived_frequency_and_actors(self):
        plan_v4.validate_execution_contract(self._execution())

    def test_rejects_missing_derivation(self):
        execution = self._execution()
        execution["frequency"].pop("derivation")
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(execution)

    def test_rejects_bad_derivation(self):
        execution = self._execution()
        execution["frequency"]["derivation"]["lifecycle"]["destroy_policy"] = "forever"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(execution)

    def test_rejects_bad_actor(self):
        execution = self._execution()
        execution["actors"][0]["inherits"] = "implicit"
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4.validate_execution_contract(execution)


class BuildExecutionV1DerivedTest(unittest.TestCase):
    def _legacy_plan(self):
        return {
            "schema": "le-calculation-plan-v1",
            "capabilities": ["hit_components"],
            "parameters": {
                "skill": {"base_damage": {"Fire": 50.0}, "added_damage_scaling": 2.5,
                          "crit_chance": 0.05, "crit_multiplier": 2.0,
                          "cooldown_s": 0.0, "action_s": 0.0},
                "crit": {},
                "ailments": [],
                "cooldown_reduction": 0.0,
            },
            "schedule": {
                "driver": "derived_event_rate",
                "derivation": derived_derivation(),
                "windows_s": [5.0, 30.0],
                "action_s": 0.0,
                "first_cast_s": 0.0,
            },
            "controls": [
                {"key": "trap_rate", "namespace": "state", "default": 4.0, "type": "number"},
                {"key": "points_siege_engineering", "namespace": "state", "default": 2, "type": "integer"},
            ],
            "effects": [
                {
                    "effect_id": "rule:actor-share",
                    "contribution_id": "rule:actor-share",
                    "emits_contribution": True,
                    "source_refs": ["rule:actor-share"],
                    "ownership_key": {"owner": "minion:test:42", "k": "actor-share"},
                    "scope": "actor",
                    "stat_key": "damage",
                    "operation": "increased",
                    "value": 0.5,
                    "damage_tags": ["Fire"],
                }
            ],
            "sources": [{"source_id": "rule:actor-share", "binding_status": "mapped"}],
            "conditions": [],
            "coverage": {"status": "complete", "gaps": []},
            "facts": {
                "actor_model": {
                    "actor_id": "minion:test:42",
                    "scope": "actor",
                    "actors": [{"actor_id": "minion:test:42", "kind": "summon",
                                "source_entity": "minion:test:42", "inherits": "none",
                                "base_effects": []}],
                },
                "stat_transfers": [{"target_actor": "minion:test:42", "transfers": [
                    {"id": "share", "input_pool": {"stat_key": "damage", "operations": ["increased"]},
                     "ratio": 0.5, "output": {"stat": "damage", "operation": "increased"}}
                ]}],
            },
            "scope": {},
        }

    def test_builds_derived_frequency_and_actor_fields(self):
        legacy_plan = self._legacy_plan()
        components = [
            {"component_id": "hit:actor-fire", "kind": "hit", "tags": ["Fire", "Bow"],
             "crit_capable": True, "adaptive_damage_type": "Fire"}
        ]
        execution_v1 = plan_v4._build_execution_v1(
            legacy_plan,
            components,
            {"hit:actor-fire": {"resistance": 0.0}},
            [5.0, 30.0],
            [{"evaluation_id": "steady_state", "primary": True}],
        )
        self.assertEqual(execution_v1["frequency"]["driver"], "derived_event_rate")
        self.assertEqual(
            execution_v1["frequency"]["derivation"]["kind"], "summon_lifecycle"
        )
        self.assertNotIn("cast_interval_s", execution_v1["frequency"])
        self.assertEqual(execution_v1["components"][0]["actor_id"], "minion:test:42")
        self.assertEqual(execution_v1["components"][0]["scope"], "actor")
        self.assertEqual(execution_v1["actors"][0]["actor_id"], "minion:test:42")
        self.assertEqual(execution_v1["stat_transfers"][0]["target_actor"], "minion:test:42")
        # 契约校验：装配产物本身必须能通过 execution-v1 校验
        plan_v4.validate_execution_contract(execution_v1)


class DerivedFrequencyGateTest(unittest.TestCase):
    def _projection(self):
        return frequency.derived_event_rate(
            derived_derivation(), states={"points_siege_engineering": 2}
        )

    def _result(self):
        projection = self._projection()
        interval = projection["event_interval_s"]
        per_hit = 1017.0225
        return {
            "frequency": projection,
            "scenario": {"states": {"points_siege_engineering": 2}, "target": {}},
            "totals": {"hit_damage": per_hit, "horizon_dps": per_hit / interval},
            "windows": [
                {"window_s": 5.0, "cast_count": math.ceil(5.0 / interval - 1e-12)},
                {"window_s": 30.0, "cast_count": math.ceil(30.0 / interval - 1e-12)},
            ],
        }

    def test_accepts_consistent_result(self):
        plan_v4._validate_derived_frequency(self._result())

    def test_rejects_interval_tamper(self):
        result = self._result()
        result["frequency"]["event_interval_s"] = 2.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4._validate_derived_frequency(result)

    def test_rejects_dps_tamper(self):
        result = self._result()
        result["totals"]["horizon_dps"] = 1.0
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4._validate_derived_frequency(result)

    def test_rejects_window_cast_tamper(self):
        result = self._result()
        result["windows"][0]["cast_count"] = 99
        with self.assertRaises(plan_v4.PlanV4Error):
            plan_v4._validate_derived_frequency(result)

    def test_no_frequency_skips_gate(self):
        plan_v4._validate_derived_frequency({"totals": {}, "windows": []})


class CapabilityWhitelistTest(unittest.TestCase):
    def test_plan_v4_capabilities(self):
        for name in ("minion_actor", "explicit_stat_transfer", "derived_event_rate"):
            self.assertIn(name, plan_v4.CAPABILITIES)
            self.assertIn(name, plan_v4.CAPABILITY_MATRIX)
            self.assertIn(name, definitions_v3.KNOWN_CAPABILITIES)
            self.assertIn(name, definitions_v3.SUPPORTED_CAPABILITIES["le-calculation-plan-v4"])

    def test_capability_requires_declaration(self):
        template = {
            "schema": "le-skill-template-v3",
            "template_id": "t",
            "ability_id": "t",
            "binding_version": "definitions_v3.selector-v1",
            "title": "t",
            "capabilities": ["minion_actor"],
            "definition": {
                "plan_version": "le-calculation-plan-v1",
                "skill": {"components": {
                    "hit": {"tags": ["Fire"]}, "dot": {"tags": ["DoT"]},
                }},
                "controls": [], "state_sources": {}, "effects": [], "node_bindings": [],
                "rules": {}, "attribute_conversion": {}, "ailments": [],
                "coverage": {}, "limitations": [], "evidence": [],
            },
        }
        with self.assertRaises(definitions_v3.DefinitionBindingError):
            definitions_v3.validate_template(template)


class DerivedAssemblyEndToEndTest(unittest.TestCase):
    """assembly-backed plan-v4：derived driver 走真实 execute/validate/render 路径。"""

    def _legacy_plan(self):
        legacy = {
            "schema": "le-calculation-plan-v1",
            "version": "1.0.0",
            "subject": {"case_id": "syn-derived-assembly", "main_ability_id": "syn-minion",
                        "build_id": "syn-build", "revision_id": "syn-rev-1"},
            "case_ref": "",
            "components": [
                {"component_id": "hit:actor-fire", "kind": "hit", "tags": ["Fire", "Bow"],
                 "crit_capable": True, "adaptive_damage_type": "Fire"}
            ],
            "parameters": {
                "skill": {"base_damage": {"Fire": 50.0}, "added_damage_scaling": 2.5,
                          "crit_chance": 0.05, "crit_multiplier": 2.0,
                          "cooldown_s": 0.0, "action_s": 0.0, "skill_name": "SYNTH"},
                "crit": {}, "ailments": [], "cooldown_reduction": 0.0,
            },
            "controls": [
                {"key": "trap_rate", "namespace": "state", "default": 4.0, "type": "number"},
                {"key": "points_siege_engineering", "namespace": "state", "default": 2,
                 "type": "integer"},
            ],
            "conditions": [],
            "effects": [
                {
                    "effect_id": "rule:actor-share",
                    "contribution_id": "rule:actor-share",
                    "emits_contribution": True,
                    "source_refs": ["rule:actor-share"],
                    "ownership_key": {"owner": "minion:syn:42", "transfer": "share"},
                    "scope": "actor",
                    "stat_key": "damage", "stat_name": "damage", "operation": "increased",
                    "value": 0.5, "damage_tags": ["Fire"], "source_kind": "derived",
                    "source_label": "synthetic transfer", "source_path": "rules.stat_transfer",
                    "unit": "percent", "value_mode": "static", "value_params": {},
                    "included_static": True, "condition_status_static": "always",
                }
            ],
            "sources": [{"source_id": "rule:actor-share", "kind": "rule_source",
                         "binding_status": "mapped"}],
            "schedule": {"driver": "derived_event_rate", "derivation": derived_derivation(),
                         "windows_s": [5.0, 30.0], "action_s": 0.0, "first_cast_s": 0.0},
            "facts": {
                "actor_model": {
                    "actor_id": "minion:syn:42", "scope": "actor",
                    "actors": [{"actor_id": "minion:syn:42", "kind": "summon",
                                "source_entity": "minion:syn:42", "inherits": "none",
                                "base_effects": []}],
                },
                "stat_transfers": [{"target_actor": "minion:syn:42", "transfers": [
                    {"id": "share", "input_pool": {"stat_key": "damage", "operations": ["increased"]},
                     "ratio": 0.5, "output": {"stat": "damage", "operation": "increased"}}
                ]}],
            },
            "scope": {}, "assumptions": [], "limitations": [],
            "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
            "capabilities": ["hit_components"],
        }
        from le_calc import calculation_plan as planmod

        return planmod.finalize_plan(legacy, schema="le-calculation-plan-v1")

    def _plan(self):
        from le_calc import calculation_plan as planmod

        legacy = self._legacy_plan()
        components = [{"component_id": "hit:actor-fire", "kind": "hit", "tags": ["Fire", "Bow"],
                       "crit_capable": True, "adaptive_damage_type": "Fire"}]
        target_entries = {
            "hit:actor-fire": {
                "ref": "hit:actor-fire", "damage_channel": "fire", "resistance": 0.0,
                "penetration": 0.0, "damage_reduction": 0.0, "strategy": "separate_multiplier",
                "negative_resistance_policy": "allow", "unit": "fraction",
                "multiplier": 1.0, "resistance_multiplier": 1.0, "penetration_multiplier": 1.0,
            }
        }
        execution_v1 = plan_v4._build_execution_v1(
            legacy, components, target_entries, [5.0, 30.0],
            [{"evaluation_id": "steady_state", "primary": True}],
        )
        execution = {"engine": "mechanics_static_v1", "source_contract": "synthetic.assembly",
                     "strategies": {}, "execution_v1": execution_v1}
        plan = {
            "schema": "le-calculation-plan-v4", "version": "4.0.0",
            "subject": dict(legacy["subject"]), "case_ref": "",
            "scenario": {"states": {}, "target": {"monster_level": 100, "resistance": 0.0}},
            "primary_evaluation": "steady_evaluation_id" if False else "steady_state",
            "capabilities": ["hit_components", "minion_actor", "explicit_stat_transfer",
                             "derived_event_rate"],
            "components": [
                {"component_id": "hit:actor-fire", "kind": "hit", "damage_type": "Fire",
                 "base_damage": 50.0, "tags": ["Fire", "Bow"], "target_ref": "hit:actor-fire"}
            ],
            "applications": [],
            "schedule": {"kind": "assembly", "horizon_s": 30.0, "windows_s": [5.0, 30.0],
                         "evaluations": [{"evaluation_id": "steady_state", "primary": True}]},
            "target_side": {"unit": "fraction", "strategy": "separate_multiplier",
                            "components": target_entries},
            "presentation": {"title": "SYNTHETIC · derived event rate assembly"},
            "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
            "issues": [], "assumptions": [], "limitations": [], "input_refs": [],
            "assembly": {"kind": "synthetic_assembly", "legacy_schema": "le-calculation-plan-v1",
                         "legacy_plan": legacy},
            "execution": execution,
        }
        plan["execution_sha256"] = plan_v4._sha256_text(plan_v4._canonical(execution))
        plan["scenario_sha256"] = planmod.scenario_sha256(plan["scenario"])
        plan["execution_fingerprint"] = plan_v4._sha256_text(
            plan_v4._canonical({
                "plan": {k: v for k, v in plan.items()
                         if k not in ("plan_sha256", "execution_fingerprint")},
                "scenario_sha256": plan["scenario_sha256"],
            })
        )
        plan["plan_sha256"] = plan_v4._sha256_text(
            plan_v4._canonical({k: v for k, v in plan.items() if k != "plan_sha256"})
        )
        return plan

    def test_execute_validate_and_render_derived_frequency(self):
        plan = self._plan()
        plan_v4.validate_plan_v4(plan)
        result = plan_v4.execute_plan_v4(plan)
        plan_v4.validate_result_v6(plan, result)
        self.assertEqual(result["schema"], "le-dps-result-v6")
        self.assertEqual(result["frequency"]["driver"], "derived_event_rate")
        self.assertAlmostEqual(result["frequency"]["event_interval_s"], 1.25, delta=1e-12)
        interval = float(result["frequency"]["event_interval_s"])
        self.assertAlmostEqual(
            float(result["totals"]["horizon_dps"]),
            float(result["totals"]["hit_damage"]) / interval,
            delta=1e-9,
        )
        self.assertEqual([window["cast_count"] for window in result["windows"]], [4, 24])
        # 命中公式：base × (1+转移增伤) × 暴击期望 × 目标倍率
        multiplier = float(result["target_side"]["components"]["hit:actor-fire"]["multiplier"])
        expected_hit = 50.0 * 1.5 * (1.0 + 0.05 * 1.0) * multiplier
        self.assertAlmostEqual(float(result["totals"]["hit_damage"]), expected_hit, delta=1e-9)
        html = plan_v4.render_report_v6(result)
        self.assertIn("SYNTHETIC", html)
        self.assertGreater(len(html), 1000)


class ActorNativeAssemblyTest(unittest.TestCase):
    """v4-actor 原生装配：actor/native_stats/transfer/node_attribute_scaled/crit 聚合。"""

    @classmethod
    def setUpClass(cls):
        from le_calc.catalog import open_catalog

        cls.catalog = open_catalog()

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    def _calc(self):
        return {
            "dataset_id": "phase1-dataset-003",
            "character": {"class": {"entity_id": "class:letools:classInfoForSources:4"}},
            "skills": {
                "specializations": [
                    {
                        "tree": {"entity_id": "tree:letools:LESkillTrees:ba1574"},
                        "nodes": [
                            {"node_id": "5", "points": 4},
                            {"node_id": "22", "points": 3},
                            {"node_id": "27", "points": 4},
                        ],
                    },
                    {
                        "tree": {"entity_id": "tree:letools:LESkillTrees:ex4tp"},
                        "nodes": [{"node_id": "30", "points": 2}],
                    },
                ]
            },
        }

    def _definition(self):
        actor_id = "minion:letools:LEAbilities.entityList:100053"
        return {
            "plan_version": "le-calculation-plan-v4",
            "assembly_plan_version": "v4-actor",
            "skill": {
                "base_damage": {"Fire": 50.0},
                "added_damage_scaling": 2.5,
                "crit_chance": 0.05,
                "crit_multiplier": 2.0,
                "cooldown_s": 0.0,
                "action_s": 0.0,
                "components": {
                    "hit": {"tags": ["Bow"], "adaptive_damage_type": "Fire", "crit_capable": True},
                    "dot": {"tags": ["DoT"]},
                },
            },
            "controls": [
                {"key": "trap_detonation_rate_per_s", "namespace": "state", "default": 4.0,
                 "type": "number"},
            ],
            "actor": {
                "actor_id": actor_id,
                "kind": "summon",
                "source_entity": actor_id,
                "damage_source": {
                    "kind": "dataset_skill",
                    "ability_entity": "skill:letools:LEAbilities.abilityList:ability_gen327",
                },
                "inherits": "none",
                "base_effects": [
                    {"id": "minion-more", "stat": "damage", "operation": "more", "value": 0.5,
                     "tags": ["Fire"]},
                ],
                "native_stats": {
                    "stat_keys": ["damage"],
                    "tags_include": ["Minion"],
                    "output_tags": ["Fire", "Minion"],
                },
            },
            "effects": [],
            "rules": {
                "stat_transfer": {
                    "target_actor": actor_id,
                    "transfers": [
                        {
                            "id": "shared_enhancements",
                            "source": {"kind": "skill_node", "tree": "ba1574", "node": "22",
                                       "stat_index": 0},
                            "input_pool": {
                                "stat_key": "damage",
                                "operations": ["increased"],
                                "tag_closure": ["Fire"],
                                "tag_policy": "component-closure",
                                "exclude_tags": ["Minion"],
                            },
                            "output": {"stat": "damage", "operation": "increased",
                                       "tags": ["Fire"], "scope": "actor"},
                        },
                        {
                            "id": "perfect_aim_chance",
                            "source": {"kind": "skill_node", "tree": "ba1574", "node": "27",
                                       "stat_index": 0},
                            "input_pool": {"stat_key": "crit_chance", "operations": ["added"],
                                           "tag_closure": [], "tag_policy": "none"},
                            "output": {"stat": "crit_chance", "operation": "added",
                                       "tags": [], "scope": "actor"},
                        },
                        {
                            "id": "perfect_aim_multi",
                            "source": {"kind": "skill_node", "tree": "ba1574", "node": "27",
                                       "stat_index": 1},
                            "input_pool": {"stat_key": "crit_multiplier", "operations": ["added"],
                                           "tag_closure": [], "tag_policy": "none"},
                            "output": {"stat": "crit_multiplier", "operation": "added",
                                       "tags": [], "scope": "actor"},
                        },
                    ],
                },
                "attribute_scaling": {
                    "ratios": {"dexterity": 0.04},
                    "output": {"stat": "damage", "operation": "increased",
                               "tags": ["Fire"], "scope": "actor"},
                },
                "node_attribute_scaled": {
                    "id": "armed-construction",
                    "source": {"kind": "skill_node", "tree": "ba1574", "node": "5",
                               "stat_index": 1},
                    "attribute": "dexterity",
                    "output": {"stat": "damage", "operation": "more",
                               "tags": ["Fire"], "scope": "actor"},
                },
            },
            "frequency": {
                "driver": "derived_event_rate",
                "derivation": {
                    "kind": "summon_lifecycle",
                    "upstream": {"kind": "scenario_control",
                                 "control": "trap_detonation_rate_per_s", "default": 4.0},
                    "gate": {"kind": "chance", "aggregate": "per_point_add",
                             "source": {"kind": "skill_node", "tree": "ex4tp", "node": "30",
                                        "stat_index": 0}},
                    "lifecycle": {"limit": 2, "duration_s": 6.5,
                                  "destroy_policy": "replace_or_expire", "events_per_summon": 1},
                    "rate_formula": "upstream_rate * gate_probability",
                    "assertions": {"max_rate": "lifecycle.limit / lifecycle.duration_s",
                                   "on_violation": "warn"},
                },
            },
            "coverage": {"status": "complete", "gap_count": 0, "gaps": []},
            "limitations": [], "evidence": [], "ailments": [],
            "attribute_conversion": {}, "state_sources": {}, "node_bindings": [],
        }

    def _owner_rows(self):
        return [
            model.Contribution(
                contribution_id="base:attribute:dexterity", source_kind="base_character",
                source_label="dex", source_path="data.character.class", stat_name="dexterity",
                stat_key="attribute", operation="added", value=46.0, target="dexterity",
            ),
            model.Contribution(
                contribution_id="owner:inc-fire", source_kind="passive_node",
                source_label="owner fire", source_path="passive", stat_name="damage",
                stat_key="damage", operation="increased", value=0.8, damage_tags=["Fire"],
            ),
            model.Contribution(
                contribution_id="owner:inc-minion", source_kind="passive_node",
                source_label="owner minion", source_path="passive", stat_name="damage",
                stat_key="damage", operation="increased", value=0.3,
                damage_tags=["Minion"],
            ),
            model.Contribution(
                contribution_id="owner:crit-chance", source_kind="equipment",
                source_label="owner crit", source_path="equip", stat_name="crit",
                stat_key="crit_chance", operation="added", value=0.45,
            ),
            model.Contribution(
                contribution_id="owner:crit-multi", source_kind="equipment",
                source_label="owner crit multi", source_path="equip", stat_name="crit multi",
                stat_key="crit_multiplier", operation="added", value=0.6,
            ),
            model.Contribution(
                contribution_id="rule:attribute_scaling", source_kind="derived",
                source_label="attribute scaling", source_path="rules.attribute_scaling",
                stat_name="damage", stat_key="damage", operation="increased",
                value=1.84, damage_tags=["Fire"],
                raw={"parts": [], "declared_scope": "actor"},
            ),
        ]

    def _compile(self):
        from le_calc import plan_compiler_actor

        owner_rows = self._owner_rows()
        result = plan_compiler_actor.compile_actor_model(
            rows=owner_rows,
            resolved_model=object(),
            definition=self._definition(),
            catalog=self.catalog,
            calc=self._calc(),
        )
        self._last_owner_rows = owner_rows
        return result

    def test_actor_rows_and_transfers(self):
        result = self._compile()
        rows = [row for row, _meta in result["row_metas"]]
        by_id = {row.contribution_id: row for row in self._last_owner_rows + rows}
        # 1) actor base effect
        base = next(row for row in rows if "base:minion-more" in row.contribution_id)
        self.assertEqual(base.raw["scope"], "actor")
        self.assertEqual(base.operation, "more")
        self.assertAlmostEqual(base.value, 0.5, delta=1e-12)
        # 2) native_stats：owner Minion 行 100% 复制，原行标 owner
        native = next(row for row in rows if row.raw.get("origin") == "native_minion_stat")
        self.assertAlmostEqual(native.value, 0.3, delta=1e-12)
        self.assertEqual(native.damage_tags, ["Fire", "Minion"])
        owner_minion = next(row for row in by_id.values() if row.contribution_id == "owner:inc-minion")
        self.assertEqual(owner_minion.raw["scope"], "owner")
        # 3) transfer：node22 每点 0.25 × 3 点 = 0.75；exclude_tags=Minion 排除该行
        transfer = next(row for row in rows if row.raw.get("transfer_ref") == "shared_enhancements")
        self.assertAlmostEqual(transfer.value, 0.8 * 0.75, delta=1e-12)
        self.assertEqual(transfer.raw["input_contribution_id"], "owner:inc-fire")
        # 4) attribute_scaling 行归 actor
        scaling = next(row for row in rows if row.contribution_id == "rule:attribute_scaling")
        self.assertEqual(scaling.raw["scope"], "actor")
        self.assertAlmostEqual(scaling.value, 1.84, delta=1e-12)
        # 5) node_attribute_scaled：node5 stat1 每点 1% × 4 点 × Dex46 = 1.84 more
        node_attr = next(row for row in rows if row.raw.get("origin") == "node_attribute_scaled")
        self.assertAlmostEqual(node_attr.value, 0.01 * 4 * 46, delta=1e-9)
        self.assertEqual(node_attr.operation, "more")
        # 6) P1-D：编译期不再吸收/覆盖 crit；crit 行原样保留，执行期 crit 池结算
        self.assertNotIn("crit_override", result)
        self.assertNotIn("crit_absorbed", result["actor_model"])
        crit_rows = [
            row
            for row in rows
            if str(row.stat_key) in ("crit_chance", "crit_multiplier")
        ]
        self.assertEqual(len(crit_rows), 2)  # perfect_aim chance/multi 转移行
        self.assertFalse(any((row.raw or {}).get("crit_absorbed") for row in rows))
        # 7) dataset 证据：minion + ability_gen327 DamageEnemyOnHit
        dataset = result["actor_model"]["dataset"]
        self.assertIn("dataset_minion", dataset)
        self.assertEqual(
            dataset["dataset_damage_source"]["base_damage"], 50.0
        )
        self.assertEqual(
            dataset["dataset_damage_source"]["added_damage_effectiveness"], 250.0
        )

    def test_frequency_gate_from_skill_node_points(self):
        result = self._compile()
        frequency = result["frequency"]
        derivation = frequency["derivation"]
        self.assertEqual(derivation["gate"]["probability"], 0.2)  # 10%/点 × 2 点
        projection = frequency_module.derived_event_rate(
            derivation, states={"trap_detonation_rate_per_s": 4.0}
        )
        self.assertAlmostEqual(projection["event_rate_per_s"], 0.8, delta=1e-12)
        self.assertAlmostEqual(projection["event_interval_s"], 1.25, delta=1e-12)
        # 结构校验（不求值）也可通过
        frequency_module.validate_derivation(derivation)

    def test_actor_component_scope_and_crit_aggregation(self):
        from le_calc import dps_components

        result = self._compile()
        rows = [row for row, _meta in result["row_metas"]]
        # 命中装配只消费 actor 行；owner 伤害/暴击行进 not_applicable
        seen_ids = {row.contribution_id for row in self._last_owner_rows}
        all_rows = list(self._last_owner_rows) + [
            row for row in rows if row.contribution_id not in seen_ids
        ]
        components, applications = dps_components.build_hit_components(
            contributions=all_rows,
            skill={
                "base_damage": {"Fire": 50.0},
                "added_damage_scaling": 2.5,
                # P1-D：执行期组件 base 只保留模板声明的基础 crit（5% / 200%）
                "crit_chance": 0.05,
                "crit_multiplier": 2.0,
            },
            component={"tags": ["Bow"], "adaptive_damage_type": "Fire", "crit_capable": True},
            crit_rules={},
            target={"resistance": 0.0},
            states={},
            target_info={"monster_damage_reduction": 0.0},
            damage_types=["Fire"],
            unit_scope="actor",
        )
        self.assertEqual(len(components), 1)
        component = components[0]
        # increase = shared 0.6 + attribute_scaling 1.84
        self.assertAlmostEqual(component["increased_multiplier"], 1.0 + 0.6 + 1.84, delta=1e-9)
        # more = (1+0.5) × (1+1.84)：actor base 与 node_attribute_scaled 不同 group
        self.assertAlmostEqual(component["more_multiplier"], 1.5 * 2.84, delta=1e-9)
        # crit = (base 0.05 + added 0.45) / (base 2.0 + added 0.6)，由执行期 crit 池逐行结算
        self.assertAlmostEqual(component["crit_chance"], 0.5, delta=1e-12)
        self.assertAlmostEqual(component["crit_multiplier"], 2.6, delta=1e-12)
        expected = 50.0 * (1.0 + 0.6 + 1.84) * (1.5 * 2.84) * (1.0 + 0.5 * 1.6)
        self.assertAlmostEqual(component["damage"], expected, delta=1e-9)
        owner_status = {
            row["contribution_id"]: (row["status"], row["reason"]) for row in applications
            if row["contribution_id"].startswith("owner:")
        }
        self.assertTrue(owner_status)
        for contribution_id, (status, reason) in owner_status.items():
            if contribution_id in ("owner:inc-fire", "owner:inc-minion"):
                self.assertEqual(status, dps_components.NOT_APPLICABLE)
                self.assertIn("scope", reason)
        # crit 行（actor 转移行）由执行期 crit 池消费并记 applied；编译期无吸收标记
        crit_applied = [
            row
            for row in applications
            if row["contribution_id"].startswith("actor:")
            and row["stat_key"] in ("crit_chance", "crit_multiplier")
        ]
        self.assertTrue(crit_applied)
        self.assertTrue(all(row["status"] == dps_components.APPLIED for row in crit_applied))
        self.assertFalse(any((row.raw or {}).get("crit_absorbed") for row in all_rows))

    def test_unknown_fields_rejected(self):
        definition = self._definition()
        definition["actor"]["native_stats"]["unknown_key"] = 1
        from le_calc import actor_pool, plan_compiler_actor

        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_actor_declaration(definition["actor"])
        definition = self._definition()
        definition["rules"]["node_attribute_scaled"]["output"]["scope"] = "owner"
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_node_attribute_scaled_rule(definition["rules"]["node_attribute_scaled"])
        definition = self._definition()
        definition["rules"]["stat_transfer"]["transfers"][0]["input_pool"]["exclude_tags"] = "Minion"
        with self.assertRaises(actor_pool.ActorPoolError):
            actor_pool.validate_stat_transfer_rule(definition["rules"]["stat_transfer"])
        definition = self._definition()
        definition["actor"]["damage_source"]["ability_entity"] = "skill:letools:LEAbilities.abilityList:ba1574"
        with self.assertRaises(plan_compiler_actor.ActorCompileError):
            self._compile_with(definition)

    def _compile_with(self, definition):
        from le_calc import plan_compiler_actor

        return plan_compiler_actor.compile_actor_model(
            rows=self._owner_rows(),
            resolved_model=object(),
            definition=definition,
            catalog=self.catalog,
            calc=self._calc(),
        )


class StatTransferPerRowTest(unittest.TestCase):
    """stat_transfer 逐行复制：保留 operation/damage_tags；排除 skill_node 输入；more 逐行唯一 group。"""

    def _rows(self):
        return [
            model.Contribution(
                contribution_id="owner:fire", source_kind="passive_node",
                source_label="fire", source_path="passive", stat_name="damage",
                stat_key="damage", operation="increased", value=0.8, damage_tags=["Fire"],
            ),
            model.Contribution(
                contribution_id="owner:melee", source_kind="equipment",
                source_label="melee", source_path="equip", stat_name="damage",
                stat_key="damage", operation="increased", value=0.4, damage_tags=["Melee"],
            ),
            model.Contribution(
                contribution_id="skill:node23", source_kind="skill_node",
                source_label="heavy bolts", source_path="effect", stat_name="damage",
                stat_key="damage", operation="increased", value=0.36,
            ),
            model.Contribution(
                contribution_id="owner:more-a", source_kind="passive_node",
                source_label="more a", source_path="passive", stat_name="damage",
                stat_key="damage", operation="more", value=0.2, damage_tags=["Fire"],
            ),
            model.Contribution(
                contribution_id="owner:more-b", source_kind="idol",
                source_label="more b", source_path="idol", stat_name="damage",
                stat_key="damage", operation="more", value=0.3, damage_tags=["Fire"],
            ),
        ]

    def _rule(self):
        return {
            "target_actor": "minion:test:7",
            "transfers": [
                {
                    "id": "share_inc",
                    "input_pool": {"stat_key": "damage", "operations": ["increased"],
                                   "tag_policy": "none"},
                    "ratio": 0.5,
                    "output": {"stat": "damage", "operation": "increased", "scope": "actor"},
                },
                {
                    "id": "share_more",
                    "input_pool": {"stat_key": "damage", "operations": ["more"],
                                   "tag_policy": "none"},
                    "ratio": 1.0,
                    "output": {"stat": "damage", "operation": "more", "scope": "actor"},
                },
            ],
        }

    def test_per_row_copy_preserves_tags_and_excludes_skill_node(self):
        produced = actor_pool.apply_stat_transfers(self._rows(), self._rule())
        increased = [row for row in produced if row.operation == "increased"]
        self.assertEqual(len(increased), 2)  # fire + melee，各一行；skill_node 行被排除
        tags = sorted(tuple(row.damage_tags) for row in increased)
        self.assertEqual(tags, [("Fire",), ("Melee",)])
        values = {tuple(row.damage_tags): row.value for row in increased}
        self.assertAlmostEqual(values[("Fire",)], 0.8 * 0.5, delta=1e-12)
        self.assertAlmostEqual(values[("Melee",)], 0.4 * 0.5, delta=1e-12)
        self.assertFalse(any("skill:node23" in str(row.raw) for row in produced))

    def test_more_rows_have_unique_groups(self):
        produced = actor_pool.apply_stat_transfers(self._rows(), self._rule())
        more_rows = [row for row in produced if row.operation == "more"]
        self.assertEqual(len(more_rows), 2)
        groups = {row.group for row in more_rows}
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(group.startswith("actor:7:more:") for group in groups))
        # 命中装配：两个 more 叠乘，而不是加算
        from le_calc import dps_components

        components, _applications = dps_components.build_hit_components(
            contributions=produced,
            skill={"base_damage": {"Fire": 10.0}, "added_damage_scaling": 0.0,
                   "crit_chance": 0.0, "crit_multiplier": 1.0},
            component={"tags": ["Fire", "Minion"], "adaptive_damage_type": "Fire",
                       "crit_capable": False},
            crit_rules={},
            target={"resistance": 0.0},
            states={},
            target_info={"monster_damage_reduction": 0.0},
            damage_types=["Fire"],
            unit_scope="actor",
        )
        self.assertAlmostEqual(components[0]["more_multiplier"], (1 + 0.2) * (1 + 0.3), delta=1e-12)

    def test_include_source_kinds_override(self):
        rule = self._rule()
        rule["transfers"][0]["input_pool"]["include_source_kinds"] = ["skill_node"]
        produced = actor_pool.apply_stat_transfers(self._rows(), rule)
        increased = [row for row in produced if row.operation == "increased"]
        self.assertEqual(len(increased), 1)
        self.assertAlmostEqual(increased[0].value, 0.36 * 0.5, delta=1e-12)


if __name__ == "__main__":
    unittest.main()
