# -*- coding: utf-8 -*-
"""Tests for the Flame Reave ``hit`` result/report layer (``le_calc.hit_report``).

Run from the repository root::

    $env:PYTHONPATH = 'tools\\phase3\\src'
    & .\\.venv\\Scripts\\python.exe -m unittest discover -s tools/phase3/tests -p 'test_hit_report.py'

The fixtures are synthetic: they describe the contractual shapes of the
fr-engine outputs (DESIGN §5.1/§5.3), they are not calculation results.
"""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from le_calc import hit_report

# ---------------------------------------------------------------------------
# Synthetic fixtures (contract shapes, NOT real numbers)
# ---------------------------------------------------------------------------

BUILD_SHA = "b012295373521495471a83b460f7142fce84a4649b41df72eb2a13e9a9b313ee"
CALC_SHA = "d4c0ffee" + "0" * 56
DATASET_LOGICAL = "979d18204631746e183dd533d647a8e7e8138ac96ded3b0f19324669b9f80527"
DATASET_FILE = "afa79e9fda0023b2ed09f4cbe8e07da436e0c74c03f34f80772da471c0adb8ab"
RULESET_SHA = "5b000a276079efd87faf9fb85c432e0049a68200f5a14ba5cff1e1e988c8654f"


def snapshot_fixture() -> dict:
    return {
        "build_meta": {
            "build": {
                "build_id": "letools:toolnative-fr-save",
                "revision_id": "rev-b012295373521495",
                "content_sha256": BUILD_SHA,
                "revision_seq": 1,
                "previous_revision_id": None,
            },
            "dataset": {
                "dataset_id": "phase1-dataset-003",
                "target_game_version": "1.5.2",
                "logical_content_sha256": DATASET_LOGICAL,
                "file_sha256": DATASET_FILE,
            },
            "calc_input": {
                "path": "examples/flame-reave/inputs/calculation-input.affix-db.corrected.json",
                "sha256": CALC_SHA,
                "mod_count": 160,
                "unresolved_inputs": 0,
                "schema_version": "calculation-input-v1",
                "generated_at": "2026-11-07T00:00:00+00:00",
            },
            "build_spec_sha256": "e1" * 32,
            "content_sha256": BUILD_SHA,
            "calc_input_sha256": CALC_SHA,
            "database_sha256": DATASET_FILE,
            "dataset_logical_content_sha256": DATASET_LOGICAL,
        },
        "character": {"level": 100, "class": "Mage", "mastery": "Spellblade"},
        "attributes": {
            "strength": 12.0,
            "vitality": 20.0,
            "intelligence": 44.0,
            "dexterity": 4.0,
            "attunement": 4.0,
        },
        "resistances": {
            "fire": {"total": 120.0, "cap": 75.0, "capped": 75.0, "overcapped": 45.0},
            "physical": {"total": 40.0, "cap": 75.0, "capped": 40.0, "overcapped": 0.0},
        },
        "derived": {
            "model": "hit",
            "skill": {
                "ability_id": "fr11mv",
                "name": "Flame Reave",
                "tags": ["Fire", "Melee"],
                "base_damage": [{"damage_type": "Fire", "value": 2.0}],
                "added_damage_scaling": 4.0,
                "crit_chance": 0.05,
                "crit_multiplier": 2.0,
                "mana_cost": 20.0,
                "cast_time_s": 0.8,
                "use_duration_s": 0.8,
                "attack_speed_base": None,
                "cooldown_s": None,
                "hit": True,
            },
            "weapon": {
                "dual_wield": True,
                "slots": [],
                "average_attack_rate": 1.07,
                "average_added": [{"damage_type": "Physical", "value": 51.0, "stat": "melee"}],
            },
            "hit_count": {"per_cast": 3, "returns": 2, "sources": [16, 18], "status": "assumed"},
            "node_effects": [],
            "applied_states": {
                "ignited": False,
                "enchant_weapon_active": True,
                "frenzy": False,
                "rhythm_stacks": 0,
                "firebrand_stacks": 0,
                "blade_weaving_stacks": 0,
            },
        },
        "issues": [],
        "excluded_sources": [],
        "contributions": [
            {
                "contribution_id": "contrib-0001",
                "source_kind": "equipment",
                "source_label": "Rune Hammer · 武器",
                "source_path": "data.equipment.weapon1",
                "stat_name": "Added Melee Damage",
                "stat_key": "added_damage",
                "operation": "added",
                "value": 64.0,
                "unit": "flat",
                "damage_tags": ["Melee", "Physical"],
                "target": None,
                "condition_code": None,
                "condition_status": "always",
                "group": None,
                "included": True,
                "exclusion_reason": None,
                "value_source": "source",
                "status": "confirmed",
                "evidence": [
                    {
                        "kind": "catalog",
                        "detail": "calculation-input-v1 mod equipment.weapon1#implicit#0",
                        "locator": "calculation-input.json#mods[0]",
                    }
                ],
                "notes": None,
            },
            {
                "contribution_id": "contrib-0002",
                "source_kind": "skill_node",
                "source_label": "Heat Wave（技能树节点 9）",
                "source_path": "data.specializations.fr11mv.nodes.9",
                "stat_name": "Increased Fire Damage",
                "stat_key": "damage",
                "operation": "more",
                "value": 0.5,
                "unit": "fraction",
                "damage_tags": ["Fire"],
                "target": None,
                "condition_code": None,
                "condition_status": "always",
                "group": "node:heat_wave",
                "included": True,
                "exclusion_reason": None,
                "value_source": "source",
                "status": "derived",
                "evidence": [
                    {
                        "kind": "game_data",
                        "detail": "fr11mv skill tree node 9 effects",
                        "locator": "data.sqlite#skill_nodes[9]",
                    }
                ],
                "notes": None,
            },
            {
                "contribution_id": "contrib-0003",
                "source_kind": "passive_node",
                "source_label": "Mage Flurry · 角色被动",
                "source_path": "data.passive_tree.nodes.7",
                "stat_name": "Increased Attack Speed",
                "stat_key": "attack_speed",
                "operation": "increased",
                "value": 0.12,
                "unit": "fraction",
                "damage_tags": ["Melee"],
                "target": None,
                "condition_code": None,
                "condition_status": "always",
                "group": None,
                "included": True,
                "exclusion_reason": None,
                "value_source": "source",
                "status": "confirmed",
                "evidence": [
                    {
                        "kind": "catalog",
                        "detail": "passive node 7",
                        "locator": "data.sqlite#passive_nodes[7]",
                    }
                ],
                "notes": None,
            },
            {
                "contribution_id": "contrib-0004",
                "source_kind": "equipment",
                "source_label": "Katana · 副手",
                "source_path": "data.equipment.weapon2",
                "stat_name": "Ignite Chance",
                "stat_key": "ailment_chance",
                "operation": "added",
                "value": 0.1,
                "unit": "fraction",
                "damage_tags": ["Fire"],
                "target": "ignite",
                "condition_code": "target_ignited",
                "condition_status": "not_satisfied",
                "group": None,
                "included": False,
                "exclusion_reason": "目标未点燃（baseline ignited=false）",
                "value_source": "source",
                "status": "assumed",
                "evidence": [],
                "notes": None,
            },
        ],
    }


