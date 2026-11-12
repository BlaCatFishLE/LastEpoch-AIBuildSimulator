"""task-22 (P3R-C fixes): baseline table, baseline alignment, offline notice."""
from __future__ import annotations

import ast
import copy
import json
import re
import unittest
from pathlib import Path

from le_calc import hit_report
from le_calc.report_ui import document, shell
from le_calc.report_ui import pages as report_pages

import test_html
import test_hit_report
from test_report_ui_shared import player_text



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def normalized_baselines() -> dict:
    return {
        "profile": "build_base",
        "note": "fixture",
        "profiles": {
            "build_base": {
                "single_hit_damage": 51831.5589095769,
                "dps_windows": {
                    "5s": 121329.04081416795,
                    "30s": 121329.04081416795,
                    "120s": 121329.04081416795,
                },
                "casts_per_second": 0.7066666666666667,
            },
            "legacy_single_skill": {
                "single_hit_damage": 34786.28114736705,
                "dps_windows": {
                    "5s": 81428.88645246171,
                    "30s": 81428.88645246172,
                    "120s": 81428.88645246172,
                },
                "casts_per_second": 0.7066666666666667,
            },
        },
    }


class NoDuplicateDefinitionsTest(unittest.TestCase):
    def test_hit_report_has_no_duplicate_top_level_defs(self):
        source = Path(hit_report.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        seen: dict[str, int] = {}
        duplicates: dict[str, int] = {}
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                seen[node.name] = seen.get(node.name, 0) + 1
                if seen[node.name] > 1:
                    duplicates[node.name] = seen[node.name]
        self.assertEqual(duplicates, {})


class PlayerTextRegressionTest(unittest.TestCase):
    FORBIDDEN = {
        "internal_locator": re.compile(
            r"\b(?:idols|equipment|blessings|passives|specializations)\.[A-Za-z0-9_]+"
        ),
        "dps_policy_id": re.compile(
            r"\bcooldown_limited\b|\bsingle_target\b|DPS 主约束[^<]*\bcooldown\b"
        ),
    }

    def _result_with_raw_candidate(self) -> dict:
        result = test_hit_report.build_result()
        screening = ((result.get("buffs") or {}).get("screening") or {})
        screening["candidates"] = list(screening.get("candidates") or []) + [
            {
                "buff_id": "unsupported-raw",
                "label": "Ambition stack gain (idols.2: on boss/rare hit, 1s cooldown)",
                "source_kind": "idol",
                "source_path": "data.idols.2",
                "coverage": "unresolved",
                "status": "unresolved",
                "included_in_baseline": False,
                "condition": None,
                "evidence": [],
            }
        ]
        result["buffs"] = dict(result.get("buffs") or {})
        result["buffs"]["screening"] = screening
        return result

    def test_unsupported_candidate_label_has_no_locator_or_engine_phrase(self):
        html = hit_report.render_hit_html(self._result_with_raw_candidate())
        text, attributes = player_text(html)
        for label, pattern in self.FORBIDDEN.items():
            self.assertIsNone(pattern.search(text), f"{label}: text")
            self.assertIsNone(pattern.search(attributes), f"{label}: attributes")
        self.assertIn("不适用", text)


class BaselineTableTest(unittest.TestCase):
    def test_normalized_baseline_rows_are_rendered(self):
        result = test_hit_report.build_result()
        result["baselines"] = normalized_baselines()
        html = hit_report.render_hit_html(result)
        text, _attrs = player_text(html)
        # both profiles and all four numeric columns must be present
        self.assertIn("完整构筑基线", text)
        self.assertIn("仅单技能基线", text)
        for value in ("51,831.56", "121,329.04", "34,786.28", "81,428.89"):
            self.assertIn(value, text, value)
        table_match = re.search(
            r'<table class="data-table"><thead><tr><th class="">基线口径</th>.*?</table>',
            html,
            re.S,
        )
        self.assertIsNotNone(table_match)
        self.assertNotIn(">—<", table_match.group(0))

    def test_original_engine_shape_is_not_required(self):
        result = test_hit_report.build_result()
        result["baselines"] = {
            "profile": "build_base",
            "profiles": {
                "build_base": {"single_hit": {"totals": {"damage": 1.0}}},
            },
        }
        html = hit_report.render_hit_html(result)
        self.assertIn("基线数据不可用", html)


class DisplayedBaselineAlignmentTest(unittest.TestCase):
    def _bh_with_conditional(self) -> dict:
        result = copy.deepcopy(test_html.load_result())
        result["conditional"] = {
            "status": "ready",
            "scenario_applied": False,
            "recompute": {
                "status": "ready",
                "baseline_profile": "build_base",
                "delta": {"ignite_total": 777808.7, "single_cast_total": 77780.0},
            },
            "view": {
                "schema": "test",
                "available": True,
                "scenario": {
                    "schema": "le-conditional-scenario-v1",
                    "baseline_profile": "build_base",
                    "preset_id": "build_base",
                    "states": {},
                    "effects": {},
                    "single_cast": {},
                    "loop": {},
                    "target": {},
                    "mode": "single",
                },
                "presets": [],
                "effects": [],
                "groups": [],
            },
        }
        return result

    def test_page_default_scenario_matches_displayed_baseline(self):
        result = self._bh_with_conditional()
        html = shell.render(result)
        view = json.loads(
            re.search(
                r'<script id="view-data" type="application/json">(.*?)</script>',
                html,
                re.S,
            ).group(1)
        )
        conditions = view.get("conditions") or {}
        scenario = conditions.get("scenario") or {}
        self.assertEqual(scenario.get("baseline_profile"), "legacy_single_skill")
        display = conditions.get("display_baseline") or {}
        self.assertEqual(display.get("profile_id"), "legacy_single_skill")
        self.assertIn("仅单技能基线", display.get("label") or "")
        self.assertIn("未修改控件", display.get("note") or "")
        self.assertIn("val-display-baseline", html)
        self.assertIn("当前页面主口径", html)

    def test_applied_scenario_keeps_its_baseline(self):
        result = self._bh_with_conditional()
        result["conditional"]["scenario_applied"] = True
        result["conditional"]["view"]["scenario"]["baseline_profile"] = "build_base"
        html = shell.render(result)
        view = json.loads(
            re.search(
                r'<script id="view-data" type="application/json">(.*?)</script>',
                html,
                re.S,
            ).group(1)
        )
        conditions = view.get("conditions") or {}
        self.assertEqual(
            (conditions.get("scenario") or {}).get("baseline_profile"), "build_base"
        )
        self.assertEqual((conditions.get("display_baseline") or {}).get("profile_id"), "build_base")


class OfflineNoticeTest(unittest.TestCase):
    def test_shared_document_has_fixed_file_offline_notice(self):
        for model_result in (test_html.load_result(), test_hit_report.build_result()):
            html = (
                shell.render(model_result)
                if model_result.get("schema") != "le-hit-result-v1"
                else hit_report.render_hit_html(model_result)
            )
            self.assertIn('id="recalc-offline-note"', html)
            self.assertIn("data-recalc-offline-note", html)
            self.assertIn(document.OFFLINE_NOTICE_TEXT, html)

    def test_shell_js_treats_file_protocol_as_offline(self):
        script = document.asset("shell.js")
        self.assertIn('location.protocol === "file:"', script)
        self.assertIn("离线打开：本机重算不可用", script)
        # serve pages with a real endpoint keep normal mode
        self.assertIn('indexOf("__LE_")', script)

    def test_document_bootstrap_handles_file_protocol_without_model_script(self):
        # BH pages only load app.js; the offline notice must come from the shared
        # document bootstrap, not from shell.js (FR-only today).
        bootstrap = document.INLINE_SHELL_SCRIPT
        self.assertIn('location.protocol === "file:"', bootstrap)
        self.assertIn("离线打开：本机重算不可用", bootstrap)
        self.assertIn("data-recalc-offline-note", bootstrap)
        bh_html = shell.render(test_html.load_result())
        self.assertIn(document.INLINE_SHELL_SCRIPT.strip(), bh_html)

    def test_shared_recalc_client_supports_explicit_file_mode_compute(self):
        # 3-A/R6: a file page with valid bindings may issue an explicitly clicked
        # request; only missing bindings/placeholder endpoints stay offline.
        client = document.asset("recalc_contract.js")
        self.assertNotIn('location.protocol === "file:"', client)
        self.assertIn("client_session_id", client)
        self.assertIn("readBindings", client)
        self.assertIn("本机重算未启用", client)
        bh_html = shell.render(test_html.load_result())
        self.assertIn("le-recalc-request-v1", bh_html)
        fr_html = hit_report.render_hit_html(test_hit_report.build_result())
        self.assertIn("le-recalc-request-v1", fr_html)


if __name__ == "__main__":
    unittest.main()
