"""统一 DPS 第一步定向验证（A1–A12）。

原则：优先最小合成输入；只有必须核对当前案例绑定时才只读加载真实输入，
不对历史报告做断言，不运行全仓回归。
"""

from __future__ import annotations

import copy
import json
import shutil
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from le_calc import contribution_mapping as mapping
from le_calc import dps, dps_components as components, effect_binding as binding, model
from le_calc.catalog import Catalog
from le_calc.mechanics.attribute_conversion import resolve_brutality

ROOT = Path(__file__).resolve().parents[3]
CASE_PATH = ROOT / "examples" / "void-cleave-20261110" / "case.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _contribution(
    cid: str,
    stat_key: str,
    operation: str,
    value: float,
    *,
    tags: tuple[str, ...] = (),
    target=None,
    included: bool = True,
    raw=None,
    status: str = model.STATUS_CONFIRMED,
    source_kind: str = "equipment",
) -> model.Contribution:
    return model.Contribution(
        contribution_id=cid,
        source_kind=source_kind,
        source_label=cid,
        source_path=cid,
        stat_name=stat_key,
        stat_key=stat_key,
        operation=operation,
        value=value,
        damage_tags=list(tags),
        target=target,
        included=included,
        exclusion_reason=None if included else "synthetic excluded",
        status=status,
        raw=raw,
    )


def _skill(*, crit_capable: bool = False, base_damage: dict | None = None) -> dict:
    return {
        "base_damage": base_damage or {"Fire": 100},
        "added_damage_scaling": 4,
        "crit_chance": 0.05,
        "crit_multiplier": 2,
        "components": {
            "hit": {"tags": ["Melee", "Hit"], "adaptive_damage_type": "Fire", "crit_capable": crit_capable},
            "dot": {"tags": ["DoT"], "crit_capable": False},
        },
    }


def _component(**overrides) -> dict:
    spec = {"tags": ["Melee", "Hit"], "adaptive_damage_type": "Fire", "crit_capable": False}
    spec.update(overrides)
    return spec


def _target_info(dr: float = 0.0) -> dict:
    return {"monster_damage_reduction": dr}


def _target() -> dict:
    return {"monster_level": 100, "resistance": 0.0, "high_health": False, "boss": True, "blinded": False, "slowed": False}


def _ailment(name: str = "Ignite") -> dict:
    return {
        "name": name,
        "label_zh": name,
        "ailment_id": 1,
        "damage_type": "Fire",
        "duration_s": 2.0,
        "base_damage": 100,
        "chance": {"source_kinds": ["equipment"], "excluded_tags": ["Spell", "Bow", "Throwing", "Minion"], "additional": []},
    }


def _load_real_case():
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    profile = dps.read(ROOT / case["dps_profile"])
    calc = json.loads((ROOT / case["inputs"]["calculation_input"]).read_text(encoding="utf-8"))
    dataset = dps.read(ROOT / case["dataset"])
    return case, profile, calc, dataset


class A1DotOnlyAttributesTest(unittest.TestCase):
    """A1：仅 DoT 的 increased 属性——Hit 不变，匹配 DoT 按独立公式变化。"""

    def test_dot_only_increased_changes_only_dot(self):
        contributions = [
            _contribution("test:dot-inc", model.STAT_DAMAGE, "increased", 0.2, tags=("DoT",)),
        ]
        hit, _ = components.build_hit_components(
            contributions=contributions,
            skill=_skill(),
            component=_component(),
            crit_rules={},
            target=_target(),
            states={},
            target_info=_target_info(),
        )
        dot, applications, _ = components.build_dot_components(
            contributions=contributions,
            ailments=[_ailment()],
            dot_component={"tags": ["DoT"]},
            penetration_rule=None,
            target=_target(),
            target_info=_target_info(),
        )
        self.assertEqual(len(hit), 1)
        self.assertAlmostEqual(hit[0]["damage"], 100.0, places=9)
        self.assertEqual(hit[0]["increased_multiplier"], 1.0)
        self.assertAlmostEqual(dot[0]["stack_damage"], 120.0, places=9)
        self.assertEqual(dot[0]["increased_multiplier"], 1.2)
        self.assertIn("test:dot-inc", dot[0]["source_ids"])


class A2ComponentApplicabilityTest(unittest.TestCase):
    """A2：Hit/DoT 与 Melee/Spell 标签组合，只作用于匹配组件，不双重计入。"""

    def test_tags_apply_only_to_matching_components(self):
        contributions = [
            _contribution("t:melee", model.STAT_DAMAGE, "increased", 0.1, tags=("Melee",)),
            _contribution("t:spell-hit", model.STAT_DAMAGE, "increased", 0.1, tags=("Spell", "Hit")),
            _contribution("t:dot", model.STAT_DAMAGE, "increased", 0.1, tags=("DoT",)),
            _contribution("t:generic", model.STAT_DAMAGE, "increased", 0.1),
        ]
        hit, hit_apps = components.build_hit_components(
            contributions=contributions,
            skill=_skill(),
            component=_component(),
            crit_rules={},
            target=_target(),
            states={},
            target_info=_target_info(),
        )
        dot, dot_apps, _ = components.build_dot_components(
            contributions=contributions,
            ailments=[_ailment()],
            dot_component={"tags": ["DoT"]},
            penetration_rule=None,
            target=_target(),
            target_info=_target_info(),
        )
        self.assertAlmostEqual(hit[0]["increased_multiplier"], 1.2, places=9)  # Melee + generic
        self.assertAlmostEqual(dot[0]["increased_multiplier"], 1.2, places=9)  # DoT + generic
        statuses = {record["contribution_id"]: record["status"] for record in hit_apps}
        self.assertEqual(statuses["t:spell-hit"], components.NOT_APPLICABLE)
        dot_statuses = {record["contribution_id"]: record["status"] for record in dot_apps}
        self.assertEqual(dot_statuses["t:melee"], components.NOT_APPLICABLE)
        self.assertEqual(dot_statuses["t:spell-hit"], components.NOT_APPLICABLE)


