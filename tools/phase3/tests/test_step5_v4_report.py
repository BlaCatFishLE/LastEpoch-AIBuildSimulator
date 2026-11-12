"""task-38: formal unified_report player report (6 pages, equipment, buffs, text)."""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import plan_v4  # noqa: E402

CASES = {
    "bh": ROOT / "reports/black-hole-beta33-v4-20261112/dps-step5-v4-beta33-13",
    "fr": ROOT / "reports/flame-reave/dps-step5-v4-fr-18",
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _render(run: Path):
    result = json.loads((run / "result.json").read_text(encoding="utf-8"))
    binding = json.loads((run / "report-binding.json").read_text(encoding="utf-8"))
    return result, binding, plan_v4.render_report_v6(result, binding)


def _visible(html: str) -> str:
    body = re.sub(r"<script.*?</script>|<style.*?</style>", "", html, flags=re.S)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))


class UnifiedPlayerReportTest(unittest.TestCase):
    def test_formal_six_page_report_and_no_raw_ledger_text(self):
        for name, run in CASES.items():
            with self.subTest(case=name):
                result, binding, html = _render(run)
                self.assertGreater(len(html.encode("utf-8")), 500_000)
                for marker in (
                    'name="le-ui-contract-version"',
                    '<style>',
                    'class="page-tabs"',
                    'class="kpi-grid"',
                    'data-page-btn="overview"',
                    'data-page-btn="damage"',
                    'data-page-btn="windows"',
                    'data-page-btn="sources"',
                    'data-page-btn="equipment"',
                    'data-page-btn="buffs"',
                    'id="source-search"',
                    'data-category="',
                    'id="target-drawer"',
                    'data-state="',
                    'id="compute"',
                ):
                    self.assertIn(marker, html)
                self.assertEqual(html.count("data-page-btn="), 6)
                text = _visible(html)
                for forbidden in ("legacy:", "applications_ledger", "effect_ledger", "contribution_id", "{", "}"):
                    self.assertNotIn(forbidden, text)
                embedded = re.search(r'<script id="result-data" type="application/json">(.*?)</script>', html, re.S)
                self.assertIsNotNone(embedded)
                embedded_result = json.loads(embedded.group(1))
                details = embedded_result.get("report_details") or {}
                self.assertEqual(details, result.get("report_details") or {})
                self.assertNotIn("target_controls", details)
                state_controls = details.get("controls") or []
                target_controls = binding.get("target_controls") or []
                self.assertEqual(len(state_controls), 10 if name == "bh" else 16)
                self.assertEqual(len(target_controls), 7 if name == "bh" else 6)
                target_keys = {"monster_level", "resistance", "high_health", "boss", "blinded", "slowed"}
                for control in state_controls:
                    self.assertNotIn(control.get("key"), target_keys)
                self.assertEqual(len(details.get("equipment") or []), 27 if name == "bh" else 28)
                self.assertGreater(sum(len(item.get("mods") or []) for item in details.get("equipment") or []), 100)
                caps = json.loads((run / "report-capabilities.json").read_text(encoding="utf-8"))
                self.assertIn("windows", caps.get("pages") or [])
                self.assertEqual(len(caps.get("controls") or []), len(state_controls))
                self.assertEqual(len(caps.get("target_controls") or []), len(target_controls))
                self.assertNotIn("target_controls", caps.get("scenario_binding", {}).get("scenario", {}))
                self.assertIn("formula-line", html)
                self.assertIn("data-target-state", html)
                self.assertTrue(binding.get("equipment"))
                equipment = binding["equipment"]
                self.assertEqual(len(equipment), 27 if name == "bh" else 28)
                self.assertTrue(any(item.get("role") == "idol" for item in equipment))
                self.assertTrue(any(item.get("role") == "blessing" for item in equipment))
                first_affix = next(
                    (affix for item in equipment for affix in item.get("affixes") or [] if affix.get("readable_text")),
                    None,
                )
                self.assertIsNotNone(first_affix)
                self.assertIn(first_affix["readable_text"].split(" · ")[0], html)
                self.assertIn("装备与神像", html)
                self.assertIn("Buff 设置", html)

    def test_report_numbers_match_v6_result(self):
        for name, run in CASES.items():
            with self.subTest(case=name):
                result, _binding, html = _render(run)
                def fmt(value):
                    return f"{float(value):,.2f}".rstrip("0").rstrip(".")
                self.assertIn(fmt(result["totals"]["horizon_damage"]), html)
                self.assertIn(fmt(result["totals"]["cast_count"]), html)
                for evaluation in result["evaluations"]:
                    self.assertIn(fmt(evaluation["horizon_dps"]), html)
                if name == "fr":
                    self.assertIn("121,329.04", html)
                    self.assertIn("51,831.56", html)
                else:
                    self.assertIn("1,492,215.31", html)
                    self.assertIn("712,381.39", html)
                    for token in ("3.745", "7.025", "52.75", "55%", "0.2015", "5,670,187.41"):
                        self.assertIn(token, html)
                    self.assertGreaterEqual(html.count("formula-line"), 2)
                    self.assertIn("cast-1", html)
                    self.assertIn("charge_events", html)


if __name__ == "__main__":
    unittest.main()