def ruleset_fixture() -> dict:
    return {
        "schema": "le-ruleset-hit-v1",
        "ruleset_id": "fr-hit-stage1-v2",
        "game_version": "1.5.2",
        "sha256": RULESET_SHA,
        "skill": {"ability_id": "fr11mv"},
        "attack_model": {"type": "dual_wield_average"},
        "rules": [
            {
                "id": "rule-hit-count",
                "title": "每次施放命中次数",
                "statement": "base 1 + Reflash 2 returns = 3（Flame Caller 是否叠加未确认）。",
                "status": "assumed",
                "evidence": [{"kind": "game_data", "locator": "skill tree node 16/18"}],
            },
            {
                "id": "rule-dual-wield-average",
                "title": "双持平均",
                "statement": "aps 与 added weapon damage 取两把武器平均。",
                "status": "assumed",
                "evidence": [{"kind": "derivation", "locator": "design §3 R2"}],
            },
            {
                "id": "rule-crit-average",
                "title": "暴击期望",
                "statement": "avgCrit = (1-critChance) + critChance * critMultiplier。",
                "status": "derived",
                "evidence": [{"kind": "derivation", "locator": "reference planner runtime"}],
            },
        ],
        "effects": [
            {
                "id": "effect-enchant-weapon",
                "label": "Enchant Weapon",
                "status": "assumed",
                "condition": "近战攻击时自动激活",
                "next_probe": "游戏内实测持续时间/冷却",
            },
            {
                "id": "effect-scorching-tide",
                "label": "Scorching Tide（目标点燃）",
                "status": "unresolved",
                "condition": "target ignited",
                "next_probe": "验证目标点燃来源",
            },
        ],
        "open_questions": ["R1 命中次数", "R4 条件 Buff 参数"],
    }