class A3EquipmentFactsTest(unittest.TestCase):
    """A3：双持按两件真实单手武器判断；单手+盾、双手各自正确。"""

    @staticmethod
    def _item(instance: str, slot: str, item_type: str, base_entity: str) -> dict:
        return {
            "instance_id": instance,
            "slot": slot,
            "item_type": item_type,
            "base_name": item_type,
            "display_name": item_type,
            "base_entity_id": base_entity,
            "definition_entity_id": f"item:test:{instance}",
        }

    def test_two_same_type_one_handed_weapons_are_dual_wield(self):
        calc = {
            "items": [
                self._item("equipment.weapon1", "weapon1", "Sword", "item:letools:itemList.equippable_9:1"),
                self._item("equipment.weapon2", "weapon2", "Sword", "item:letools:itemList.equippable_9:2"),
            ]
        }
        facts = binding.equipment_facts(calc)
        self.assertTrue(facts.dual_wield)
        self.assertEqual(facts.weapon_type_ids, frozenset({9}))
        self.assertEqual(len(facts.weapons), 2)
        self.assertEqual(binding.evaluate_planner_condition({"dualWield": True}, facts)[0], True)
        self.assertEqual(binding.evaluate_planner_condition({"weapon": [9]}, facts)[0], True)
        self.assertEqual(binding.evaluate_planner_condition({"weapon": [12]}, facts)[0], False)

    def test_one_hand_and_shield_is_not_dual_wield(self):
        calc = {
            "items": [
                self._item("equipment.weapon1", "weapon1", "Sword", "item:letools:itemList.equippable_9:1"),
                self._item("equipment.weapon2", "weapon2", "Shield", "item:letools:itemList.equippable_18:1"),
            ]
        }
        facts = binding.equipment_facts(calc)
        self.assertFalse(facts.dual_wield)
        self.assertIsNotNone(facts.shield)
        self.assertEqual(facts.weapon_type_ids, frozenset({9}))

    def test_two_handed_weapon_is_not_dual_wield(self):
        calc = {
            "items": [
                self._item("equipment.weapon1", "weapon1", "Two-Handed Axe", "item:letools:itemList.equippable_12:10"),
            ]
        }
        facts = binding.equipment_facts(calc)
        self.assertFalse(facts.dual_wield)
        self.assertEqual(facts.weapon_type_ids, frozenset({12}))
        self.assertTrue(facts.main_hand.two_handed)
        self.assertEqual(binding.evaluate_planner_condition({"weapon": [12, 16, 13, 14, 15]}, facts)[0], True)
        self.assertEqual(binding.evaluate_planner_condition({"weapon": [9, 16]}, facts)[0], False)


class A4ConversionSourceTest(unittest.TestCase):
    """A4：移除/替换转换装备后，原转换及派生效果消失（状态仍开也不生效）。"""

    @staticmethod
    def _calc(mod_id: str, stat_name: str) -> dict:
        return {
            "items": [
                {
                    "instance_id": "equipment.feet",
                    "slot": "feet",
                    "item_type": "Boots",
                    "base_entity_id": "item:letools:itemList.equippable_1:1",
                    "definition_entity_id": "item:letools:uniqueList.uniques:1",
                }
            ],
            "mods": [
                {
                    "mod_id": mod_id,
                    "item_instance_id": "equipment.feet",
                    "property": 98,
                    "special_tag": 0,
                    "tags": 93,
                    "modifier_type": 0,
                    "value_numeric": 1.0,
                    "stat": {"name": stat_name},
                }
            ],
        }

    @staticmethod
    def _profile(mod_id: str) -> dict:
        return {
            "schema": "le-dps-profile-v2",
            "ability_id": "v01cv",
            "rules": {
                "movement_conversion": {
                    "label": "Vaion's Chariot",
                    "source": {
                        "kind": "equipment_mod",
                        "item_instance_id": "equipment.feet",
                        "mod_id": mod_id,
                        "expected_property": 98,
                        "expected_definition_entity_id": "item:letools:uniqueList.uniques:1",
                        "expected_stat_name_contains": "Increased Damage per 100% Increased Movement Speed",
                    },
                    "input_stat": "movement_speed",
                    "ratio": 1.0,
                    "output": {"stat": "damage", "operation": "increased", "tags": []},
                }
            },
            "controls_by_key": {},
        }

    def test_conversion_present(self):
        mod_id = "equipment.feet#unique_mod#1"
        result = binding.bind_movement_conversion(
            self._calc(mod_id, "Increased Damage per 100% Increased Movement Speed"),
            self._profile(mod_id),
            movement_total=0.5,
        )
        self.assertEqual(result.status, binding.BINDING_APPLIED)
        self.assertAlmostEqual(result.contribution.value, 0.5)

    def test_conversion_source_removed(self):
        mod_id = "equipment.feet#unique_mod#1"
        calc = self._calc("equipment.feet#unique_mod#9", "Increased Damage per 100% Increased Movement Speed")
        result = binding.bind_movement_conversion(calc, self._profile(mod_id), movement_total=0.5)
        self.assertEqual(result.status, binding.BINDING_SOURCE_ABSENT)
        self.assertIsNone(result.contribution)

    def test_conversion_same_slot_different_definition(self):
        mod_id = "equipment.feet#unique_mod#1"
        calc = self._calc(mod_id, "18% increased Movement Speed")
        result = binding.bind_movement_conversion(calc, self._profile(mod_id), movement_total=0.5)
        self.assertEqual(result.status, binding.BINDING_SEMANTICS_MISMATCH)
        self.assertIsNone(result.contribution)

    def test_conversion_same_slot_different_definition_identity_mismatch(self):
        mod_id = "equipment.feet#unique_mod#1"
        calc = self._calc(mod_id, "Increased Damage per 100% Increased Movement Speed")
        calc["items"][0]["definition_entity_id"] = "item:letools:uniqueList.uniques:999"
        result = binding.bind_movement_conversion(calc, self._profile(mod_id), movement_total=0.5)
        self.assertEqual(result.status, binding.BINDING_IDENTITY_MISMATCH)
        self.assertIsNone(result.contribution)


