"""装备页/报告绑定完整性（UR-15 / 报告契约 §6.9）定向测试。

覆盖：
1. `render_stage` 用 plan 的 calculation_input 权威投影完整 mods（不是 affixes 摘要）；
2. 词条清单按实例/词条身份与输入一致（不丢行、不覆盖）；
3. 装备页按通道展示全部可读词条（implicit/affix/unique/unique_description/corrupted），
   含未参与计算的效果；
4. Mourningfrost 的 unique_description#0/#1 在 binding 与 HTML 中可见；
5. idols/blessings 同样完整展示；
6. 旧摘要绑定（affixes）渲染兼容且明确标注非完整；
7. 缺 readable_text 的词条保留并给出缺失说明；
8. 投射不按 DPS 相关性过滤。
"""
from __future__ import annotations

import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc.report_ui import unified_report  # noqa: E402
from le_calc.workflow import unified  # noqa: E402

CASE_REL = "examples/ballista-beta5-native-20261112/case.json"
CALC_REL = "examples/ballista-beta5-native-20261112/inputs/calculation-input.corrected.json"


def _load_case() -> dict:
    case = json.loads((ROOT / CASE_REL).read_text(encoding="utf-8"))
    case["_path"] = CASE_REL
    return case


def _plan_with_calc_ref() -> dict:
    """内存 plan：只提供本投影需要的 calculation_input 权威引用（含 SHA 绑定）。"""
    return {
        "input_refs": [
            {
                "role": "calculation_input",
                "path": CALC_REL,
                "sha256": planmod.sha256_file(ROOT / CALC_REL),
            }
        ]
    }


def _minimal_result(equipment: list[dict]) -> dict:
    return {
        "presentation": {"title": "装备投影测试"},
        "scenario": {"target": {"monster_level": 100, "resistance": 0.0}, "states": {}},
        "windows": [
            {
                "window_s": 30.0, "hit_damage": 0, "dot_damage": 0, "application_damage": 0,
                "total_damage": 0, "dps": 0, "cast_count": 0, "unsettled_tail_damage": 0,
            }
        ],
        "totals": {
            "horizon_damage": 0, "horizon_dps": 0, "hit_damage": 0, "dot_damage": 0,
            "application_damage": 0, "unsettled_tail_damage": 0, "cast_count": 0,
            "max_concurrent": 0,
        },
        "components": [],
        "contributions": [],
        "coverage": {"status": "complete"},
        "evaluations": [],
        "policy_results": [],
        "report_details": {
            "components": [], "loops": [], "single_cast": {}, "ignite": {}, "cast": {},
            "cooldown": {}, "equipment": equipment, "ailments": [], "controls": [],
        },
    }


def _render(equipment: list[dict], result: dict | None = None) -> str:
    result = copy.deepcopy(result) if result is not None else _minimal_result(equipment)
    return unified_report.render(result, {"equipment": equipment})


def _input_identity_pairs() -> list[tuple[str, str]]:
    calc = json.loads((ROOT / CALC_REL).read_text(encoding="utf-8"))
    pairs: list[tuple[str, str]] = []
    for item in calc["items"]:
        instance_id = str(item.get("instance_id") or "")
        for mod in item.get("mods") or []:
            pairs.append((instance_id, str(mod.get("mod_id") or "")))
    return pairs


def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()


class EquipmentProjectionTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.case = _load_case()
        cls.plan = _plan_with_calc_ref()
        cls.equipment = unified._project_equipment(cls.case, ROOT, cls.plan)
        cls.mod_count = sum(len(item.get("mods") or []) for item in cls.equipment)


