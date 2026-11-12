"""P3R review defects D1-D7 regression tests (P3R-A2 round)."""

from __future__ import annotations

import copy
import json
import re
import unittest

from le_calc import hit_report
from le_calc.report_ui import document, present, shell
from le_calc.report_ui import player_text

import test_html
import test_hit_report



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class D1FingerprintTest(unittest.TestCase):
    def test_meta_uses_service_provided_fingerprint(self):
        from test_recalc_contract_page import synthetic_bindings

        bindings = synthetic_bindings("black-hole")
        provided = bindings["renderer_fingerprint"]
        html = shell.render(test_html.load_result(), bindings=bindings)
        match = re.search(r'<meta name="le-renderer-fingerprint" content="([0-9a-f]{64})">', html)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), provided)
        # capabilities-provided value is the second source
        capabilities = {"model_id": "black-hole", "renderer_fingerprint": "9" * 64}
        html = shell.render(test_html.load_result(), capabilities=capabilities)
        match = re.search(r'<meta name="le-renderer-fingerprint" content="([0-9a-f]{64})">', html)
        self.assertEqual(match.group(1), "9" * 64)


class D2PageQuestionTest(unittest.TestCase):
    def test_fr_page_subtitles_are_declarative(self):
        html = hit_report.render_hit_html(test_hit_report.build_result())
        self.assertNotIn("这次模拟算出多少伤害？", html)
        self.assertNotIn("增伤来自哪里？", html)
        subtitles = re.findall(r'<p class="page-question">([^<]*)</p>', html)
        self.assertEqual(len(subtitles), 5)
        for subtitle in subtitles:
            self.assertNotIn("？", subtitle)
        for declarative in ("本次模拟的关键结果与基线口径", "按来源与状态列出的增伤构成"):
            self.assertIn(declarative, html)


class D3SourceInstanceTest(unittest.TestCase):
    def test_data_prefix_is_stripped_for_grouping(self):
        for location, expected in (
            ("data.idols.2.ir", "idols.2"),
            ("data.equipment.weapon1", "equipment.weapon1"),
            ("data.blessings.7", "blessings.7"),
            ("idols.2.ir", "idols.2"),
        ):
            self.assertEqual(hit_report._source_instance(location), expected)


class D4DrawerPlayerTextTest(unittest.TestCase):
    FORBIDDEN = (
        re.compile(r"property\s*[=:]\s*\d", re.I),
        re.compile(r"\bnode:[A-Za-z0-9_:]+"),
        re.compile(r"\bconfirmed\b", re.I),
        re.compile(r"\bevidence\b", re.I),
        re.compile(r"\+\s*-\d"),
    )

    def test_sanitizer_removes_engineering_tokens(self):
        cleaned = player_text.clean_engineering_text(
            "property=40 node:bh2:25 confirmed evidence_refs +-0.01"
        )
        for pattern in self.FORBIDDEN:
            self.assertIsNone(pattern.search(cleaned), pattern.pattern)
        self.assertEqual(player_text.player_raw_text('{"included": true, "value": 1}'), "")
        self.assertIn("辅助/行为类属性", player_text.player_reason("utility/behaviour stat; does not affect hit damage"))

    def test_rendered_drawer_payload_has_no_forbidden_text(self):
        result = copy.deepcopy(test_html.load_result())
        view = present.build_view(result)
        sources = (view.get("sources") or {})
        drawer_fields = (
            "category_label",
            "channel_label",
            "detail_line",
            "name",
            "display_name",
            "effect_label",
            "raw_text",
            "imported_display",
            "stat_label",
            "status_label",
            "condition_label",
            "reason",
            "notes",
        )
        for source in sources.values():
            values = [str(source.get(field) or "") for field in drawer_fields]
            for item in source.get("applies") or []:
                values.extend([str(item.get("label") or ""), str(item.get("detail") or "")])
            for value in values:
                for pattern in self.FORBIDDEN:
                    self.assertIsNone(pattern.search(value), f"{pattern.pattern}: {value!r}")


class D5WindowRoundingTest(unittest.TestCase):
    def test_hit_counts_are_display_rounded(self):
        result = copy.deepcopy(test_hit_report.build_result())
        windows = list((result.get("dps") or {}).get("windows") or [])
        if not windows:
            self.skipTest("fixture has no dps windows")
        windows[0]["hits"] = 14.133
        result["dps"]["windows"] = windows
        html = hit_report.render_hit_html(result)
        self.assertNotIn(">14.133<", html)
        self.assertIn(">14<", html)


class D6LabelFormatTest(unittest.TestCase):
    def test_resistance_label_is_not_duplicated(self):
        text = hit_report._player_effect(
            {"stat_key": "resistance", "target": "fire", "unit": "percent", "value": 0.19, "operation": "added"}
        )
        self.assertEqual(text, "火焰抗性 +19%")
        self.assertNotIn("抗性抗性", text)

    def test_readonly_unique_category_not_duplicated(self):
        label = hit_report._readonly_candidate_label(
            {"label": "Ambition stack gain (idols.2: on boss/rare hit, 1s cooldown)"},
            "传奇效果",
        )
        self.assertNotIn("传奇效果效果", label)

    def test_unsupported_stat_has_readable_label(self):
        text = hit_report._player_effect(
            {"stat_key": "unsupported", "stat_name": "Armor", "unit": "flat", "value": 0.55, "operation": "flag"}
        )
        self.assertIn("护甲", text)
        self.assertNotIn("属性 +0.55", text)

    def test_resource_note_has_no_raw_english_engine_text(self):
        note = hit_report._player_resource_note(
            "mana is not simulated (infinite mana assumption): Flame Reave estimated cost 13.2/cast"
        )
        self.assertNotRegex(note, r"[A-Za-z]{4,}")
        self.assertIn("未模拟法力", note)


class D7TargetResistanceTest(unittest.TestCase):
    def test_target_drawer_has_fire_resistance_control(self):
        html = hit_report.render_hit_html(test_hit_report.build_result())
        self.assertIn("data-cond-fire-res", html)
        self.assertIn("火焰抗性", html)
        scripts = "\n".join(re.findall(r"<script>(.*?)</script>", html, re.S))
        self.assertIn("[data-cond-fire-res]", scripts)
        self.assertNotIn("fire_resistance: 0.0", scripts)


if __name__ == "__main__":
    unittest.main()