class A5HellishNodeBindingTest(unittest.TestCase):
    """A5：Hellish 绑定真实节点与当前点数；0 点或状态关闭都不生效。"""

    @classmethod
    def setUpClass(cls):
        cls.case, cls.profile, cls.calc, cls.dataset = _load_real_case()
        cls.profile = binding.validate_profile(cls.profile)
        cls.catalog = Catalog(ROOT / cls.dataset["database"], root=ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    def test_real_node_value(self):
        value, points, check = binding.bind_node_value(
            self.calc, self.catalog, tree_id="v01cv", node_id="3", stat_index=2
        )
        self.assertTrue(check.ok)
        self.assertEqual(points, 1)
        self.assertAlmostEqual(value, 0.3, places=12)

    def test_zero_points_source_absent(self):
        calc = copy.deepcopy(self.calc)
        for spec in calc["skills"]["specializations"]:
            if spec["tree"]["entity_id"].endswith(":v01cv"):
                for node in spec["nodes"]:
                    if str(node["node_id"]) == "3":
                        node["points"] = 0
        value, points, check = binding.bind_node_value(
            calc, self.catalog, tree_id="v01cv", node_id="3", stat_index=2
        )
        self.assertIsNone(value)
        self.assertEqual(points, 0)
        self.assertEqual(check.status, binding.BINDING_NOT_APPLICABLE)

    def test_effect_requires_state(self):
        effects = {item["id"]: item for item in self.profile["effects"]}
        self.assertIn("hellish", effects)
        source = effects["hellish"]["source"]
        self.assertEqual(source["kind"], binding.SKILL_NODE)
        self.assertEqual((source["tree"], source["node"]), ("v01cv", "3"))
        results = binding.bind_profile_effects(
            self.calc,
            self.catalog,
            self.profile,
            states={key: False for key in self.profile["controls_by_key"]},
            target=_target(),
            pool_lookup=lambda _stat, _target: 0.0,
        )
        hellish = next(result for result in results if result.effect_id == "hellish")
        self.assertEqual(hellish.status, binding.BINDING_CONDITION_NOT_SATISFIED)
        self.assertFalse(hellish.contribution.active)


class A6ProfileWithoutSpecialKeysTest(unittest.TestCase):
    """A6：合法 profile 不含 incisive/crescendo/vulnerability 时无专用键异常、无默认效果注入。"""

    def test_no_special_state_keys_required(self):
        profile = {
            "schema": "le-dps-profile-v2",
            "controls": [{"key": "dummy", "label": "dummy", "type": "boolean", "default": False, "min": 0, "max": 1}],
            "skill": {
                "base_damage": {"Fire": 10},
                "added_damage_scaling": 1,
                "crit_chance": 0.05,
                "crit_multiplier": 2,
                "components": {
                    "hit": {"tags": ["Melee", "Hit"], "adaptive_damage_type": "Fire", "crit_capable": True},
                    "dot": {"tags": ["DoT"], "crit_capable": False},
                },
            },
            "effects": [],
            "node_bindings": [],
            "ailments": [],
            "rules": {},
        }
        validated = binding.validate_profile(profile)
        payload = binding.component_rule_payload(validated, controls_by_key=validated["controls_by_key"])
        self.assertEqual(payload["crit"], {})
        self.assertIsNone(payload["every_nth_hit"])
        hit, _ = components.build_hit_components(
            contributions=[],
            skill=validated["skill"],
            component=validated["skill"]["components"]["hit"],
            crit_rules=payload["crit"],
            target=_target(),
            states={"dummy": False},
            target_info=_target_info(),
        )
        self.assertEqual(len(hit), 1)
        self.assertAlmostEqual(hit[0]["average_crit"], 1.05)  # 只有暴击基础期望，无 deadly / 易伤注入
        self.assertEqual(hit[0]["deadly_chance_on_crit"], 0.0)
        self.assertEqual(hit[0]["crit_chance"], 0.05)
        self.assertAlmostEqual(hit[0]["damage"], 10.0 * 1.05, places=9)


class A7OwnershipTest(unittest.TestCase):
    """A7：同源同输出只计一次；不同实例不误去重。"""

    def test_duplicate_owner_suppressed(self):
        rows: list = []
        owned: dict = {}
        first = _contribution(
            "mod:a",
            model.STAT_ADDED_DAMAGE,
            "added",
            30.0,
            tags=("Melee",),
            raw={"mod_id": "mod:a"},
        )
        self.assertIsNone(dps._append_owned(rows, owned, first))
        duplicate = _contribution(
            "effect:a",
            model.STAT_ADDED_DAMAGE,
            "added",
            30.0,
            tags=("Melee",),
            raw={"mod_id": "mod:a"},
        )
        self.assertEqual(dps._append_owned(rows, owned, duplicate), "mod:a")
        self.assertEqual(len(rows), 1)
        second = _contribution(
            "mod:b",
            model.STAT_ADDED_DAMAGE,
            "added",
            30.0,
            tags=("Melee",),
            raw={"mod_id": "mod:b"},
        )
        self.assertIsNone(dps._append_owned(rows, owned, second))
        self.assertEqual(len(rows), 2)


class A8CoverageTest(unittest.TestCase):
    """A8：可能影响伤害的未知项进入覆盖缺口；确认防御属性不误报；assumption 保留。"""

    def test_unknown_damage_source_is_gap_and_defensive_is_not(self):
        contributions = [
            _contribution(
                "unknown:damage",
                model.STAT_UNSUPPORTED,
                "flag",
                0.0,
                included=False,
                raw={"coverage_class": mapping.COVERAGE_DAMAGE_RELEVANT},
                status=model.STATUS_UNRESOLVED,
            ),
            _contribution(
                "known:armor",
                model.STAT_UNSUPPORTED,
                "flag",
                0.0,
                included=False,
                raw={"coverage_class": mapping.COVERAGE_DEFENSIVE},
            ),
        ]
        coverage = dps.build_coverage(
            contributions=contributions,
            claimed_mod_ids=set(),
            effect_results=[],
            unmodeled_chance_records=[],
            declared_inactive_checks=[],
            skill_constants={"status": binding.BINDING_VERIFIED},
            per_attribute_problems=[],
        )
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["gap_count"], 1)
        self.assertIn("unmapped:damage_relevant_unknown", coverage["gaps"][0]["code"])

    def test_only_defensive_is_complete(self):
        contributions = [
            _contribution(
                "known:armor",
                model.STAT_UNSUPPORTED,
                "flag",
                0.0,
                included=False,
                raw={"coverage_class": mapping.COVERAGE_DEFENSIVE},
            ),
            _contribution(
                "known:state",
                model.STAT_UNSUPPORTED,
                "flag",
                0.0,
                included=False,
                raw={"coverage_class": mapping.COVERAGE_STATE_MARKER},
            ),
        ]
        coverage = dps.build_coverage(
            contributions=contributions,
            claimed_mod_ids=set(),
            effect_results=[],
            unmodeled_chance_records=[],
            declared_inactive_checks=[],
            skill_constants={"status": binding.BINDING_VERIFIED},
            per_attribute_problems=[],
        )
        self.assertEqual(coverage["status"], "complete")
        self.assertEqual(coverage["gaps"], [])

    def test_effect_source_absent_is_gap(self):
        effect = binding.EffectResult(
            "test_effect", "Test", binding.BINDING_SOURCE_ABSENT, "来源缺失", None
        )
        coverage = dps.build_coverage(
            contributions=[],
            claimed_mod_ids=set(),
            effect_results=[effect],
            unmodeled_chance_records=[],
            declared_inactive_checks=[],
            skill_constants={"status": binding.BINDING_VERIFIED},
            per_attribute_problems=[],
        )
        self.assertEqual(coverage["status"], "partial")