class CompleteProjectionTest(EquipmentProjectionTestBase):
    def test_plan_binding_projects_complete_equipment(self):
        self.assertEqual(len(self.equipment), 30)
        self.assertTrue(all(item.get("mods_complete") for item in self.equipment))
        roles = {}
        for item in self.equipment:
            roles[item.get("role")] = roles.get(item.get("role"), 0) + 1
        self.assertEqual(roles, {"equipment": 12, "idol": 8, "blessing": 10})
        feet = next(item for item in self.equipment if item.get("instance_id") == "equipment.feet")
        self.assertEqual(feet.get("display_name"), "Mourningfrost")
        texts = [mod.get("readable_text") for mod in feet["mods"]]
        self.assertTrue(any(text and "cold damage to attacks and spells" in text for text in texts))
        self.assertTrue(any(text and "physical and cold resistance" in text for text in texts))

    def test_mod_identity_matches_calculation_input(self):
        projected = [
            (str(item.get("instance_id")), str(mod.get("mod_id")))
            for item in self.equipment
            for mod in item.get("mods") or []
        ]
        expected = _input_identity_pairs()
        self.assertEqual(len(projected), len(expected))
        self.assertEqual(projected, expected)  # 顺序一致的完整恢复，不丢行/不多行
        self.assertEqual(self.mod_count, 189)
        # 每个 mod_id 必须可追溯到所属实例（instance_id + '#' 前缀）
        for instance_id, mod_id in projected:
            self.assertTrue(mod_id.startswith(instance_id + "#"), (instance_id, mod_id))

    def test_projection_not_filtered_by_dps_relevance(self):
        mods = [mod for item in self.equipment for mod in item.get("mods") or []]
        text = " ".join(str(mod.get("readable_text") or "") for mod in mods)
        # 防御/功能/与主技能无关词条必须保留（不按贡献/DPS 相关性过滤）
        self.assertIn("Increased Experience", text)
        self.assertIn("Lightning Resistance", text)
        self.assertIn("Armor", text)
        channels = {unified_report.mod_channel(mod) for mod in mods}
        self.assertTrue({"implicit", "affix", "corrupted", "unique_mod", "unique_description"} <= channels)


class EquipmentHtmlTest(EquipmentProjectionTestBase):
    def test_html_contains_all_channels_and_mourningfrost(self):
        html = _render(self.equipment)
        for needle in (
            "Mourningfrost",
            "cold damage to attacks and spells per point of dexterity",
            "physical and cold resistance per point of dexterity",
            "68% increased Freeze Rate Multiplier",
            "基底隐含",
            "普通词条",
            "暗金固有效果",
            "暗金说明",
            "腐化词条",
            'data-instance-id="equipment.feet"',
            'data-mod-id="equipment.feet#unique_description#0"',
            'data-mod-id="equipment.feet#unique_description#1"',
            # 未参与计算/防御与功能词条也必须出现在装备页（UR-15）
            "Increased Experience",
            "Lightning Resistance",
            "Armor",
        ):
            self.assertIn(needle, html)

    def test_html_contains_idols_and_blessings_complete(self):
        html = _render(self.equipment)
        idol_rows = [item for item in self.equipment if item.get("role") == "idol"]
        blessing_rows = [item for item in self.equipment if item.get("role") == "blessing"]
        self.assertTrue(idol_rows and blessing_rows)
        idol_text = next(
            mod.get("readable_text") for mod in idol_rows[0]["mods"] if mod.get("readable_text")
        )
        blessing_text = next(
            mod.get("readable_text") for mod in blessing_rows[0]["mods"] if mod.get("readable_text")
        )
        self.assertIn(idol_text, html)
        self.assertIn(blessing_text, html)
        self.assertIn('data-role="idol"', html)
        self.assertIn('data-role="blessing"', html)
        self.assertIn("神像 8 件", html)
        self.assertIn("祝福 10 件", html)