def engine_outputs_fixture() -> dict:
    return {
        "single_hit": {
            "components": [
                {
                    "component_id": "component-fire",
                    "label": "Flame Reave 本体（Fire）",
                    "damage_type": "Fire",
                    "damage_tags": ["Fire", "Melee"],
                    "base": 2.0,
                    "added_scaled": 232.0,
                    "increased": 3.5,
                    "more_product": 4.095,
                    "avg_crit": 1.05,
                    "crit_chance": 0.05,
                    "crit_multiplier": 2.0,
                    "penetration": 1.35,
                    "target_multiplier": 1.0,
                    "damage": 1500.0,
                },
                {
                    "component_id": "component-added-physical",
                    "label": "武器附加（Physical）",
                    "damage_type": "Physical",
                    "damage_tags": ["Physical", "Melee"],
                    "base": 0.0,
                    "added_scaled": 102.0,
                    "increased": 1.2,
                    "more_product": 2.0,
                    "avg_crit": 1.05,
                    "crit_chance": 0.05,
                    "crit_multiplier": 2.0,
                    "penetration": 1.0,
                    "target_multiplier": 1.0,
                    "damage": 500.0,
                },
            ],
            "totals": {
                "base": 2.0,
                "added_scaled": 334.0,
                "increased_total": 3.5,
                "more_product": 4.095,
                "avg_crit": 1.05,
                "crit_chance": 0.05,
                "crit_multiplier": 2.0,
                "penetration_multiplier": 1.35,
                "target_multiplier": 1.0,
                "damage": 2000.0,
            },
            "assumptions": ["忽略目标移动与命中判定时间"],
        },
        "simulate": {
            "hits_per_cast": 3,
            "attack_rate": {
                "base_aps": 1.07,
                "increased_attack_speed": 0.35,
                "more_attack_speed": 1.0,
                "final_aps": 1.4445,
                "source": ["weapon1", "weapon2"],
            },
            "windows": [
                {"window_s": 5, "hits": 21.67, "total_damage": 43333.0, "dps": 8666.6},
                {"window_s": 30, "hits": 130.0, "total_damage": 260000.0, "dps": 8666.6},
                {"window_s": 120, "hits": 520.0, "total_damage": 1040000.0, "dps": 8666.6},
            ],
            "policy": {"id": "single_target", "label": "单一目标持续攻击"},
            "resource_note": "不模拟法力消耗/恢复（无限 mana 假设）",
            "assumptions": [],
        },
        "buff_screening": {
            "schema": "le-hit-buff-screening-v1",
            "summary": {
                "total": 3,
                "modeled": 1,
                "included_in_baseline": 1,
                "not_modeled": 1,
                "unresolved": 1,
            },
            "candidates": [
                {
                    "buff_id": "effect-enchant-weapon",
                    "label": "Enchant Weapon",
                    "source_kind": "skill",
                    "source_path": "skill:sb44eQ",
                    "condition": "近战攻击时自动激活",
                    "status": "modeled",
                    "modeled": True,
                    "included_in_baseline": True,
                    "evidence": [
                        {
                            "kind": "third_party_reference",
                            "locator": "https://lastepoch.tunklab.com/example",
                            "detail": "planner runtime 参考",
                        }
                    ],
                    "next_probe": "游戏内实测持续时间/冷却",
                },
                {
                    "buff_id": "effect-frenzy",
                    "label": "Frenzy",
                    "source_kind": "skill_node",
                    "source_path": "skill:fr11mv.node.6",
                    "condition": "10% 命中几率",
                    "status": "not_modeled",
                    "modeled": False,
                    "included_in_baseline": False,
                    "evidence": [],
                    "next_probe": "确定触发频率",
                },
                {
                    "buff_id": "effect-scorching-tide",
                    "label": "Scorching Tide（目标点燃）",
                    "source_kind": "skill_node",
                    "source_path": "skill:fr11mv.node.23",
                    "condition": "target ignited",
                    "status": "unresolved",
                    "modeled": False,
                    "included_in_baseline": False,
                    "evidence": [],
                    "next_probe": "验证目标点燃来源",
                },
            ],
        },
    }


