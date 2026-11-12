"""D10: both models use the shared source/effect/action components + config actions."""

from __future__ import annotations

import re
import unittest

from le_calc import hit_report
from le_calc.report_ui import document, shell, shared_ui

import test_html
import test_hit_report



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class SharedComponentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bh = shell.render(test_html.load_result())
        cls.fr = hit_report.render_hit_html(test_hit_report.build_result())

    def test_both_models_render_shared_source_table(self):
        for html in (self.bh, self.fr):
            self.assertIn('data-shared-component="source-table"', html)
            self.assertIn("data-source-row", html)
            self.assertIn("data-source-id", html)
        self.assertIn("data-source-group", self.fr)
        self.assertIn("data-source-group", self.bh)

    def test_both_models_render_shared_effect_cards(self):
        for html in (self.bh, self.fr):
            self.assertGreaterEqual(html.count('data-shared-component="effect-card"'), 1)
            self.assertIn("cond-effect-head", html)

    def test_both_models_render_shared_action_bars(self):
        for html in (self.bh, self.fr):
            self.assertGreaterEqual(html.count('data-shared-component="action-bar"'), 1)
            self.assertIn("data-cond-status", html)

    def test_shared_ui_primitives_are_used_by_both(self):
        self.assertIn('data-shared-component="source-table"', shared_ui.source_table(rows=[]))
        self.assertIn('data-shared-component="effect-card"', shared_ui.effect_card(title="x"))
        self.assertIn('data-shared-component="action-bar"', shared_ui.action_bar("", aria_label="x"))

    def test_shared_config_actions_asset_in_both_pages(self):
        for html in (self.bh, self.fr):
            self.assertIn("window.ReportActions", html)
            self.assertIn("validateEnvelope", html)
            self.assertIn("legacy_config_no_binding", html)
        self.assertIn("ReportActions.validateEnvelope", re.sub(r"\s+", " ", self.fr))
        # BH save/load goes through the same envelope validator
        self.assertIn("validateEnvelope", self.bh)


if __name__ == "__main__":
    unittest.main()
