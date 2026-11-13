"""完整解码装备投影：通用显示边界及当前输入的逐词条核对。"""
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))
from le_calc.workflow import unified
from le_calc.report_ui import unified_report


def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()


class ModParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = []
    def handle_starttag(self, tag, attrs):
        if tag == "li":
            identity = dict(attrs).get("data-mod-id")
            if identity:
                self.ids.append(identity)


class UniqueEquipmentProjectionTest(unittest.TestCase):
    def test_plan_input_authority_and_sha_guard(self):
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch") as temp:
            root = Path(temp)
            content = json.dumps({"items": [{"instance_id": "equipment.feet", "mods": [
                {"mod_id": "equipment.feet#unique_description#0", "readable_text": "effect"}]}]})
            (root / "actual.json").write_text(content, encoding="utf-8")
            plan = {"input_refs": [{"role": "calculation_input", "path": "actual.json",
                     "sha256": hashlib.sha256(content.encode()).hexdigest()}]}
            case = {"inputs": {"calculation_input": "wrong-case-path.json"}}
            self.assertEqual(unified._project_equipment(case, root, plan)[0]["mods"][0]["readable_text"], "effect")
            (root / "actual.json").write_text(content + " ", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA"):
                unified._project_equipment(case, root, plan)

    def test_summary_does_not_replace_existing_complete_mods(self):
        complete = {"instance_id": "ring:1", "mods": [{"mod_id": "unique:1", "readable_text": "unique"}]}
        result = {"report_details": {"equipment": [complete], "components": ["preserve"]}}
        out = unified._report_details({}, result, {"equipment": [{"instance_id": "ring:1", "affixes": []}]})
        self.assertEqual(out["equipment"], [complete])
        self.assertEqual(out["components"], ["preserve"])

    def test_all_current_mod_identities_reach_equipment_html(self):
        case = json.loads((ROOT / "examples/ballista-beta5-native-20261112/case.json").read_text("utf-8"))
        calc = json.loads((ROOT / case["inputs"]["calculation_input"]).read_text("utf-8"))
        equipment = unified._project_equipment(case, ROOT)
        expected = Counter(m["mod_id"] for i in calc["items"] for m in i["mods"])
        self.assertEqual([(i["instance_id"], i["mods"]) for i in equipment],
                         [(i["instance_id"], i["mods"]) for i in calc["items"]])
        # 不相关防御词条和无数值的独特效果说明同样保留。
        result = {"presentation": {"title": "current input"}, "report_details": {"equipment": [
            {"instance_id": "equipment.weapon1", "affixes": [{"readable_text": "SHORT SUMMARY"}]}]}}
        html = unified_report.render(result, {"equipment": equipment})
        parser = ModParser()
        parser.feed(html)
        self.assertEqual(Counter(parser.ids), expected)
        self.assertIn("+14 Fire Damage", html)
        self.assertIn("cold damage to attacks and spells per point of dexterity", html)
        self.assertNotIn("SHORT SUMMARY", html)

    def test_unknown_description_is_visible_and_instances_are_separate(self):
        items = [{"instance_id": iid, "mods_complete": True, "mods": [
            {"mod_id": iid + "#unique#0", "value_numeric": None}]} for iid in ("ring:1", "ring:2")]
        html = unified_report.render({"report_details": {}}, {"equipment": items})
        parser = ModParser()
        parser.feed(html)
        self.assertEqual(set(parser.ids), {"ring:1#unique#0", "ring:2#unique#0"})
        self.assertTrue("说明尚未解析" in html)


if __name__ == "__main__":
    unittest.main()
