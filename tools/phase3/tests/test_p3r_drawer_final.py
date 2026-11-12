"""task-25: BH source drawer is player-readable (no engineering labels/raw text)."""

from __future__ import annotations

import re
import unittest

from le_calc.report_ui import document

import test_html


class BhSourceDrawerFinalTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app_js = document.asset("app.js")
        match = re.search(
            r"function sourceDetailHtml\(source\) \{(.*?)\n  function openSource",
            cls.app_js,
            re.S,
        )
        assert match, "sourceDetailHtml not found in app.js"
        cls.drawer_js = match.group(1)

    def test_drawer_renders_no_engineering_labels_or_raw_text(self):
        for banned in (
            "计算状态",
            "已确认",
            "恒定生效",
            "词条 / 原始文本",
            "词条/原始文本",
            "raw_text",
            "status_label",
            "imported_display",
            "source.reason",
            "source.notes",
            "valueRows",
            "calculationState",
        ):
            self.assertNotIn(banned, self.drawer_js, banned)

    def test_drawer_only_renders_player_readable_fields(self):
        for required in (
            "source.effect_label",
            "本次参与",
            "适用条件",
            "source.condition_status",
            "labels.indexOf",
            "source.name",
        ):
            self.assertIn(required, self.drawer_js)

    def test_drawer_keeps_raw_fields_out_of_visible_text(self):
        # raw fields may only be exposed as DOM data-* attributes (audit/debug)
        self.assertIn("sourcePanel.dataset.sourceRawText", self.app_js)
        self.assertIn('sourcePanel.dataset.sourceStatus', self.app_js)
        # no visible label for the raw text / imported value rows
        for banned in ("原始文本", "导入后显示值", "本次计入", "已计入计算"):
            self.assertNotIn(banned, self.drawer_js)

    def test_drawer_deduplicates_category_labels(self):
        self.assertIn("labels.indexOf(text) === -1", self.drawer_js)
        # the same label must not be appended twice from category/channel/detail
        self.assertIn("source.channel_label !== source.category_label", self.drawer_js)
        self.assertIn("source.detail_line !== source.channel_label", self.drawer_js)



if __name__ == "__main__":
    unittest.main()