class CompatibilityAndFallbackTest(EquipmentProjectionTestBase):
    def test_legacy_summary_binding_renders_without_loss(self):
        summary = [
            {
                "slot": "feet", "role": "equipment", "instance_id": "equipment.feet",
                "name": "Mourningfrost", "base": "Leather Boots", "rarity": "unique",
                "affixes": [
                    {"readable_text": "+15 Armor", "mod_id": "equipment.feet#implicit#0"},
                    {"readable_text": "18% increased Movement Speed",
                     "mod_id": "equipment.feet#unique_mod#1"},
                ],
            }
        ]
        html = _render(summary)
        self.assertIn("Mourningfrost", html)
        self.assertIn("+15 Armor", html)
        self.assertIn("18% increased Movement Speed", html)
        # 摘要不能被标成完整词条
        self.assertIn("摘要词条", html)
        self.assertNotIn("完整词条 · 2 条", html)

    def test_unresolved_mod_kept_with_missing_note(self):
        item = {
            "slot": "weapon1", "role": "equipment", "instance_id": "equipment.weapon1",
            "name": "测试武器", "base": "Sword", "mods_complete": True,
            "mods": [
                {"mod_id": "equipment.weapon1#unique_mod#5", "channel": "unique",
                 "kind": "mod", "hidden": True},
                {"mod_id": "equipment.weapon1#implicit#0", "channel": "implicit",
                 "kind": "mod", "display_value": "32", "value_numeric": 32,
                 "stat": {"name": "Added Fire Damage", "unit": "flat"}},
            ],
        }
        html = _render([item])
        self.assertIn('data-mod-id="equipment.weapon1#unique_mod#5"', html)
        self.assertIn("内部记录", html)
        self.assertIn("32 Added Fire Damage", html)

    def test_render_stage_uses_plan_for_complete_equipment(self):
        run_dir = Path(tempfile.mkdtemp(dir=str(ROOT / ".scratch"), prefix="equip-render-"))
        self.addCleanup(shutil.rmtree, run_dir, True)
        (run_dir / "result.json").write_text(
            json.dumps(_minimal_result(self.equipment), ensure_ascii=False), encoding="utf-8"
        )
        captured = {}
        original_project = unified._project_equipment

        def fake_project(case, root_path, plan=None):
            captured["plan"] = plan
            return original_project(case, root_path, plan)

        def fake_render(result, binding):
            captured["binding"] = binding
            return "<html>ok</html>"

        with mock.patch.object(unified, "load_plan", return_value=self.plan), \
                mock.patch.object(unified, "_project_equipment", side_effect=fake_project), \
                mock.patch.object(unified.dps_report, "render", side_effect=fake_render), \
                mock.patch.object(unified.calculation_route, "dependency_fingerprints",
                                  return_value={}), \
                mock.patch.object(unified.calculation_route, "resolve_calculation_route",
                                  return_value={"engine": "unified_dps", "status": "ready",
                                                "source": {}}), \
                mock.patch.object(unified.recalc, "build_report_ref", return_value={"report_key": "k"}), \
                mock.patch.object(unified.recalc, "build_recalc_bindings", return_value={}):
            out = unified.render_stage(
                self.case, root=ROOT, run_dir=run_dir,
                endpoint="http://127.0.0.1:1/recompute", display_dir=run_dir,
            )
        self.assertEqual(out["execution_status"], "completed")
        self.assertIsNotNone(captured["plan"])
        self.assertEqual(
            captured["plan"]["input_refs"][0]["role"], "calculation_input"
        )
        binding = captured["binding"]
        self.assertEqual(binding["equipment_projection"]["mode"], "complete")
        self.assertEqual(binding["equipment_projection"]["item_count"], 30)
        self.assertEqual(binding["equipment_projection"]["mod_count"], 189)
        feet = next(
            item for item in binding["equipment"] if item["instance_id"] == "equipment.feet"
        )
        self.assertIn(
            "equipment.feet#unique_description#0",
            [mod["mod_id"] for mod in feet["mods"]],
        )


if __name__ == "__main__":  # pragma: no cover - 直接运行入口
    import unittest

    unittest.main()
