"""R6-b: file:// recompute availability follows bindings, not protocol."""

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

class AvailabilityNoticeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bh = shell.render(test_html.load_result())
        cls.fr = hit_report.render_hit_html(test_hit_report.build_result())
        cls.shell_js = document.asset("shell.js")
        cls.client = document.asset("recalc_contract.js")
        cls.inline = document.INLINE_SHELL_SCRIPT

    def test_availability_is_not_protocol_based(self):
        self.assertNotIn("offline = fileMode ||", self.shell_js)
        for source in (self.shell_js, self.inline, self.client):
            self.assertIn("data-recalc-available", source)
        self.assertIn("le-recalc-bindings", self.shell_js)
        self.assertIn("le-recalc-bindings", self.inline)
        self.assertIn("recompute_endpoint", self.shell_js)
        self.assertIn("recompute_endpoint", self.inline)

    def test_placeholder_endpoint_is_reported_unavailable(self):
        for source in (self.shell_js, self.inline):
            self.assertIn("__LE_", source)
            self.assertIn("本机重算未启用", source)
            self.assertIn("离线打开：本机重算不可用", source)

    def test_shared_client_updates_availability_after_requests(self):
        self.assertIn("setRecalcAvailability", self.client)
        self.assertIn("setRecalcAvailability(true);", self.client)
        # success hide notice
        accepted = self.client.index("setRecalcAvailability(true);")
        self.assertLess(accepted, self.client.index("options.onAccepted(payload)"))
        # network failure keeps availability (never mislabels file mode as offline)
        self.assertIn("if (!bindings || !bindings.error) { setRecalcAvailability(true); }", self.client)

    def test_both_model_pages_include_same_availability_bootstrap(self):
        for html in (self.bh, self.fr):
            self.assertIn("data-recalc-available", html)
            self.assertIn("data-recalc-mode", html)
            self.assertIn("le-recalc-bindings", html)
            self.assertIn("[data-recalc-offline-note]", html)


if __name__ == "__main__":
    unittest.main()
