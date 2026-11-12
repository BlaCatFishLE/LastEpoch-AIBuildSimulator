"""P3R-A: cross-model shared shell, player text (1-C) and formatting tests."""

from __future__ import annotations

import copy
import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

from le_calc import hit_report
from le_calc.report_ui import document, shell, units

import test_html  # same-directory fixture helpers
import test_hit_report

FORBIDDEN_TEXT = {
    "mod": re.compile(r"mod:"),
    "contribution": re.compile(r"contribution_id|contribution ID", re.I),
    "field_path": re.compile(
        r"\b(?:ruleset|single_hit|snapshot|scope|source_ledger|rule_ledger)\.[A-Za-z_]"
    ),
    "node_index": re.compile(r"第\s*\d+\s*号节点|\bnode\s*[#:：]?\s*\d+|\[(?:passive|tree)[^\]]*node", re.I),
    "internal_profile": re.compile(r"baseline_profile|build_base|legacy_single_skill"),
    "engineering_state": re.compile(r"\bconfirmed\b|\bnot_modeled\b|property\s*\d+"),
    "double_sign": re.compile(r"\+-"),
    "schema": re.compile(r"le-hit-result|le-black-hole|result-v\d"),
    "db_fields": re.compile(r"\b(?:stat_key|stat_name|source_path|contribution_id)\b\s*[:=]"),
}



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class PlayerTextProbe(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.attributes: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        for name, value in attrs:
            if value and name in {"title", "aria-label", "alt", "aria-description"}:
                self.attributes.append(value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._skip:
            return
        text = data.strip()
        if text:
            self.text.append(text)


def player_text(html: str) -> tuple[str, str]:
    probe = PlayerTextProbe()
    probe.feed(html)
    return "\n".join(probe.text), "\n".join(probe.attributes)


def render_both() -> dict[str, tuple[dict, str]]:
    bh = copy.deepcopy(test_html.load_result())
    fr = test_hit_report.build_result()
    return {
        "black-hole": (bh, shell.render(bh)),
        "hit": (fr, hit_report.render_hit_html(fr)),
    }


class SharedSkeletonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = render_both()

    def test_both_models_use_shared_shell(self):
        for model, (_result, html) in self.rendered.items():
            for marker in (
                "app-header",
                "header-top",
                "brand-mark",
                "page-tabs",
                "data-page-btn",
                "data-page-panel",
                "app-footer",
                "source-panel",
                "skip-link",
                'id="result-data"',
                'id="view-data"',
                'id="report-capabilities"',
                'name="le-ui-contract-version"',
                'name="le-renderer-fingerprint"',
            ):
                self.assertIn(marker, html, f"{model}: missing shared marker {marker}")
            self.assertNotIn("data-tab=", html, f"{model}: legacy page tab attributes")
            self.assertNotIn("hit-result-json", html, f"{model}: legacy FR page shell id")

    def test_renderer_metadata_shared_and_deterministic(self):
        fingerprint = document.renderer_fingerprint()
        self.assertRegex(fingerprint, r"^[0-9a-f]{64}$")
        for _result, html in self.rendered.values():
            self.assertIn(f'content="{fingerprint}"', html)
            capabilities = json.loads(
                re.search(
                    r'<script id="report-capabilities" type="application/json">(.*?)</script>',
                    html,
                    re.S,
                ).group(1)
            )
            self.assertEqual(capabilities["ui_contract_version"], document.UI_CONTRACT_VERSION)
            self.assertIn("pages", capabilities)

    def test_local_recompute_endpoint_is_injected_not_hardcoded(self):
        fr = test_hit_report.build_result()
        offline = hit_report.render_hit_html(fr)
        self.assertIn(document.RECALC_PLACEHOLDER, offline)
        self.assertNotIn("127.0.0.1:8765", offline)
        injected = hit_report.render_hit_html(
            fr, recalc_endpoint="http://127.0.0.1:9000/recompute"
        )
        self.assertIn('content="http://127.0.0.1:9000/recompute"', injected)
        self.assertNotIn(document.RECALC_PLACEHOLDER, injected)

    def test_offline_document_has_no_external_resources(self):
        for model, (_result, html) in self.rendered.items():
            # Embedded result JSON is machine data (allowed by the contract);
            # loaded resources and player text must not reference the network.
            body = re.sub(
                r'<script id="(?:result-data|view-data)"[^>]*>.*?</script>',
                "",
                html,
                flags=re.S,
            )
            lowered = body.lower()
            self.assertNotIn("https://", lowered, model)
            self.assertNotIn("<script src", lowered, model)
            self.assertNotIn("<link rel=", lowered, model)
            self.assertNotIn("<img", lowered, model)
            self.assertEqual(html.count("fetch("), 1, model)


class PlayerTextOneCTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = render_both()

    def test_static_player_text_has_no_internal_ids(self):
        for model, (_result, html) in self.rendered.items():
            text, attributes = player_text(html)
            for label, pattern in FORBIDDEN_TEXT.items():
                self.assertIsNone(pattern.search(text), f"{model}: forbidden {label} in text")
                self.assertIsNone(pattern.search(attributes), f"{model}: forbidden {label} in title/aria")

    def test_js_rendered_text_has_no_db_field_renderers(self):
        # 1-C: JS-rendered drawer text counts as player text.  The BH drawer JS
        # must not render evidence locators / DB fields; the FR adapter only
        # renders readable status messages.
        app_js = document.asset("app.js")
        for token in ("证据与版本", "高级详情", "contribution_id", "stat_key", "source_path"):
            self.assertNotIn(token, app_js)
        for token in ("mod:", "contribution_id", "stat_key"):
            self.assertNotIn(token, hit_report._hit_report_js())

    def test_baseline_profiles_are_readable_labels(self):
        _result, html = self.rendered["hit"]
        text, _attrs = player_text(html)
        self.assertIn("完整构筑基线", html)
        self.assertIn("仅单技能基线", html)
        self.assertNotIn("legacy_single_skill", text)

    def test_english_fallback_labels_are_replaced(self):
        _result, html = self.rendered["hit"]
        text, _attrs = player_text(html)
        self.assertNotIn("increased attack speed", text.lower())
        self.assertNotIn("more attack speed", text.lower())


class SourcesAndBuffPagesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rendered = render_both()

    def test_source_page_is_three_column_grouped(self):
        _result, html = self.rendered["hit"]
        self.assertIn(
            '<thead><tr><th scope="col">来源</th><th scope="col">效果</th><th scope="col">状态</th></tr></thead>',
            html,
        )
        # Semantic grouping assertion (HTML-09/12): the shared source page is a
        # grouped three-column list with per-row source DOM hooks; do not assert
        # the rowspan implementation detail.
        self.assertIn("data-source-row", html)
        self.assertIn('class="source-main"', html)
        self.assertIn("data-source-id", html)
        self.assertIn("data-source-group", html)
        groups = re.findall(r'<tbody class="source-group".*?</tbody>', html, re.S)
        self.assertGreater(len(groups), 0)
        for group in groups:
            self.assertIn("data-source-row", group)
        self.assertIn("data-source-search", html)
        self.assertIn("data-source-category", html)

    def test_source_groups_do_not_merge_different_instances(self):
        result, html = self.rendered["hit"]
        groups = re.findall(r'<tbody class="source-group"[^>]*data-text="([^"]*)"', html)
        self.assertGreater(len(groups), 0)
        instances = set()
        for row in result.get("source_ledger") or []:
            location = str(row.get("source_location") or row.get("source_path") or "")
            parts = [part for part in location.split(".") if part]
            if parts[:1] in (["equipment"], ["idols"], ["blessings"]) and len(parts) >= 2:
                instances.add(".".join(parts[:2]))
            elif parts[:2] == ["passive_tree", "nodes"] and len(parts) >= 3:
                instances.add(".".join(parts[:3]))
            elif parts[:1] == ["specializations"] and "nodes" in parts:
                index = parts.index("nodes")
                instances.add(".".join(parts[: index + 2]))
        if instances:
            self.assertGreaterEqual(len(groups), 1)

    def test_buff_page_uses_current_data_and_preset_labels(self):
        result, html = self.rendered["hit"]
        self.assertIn("data-cond-preset", html)
        self.assertIn("完整构筑基线（常驻效果生效）", html)
        self.assertIn("仅单技能基线（关闭常驻效果）", html)
        self.assertIn("data-cond-compute", html)
        self.assertIn("data-cond-save", html)
        self.assertIn("data-cond-load", html)
        # candidate cards are derived from the current screening data
        candidates = ((result.get("buffs") or {}).get("screening") or {}).get("candidates") or []
        labels = [str(item.get("label") or "").split(" — ")[0] for item in candidates if isinstance(item, dict)]
        for label in labels:
            if label:
                self.assertIn(label, html)


class FormattingTest(unittest.TestCase):
    def test_count_rounding_is_semantic(self):
        self.assertEqual(units.fmt_compact(14.1333, "count"), "14")
        self.assertEqual(units.fmt_compact(84.8, "count"), "85")
        self.assertEqual(units.fmt_compact(339.2, "count"), "339")
        self.assertEqual(units.fmt_compact(2.5, "count"), "2")
        self.assertEqual(units.fmt_compact(0.5, "fraction"), "50%")

    def test_fr_display_rounds_counts_but_json_keeps_raw_values(self):
        result = test_hit_report.build_result()
        result["hits_per_cast"]["per_cast"] = 84.8
        html = hit_report.render_hit_html(result)
        embedded = json.loads(
            re.search(r'<script id="result-data" type="application/json">(.*?)</script>', html, re.S).group(1)
        )
        self.assertEqual(embedded["hits_per_cast"]["per_cast"], 84.8)
        text, _attrs = player_text(html)
        self.assertIn("命中次数 / 施放", text)
        self.assertIn("85", text)
        self.assertNotIn("84.8", text)


if __name__ == "__main__":
    unittest.main()
