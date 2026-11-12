"""task-31: shared page/mode/scroll state save-restore + BH status state."""
from __future__ import annotations

import re
import unittest

from le_calc import hit_report
from le_calc.report_ui import document, shell

import test_html
import test_hit_report



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class SharedViewStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = document.asset("recalc_contract.js")
        cls.bh = shell.render(test_html.load_result())
        cls.fr = hit_report.render_hit_html(test_hit_report.build_result())

    def test_shared_client_captures_and_restores_view_state(self):
        for token in (
            "VIEW_STATE_KEY",
            "captureViewState",
            "restoreViewState",
            "data-page-btn",
            "data-mode-btn",
            "data-window-btn",
            "scrollTo",
            "sessionStorage.setItem(VIEW_STATE_KEY",
        ):
            self.assertIn(token, self.client)
        # capture must happen before the model replaces the document
        self.assertIn("captureViewState();", self.client)
        self.assertLess(self.client.index("captureViewState();"), self.client.index("options.onAccepted(payload)"))

    def test_both_models_expose_page_tabs_and_bh_modes(self):
        self.assertIn('data-page-btn="conditions"', self.bh)
        self.assertIn('data-page-panel="conditions"', self.bh)
        self.assertIn('data-page-btn="buffs"', self.fr)
        self.assertIn('data-page-panel="buffs"', self.fr)
        for attribute in ('data-mode-btn="single"', 'data-mode-btn="loop"', 'data-window-btn="5"'):
            self.assertIn(attribute, self.bh)

    def test_bh_and_fr_status_state_wiring(self):
        # BH status helper forwards the shared state vocabulary.
        app_js = document.asset("app.js")
        self.assertIn("function liveStatus(text, tone, state)", app_js)
        self.assertIn("onStatus: function (text, tone, state)", app_js)
        self.assertIn("resolved", app_js)
        # FR forwards the state hint as well.
        fr_js = hit_report._hit_report_js()
        self.assertIn("onStatus: function (message, tone, stateHint)", fr_js)
        self.assertIn('data-status-state', fr_js)
        # Both documents include the shared actions/state helpers.
        for html in (self.bh, self.fr):
            self.assertIn("window.ReportActions", html)
            self.assertIn("setStatus", html)

    def test_shared_client_reports_applied_after_restore(self):
        self.assertIn("__leRecalcRestoredApplied", self.client)
        self.assertIn("已应用（本次计算结果）。", self.client)
        fr_js = hit_report._hit_report_js()
        self.assertIn("__leRecalcRestoredApplied", fr_js)
        self.assertIn("已应用（本次计算结果）。", fr_js)

    def test_print_media_has_no_engineering_markers(self):
        for html in (self.bh, self.fr):
            print_css = re.findall(r"@media print\s*\{(.*?)\}\s*</style>", html, re.S)
            visible = re.sub(r"<script.*?</script>", " ", html, flags=re.S)
            visible = re.sub(r"<style.*?</style>", " ", visible, flags=re.S)
            text = re.sub(r"<[^>]+>", " ", visible)
            self.assertNotIn("mod:", text)
            self.assertNotIn("source_path", text)
            for token in ("计算状态", "词条 / 原始文本"):
                self.assertNotIn(token, text)


if __name__ == "__main__":
    unittest.main()