def case_fixture() -> dict:
    return {
        "schema": "le-case-v1",
        "case_id": "flame-reave",
        "title": "Flame Reave hit fixture",
        "model": "hit",
        "game_version": "1.5.2",
        "monster_level": 100,
        "main_skill": {
            "ability_id": "fr11mv",
            "name": "Flame Reave",
            "class": "Mage",
            "mastery": "Spellblade",
            "damage_tags": ["Fire", "Melee"],
        },
        "output": {"root": "reports/flame-reave", "run_id_template": "{case_id}-{utcstamp}"},
    }


def build_result() -> dict:
    return hit_report.build_hit_result(
        snapshot_fixture(),
        ruleset_fixture(),
        case_fixture(),
        run_id="fixture-run",
        generated_at="2026-11-07T00:00:00+00:00",
        ruleset_path="tools/phase3/rules/ruleset-fr-hit-stage1-v2.json",
        out_dir="reports/flame-reave/fixture-run",
        engine_outputs=engine_outputs_fixture(),
        repro_command="python -m le_calc hit-run ...",
    )


# ---------------------------------------------------------------------------
# result.json structure / consistency
# ---------------------------------------------------------------------------


class HitResultStructureTests(unittest.TestCase):
    def test_top_level_contract(self) -> None:
        result = build_result()
        self.assertEqual(result["schema"], "le-hit-result-v1")
        for key in (
            "run",
            "input",
            "scope",
            "scenario",
            "snapshot",
            "skill",
            "single_hit",
            "hits_per_cast",
            "dps",
            "buffs",
            "source_ledger",
            "rule_ledger",
            "issues",
            "consistency",
        ):
            self.assertIn(key, result)
        self.assertEqual(result["run"]["ruleset"]["id"], "fr-hit-stage1-v2")
        self.assertEqual(result["run"]["ruleset"]["sha256"], RULESET_SHA)
        self.assertEqual(result["skill"]["ability_id"], "fr11mv")
        self.assertEqual(result["run"]["scope_note"], hit_report.SCOPE_NOTE)
        self.assertIn("不是完整 BD 实战 DPS", result["run"]["scope_note"])

    def test_scope_declares_pipeline_reuse(self) -> None:
        """The hit model is an extension of the existing pipeline, not a second one."""
        result = build_result()
        pipeline = result["scope"]["pipeline"]
        self.assertIn("技能模型扩展", pipeline["note"])
        self.assertIn("le.ps1 run", pipeline["entrypoint"])
        reused = " ".join(pipeline["reused"])
        self.assertIn("case.json", reused)
        self.assertIn("model.Snapshot", reused)
        self.assertIn("run-manifest", reused)
        new = " ".join(pipeline["new"])
        self.assertIn("hit", new.lower())
        self.assertIn("le-hit-result-v1", " ".join(pipeline["new"]) + " ".join(new))

    def test_assumption_status_and_issues(self) -> None:
        result = build_result()
        # ruleset has assumed/unresolved rules + effects -> must stay "assumption"
        self.assertEqual(result["run"]["status"], "assumption")
        self.assertTrue(result["run"]["status_reason"])
        statuses = {row["status"] for row in result["rule_ledger"]}
        self.assertIn("assumed", statuses)
        self.assertIn("derived", statuses)
        self.assertTrue(result["issues"], "assumed/unresolved rules must surface issues")

    def test_status_ok_when_everything_confirmed(self) -> None:
        ruleset = ruleset_fixture()
        for rule in ruleset["rules"]:
            rule["status"] = "confirmed"
        ruleset["effects"] = []
        ruleset["open_questions"] = []
        outputs = engine_outputs_fixture()
        outputs["single_hit"]["assumptions"] = []
        outputs["buff_screening"]["candidates"] = [
            candidate
            for candidate in outputs["buff_screening"]["candidates"]
            if candidate["status"] != "unresolved"
        ]
        outputs["buff_screening"]["summary"]["unresolved"] = 0
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset,
            case_fixture(),
            run_id="ok-run",
            generated_at="2026-11-07T00:00:00+00:00",
            engine_outputs=outputs,
        )
        self.assertEqual(result["run"]["status"], "ok")

    def test_input_fingerprints_come_from_snapshot_build_meta(self) -> None:
        result = build_result()
        meta = snapshot_fixture()["build_meta"]
        self.assertEqual(result["input"]["build"]["content_sha256"], BUILD_SHA)
        self.assertEqual(result["input"]["calc_input"]["sha256"], CALC_SHA)
        self.assertEqual(result["input"]["dataset"]["dataset_id"], "phase1-dataset-003")
        self.assertEqual(result["input"]["fingerprints"]["content_sha256"], BUILD_SHA)
        self.assertEqual(result["input"]["fingerprints"]["calc_input_sha256"], CALC_SHA)
        self.assertEqual(result["input"]["build"], meta["build"])

    def test_consistency_all_pass(self) -> None:
        result = build_result()
        consistency = result["consistency"]
        self.assertTrue(consistency["all_pass"])
        self.assertTrue(consistency["single_hit_components_sum_equals_total"])
        self.assertTrue(consistency["dps_windows_ordered"])
        self.assertTrue(consistency["dps_windows_complete"])
        self.assertTrue(consistency["snapshot_binding_matches_result_input"])
        self.assertEqual(consistency["all_pass"], all(
            value for key, value in consistency.items() if key != "all_pass"
        ))

    def test_components_sum_to_total(self) -> None:
        result = build_result()
        components = result["single_hit"]["components"]
        total = result["single_hit"]["totals"]["damage"]
        self.assertAlmostEqual(sum(component["damage"] for component in components), total, places=6)

    def test_dps_windows_are_ordered(self) -> None:
        result = build_result()
        windows = result["dps"]["windows"]
        self.assertEqual([window["window_s"] for window in windows], [5.0, 30.0, 120.0])
        totals = [window["total_damage"] for window in windows]
        self.assertEqual(totals, sorted(totals))
        self.assertTrue(windows[0]["dps"] > 0)

    def test_hits_per_cast_contract(self) -> None:
        result = build_result()
        hits = result["hits_per_cast"]
        self.assertEqual(hits["per_cast"], 3)
        self.assertEqual(hits["returns"], 2)
        self.assertEqual(hits["sources"], [16, 18])
        self.assertEqual(hits["status"], "assumed")

    def test_source_ledger_rows(self) -> None:
        result = build_result()
        rows = result["source_ledger"]
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["row_id"], "sl-0001")
        by_id = {row["contribution_id"]: row for row in rows}
        self.assertTrue(by_id["contrib-0001"]["active"])
        self.assertFalse(by_id["contrib-0004"]["active"])
        self.assertEqual(by_id["contrib-0004"]["condition_status"], "not_satisfied")
        self.assertEqual(by_id["contrib-0002"]["stat_key"], "damage")
        self.assertEqual(result["snapshot"]["contribution_summary"]["total"], 4)

    def test_rule_ledger_rows(self) -> None:
        result = build_result()
        rows = {row["rule_id"]: row for row in result["rule_ledger"]}
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows["rule-hit-count"]["status"], "assumed")
        self.assertEqual(rows["rule-crit-average"]["status"], "derived")
        self.assertTrue(rows["rule-hit-count"]["evidence"])

    def test_buffs_screening_summary_and_candidates(self) -> None:
        result = build_result()
        buffs = result["buffs"]
        self.assertEqual(len(buffs["applied"]), 6)
        states = {row["state"]: row for row in buffs["applied"]}
        self.assertTrue(states["enchant_weapon_active"]["active"])
        self.assertFalse(states["ignited"]["active"])
        self.assertEqual(len(buffs["screening"]["candidates"]), 3)
        summary = buffs["screening"]["summary"]
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["unresolved"], 1)

    def test_issues_are_grouped_and_deduped(self) -> None:
        result = build_result()
        issue_ids = [issue["issue_id"] for issue in result["issues"]]
        # one grouped issue per status instead of one issue per rule
        self.assertIn("issue-rules-assumed", issue_ids)
        self.assertIn("issue-effects-blocking", issue_ids)
        self.assertNotIn("issue-rule-rule-hit-count", issue_ids)
        # engine assumptions are consolidated (single-hit + DPS calls repeat them)
        self.assertIn("issue-engine-assumptions", issue_ids)
        engine_issues = [i for i in issue_ids if i.startswith("issue-engine-assumption-")]
        self.assertEqual(engine_issues, [])
        self.assertEqual(len(issue_ids), len(set(issue_ids)))

    def test_component_sum_mismatch_fails_consistency(self) -> None:
        outputs = engine_outputs_fixture()
        outputs["single_hit"]["totals"]["damage"] = 9999.0
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset_fixture(),
            case_fixture(),
            run_id="bad-run",
            generated_at="2026-11-07T00:00:00+00:00",
            engine_outputs=outputs,
        )
        self.assertFalse(result["consistency"]["all_pass"])
        self.assertFalse(result["consistency"]["single_hit_components_sum_equals_total"])
        self.assertTrue(any(issue["issue_id"] == "issue-consistency" for issue in result["issues"]))

    def test_snapshot_binding_mismatch_fails_consistency(self) -> None:
        result = build_result()
        result["input"]["build"]["content_sha256"] = "ffff" + "0" * 60
        checks = hit_report.consistency_checks(
            snapshot_doc=snapshot_fixture(),
            input_block=result["input"],
            single_hit=result["single_hit"],
            dps=result["dps"],
            hits_per_cast=result["hits_per_cast"],
        )
        self.assertFalse(checks["snapshot_binding_matches_result_input"])
        self.assertFalse(checks["all_pass"])

    def test_lead_scenario_profiles_shape(self) -> None:
        """``le-hit-scenario-v1`` (Lead-owned) uses profiles + single_hit/dps states."""
        scenario = {
            "schema": "le-hit-scenario-v1",
            "case_id": "flame-reave",
            "mode": "fixed_conditions",
            "baseline_profile": "build_base",
            "binding": {"build_id": "letools:toolnative-fr-save"},
            "profiles": {
                "legacy_single_skill": {
                    "states": {"ignited": False, "enchant_weapon_active": False}
                },
                "build_base": {
                    "states": {"ignited": False, "enchant_weapon_active": True}
                },
            },
            "single_hit": {"variant": "normal", "states": {}},
            "dps": {"policy": "flame_reave_only", "windows_s": [5, 30, 120], "states": {}},
            "target": {"boss_or_rare": True, "monster_level": 100, "fire_resistance": 0.0},
            "assumptions": ["无折射神像", "目标 ignited 默认关闭"],
        }
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset_fixture(),
            case_fixture(),
            run_id="lead-scenario-run",
            generated_at="2026-11-07T00:00:00+00:00",
            scenario=scenario,
            engine_outputs=engine_outputs_fixture(),
        )
        block = result["scenario"]
        self.assertEqual(block["baseline_profile"], "build_base")
        self.assertEqual(block["scenario_id"], "flame-reave:build_base")
        self.assertEqual(block["policy"], "flame_reave_only")
        self.assertEqual(block["windows_s"], [5, 30, 120])
        self.assertEqual(block["target"]["monster_level"], 100)
        self.assertEqual(block["assumptions"], ["无折射神像", "目标 ignited 默认关闭"])
        states = {row["state"]: row for row in result["buffs"]["applied"]}
        self.assertTrue(states["enchant_weapon_active"]["active"])
        self.assertFalse(states["ignited"]["active"])

    def test_scenario_states_override_snapshot_defaults(self) -> None:
        scenario = {
            "scenario_id": "ignited-boss",
            "name": "目标已点燃",
            "states": {"ignited": True, "frenzy": True},
            "target": {"monster_level": 100},
        }
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset_fixture(),
            case_fixture(),
            run_id="scenario-run",
            generated_at="2026-11-07T00:00:00+00:00",
            scenario=scenario,
            engine_outputs=engine_outputs_fixture(),
        )
        states = {row["state"]: row for row in result["buffs"]["applied"]}
        self.assertTrue(states["ignited"]["active"])
        self.assertTrue(states["frenzy"]["active"])
        self.assertEqual(result["scenario"]["scenario_id"], "ignited-boss")
        self.assertEqual(result["scenario"]["target"]["monster_level"], 100)
        self.assertEqual(result["scenario"]["source"], "scenario")