class A9BoundariesTest(unittest.TestCase):
    """A9：每第 N 次命中边界、暴击上限、非法持续时间/施放间隔明确拒绝。"""

    def test_every_nth_hit_counting(self):
        windows, steady = components.hit_windows(
            single_hit=10.0,
            ailments=[],
            interval=1.0,
            every_nth={"n": 3, "extra_more": 0.5},
        )
        by_seconds = {window["seconds"]: window for window in windows}
        self.assertEqual(by_seconds[5]["casts"], 5)
        self.assertAlmostEqual(by_seconds[5]["hit_damage"], 10.0 * (5 + 1 * 0.5))
        self.assertAlmostEqual(by_seconds[30]["hit_damage"], 10.0 * (30 + 10 * 0.5))
        self.assertAlmostEqual(steady["hit_dps"], 10.0 * (1.0 + 0.5 / 3.0))

    def test_crit_chance_cap(self):
        contributions = [
            _contribution(
                "crit:added", model.STAT_CRIT_CHANCE, "added", 5.0, source_kind="derived"
            )
        ]
        hit, _ = components.build_hit_components(
            contributions=contributions,
            skill=_skill(crit_capable=True),
            component=_component(crit_capable=True),
            crit_rules={},
            target=_target(),
            states={},
            target_info=_target_info(),
        )
        self.assertEqual(hit[0]["crit_chance"], 1.0)

    def test_illegal_inputs_rejected(self):
        with self.assertRaises(ValueError):
            components.cast_interval(
                cooldown_s=0.0,
                action_s=0.0,
                cooldown_reduction=0.0,
                cooldown_recovery=0.0,
                attack_speed=0.0,
            )
        with self.assertRaises(ValueError):
            components.cast_interval(
                cooldown_s=-1.0,
                action_s=1.0,
                cooldown_reduction=0.0,
                cooldown_recovery=0.0,
                attack_speed=0.0,
            )
        with self.assertRaises(ValueError):
            components.build_dot_components(
                contributions=[],
                ailments=[{**_ailment(), "duration_s": 0}],
                dot_component={"tags": ["DoT"]},
                penetration_rule=None,
                target=_target(),
                target_info=_target_info(),
            )
        with self.assertRaises(binding.BindingError):
            binding.validate_profile(
                {
                    "schema": "le-dps-profile-v2",
                    "controls": [{"key": "a", "label": "a", "type": "boolean", "default": False, "min": 0, "max": 1}],
                    "skill": {
                        "base_damage": {"Fire": 1},
                        "components": {"hit": {"tags": ["Melee"], "adaptive_damage_type": "Fire"}, "dot": {"tags": ["DoT"]}},
                    },
                    "effects": [
                        {
                            "id": "bad",
                            "label": "bad",
                            "source": {"kind": "unknown_source"},
                            "stat": "damage",
                            "operation": "more",
                            "value": float("nan"),
                        }
                    ],
                }
            )

    def test_legacy_profile_migration_error(self):
        legacy = json.loads(
            (ROOT / "examples" / "void-cleave-20261110" / "inputs" / "dps-profile.json").read_text(encoding="utf-8")
        )
        with self.assertRaises(binding.BindingError) as context:
            binding.validate_profile(legacy)
        self.assertIn("le-dps-profile-v2", str(context.exception))


class A10CliAndRecomputeTest(unittest.TestCase):
    """A10：CLI run 与本机重算使用同一入口，数值/范围一致且 HTML 渲染成功。"""

    def test_recompute_matches_cli(self):
        from le_calc import dps_report

        run_dir = ROOT / ".scratch" / "test-unified-dps-step1"
        if run_dir.exists():
            shutil.rmtree(run_dir)
        client_socket = socket.socket()
        client_socket.bind(("127.0.0.1", 0))
        port = client_socket.getsockname()[1]
        client_socket.close()
        case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
        try:
            result = dps.run(ROOT, case, run_dir, port=port)
            html = (run_dir / "dps-report.html").read_text(encoding="utf-8")
            self.assertIn("le-recalc-bindings", html)
            self.assertIn("计算覆盖", html)
            server = self._serve(run_dir, port)
            try:
                binding_payload = json.loads((run_dir / "report-binding.json").read_text(encoding="utf-8"))
                scenario = dps.defaults(dps.read(ROOT / case["dps_profile"]))
                request_body = json.dumps(
                    {
                        "binding": binding_payload,
                        "scenario": scenario,
                        "request_id": "a10-request",
                        "client_session_id": "a10-session",
                        "client_edit_revision": 0,
                    }
                ).encode("utf-8")
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/recompute",
                    data=request_body,
                    headers={"Content-Type": "application/json"},
                )
                for attempt in range(40):
                    try:
                        with urllib.request.urlopen(request, timeout=5) as response:
                            payload = json.loads(response.read().decode("utf-8"))
                        break
                    except Exception:
                        if attempt == 39:
                            raise
                        time.sleep(0.1)
                self.assertEqual(payload["accepted_revision"], 0)
                recomputed = payload["result"]
                self.assertEqual(recomputed["sustained_dps"], result["sustained_dps"])
                self.assertEqual(recomputed["single_hit"], result["single_hit"])
                self.assertEqual(recomputed["scope"], result["scope"])
                self.assertEqual(recomputed["coverage"]["status"], result["coverage"]["status"])
                self.assertIn("le-recalc-bindings", payload["html"])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            if run_dir.exists():
                shutil.rmtree(run_dir)

    @staticmethod
    def _serve(run_dir: Path, port: int):
        from le_calc import dps_report

        case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
        holder: dict = {}
        original = dps_report.ThreadingHTTPServer

        class _Capturing(original):
            def serve_forever(self, poll_interval=0.5):
                holder["server"] = self
                return super().serve_forever(poll_interval)

        dps_report.ThreadingHTTPServer = _Capturing
        try:
            thread = threading.Thread(
                target=lambda: dps_report.serve(ROOT, case, run_dir, port), daemon=True
            )
            thread.start()
            for _ in range(200):
                if "server" in holder:
                    break
                time.sleep(0.05)
        finally:
            dps_report.ThreadingHTTPServer = original
        if "server" not in holder:
            raise RuntimeError("recompute server did not start")
        return holder["server"]


