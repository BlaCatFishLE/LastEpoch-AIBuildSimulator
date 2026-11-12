"""Fixed DPS inspection checklist tests (P2)."""

from __future__ import annotations

import json
import unittest

from le_calc.workflow import checklist

EXPECTED_IDS = [
    "damage_components",
    "base_and_added_damage",
    "increased_reduced",
    "more_less",
    "critical_strike",
    "target_side",
    "rate_and_frequency",
    "conditions_and_stacks",
    "aggregation_and_coverage",
]


class ChecklistTest(unittest.TestCase):
    def test_nine_items_in_fixed_order(self):
        items = checklist.checklist()
        self.assertEqual(len(items), 9)
        self.assertEqual(checklist.EXPECTED_COUNT, 9)
        self.assertEqual([item["id"] for item in items], EXPECTED_IDS)

    def test_ids_unique_and_stable(self):
        ids = [item["id"] for item in checklist.CHECKLIST]
        self.assertEqual(len(ids), len(set(ids)))
        # calling twice must not mutate the module constant
        first = checklist.checklist()
        second = checklist.checklist()
        self.assertEqual(first, second)
        first[0]["id"] = "mutated"
        self.assertEqual(checklist.CHECKLIST[0]["id"], "damage_components")

    def test_required_fields(self):
        for item in checklist.checklist():
            for key in ("id", "title_zh", "title_en", "automatic_reuse", "agent_gap",
                        "components", "calculation_blocks"):
                self.assertIn(key, item)
                if key not in ("components", "calculation_blocks"):
                    self.assertTrue(str(item[key]).strip(), f"{item['id']}.{key} is empty")
            self.assertIsInstance(item["components"], list)
            self.assertIsInstance(item["calculation_blocks"], list)

    def test_item_by_id_and_component_index(self):
        item = checklist.item_by_id("more_less")
        self.assertEqual(item["title_zh"], "More / Less")
        index = checklist.component_index()
        self.assertIn("more_less", index.get("ignite", []))
        self.assertIn("target_side", index)
        with self.assertRaises(KeyError):
            checklist.item_by_id("does_not_exist")

    def test_checklist_is_plain_json_data(self):
        text = json.dumps(checklist.checklist(), ensure_ascii=False)
        self.assertIn("damage_components", text)
        self.assertNotIn("<function", text)

    def test_blocks_reference_known_skill_template_blocks(self):
        known = {"tag_match", "added_scaling", "increased_aggregate", "more_groups",
                 "crit", "target_side", "frequency", "ledger"}
        for item in checklist.CHECKLIST:
            for block in item["calculation_blocks"]:
                self.assertIn(block, known, f"{item['id']} references unknown block {block}")


if __name__ == "__main__":
    unittest.main()