# ---------------------------------------------------------------------------
# run directory artefacts
# ---------------------------------------------------------------------------


class HitRunArtefactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hit-report-")
        self.out = Path(self._tmp.name) / "run"
        self.result = build_result()
        self.written = hit_report.write_hit_run(
            self.result,
            self.out,
            snapshot_document=snapshot_fixture(),
            ruleset_document=ruleset_fixture(),
            screening_document=engine_outputs_fixture()["buff_screening"],
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_all_expected_files_written(self) -> None:
        expected = {
            "result.json",
            "input-snapshot.json",
            "source-ledger.json",
            "rule-ledger.json",
            "buff-screening.json",
            "flame-reave-damage.html",
            "README.zh-CN.md",
            "run-manifest.json",
        }
        self.assertEqual(set(self.written), expected)
        for name in expected:
            self.assertTrue((self.out / name).is_file(), name)

    def test_result_json_roundtrip(self) -> None:
        document = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(document["schema"], "le-hit-result-v1")
        self.assertEqual(document["run"]["run_id"], "fixture-run")
        self.assertEqual(document["consistency"]["all_pass"], True)

    def test_manifest_sha256_chain_and_consistency(self) -> None:
        import hashlib

        manifest = json.loads((self.out / "run-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema"], "le-hit-run-manifest-v1")
        self.assertTrue(manifest["consistency"]["all_pass"])
        files = manifest["files"]
        self.assertEqual(set(files), set(self.written) - {"run-manifest.json"})
        for name, expected in files.items():
            actual = hashlib.sha256((self.out / name).read_bytes()).hexdigest()
            self.assertEqual(actual, expected, name)
        self.assertEqual(manifest["input"]["build"]["content_sha256"], BUILD_SHA)
        result = json.loads((self.out / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["input"], result["input"])

    def test_ledger_artefacts(self) -> None:
        source_ledger = json.loads((self.out / "source-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(source_ledger["schema"], "le-hit-source-ledger-v1")
        self.assertEqual(source_ledger["summary"]["total_rows"], 4)
        rule_ledger = json.loads((self.out / "rule-ledger.json").read_text(encoding="utf-8"))
        self.assertEqual(rule_ledger["schema"], "le-hit-rule-ledger-v1")
        self.assertEqual(rule_ledger["ruleset"]["id"], "fr-hit-stage1-v2")
        self.assertEqual(rule_ledger["summary"]["blocking_rows"], 2)
        screening = json.loads((self.out / "buff-screening.json").read_text(encoding="utf-8"))
        self.assertEqual(len(screening["candidates"]), 3)

    def test_snapshot_artifact_is_full_snapshot(self) -> None:
        document = json.loads((self.out / "input-snapshot.json").read_text(encoding="utf-8"))
        self.assertIn("contributions", document)
        self.assertEqual(len(document["contributions"]), 4)
        self.assertEqual(document["derived"]["model"], "hit")

    def test_readme_mentions_status_and_files(self) -> None:
        readme = (self.out / "README.zh-CN.md").read_text(encoding="utf-8")
        self.assertIn("status=assumption", readme.replace("**", ""))
        self.assertIn("Flame Reave", readme)
        self.assertIn("run-manifest.json", readme)
        self.assertIn("不是完整 BD 实战总 DPS", readme)
        self.assertIn("与现有管线的关系", readme)
        self.assertIn("技能模型扩展", readme)
        self.assertIn("不是第二套管线", readme)

    # -- offline HTML ---------------------------------------------------

    def test_html_is_self_contained_offline(self) -> None:
        html = (self.out / "flame-reave-damage.html").read_text(encoding="utf-8")
        # Offline single file: no external references.  The local-recompute
        # endpoint is a placeholder that the service adapter injects later
        # (never a hardcoded loopback port).
        self.assertNotIn("https://", html.lower())
        self.assertNotIn("http://127.0.0.1", html.lower())
        self.assertIn("__LE_RECALC_ENDPOINT__", html)
        self.assertIn('name="le-recalc-endpoint"', html)
        self.assertNotIn("<script src", html.lower())
        self.assertNotIn("<link rel=\"stylesheet\"", html.lower())
        self.assertNotIn("@import", html.lower())
        self.assertNotIn("<img", html.lower())
        # CSS/data URI external references are forbidden; the inline script may
        # use URL.createObjectURL for the local result export.
        self.assertNotIn("url(http", html.lower())
        self.assertNotIn("url(//", html.lower())
        self.assertNotIn("url(data:", html.lower())
        # The only fetch is the user-clicked local recompute request.
        self.assertEqual(html.count("fetch("), 1)
        self.assertIn("<style>", html)
        self.assertIn("<script>", html)

    def test_html_player_pages(self) -> None:
        html = (self.out / "flame-reave-damage.html").read_text(encoding="utf-8")
        for label in ("总览", "伤害构成", "命中与 DPS", "增伤来源", "Buff 设置"):
            self.assertIn(label, html)
        # player page: no technical status banner / run identity / hard-coded class
        self.assertNotIn("status = assumption", html)
        self.assertNotIn("Rogue", html)
        self.assertIn("Mage / Spellblade", html)
        self.assertIn("预计伤害", html)
        self.assertIn("data-cond-compute", html)
        self.assertIn("data-cond-save", html)

    def test_html_embeds_result_json_and_keeps_numbers(self) -> None:
        html = (self.out / "flame-reave-damage.html").read_text(encoding="utf-8")
        match = re.search(
            r'<script id="result-data" type="application/json">(.*?)</script>',
            html,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        embedded = json.loads(match.group(1))
        self.assertEqual(embedded["schema"], "le-hit-result-v1")
        self.assertEqual(embedded["single_hit"]["totals"]["damage"], 2000.0)
        self.assertEqual(embedded["run"]["run_id"], "fixture-run")
        # evidence URLs are displayed without the scheme -> no external reference bytes
        self.assertNotIn("https://", json.dumps(embedded, ensure_ascii=False))

    def test_html_does_not_recompute_damage_rules(self) -> None:
        """Display-only front end: no formula operators over damage inputs."""
        html = (self.out / "flame-reave-damage.html").read_text(encoding="utf-8")
        scripts = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        self.assertTrue(scripts)
        combined = "\n".join(scripts)
        for forbidden in ("addedDamageScaling", "critMultiplier", "penetration", "damage_types"):
            self.assertNotIn(forbidden, combined)
        # shared chrome JS (tabs/source panel) + the model interaction script
        self.assertIn("classList", combined)
        self.assertIn("readScenario", combined)
        self.assertIn("data-cond-compute", combined)


class HitReportEdgeCaseTests(unittest.TestCase):
    def test_missing_engine_output_degrades_but_flags_consistency(self) -> None:
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset_fixture(),
            case_fixture(),
            run_id="empty-run",
            generated_at="2026-11-07T00:00:00+00:00",
            engine_outputs={},
        )
        self.assertEqual(result["schema"], "le-hit-result-v1")
        self.assertFalse(result["consistency"]["all_pass"])
        self.assertTrue(any(issue["severity"] == "error" for issue in result["issues"]))

    def test_result_summary_is_json_serialisable(self) -> None:
        summary = hit_report.result_summary(build_result())
        json.dumps(summary)
        self.assertEqual(summary["run_id"], "fixture-run")
        self.assertEqual(summary["status"], "assumption")
        self.assertEqual(summary["windows"][0]["window_s"], 5.0)

    def test_invalid_status_in_rule_is_treated_as_blocking(self) -> None:
        ruleset = ruleset_fixture()
        ruleset["rules"][0]["status"] = "unresolved"
        result = hit_report.build_hit_result(
            snapshot_fixture(),
            ruleset,
            case_fixture(),
            run_id="unresolved-run",
            generated_at="2026-11-07T00:00:00+00:00",
            engine_outputs=engine_outputs_fixture(),
        )
        self.assertEqual(result["run"]["status"], "assumption")


if __name__ == "__main__":
    unittest.main()