class A11LegacyHitCompatibilityTest(unittest.TestCase):
    """A11：被提取的映射对旧 hit 路径兼容签名与支持范围不变。"""

    @staticmethod
    def _mod(name: str, *, property_id=0, tags=0, modifier_type=0, value=10.0, readable=None):
        return {
            "mod_id": "test#mod",
            "item_instance_id": "equipment.weapon1",
            "property": property_id,
            "tags": tags,
            "special_tag": 0,
            "modifier_type": modifier_type,
            "value_numeric": value,
            "value_status": "actual",
            "readable_text": readable,
            "stat": {"name": name, "unit": "flat"},
        }

    def test_dot_only_still_excluded_from_legacy_hit(self):
        from le_calc import hit_inputs

        rows = hit_inputs.map_mod(self._mod("Damage", tags=4096))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].stat_key, model.STAT_UNSUPPORTED)
        self.assertIn("Flame Reave hit damage does not inherit DoT", rows[0].exclusion_reason)

    def test_extended_mapping_keeps_dot_only(self):
        rows = mapping.map_mod(self._mod("Damage", tags=4096, modifier_type=1))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].stat_key, model.STAT_DAMAGE)
        self.assertEqual(rows[0].damage_tags, ["DoT"])

    def test_legacy_unsupported_reason_text(self):
        from le_calc import hit_inputs

        rows = hit_inputs.map_mod(self._mod("Armor", property_id=10, value=100.0))
        self.assertEqual(rows[0].stat_key, model.STAT_UNSUPPORTED)
        self.assertEqual(rows[0].exclusion_reason, hit_inputs.REASON_DEFENSIVE)

    def test_legacy_unknown_stat_still_ailment_chance_unsupported(self):
        from le_calc import hit_inputs

        rows = hit_inputs.map_mod(self._mod("Ailment Chance", property_id=1, value=0.5))
        self.assertEqual(rows[0].stat_key, model.STAT_UNSUPPORTED)

    def test_extended_mapping_maps_ailment_chance(self):
        rows = mapping.map_mod(self._mod("Chance to inflict Bleed on Hit", property_id=1, value=1.9))
        self.assertEqual(rows[0].stat_key, model.STAT_AILMENT_CHANCE)
        self.assertEqual(rows[0].target, "0")


class A12AttributeConversionOrderTest(unittest.TestCase):
    """A12：属性转换与依赖属性的求值顺序可解释，原成果未丢失且来源可追溯。"""

    @classmethod
    def setUpClass(cls):
        cls.case, cls.profile, cls.calc, cls.dataset = _load_real_case()
        cls.result = dps.calculate(
            ROOT,
            cls.case,
            cls.profile,
            dps.defaults(cls.profile)["states"],
            dps.defaults(cls.profile)["target"],
        )

    def test_conversion_uses_existing_operator(self):
        conversion = self.result["attribute_conversion"]
        self.assertIsNotNone(conversion)
        rules = json.loads((ROOT / "tools/phase3/rules/attribute-conversions-v1.json").read_text(encoding="utf-8"))
        direct = resolve_brutality(
            conversion["strength_before"],
            conversion["conversion_fraction"],
            conversion["mana_cost"],
            rules["brutality"],
        )
        self.assertAlmostEqual(direct["more_damage"], conversion["more_damage"], places=12)
        self.assertAlmostEqual(direct["brutality"], conversion["brutality"], places=12)
        self.assertEqual(conversion["strength"], 0.0)
        self.assertAlmostEqual(conversion["effective_strength"], conversion["strength_before"], places=12)

    def test_attribute_scaling_uses_effective_strength(self):
        scaling = self.result["effect_ledger"]["attribute_scaling"]
        parts = {part["attribute"]: part for part in scaling["payload"]["parts"]}
        self.assertAlmostEqual(parts["strength"]["value"], 191 * 0.04, places=9)
        self.assertAlmostEqual(parts["vitality"]["value"], 86 * 0.04, places=9)
        contributions = {row["contribution_id"]: row for row in self.result["contributions"]}
        self.assertIn("rule:attribute_scaling", contributions)
        self.assertAlmostEqual(contributions["rule:attribute_scaling"]["value"], 11.08, places=9)

    def test_conversion_contribution_ordered_before_attribute_scaling(self):
        ids = [row["contribution_id"] for row in self.result["contributions"]]
        brutality = next(index for index, cid in enumerate(ids) if cid.startswith("attribute_conversion:"))
        scaling = ids.index("rule:attribute_scaling")
        self.assertLess(brutality, scaling)
        self.assertIn("attribute_conversion", {row["stat_key"] for row in self.result["contributions"]})

    def test_per_attribute_uses_pre_conversion_strength(self):
        contributions = {row["contribution_id"]: row for row in self.result["contributions"]}
        self.assertAlmostEqual(contributions["passive:45:3"]["value"], 0.382, places=9)
        self.assertAlmostEqual(contributions["passive:111:1"]["value"], 0.955, places=9)


class P1CoverageStatusRegressionTest(unittest.TestCase):
    """补修 P1-A：未验证相关效果必须进缺口；可选来源缺失不得误报。"""

    @staticmethod
    def _coverage(effect_results):
        return dps.build_coverage(
            contributions=[],
            claimed_mod_ids=set(),
            effect_results=effect_results,
            unmodeled_chance_records=[],
            declared_inactive_checks=[],
            skill_constants={"status": binding.BINDING_VERIFIED},
            per_attribute_problems=[],
        )

    def test_unverified_effect_is_gap(self):
        coverage = self._coverage(
            [binding.EffectResult("x", "Unverified", binding.BINDING_UNVERIFIED, "缺少身份锚点", None)]
        )
        self.assertEqual(coverage["status"], "partial")
        self.assertEqual(coverage["gaps"][0]["code"], "effect:unverified")

    def test_optional_source_absent_is_not_gap(self):
        coverage = self._coverage(
            [binding.EffectResult("x", "Optional", binding.BINDING_NOT_APPLICABLE, "可选来源不存在", None)]
        )
        self.assertEqual(coverage["status"], "complete")

    _ITEM = {
        "instance_id": "equipment.weapon1",
        "slot": "weapon1",
        "item_type": "Two-Handed Axe",
        "base_name": "Bardiche",
        "display_name": "Sword Catcher",
        "base_entity_id": "item:letools:itemList.equippable_12:10",
        "definition_entity_id": "item:letools:uniqueList.uniques:338",
    }

    def _bind_optional_effect(self, required: bool):
        calc = {"items": [self._ITEM], "mods": []}
        profile = binding.validate_profile(
            {
                "schema": "le-dps-profile-v2",
                "controls": [{"key": "frenzy", "label": "f", "type": "boolean", "default": False, "min": 0, "max": 1}],
                "skill": {
                    "base_damage": {"Fire": 1},
                    "components": {"hit": {"tags": ["Melee"], "adaptive_damage_type": "Fire"}, "dot": {"tags": ["DoT"]}},
                },
                "effects": [
                    {
                        "id": "optional_effect",
                        "label": "Optional",
                        "required": required,
                        "source": {
                            "kind": "equipment_mod",
                            "item_instance_id": "equipment.weapon1",
                            "mod_id": "equipment.weapon1#unique_mod#9",
                            "expected_definition_entity_id": "item:letools:uniqueList.uniques:338",
                            "expected_property": 98,
                        },
                        "stat": "damage",
                        "operation": "more",
                        "value": 0.1,
                    }
                ],
                "rules": {},
                "ailments": [],
            }
        )
        results = binding.bind_profile_effects(
            calc, None, profile, states={"frenzy": False}, target={}, pool_lookup=lambda *_: 0.0
        )
        return results[0]

    def test_optional_source_absent_binding_status(self):
        result = self._bind_optional_effect(required=False)
        self.assertEqual(result.status, binding.BINDING_NOT_APPLICABLE)

    def test_required_source_absent_binding_status(self):
        result = self._bind_optional_effect(required=True)
        self.assertEqual(result.status, binding.BINDING_SOURCE_ABSENT)

    def test_node_not_allocated_is_not_gap(self):
        case, profile, calc, dataset = _load_real_case()
        profile = binding.validate_profile(profile)
        zeroed = copy.deepcopy(calc)
        for spec in zeroed["skills"]["specializations"]:
            if spec["tree"]["entity_id"].endswith(":ab0lh"):
                for node in spec["nodes"]:
                    node["points"] = 0
        with Catalog(ROOT / dataset["database"], root=ROOT) as catalog:
            node_results = binding.bind_node_bindings(zeroed, catalog, profile, states={})
        ab_hits = [row for row in node_results if (row.source or {}).get("tree") == "ab0lh"]
        self.assertTrue(ab_hits)
        self.assertTrue(all(row.status == binding.BINDING_NOT_APPLICABLE for row in ab_hits))
        coverage = self._coverage(ab_hits)
        self.assertEqual(coverage["status"], "complete")


class P1IdentityRegressionTest(unittest.TestCase):
    """补修 P1-B：来源类型必须同时具备稳定身份与效果语义锚点。"""

    ITEM = {
        "instance_id": "equipment.weapon1",
        "slot": "weapon1",
        "item_type": "Two-Handed Axe",
        "base_entity_id": "item:letools:itemList.equippable_12:10",
        "definition_entity_id": "item:letools:uniqueList.uniques:338",
    }
    MOD = {
        "mod_id": "equipment.weapon1#unique_mod#5",
        "item_instance_id": "equipment.weapon1",
        "property": 98,
        "special_tag": 0,
        "tags": 411,
        "modifier_type": 0,
        "value_numeric": 30,
        "readable_text": "+30 Melee Damage while you have Frenzy",
        "stat": {"name": "Melee Damage while you have Frenzy"},
    }

    def _calc(self):
        return {"items": [dict(self.ITEM)], "mods": [dict(self.MOD)]}

    def test_property_only_without_identity_is_unverified(self):
        check = binding.verify_mod(
            self._calc(),
            mod_id=self.MOD["mod_id"],
            item_instance_id="equipment.weapon1",
            expected_property=98,
        )
        self.assertEqual(check.status, binding.BINDING_UNVERIFIED)
        self.assertFalse(check.facts["identity_declared"])

    def test_identity_without_semantics_is_unverified(self):
        check = binding.verify_mod(
            self._calc(),
            mod_id=self.MOD["mod_id"],
            item_instance_id="equipment.weapon1",
            expected_definition_entity_id=self.ITEM["definition_entity_id"],
        )
        self.assertEqual(check.status, binding.BINDING_UNVERIFIED)
        self.assertFalse(check.facts["semantics_declared"])

    def test_identity_plus_semantics_is_verified(self):
        check = binding.verify_mod(
            self._calc(),
            mod_id=self.MOD["mod_id"],
            item_instance_id="equipment.weapon1",
            expected_definition_entity_id=self.ITEM["definition_entity_id"],
            expected_property=98,
            expected_readable_text_contains="Melee Damage while you have Frenzy",
        )
        self.assertEqual(check.status, binding.BINDING_VERIFIED)

    def test_same_property_different_definition_is_identity_mismatch(self):
        calc = self._calc()
        calc["items"][0]["definition_entity_id"] = "item:letools:uniqueList.uniques:999"
        check = binding.verify_mod(
            calc,
            mod_id=self.MOD["mod_id"],
            item_instance_id="equipment.weapon1",
            expected_definition_entity_id=self.ITEM["definition_entity_id"],
            expected_property=98,
        )
        self.assertEqual(check.status, binding.BINDING_IDENTITY_MISMATCH)

    def test_unique_description_requires_nonempty_text_and_definition(self):
        calc = {
            "items": [dict(self.ITEM)],
            "mods": [
                {
                    "mod_id": "equipment.weapon1#unique_description#0",
                    "item_instance_id": "equipment.weapon1",
                    "kind": "description",
                    "readable_text": "+39 Melee Damage for you and your Minions",
                }
            ],
        }
        empty_text = binding.verify_unique_description(
            calc,
            item_instance_id="equipment.weapon1",
            description_index=0,
            expected_text_contains="",
            expected_definition_entity_id=self.ITEM["definition_entity_id"],
        )
        self.assertEqual(empty_text.status, binding.BINDING_UNVERIFIED)
        no_definition = binding.verify_unique_description(
            calc,
            item_instance_id="equipment.weapon1",
            description_index=0,
            expected_text_contains="39 Melee Damage",
        )
        self.assertEqual(no_definition.status, binding.BINDING_UNVERIFIED)
        wrong_definition = binding.verify_unique_description(
            calc,
            item_instance_id="equipment.weapon1",
            description_index=0,
            expected_text_contains="39 Melee Damage",
            expected_definition_entity_id="item:letools:uniqueList.uniques:999",
        )
        self.assertEqual(wrong_definition.status, binding.BINDING_IDENTITY_MISMATCH)

    def test_profile_requires_source_identity(self):
        with self.assertRaises(binding.BindingError):
            binding.validate_profile(
                {
                    "schema": "le-dps-profile-v2",
                    "controls": [{"key": "a", "label": "a", "type": "boolean", "default": False, "min": 0, "max": 1}],
                    "skill": {
                        "base_damage": {"Fire": 1},
                        "components": {"hit": {"tags": ["Melee"], "adaptive_damage_type": "Fire"}, "dot": {"tags": ["DoT"]}},
                    },
                    "effects": [
                        {
                            "id": "no_identity",
                            "label": "no identity",
                            "source": {
                                "kind": "equipment_mod",
                                "item_instance_id": "equipment.weapon1",
                                "mod_id": "equipment.weapon1#unique_mod#5",
                                "expected_property": 98,
                            },
                            "stat": "damage",
                            "operation": "more",
                            "value": 0.1,
                        }
                    ],
                }
            )


class P2RuleParameterRegressionTest(unittest.TestCase):
    """补修 P2：每第 N 次命中 N 必须为正整数；不支持的口径明确拒绝。"""

    @staticmethod
    def _profile(every_nth):
        return binding.validate_profile(
            {
                "schema": "le-dps-profile-v2",
                "controls": [{"key": "a", "label": "a", "type": "boolean", "default": False, "min": 0, "max": 1}],
                "skill": {
                    "base_damage": {"Fire": 1},
                    "components": {"hit": {"tags": ["Melee"], "adaptive_damage_type": "Fire"}, "dot": {"tags": ["DoT"]}},
                },
                "rules": {"every_nth_hit": every_nth},
            }
        )

    def test_fractional_n_rejected(self):
        with self.assertRaises(binding.BindingError):
            self._profile({"n": 2.5})
        with self.assertRaises(binding.BindingError):
            binding.component_rule_payload(
                {"rules": {"every_nth_hit": {"n": 2.5}}}, controls_by_key={}
            )

    def test_boolean_and_zero_n_rejected(self):
        for bad in (True, 0, -1, "3"):
            with self.assertRaises(binding.BindingError):
                self._profile({"n": bad})

    def test_unsupported_counting_and_phase_rejected(self):
        with self.assertRaises(binding.BindingError):
            self._profile({"n": 3, "counting": "cast"})
        with self.assertRaises(binding.BindingError):
            self._profile({"n": 3, "phase": "after_charge"})
        with self.assertRaises(binding.BindingError):
            self._profile({"n": 3, "unsupported_field": 1})

    def test_valid_rule_accepted(self):
        payload = binding.component_rule_payload(
            self._profile({"n": 3, "extra_more": 0.5, "counting": "hit"}), controls_by_key={}
        )
        self.assertEqual(payload["every_nth_hit"]["n"], 3)
        self.assertEqual(payload["every_nth_hit"]["counting"], "hit")


class SharedConditionModuleTest(unittest.TestCase):
    """条件求值抽到共享模块；新旧路径调用同一实现。"""

    def test_shared_module_aliases(self):
        from le_calc import condition_codes, hit_damage

        self.assertTrue(hasattr(condition_codes, "evaluate_condition"))
        self.assertIs(hit_damage.evaluate_condition, condition_codes.evaluate_condition)
        self.assertIs(hit_damage._evaluate_atom, condition_codes.evaluate_atom)
        self.assertIs(hit_damage._STATE_ALIASES, condition_codes.STATE_ALIASES)
        self.assertIn("frenzy_active", condition_codes.default_states())

    def test_evaluation_semantics(self):
        from le_calc import condition_codes

        self.assertTrue(condition_codes.evaluate_condition("mana_ge_300", {"mana_ge_300": True}))
        self.assertFalse(condition_codes.evaluate_condition("mana_ge_300", {"mana_ge_300": False}))
        self.assertIsNone(condition_codes.evaluate_condition("unknown_condition", {}))
        self.assertIsNone(condition_codes.evaluate_condition("frenzy", {}))
        self.assertIsNone(condition_codes.evaluate_condition("passive_condition:{}", {}))
        self.assertTrue(condition_codes.evaluate_condition(None, {}))

    def test_contribution_mapping_uses_shared_condition(self):
        from le_calc import contribution_mapping as mapping_module

        status = mapping_module.condition_status("mana_ge_300", {"mana_ge_300": True})
        self.assertEqual(status, "satisfied")
        status_unknown = mapping_module.condition_status("unknown_condition", {})
        self.assertEqual(status_unknown, "unknown")


class AttributeConversionIdentityRegressionTest(unittest.TestCase):
    """属性转换：加载阶段与运行时都必须要求有效 expected 身份锚点。"""

    @classmethod
    def setUpClass(cls):
        cls.case, cls.profile, cls.calc, cls.dataset = _load_real_case()
        cls.catalog = Catalog(ROOT / cls.dataset["database"], root=ROOT)

    @classmethod
    def tearDownClass(cls):
        cls.catalog.close()

    def _bind(self, conversion=None):
        profile = copy.deepcopy(self.profile)
        if conversion is None:
            profile.pop("attribute_conversion", None)
        else:
            profile["attribute_conversion"] = conversion
        return binding.bind_attribute_conversion(
            self.calc,
            self.catalog,
            profile,
            attributes={"strength": 191.0, "vitality": 86.0},
            hit_component_tags=["Melee", "Hit", "Fire"],
            root=ROOT,
        )

    def _valid_conversion(self) -> dict:
        return copy.deepcopy(self.profile["attribute_conversion"])

    # -- 加载阶段 ---------------------------------------------------------
    def test_load_requires_expected_identity(self):
        variants = []
        missing = copy.deepcopy(self.profile)
        del missing["attribute_conversion"]["expected"]
        variants.append(missing)
        empty_expected = copy.deepcopy(self.profile)
        empty_expected["attribute_conversion"]["expected"] = {}
        variants.append(empty_expected)
        only_instance = copy.deepcopy(self.profile)
        only_instance["attribute_conversion"]["expected"] = {"item_instance_id": "equipment.amulet"}
        variants.append(only_instance)
        empty_definition = copy.deepcopy(self.profile)
        empty_definition["attribute_conversion"]["expected"] = {"definition_entity_id": ""}
        variants.append(empty_definition)
        unknown_field = copy.deepcopy(self.profile)
        unknown_field["attribute_conversion"]["expected"] = {
            "item_instance_id": "equipment.amulet",
            "definition_entity_id": "item:letools:uniqueList.uniques:469",
            "unexpected": "x",
        }
        variants.append(unknown_field)
        empty_conversion = copy.deepcopy(self.profile)
        empty_conversion["attribute_conversion"] = {}
        variants.append(empty_conversion)
        for variant in variants:
            with self.assertRaises(binding.BindingError, msg=str(variant.get("attribute_conversion"))):
                binding.validate_profile(variant)

    def test_load_accepts_valid_anchor(self):
        validated = binding.validate_profile(copy.deepcopy(self.profile))
        self.assertIn("definition_entity_id", validated["attribute_conversion"]["expected"])

    # -- 运行时（可绕过 validate_profile 直接调用） ------------------------
    def test_runtime_missing_or_empty_identity_never_applied(self):
        variants = []
        missing = self._valid_conversion()
        del missing["expected"]
        variants.append(missing)
        empty_expected = self._valid_conversion()
        empty_expected["expected"] = {}
        variants.append(empty_expected)
        only_instance = self._valid_conversion()
        only_instance["expected"] = {"item_instance_id": "equipment.amulet"}
        variants.append(only_instance)
        empty_definition = self._valid_conversion()
        empty_definition["expected"] = {"definition_entity_id": ""}
        variants.append(empty_definition)
        unknown_field = self._valid_conversion()
        unknown_field["expected"] = {
            "item_instance_id": "equipment.amulet",
            "definition_entity_id": "item:letools:uniqueList.uniques:469",
            "unexpected": "x",
        }
        variants.append(unknown_field)
        for variant in variants:
            result = self._bind(variant)
            self.assertNotEqual(result["status"], binding.BINDING_APPLIED, str(variant))
            self.assertIsNone(result["contribution"])
            self.assertIsNone(result["conversion_info"])
            self.assertEqual(result["status"], binding.BINDING_UNVERIFIED)

    def test_runtime_empty_conversion_object_not_applied(self):
        result = self._bind({})
        self.assertNotEqual(result["status"], binding.BINDING_APPLIED)
        self.assertEqual(result["status"], binding.BINDING_UNVERIFIED)

    def test_runtime_missing_conversion_key_is_not_applicable(self):
        result = self._bind(None)
        self.assertEqual(result["status"], binding.BINDING_NOT_APPLICABLE)

    def test_runtime_wrong_definition_rejected(self):
        conversion = self._valid_conversion()
        conversion["expected"]["definition_entity_id"] = "item:letools:uniqueList.uniques:999"
        result = self._bind(conversion)
        self.assertEqual(result["status"], binding.BINDING_IDENTITY_MISMATCH)
        self.assertIsNone(result["contribution"])
        self.assertIsNone(result["conversion_info"])

    def test_runtime_wrong_instance_rejected(self):
        conversion = self._valid_conversion()
        conversion["expected"]["item_instance_id"] = "equipment.ring1"
        result = self._bind(conversion)
        self.assertEqual(result["status"], binding.BINDING_IDENTITY_MISMATCH)
        self.assertIsNone(result["contribution"])

    def test_runtime_valid_source_applied(self):
        result = self._bind(self._valid_conversion())
        self.assertEqual(result["status"], binding.BINDING_APPLIED)
        self.assertIsNotNone(result["contribution"])
        self.assertGreater(result["conversion_info"]["more_damage"], 0.0)
        self.assertIn("equipment.amulet#affix#sealed#0.1", result["source_mod_ids"])

    def test_calculate_rejects_profile_without_expected_identity(self):
        profile = copy.deepcopy(self.profile)
        del profile["attribute_conversion"]["expected"]
        with self.assertRaises(binding.BindingError):
            dps.calculate(
                ROOT,
                self.case,
                profile,
                dps.defaults(profile)["states"],
                dps.defaults(profile)["target"],
            )


if __name__ == "__main__":
    unittest.main()
