"""Tests for the offline single-file HTML report (le_calc.html → report_ui)."""

from __future__ import annotations

import copy
import json
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from le_calc import html as report_html
from le_calc.report_ui import present, units
from le_calc.report_ui.pages import composition as page_composition
from le_calc.report_ui.pages import conditions as page_conditions
from le_calc.report_ui.pages import ignite as page_ignite
from le_calc.report_ui.pages import overview as page_overview
from le_calc.report_ui.pages import timeline as page_timeline
from le_calc.report_ui.pages import validation as page_validation

REPO_ROOT = Path(__file__).resolve().parents[3]
RESULT_PATH = REPO_ROOT / "reports" / "black-hole" / "save-build-bh-affix-db" / "result.json"

_DATA_RE = re.compile(
    r'<script id="(?P<name>result-data|view-data)" type="application/json">(?P<body>.*?)</script>',
    re.DOTALL,
)
_SCRIPT_BLOCK_RE = re.compile(r"<script\b.*?</script>", re.DOTALL)
_STYLE_BLOCK_RE = re.compile(r"<style\b.*?</style>", re.DOTALL)
_DETAILS_RE = re.compile(r"<details\b.*?</details>", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_SOURCE_ROW_RE = re.compile(r'<tr class="val-source-row".*?</tr>', re.DOTALL)
_CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.DOTALL)

_CONDITIONAL_STATUS_IDS = {
    "not_available",
    "unsupported_action",
    "auto_active",
    "auto_inactive",
    "auto_unknown",
    "already_in_baseline",
    "not_selected",
    "selected_condition_unmet",
    "selected_ready_needs_rules",
    "selected_ready_needs_time",
    "selected_unconfirmed",
    "active_supported",
}


def load_result() -> dict:
    return json.loads(RESULT_PATH.read_text(encoding="utf-8"))


def data_block(document: str, name: str = "result-data") -> str:
    for match in _DATA_RE.finditer(document):
        if match.group("name") == name:
            return match.group("body")
    raise AssertionError(f"embedded {name} block not found")


def embedded(document: str, name: str = "result-data") -> dict:
    return json.loads(data_block(document, name))


def strip_data_blocks(document: str) -> str:
    return _DATA_RE.sub("", document)


def visible_text(document: str) -> str:
    text = _SCRIPT_BLOCK_RE.sub(" ", document)
    text = _STYLE_BLOCK_RE.sub(" ", text)
    text = _TAG_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text)


class _DetailsStripper(HTMLParser):
    """Nesting-aware main-reading extractor (``<details>`` content excluded)."""

    _BLOCK_TAGS = {"p", "div", "li", "tr", "td", "th", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "table", "summary"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.depth += 1
        elif self.depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "details" and self.depth:
            self.depth -= 1
        elif self.depth == 0 and tag in self._BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data):
        if self.depth == 0 and data.strip():
            self.parts.append(data)


def text_without_details(document: str) -> str:
    stripped = _SCRIPT_BLOCK_RE.sub(" ", document)
    stripped = _STYLE_BLOCK_RE.sub(" ", stripped)
    stripper = _DetailsStripper()
    stripper.feed(stripped)
    return re.sub(r"\s+", " ", " ".join(stripper.parts)).strip()


def page_section(document: str, page_id: str) -> str:
    """Return the rendered section for one page id (markup only)."""

    marker = f'id="page-{page_id}"'
    start = document.find(marker)
    if start < 0:
        raise AssertionError(f"page-{page_id} not found")
    start = document.rfind("<section", 0, start)
    end = document.find("</section>", start)
    return document[start:end] if end >= 0 else document[start:]


def page_main_text(document: str, page_id: str) -> str:
    return text_without_details(page_section(document, page_id))


def display_variants(value, kind: str) -> set[str]:
    """Accept compact, full or explicit 3-decimal renderings of one number."""

    variants = {units.fmt_compact(value, kind), units.fmt_full(value, kind)}
    try:
        number = float(value)
    except (TypeError, ValueError):
        return {item for item in variants if item}
    if kind == "seconds":
        variants.add(("%.3f" % number).rstrip("0").rstrip("."))
    if kind == "stacks":
        variants.add("%.3f" % number)
    if kind == "damage":
        variants.add(units.fmt_plain(number, 2))
    return {item for item in variants if item}


def assert_any_rendered(test: unittest.TestCase, visible: str, value, kind: str) -> None:
    variants = display_variants(value, kind)
    if not any(variant in visible for variant in variants):
        test.fail(f"none of {sorted(variants)} rendered for value {value!r} ({kind})")


class _StructureProbe(HTMLParser):
    """Collects element/attribute facts used by the offline-safety tests."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags = []
        self.script_sources = []
        self.on_attributes = []
        self.link_tags = 0
        self.img_tags = 0
        self.iframe_tags = 0
        self.style_tags = 0

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        names = [name.lower() for name, _value in attrs]
        attributes = dict(attrs)
        if tag == "script" and "src" in names:
            self.script_sources.append(attributes.get("src"))
        if tag == "link":
            self.link_tags += 1
        if tag == "img":
            self.img_tags += 1
        if tag == "iframe":
            self.iframe_tags += 1
        if tag == "style":
            self.style_tags += 1
        for name, _value in attrs:
            if name.lower().startswith("on"):
                self.on_attributes.append((tag, name))


@unittest.skipUnless(RESULT_PATH.is_file(), f"result.json not present: {RESULT_PATH}")
class ReportRenderTests(unittest.TestCase):
    """Renders the real save-build result once and checks the redesign contract."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = load_result()
        cls.document = report_html.render(copy.deepcopy(cls.result))
        cls.view = present.build_view(copy.deepcopy(cls.result))
        cls.structure = strip_data_blocks(cls.document)

    # -- roundtrip / determinism ------------------------------------------

    def test_embedded_result_roundtrip(self) -> None:
        self.assertEqual(embedded(self.document), self.result)

    def test_embedded_view_has_pages(self) -> None:
        view = embedded(self.document, "view-data")
        self.assertEqual(view["schema"], "black-hole-report-view-v1")
        self.assertEqual([page["id"] for page in view["pages"]], present.PAGE_IDS)
        self.assertIn("sources", view)
        self.assertIn("timeline", view)

    def test_render_is_deterministic(self) -> None:
        first = report_html.render(copy.deepcopy(self.result))
        second = report_html.render(copy.deepcopy(self.result))
        self.assertEqual(first, second)
        self.assertEqual(first.encode("utf-8"), second.encode("utf-8"))

    # -- offline safety ---------------------------------------------------

    def test_no_external_references(self) -> None:
        forbidden = [
            (r"<script\b[^>]*\ssrc\s*=", "<script src=>"),
            (r"<link\b", "<link>"),
            (r"@import", "@import"),
            (r"url\(\s*['\"]?(?:https?:)?//", "CSS url() to network"),
            (r"\bfetch\s*\(\s*['\"]\s*(?:https?:)?//", "fetch() to an absolute URL"),
            (r"\bnew\s+WebSocket", "WebSocket"),
            (r"\bimport\s*\(", "dynamic import()"),
            (r"\b(?:src|href)\s*=\s*['\"]?(?:https?:)?//", "network attribute"),
        ]
        for pattern, label in forbidden:
            with self.subTest(pattern=label):
                self.assertIsNone(
                    re.search(pattern, self.structure, re.IGNORECASE),
                    f"forbidden external reference found: {label}",
                )

    def test_structure_is_offline_single_file(self) -> None:
        probe = _StructureProbe()
        probe.feed(self.document)
        self.assertEqual(probe.link_tags, 0)
        self.assertEqual(probe.img_tags, 0)
        self.assertEqual(probe.iframe_tags, 0)
        self.assertEqual(probe.script_sources, [])
        self.assertEqual(probe.on_attributes, [])
        self.assertEqual(probe.style_tags, 1)
        # result/view/capabilities JSON + shell bootstrap + shared recalc client + app.js
        self.assertEqual(sum(1 for tag in probe.tags if tag == "script"), 7)

    def test_embedded_data_cannot_break_script(self) -> None:
        for name in ("result-data", "view-data"):
            payload = data_block(self.document, name)
            self.assertNotIn("</script", payload.lower())
            self.assertNotIn("<!--", payload)
            self.assertNotIn("<", payload)
            self.assertNotIn(">", payload)
            json.loads(payload)

    def test_hostile_fields_are_escaped(self) -> None:
        hostile = "</script><script>alert(1)</script><img src=x onerror=alert(1)>"
        result = copy.deepcopy(self.result)
        result["run"]["status_reason"] = hostile
        result["scenario"]["name"] = hostile
        result["scenario"]["assumptions"] = [hostile]
        result["scope"]["included"] = [hostile]
        # fields that the player pages actually render
        conditional_effects = (
            ((result.get("conditional") or {}).get("view") or {}).get("effects") or []
        )
        if conditional_effects:
            conditional_effects[0]["name"] = hostile
            if conditional_effects[0].get("value_lines"):
                conditional_effects[0]["value_lines"][0]["label"] = hostile
        for row in result["source_ledger"]:
            if str(row.get("contribution_id", "")).startswith("mod:equipment.weapon1#affix"):
                row["source_label"] = hostile
                break
        document = report_html.render(result)
        visible = visible_text(document)
        self.assertNotIn("</script><script>alert(1)</script>", document)
        self.assertNotIn("<img src=x onerror", document)
        self.assertIn("&lt;/script&gt;", visible)
        self.assertEqual(embedded(document), result)

    def test_print_and_responsive_css(self) -> None:
        self.assertIn("@media print", self.document)
        self.assertIn("@media (max-width: 1080px)", self.document)
        self.assertIn("page-break-inside: avoid", self.document)
        self.assertIn("print-hide", self.document)

    # -- report identity / scenario --------------------------------------

    def test_all_pages_present(self) -> None:
        for page_id in present.PAGE_IDS:
            with self.subTest(page=page_id):
                self.assertIn(f'id="page-{page_id}"', self.document)
                self.assertIn(f'data-page-btn="{page_id}"', self.document)
                self.assertIn(present.PAGE_LABELS[page_id], self.document)

    def test_status_and_model_value_are_explicit(self) -> None:
        # player version: the report subtitle says 预计伤害; technical status
        # badges/banners are not part of the main reading any more
        self.assertIn('data-status="assumption"', self.document)
        visible = visible_text(self.document)
        self.assertIn("预计伤害", visible)
        self.assertNotIn("假设结果", visible)
        self.assertNotIn("模型预期值", visible)
        self.assertIn("预计伤害", self.document.split("class=\"app-header\"", 1)[1].split("</header>", 1)[0])

    def test_target_settings_controls_and_sensitivity(self) -> None:
        visible = visible_text(self.document)
        header = self.document.split('class="app-header"', 1)[1].split("</header>", 1)[0]
        # header only carries the compact target summary and the 目标设置 entry
        self.assertIn('data-open-drawer="scenario"', header)
        self.assertIn("目标设置", header)
        self.assertIn('data-sc="stat:level"', header)
        self.assertIn("data-export-json", header)
        # the header must not expose a report-wide monster level switch
        self.assertNotIn("<select", header)
        self.assertNotIn("data-scenario-level", header)
        # the editable target panel lives in the 目标设置 drawer
        self.assertIn('id="drawer-scenario"', self.document)
        self.assertIn("data-scenario-level", self.document)
        self.assertIn("data-scenario-fire-res", self.document)
        self.assertIn("data-apply-scenario", self.document)
        self.assertIn("data-reset-scenario", self.document)
        self.assertIn("data-reset-target", self.document)
        self.assertIn("目标条件", self.document)
        for state_id in (
            "target.boss_or_rare",
            "target.within_center_1m",
            "target.both_stars_cover",
            "target.chilled",
            "target.frozen",
        ):
            with self.subTest(state=state_id):
                self.assertIn(f'data-cond-state="{state_id}"', self.document)
        self.assertIn("等级影响", self.document)
        self.assertGreaterEqual(self.document.count("data-sens-row="), 100)
        self.assertIn('data-sens-cell="single"', self.document)
        self.assertNotIn("场景与敏感性", visible)
        self.assertNotIn("恢复基线", visible)
        self.assertIn("目标设置", visible)

    # -- dynamic target scenario ------------------------------------------

    def test_scenario_contract_exposed(self) -> None:
        scenario = self.view["scenario"]
        self.assertEqual(scenario["baseline"]["level"], 100)
        self.assertAlmostEqual(scenario["baseline"]["fire_resistance"], 0.0)
        self.assertAlmostEqual(scenario["penetration"]["body_fire"], 0.59)
        self.assertAlmostEqual(scenario["penetration"]["ignite"], 0.94)
        self.assertAlmostEqual(scenario["multipliers"]["body_fire"], 1.59, places=9)
        self.assertAlmostEqual(scenario["multipliers"]["ignite"], 1.94, places=9)
        components = scenario["components"]
        self.assertAlmostEqual(
            components["total"], self.view["context"]["single"]["total"], places=6
        )
        self.assertIn("data-scenario-level", self.document)
        self.assertIn("data-scenario-fire-res", self.document)
        self.assertIn("data-apply-scenario", self.document)
        self.assertGreaterEqual(self.document.count("data-sc="), 150)
        self.assertGreaterEqual(self.document.count("data-sc-bin"), 30)

    def test_scenario_transform_matches_engine_level_table(self) -> None:
        rows = {row["level"]: row for row in self.view["sensitivity"]["rows"]}
        scenario = self.view["scenario"]
        base = scenario["baseline"]
        components = scenario["components"]
        penetration = scenario["penetration"]
        multipliers = scenario["multipliers"]
        single_total = self.view["context"]["single"]["total"]

        # level-only change: uniform DR ratio must reproduce the engine table
        for level in (1, 25, 50, 75, 100):
            row = rows[level]
            dr_ratio = row["factor"] / base["dr_factor"]
            self.assertAlmostEqual(single_total * dr_ratio, row["single"], places=3)
            self.assertAlmostEqual(row["single"], row["single"], places=3)

        # combined level + target fire resistance: component-wise target-side transform
        level = 50
        fire_res = 0.5
        row = rows[level]
        dr_ratio = row["factor"] / base["dr_factor"]
        fire_mult = 1 - (fire_res - penetration["body_fire"])
        ignite_mult = 1 - (fire_res - penetration["ignite"])
        fire_ratio = dr_ratio * fire_mult / multipliers["body_fire"]
        ignite_ratio = dr_ratio * ignite_mult / multipliers["ignite"]
        cold_ratio = dr_ratio * 1.0 / multipliers["body_cold"]
        expected_body = (
            components["body_fire"] * fire_ratio + components["body_cold"] * cold_ratio
        )
        expected_total = expected_body + components["ignite"] * ignite_ratio
        self.assertAlmostEqual(expected_body / components["body"], 2.706258416432313, places=9)
        self.assertAlmostEqual(expected_total / single_total, 2.6350484978024244, places=9)

    # -- KPI / content ----------------------------------------------------

    def test_key_numbers_render(self) -> None:
        context = self.view["context"]
        single = context["single"]
        expected = [
            units.fmt_compact(single["total"], "damage"),
            units.fmt_compact(single["body_total"], "damage"),
            units.fmt_compact(single["ignite_total"], "damage"),
            units.fmt_compact(single["ignite_tail"], "damage"),
        ]
        for text in expected:
            with self.subTest(value=text):
                self.assertIn(text, self.document)
        for value, kind in (
            (single["total"], "damage"),
            (single["body_total"], "damage"),
            (single["ignite_total"], "damage"),
            (single["ignite_tail"], "damage"),
        ):
            with self.subTest(value=value, kind=kind):
                assert_any_rendered(self, visible_text(self.document), value, kind)

    def test_composition_formula_and_source_split(self) -> None:
        composition = self.view["composition"]
        fire = composition["components"][0]
        visible = text_without_details(self.document)
        # formula chain steps exist for both body branches and the ignite branch
        self.assertGreaterEqual(self.document.count("data-step-target"), 10)
        self.assertIn(units.fmt_compact(fire["inc"]["factor"], "factor"), visible)
        self.assertIn(units.fmt_compact(fire["more"]["product"], "factor"), visible)
        self.assertIn(units.fmt_compact(fire["pen"]["resistance_multiplier"], "factor"), visible)
        self.assertIn(units.fmt_compact(fire["pen"]["dr_factor"], "factor"), visible)
        # penetration sources add up to the reported totals
        for text in ("40%", "2%", "15%", "35%", "59%", "94%"):
            with self.subTest(text=text):
                self.assertIn(text, visible)
        self.assertIn("Spine of Malatros", visible)
        self.assertIn("Lavamancer", visible)
        # source chips open the sidebar
        self.assertIn('data-source-id="mod:equipment.weapon1#affix#normal#0.0"', self.document)

    def test_ignite_four_steps(self) -> None:
        visible = text_without_details(self.document)
        ignite = self.view["ignite"]
        application = ignite["application"]
        for value, kind in (
            (application["uncapped_fire_resistance"], "pp"),
            (application["per_second"], "stacks_rate"),
            (application["per_roll"], "stacks"),
            (application["total_stacks"], "stacks"),
            (ignite["per_stack"]["per_stack_dps"], "damage"),
            (ignite["duration"]["duration_s"], "seconds"),
            (ignite["duration"]["per_stack_total"], "damage"),
        ):
            with self.subTest(value=value, kind=kind):
                assert_any_rendered(self, visible, value, kind)
        for step in ("第一步", "第二步", "第三步", "第四步"):
            self.assertIn(step, visible)
        self.assertIn("尾部", visible)
        self.assertIn("data-goto-time", self.document)
        self.assertGreaterEqual(self.document.count("data-goto-time"), 15)

    def test_timeline_tracks_and_readout(self) -> None:
        self.assertIn('id="chart-stack"', self.document)
        for track in ("cumulative", "dps", "stacks", "state"):
            self.assertIn(f'data-track="{track}"', self.document)
        self.assertIn('id="timeline-readout"', self.document)
        for key in ("t", "body", "ignite", "total", "dps", "stacks", "bh"):
            self.assertIn(f'data-readout="{key}"', self.document)
        self.assertIn("t = 0 为开始施法", self.document)
        self.assertIn("每 0.5 秒平均 DPS", visible_text(self.document))
        self.assertNotIn("0.5 秒分箱", visible_text(self.document))
        self.assertNotIn("记账切片", visible_text(self.document))

    def test_timeline_milestones_use_player_labels(self) -> None:
        """§12: important moments use short player wording, no audit phrasing."""

        milestones = self.view["timeline"]["single"]["milestones"]
        labels = [item["label"] for item in milestones]
        for expected in ("施法开始", "黑洞生成", "黑洞结束", "点燃结束"):
            with self.subTest(label=expected):
                self.assertIn(expected, labels)
        for removed in ("开始施法", "黑洞消失", "最后一批点燃到期", "首次点燃判定", "最后一次点燃判定"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, labels)
        key_event_labels = {
            item["label"] for item in self.view["timeline"]["single"]["key_events"]
        }
        for expected in ("施法开始", "黑洞生成", "黑洞结束", "点燃结束"):
            with self.subTest(key_event=expected):
                self.assertIn(expected, key_event_labels)
        self.assertNotIn("观察结束", key_event_labels)

    def test_conditions_page_renders_configuration(self) -> None:
        view = embedded(self.document, "view-data")
        conditions = view["conditions"]
        self.assertTrue(conditions["available"], conditions.get("unavailable_reason"))
        self.assertEqual(conditions["schema"], "bh-conditional-view-v1")
        self.assertGreaterEqual(len(conditions["effects"]), 10)
        # the page and its configuration anchors are in the single document
        self.assertIn('id="page-conditions"', self.document)
        self.assertIn('data-page-btn="conditions"', self.document)
        # the page fragment carries the configuration anchors (server-rendered)
        fragment = page_conditions.render_conditions(copy.deepcopy(self.view))
        self.assertTrue(fragment.lstrip().startswith("<section"))
        self.assertNotIn("<script", fragment)
        self.assertNotIn("<style", fragment)
        self.assertNotIn(" onclick", fragment)
        self.assertNotRegex(fragment, r'(?:src|href)\s*=\s*["\']?(?:https?:)?//')
        for anchor in (
            "data-cond-row",
            "data-cond-toggle",
            "data-cond-stacks",
            "data-cond-cast",
            "data-cond-preset",
            "data-cond-export",
            "data-cond-import",
            "data-cond-status-for",
            "data-cond-check-for",
            "data-cond-count",
            "data-cond-search",
            "data-cond-category",
            "data-cond-status",
            "data-cond-proposal-for",
            "data-cond-live-recompute",
            "data-cond-live-status",
            "data-cond-reset",
        ):
            with self.subTest(anchor=anchor):
                self.assertIn(anchor, fragment)
        visible = visible_text(self.document)
        self.assertIn("Buff 设置", visible)
        # player setup page: technical banners/stat cards are not rendered
        self.assertNotIn("本页不重算 DPS", visible)
        self.assertNotIn("固定条件估算", visible)
        self.assertNotIn("已检查对象", visible)
        for action in ("保存配置", "载入配置", "计算 DPS"):
            with self.subTest(action=action):
                self.assertIn(action, visible)

    def test_conditions_page_falls_back_for_legacy_result(self) -> None:
        legacy = copy.deepcopy(self.result)
        legacy.pop("conditional", None)
        document = report_html.render(legacy)
        visible = visible_text(document)
        self.assertIn('id="page-conditions"', document)
        self.assertIn("未包含条件 Buff", visible)
        # controls must not be rendered for old results (ignore the always-inlined JS text)
        markup = _SCRIPT_BLOCK_RE.sub(" ", _STYLE_BLOCK_RE.sub(" ", strip_data_blocks(document)))
        self.assertNotIn("data-cond-toggle", markup)
        self.assertNotIn("data-cond-row", markup)
        self.assertNotRegex(markup, r'(?:src|href)\s*=\s*["\']?(?:https?:)?//')

    # -- player-facing 增伤来源 page -------------------------------------

    def _player_rows(self, document: str | None = None) -> list[dict]:
        document = document or self.document
        section = page_section(document, "validation")
        rows = []
        for match in _SOURCE_ROW_RE.finditer(section):
            row_html = match.group(0)
            opening = row_html.split(">", 1)[0] + ">"
            cells = [
                re.sub(r"\s+", " ", _TAG_RE.sub(" ", cell)).strip()
                for cell in _CELL_RE.findall(row_html)
            ]
            attributes = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', opening))
            rows.append({"html": row_html, "cells": cells, "attrs": attributes})
        return rows

    def _player_row(self, title: str, document: str | None = None) -> dict:
        for row in self._player_rows(document):
            if row["cells"] and row["cells"][0].startswith(title):
                return row
        raise AssertionError(f"player source row not found: {title}")

    def test_validation_page_is_player_source_list(self) -> None:
        """§9.5: 增伤来源 is a searchable source list, not an audit report."""

        self.assertIn('id="page-validation"', self.document)
        self.assertIn('data-page-btn="validation"', self.document)
        section = page_section(self.document, "validation")
        self.assertIn("增伤来源", section)
        self.assertIn('id="val-sources"', section)
        self.assertIn("data-source-search", section)
        self.assertIn("data-source-category", section)
        self.assertIn("data-source-included", section)
        self.assertIn('data-expand-all="source"', section)
        rows = self._player_rows()
        self.assertGreaterEqual(len(rows), 50)
        for removed_block in (
            "规则依据",
            "数据检查",
            "报告信息",
            "原始构筑来源覆盖",
            "已检查但未纳入",
            "当前结果的范围与限制",
            "问题台账",
            "一致性检查",
        ):
            with self.subTest(removed_block=removed_block):
                self.assertNotIn(removed_block, section)
        # engineering vocabulary does not appear in the player reading copy
        main_text = page_main_text(self.document, "validation")
        for token in (
            "依据",
            "证据",
            "含假设",
            "已计入",
            "未计入",
            "schema",
            "stat_key",
            "contribution_id",
            "node:bh2",
            "enchant_weapon.",
            "revision",
            "sha256",
            "locator",
            "高级详情",
            "来源与效果 ID",
        ):
            with self.subTest(token=token):
                self.assertNotIn(token, main_text)

    def test_readable_conditional_source_names(self) -> None:
        """§7.1: in-game names, never truncated internal ids, as row titles."""

        eternal = self._player_row("Eternal Flames")
        self.assertIn("Enchant Weapon", eternal["cells"][0])
        # 常驻 and 激活额外 stay distinguishable, each value rendered once
        effect_cell = eternal["cells"][1]
        self.assertIn("常驻", effect_cell)
        self.assertIn("点燃持续时间", effect_cell)
        self.assertIn("激活额外", effect_cell)
        self.assertNotIn("+25% +25%", effect_cell)
        self.assertEqual(effect_cell.count("+25%"), 2)
        self.assertNotIn("enchant_weapon", eternal["html"])
        # a single-effect source has its title exactly once (no double title)
        craterborn = self._player_row("Craterborn")
        self.assertEqual(craterborn["cells"][0].count("Craterborn"), 1)
        self.assertIn("Meteor", craterborn["cells"][0])
        self.assertIn("未启用", craterborn["cells"][2])
        # multi-attribute source rows keep one row per source identity
        idol_row = self._player_row("神像 3 · Grand Arcane Omen Idol")
        self.assertIn("点燃持续时间", idol_row["cells"][1])
        self.assertIn("已生效", idol_row["cells"][2])
        # unavailable effects use the short player status, not support jargon
        frozen = self._player_row("Frozen Steel")
        self.assertIn("暂不可用", frozen["cells"][2])
        self.assertIn("需要 4 点", frozen["cells"][2])

    def test_player_source_values_are_single_and_compact(self) -> None:
        """§7.2/§7.4: one value per attribute, compact main row, meaningful details."""

        document = self.document
        self.assertNotIn("+25% +25%", document)
        self.assertNotIn("+75% +75%", document)
        self.assertNotIn("+45.6% +45.6%", document)
        # the tooltip/expand container for old evidence/ID blocks is gone
        self.assertNotIn("属性贡献（计入", document)
        # engineering multiplier wording is not part of player labels
        rows = self._player_rows()
        player_text = " ".join(" ".join(row["cells"]) for row in rows)
        for noise in ("（加算 inc）", "加算 inc", "inc 池", "独立增伤 more", "increased ", " more ", "未建模"):
            with self.subTest(noise=noise):
                self.assertNotIn(noise, player_text)
        self.assertIn("点燃持续时间", player_text)
        self.assertNotIn("异常状态持续时间", player_text)
        with_details = [row for row in rows if "<summary>属性明细</summary>" in row["html"]]
        self.assertTrue(with_details, "multi-attribute rows must expose 属性明细")
        # a single-attribute source does not get a nearly duplicate expansion
        single_attribute_rows = [
            row
            for row in rows
            if "<details" not in row["html"] and row["cells"]
        ]
        self.assertTrue(single_attribute_rows)
        # details only exist when they add information beyond the main row
        for row in rows:
            if "<details" in row["html"]:
                with self.subTest(title=row["cells"][0][:40]):
                    self.assertIn("属性明细", row["html"])
                    self.assertNotIn("来源与效果 ID", row["html"])


    def test_source_browser_merges_conditional_sources(self) -> None:
        """§7/§9.5: related conditional Buffs appear once, with a player status."""

        browser = self.view["source_browser"]
        primary = browser["primary"]
        self.assertGreaterEqual(len(primary), 50)
        # Craterborn exists once with the player status 未启用
        craterborn_rows = [row for row in primary if row["player"]["title"] == "Craterborn"]
        self.assertEqual(len(craterborn_rows), 1)
        self.assertEqual(craterborn_rows[0]["player"]["status_label"], "未启用")
        # a baseline effect is joined onto its ledger row instead of duplicated
        supermassive = next(row for row in primary if row["player"]["title"] == "Supermassive")
        self.assertTrue(supermassive["effects"])
        supermassive_effect_rows = [
            row
            for row in primary
            for effect in row["effects"]
            if effect.get("effect_id") == "main_skill.supermassive"
        ]
        self.assertEqual(len(supermassive_effect_rows), 1)
        # no ledger contribution id is shown twice across browser rows
        seen_ids: list[str] = []
        for row in primary:
            for source in row["sources"]:
                contribution_id = source["id"]
                self.assertNotIn(contribution_id, seen_ids)
                seen_ids.append(contribution_id)
        # every rendered source row comes from a contributing/related source
        rendered = self._player_rows()
        self.assertTrue(rendered)
        for row in rendered:
            self.assertTrue(row["attrs"].get("search"))
            self.assertIn(row["attrs"].get("included"), ("0", "1"))

    def test_conditional_application_states_are_used(self) -> None:
        """State labels prefer recompute.application_states (task-6 shape)."""

        result = copy.deepcopy(self.result)
        conditional = result.setdefault("conditional", {})
        conditional["scenario_applied"] = True
        recompute = conditional.setdefault("recompute", {})
        effects = [
            effect["effect_id"]
            for effect in (conditional.get("view") or {}).get("effects") or []
            if effect.get("effect_id")
        ]
        self.assertGreaterEqual(len(effects), 10)
        states = {}
        for index, effect_id in enumerate(sorted(effects)):
            if index == 0:
                states[effect_id] = {
                    "effect_id": effect_id,
                    "state": "applied_both",
                    "single": True,
                    "loop": True,
                    "reason": "",
                    "reason_detail": "",
                    "support_level": "needs_rule_mapping",
                    "assumptions": [],
                    "assumption_note": "",
                    "modifier_ids": [],
                    "branches": ["ignite"],
                    "applied_rows": [],
                }
            else:
                states[effect_id] = {
                    "effect_id": effect_id,
                    "state": "skipped",
                    "single": False,
                    "loop": False,
                    "reason": "auto_excluded",
                    "reason_detail": "未选择",
                    "support_level": "needs_rule_mapping",
                    "assumptions": [],
                    "assumption_note": "",
                    "modifier_ids": [],
                    "branches": [],
                    "applied_rows": [],
                }
        recompute["application_states"] = states
        view = present.build_view(result)
        conditional_state = view["context"]["conditional"]
        self.assertTrue(conditional_state["scenario_applied"])
        applied_effect_ids = set(conditional_state["applied_effect_ids"])
        self.assertEqual(len(applied_effect_ids), 1)
        first_effect = sorted(effects)[0]
        self.assertIn(first_effect, applied_effect_ids)
        self.assertEqual(conditional_state["effects"][first_effect]["label"], "已计入")
        self.assertGreaterEqual(len(conditional_state["selected_not_applied"]), 0)

    def test_single_cast_only_state_is_not_claimed_for_loops(self) -> None:
        """§9.2/§9.4: a single-cast effect must not render as 已计入 for loops."""

        result = copy.deepcopy(self.result)
        conditional = result.setdefault("conditional", {})
        conditional["scenario_applied"] = True
        recompute = conditional.setdefault("recompute", {})
        effects = [
            effect["effect_id"]
            for effect in (conditional.get("view") or {}).get("effects") or []
            if effect.get("effect_id")
        ]
        target = "ring.crescendoing_rage"
        self.assertIn(target, effects)
        recompute["application_states"] = {
            target: {
                "effect_id": target,
                "state": "applied_single",
                "single": True,
                "loop": False,
                "reason": "",
                "reason_detail": "",
                "support_level": "needs_action_or_time_simulation",
                "assumptions": [],
                "assumption_note": "",
                "modifier_ids": [],
                "branches": ["single_cast"],
                "applied_rows": [],
            }
        }
        view = present.build_view(result)
        state = view["context"]["conditional"]["effects"][target]
        self.assertEqual(state["label"], "仅单次")
        self.assertTrue(state["single"])
        self.assertFalse(state["loop"])
        fragment = page_validation.render_validation(copy.deepcopy(view))
        # the player page uses the short vocabulary; single-cast is never
        # presented as fully applied to loops
        self.assertIn("仅本次施法", fragment)

    def test_scenario_condition_aliases_are_unified(self) -> None:
        """§9.5: one condition identity per meaning, aliases not OR-ed."""

        scenario = self.view["context"]["scenario"]
        ids = [condition["id"] for condition in scenario["conditions"]]
        self.assertEqual(ids.count("target_within_center_1m"), 1)
        self.assertEqual(ids.count("both_stars_cover"), 1)
        self.assertEqual(ids.count("target_is_boss_or_rare"), 1)
        labels = [condition["label"] for condition in scenario["conditions"]]
        self.assertIn("目标位于中心 1 米内", labels)
        self.assertNotIn("目标处于中心 1 米内", labels)
        self.assertNotIn("中心 1 米条件满足", labels)
        self.assertEqual(scenario["condition_summary"]["total"], 3)
        # legacy aliases are still read as compatibility aliases of one identity
        center = next(condition for condition in scenario["conditions"] if condition["id"] == "target_within_center_1m")
        self.assertIn("coverage.center_condition_met", center["aliases"])
        self.assertIn("target_conditions.target_within_center_1m", center["aliases"])

    def test_conflicting_condition_aliases_warn_instead_of_or(self) -> None:
        result = copy.deepcopy(self.result)
        result["scenario"]["coverage"]["center_condition_met"] = False
        result["scenario"]["target_conditions"]["target_within_center_1m"] = True
        view = present.build_view(result)
        center = next(
            condition
            for condition in view["context"]["scenario"]["conditions"]
            if condition["id"] == "target_within_center_1m"
        )
        self.assertTrue(center["conflict"])
        self.assertIsNone(center["met"])
        self.assertEqual(len(view["context"]["scenario"]["condition_conflicts"]), 1)

    def test_review_copy_removed(self) -> None:
        """§9.6: the two requested paragraphs and the dynamic template are gone."""

        visible = visible_text(self.document)
        for removed in (
            "计数来自结果文件中的校验台账",
            "计算状态与依据状态分开统计",
            "找装备或节点",
            "数值单位来自各行的来源类型",
            "当前固定场景",
        ):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, self.document)
        self.assertNotIn("校验台账", visible)
        # regeneration must not bring the template back from the dynamic counts
        self.assertNotIn("警告及以上", visible)

    def test_target_drawer_reflects_current_result(self) -> None:
        """§6: 目标设置 shows the current result's target, no duplicated text."""

        drawer = self.document.split('id="drawer-scenario"', 1)[1]
        drawer = drawer.split("<script", 1)[0]
        self.assertIn("目标设置", drawer)
        self.assertIn("怪物等级", drawer)
        self.assertIn("火焰抗性", drawer)
        # inputs are initialised from the displayed result
        level_input = re.search(r'data-scenario-level[^>]*value="([^"]*)"', drawer)
        self.assertIsNotNone(level_input)
        self.assertEqual(level_input.group(1), "100")
        fire_input = re.search(r'data-scenario-fire-res[^>]*value="([^"]*)"', drawer)
        self.assertIsNotNone(fire_input)
        self.assertEqual(fire_input.group(1), "0")
        self.assertIn("目标条件", drawer)
        self.assertIn("恢复默认目标", drawer)
        self.assertIn("等级影响", drawer)
        # stale duplicated labels from the old drawer are gone
        for removed in ("当前计算条件", "编辑条件", "场景来源与假设", "DR 来源", "条件别名"):
            with self.subTest(removed=removed):
                self.assertNotIn(removed, drawer)

    def test_limitations_block_is_not_rendered_on_player_pages(self) -> None:
        """§8.1: the limitations module is removed from the player HTML."""

        limitations = self.view["limitations"]
        # the data stays available for engineering consumers/tests
        self.assertTrue(limitations["statistics_scope"])
        self.assertIn("Black Hole", limitations["statistics_scope"])
        base_gaps = " ".join(gap["text"] for gap in limitations["gaps"])
        self.assertIn("暂不支持", base_gaps)
        # but no player page renders the module (not even a folded shell)
        validation_fragment = page_validation.render_validation(copy.deepcopy(self.view))
        self.assertNotIn("当前结果的范围与限制", validation_fragment)
        self.assertNotIn("统计范围", validation_fragment)
        self.assertNotIn("当前估算方式", validation_fragment)
        self.assertNotIn("需要留意", validation_fragment)
        self.assertNotIn("排除 Meteor", validation_fragment)
        self.assertNotIn("不考虑其他技能树", self.document)
        self.assertNotIn("当前结果的范围与限制", self.document)

    def test_original_coverage_is_not_rendered(self) -> None:
        result = copy.deepcopy(self.result)
        conditional = result.setdefault("conditional", {})
        catalog = conditional.setdefault("catalog", {})
        catalog["original_source_coverage"] = {
            "counts": {"action_bar_skills": 5, "specialized": 4, "unspecialized": 1},
            "sources": [
                {"name": "Black Hole", "specialized": True, "check_status": "checked", "reason": "主技能"},
                {"name": "Teleport", "specialized": False, "check_status": "unknown", "reason": "未专精，未进入计算输入"},
            ],
            "unknowns": [{"label": "Teleport", "reason": "未专精：没有独立扫描入口"}],
            "notes": ["原始来源覆盖与内部检查记录分开统计。"],
        }
        view = present.build_view(result)
        # engineering data is still collected, but never rendered to players
        self.assertTrue(view["original_coverage"]["available"])
        self.assertEqual(len(view["original_coverage"]["entries"]), 2)
        fragment = page_validation.render_validation(copy.deepcopy(view))
        self.assertNotIn("原始构筑来源覆盖", fragment)
        self.assertNotIn("Teleport", fragment)

    def test_validation_survives_missing_conditional_blocks(self) -> None:
        """Fallback: old results without recompute/application states still render."""

        result = copy.deepcopy(self.result)
        conditional = result.get("conditional") or {}
        conditional.pop("recompute", None)
        result["conditional"] = conditional
        view = present.build_view(result)
        conditional_state = view["context"]["conditional"]
        self.assertFalse(conditional_state["scenario_applied"])
        fragment = page_validation.render_validation(copy.deepcopy(view))
        self.assertIn("增伤来源", fragment)
        self.assertIn("data-source-row", fragment)
        # base-report fallback states still come from channel_support itself
        craterborn = view["context"]["conditional"]["effects"]["meteor.craterborn"]
        self.assertEqual(craterborn["label"], "未启用")
        self.assertIn("Craterborn", fragment)

    def test_source_usage_is_explicit(self) -> None:
        weapon = self.view["sources"]["mod:equipment.weapon1#affix#normal#0.0"]
        scopes = {item["scope"] for item in weapon["applies"]}
        self.assertIn("body-fire", scopes)
        self.assertIn("ignite", scopes)
        shoes = self.view["sources"]["mod:equipment.feet#unique_mod#1"]
        self.assertNotIn("body-fire", {item["scope"] for item in shoes["applies"]})
        self.assertIn("ignite", {item["scope"] for item in shoes["applies"]})
        quest = self.view["sources"]["mod:quest.reward#all_attributes"]
        self.assertEqual(quest["category"], "quest")
        self.assertIn("任务", quest["display_name"])

    def test_no_stale_or_internal_text_in_main_reading(self) -> None:
        visible = text_without_details(self.document)
        for stale in ("每秒一次判定", "同时存续上限 1", "Int42"):
            with self.subTest(stale=stale):
                self.assertNotIn(stale, visible)
        for internal in ("node:bh2:", "expr:", "stat_key", "contribution_id"):
            with self.subTest(internal=internal):
                self.assertNotIn(internal, visible)

    def test_write_html_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "nested" / "report.html"
            report_html.write_html(copy.deepcopy(self.result), target)
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_text(encoding="utf-8"), self.document)
            self.assertIn('charset="utf-8"', self.document)

    def test_cli_writes_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "result.json"
            source.write_text(json.dumps(self.result, ensure_ascii=False), encoding="utf-8")
            out = Path(tmp) / "report.html"
            exit_code = report_html.main(["--result", str(source), "--out", str(out)])
            self.assertEqual(exit_code, 0)
            self.assertTrue(out.is_file())
            self.assertIn("<html", out.read_text(encoding="utf-8"))


@unittest.skipUnless(RESULT_PATH.is_file(), f"result.json not present: {RESULT_PATH}")
class ViewLayerTests(unittest.TestCase):
    """Checks the display-data layer independently of the HTML shell."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.result = load_result()
        cls.view = present.build_view(copy.deepcopy(cls.result))

    def test_view_is_json_serializable_and_deterministic(self) -> None:
        first = present.build_view(copy.deepcopy(self.result))
        second = present.build_view(copy.deepcopy(self.result))
        self.assertEqual(
            json.dumps(first, ensure_ascii=False, sort_keys=True),
            json.dumps(second, ensure_ascii=False, sort_keys=True),
        )
        json.dumps(first, ensure_ascii=False)

    def test_engine_totals_are_preserved(self) -> None:
        totals = self.result["single_cast"]["totals"]
        single = self.view["context"]["single"]
        self.assertEqual(single["total"], totals["single_cast_total"])
        self.assertEqual(single["body_total"], totals["body_total"])
        self.assertEqual(single["ignite_total"], totals["ignite_total"])
        self.assertEqual(single["ignite_tail"], totals["ignite_tail_damage"])

    def test_derived_timeline_closes_against_engine_totals(self) -> None:
        single = self.view["timeline"]["single"]
        self.assertTrue(single["closures"]["body"])
        self.assertTrue(single["closures"]["ignite"])
        self.assertTrue(single["closures"]["bin_body"])
        self.assertTrue(single["closures"]["bin_ignite"])
        self.assertEqual(len(single["bins"]), 31)
        self.assertEqual(single["precision"], "exact-derived")

    def test_ignite_cohort_split_closes(self) -> None:
        totals = self.view["ignite"]["totals"]
        self.assertTrue(totals["closed_damage"])
        self.assertTrue(totals["closed_split"])
        self.assertTrue(totals["closed_before"])
        self.assertTrue(totals["closed_tail"])
        self.assertEqual(len(totals["cohorts"]), 15)
        self.assertAlmostEqual(totals["before_sum"], totals["before_bh_end"], places=3)
        self.assertAlmostEqual(totals["tail_sum"], totals["tail"], places=3)

    def test_composition_inc_and_more_close(self) -> None:
        for component in self.view["composition"]["components"]:
            with self.subTest(component=component["key"]):
                self.assertTrue(component["inc"]["closed"])
                self.assertAlmostEqual(
                    component["inc"]["factor"],
                    1.0 + component["inc"]["total"],
                    places=9,
                )
                self.assertAlmostEqual(
                    component["more"]["product"],
                    __import__("math").prod(
                        entry["factor"] for entry in component["more"]["entries"]
                    ),
                    places=9,
                )

    def test_fire_resistance_pool_closes(self) -> None:
        pool = self.view["ignite"]["application"]["pool"]
        self.assertTrue(pool["closed"])
        self.assertAlmostEqual(pool["derived_total"], pool["total"], places=6)

    def test_source_usage_maps_real_consumption(self) -> None:
        sources = self.view["sources"]
        duration = sources["skill:bh2:4:duration"]
        self.assertIn("mechanism:bh-duration", {item["scope"] for item in duration["applies"]})
        added = sources["mod:equipment.weapon1#unique_mod#5"]
        scopes = {item["scope"] for item in added["applies"]}
        self.assertIn("body-fire", scopes)
        self.assertIn("body-cold", scopes)
        heat_wave = sources[self.view["ignite"]["application"]["heat_wave_source_id"]]
        self.assertIn("mechanism:heat-wave", {item["scope"] for item in heat_wave["applies"]})
        doubled = sources["mod:idols.2#affix#normal#1.0:doubled"]
        self.assertTrue(doubled["applies"], "doubled idol affix must inherit the base row usage")
        unused = [source for source in sources.values() if source["included"] and not source["applies"]]
        self.assertLess(len(unused), 40)
        self.assertEqual(self.view["ignite"]["application"]["heat_wave_name"], "Heat Wave")
        self.assertEqual(self.view["ignite"]["exclusions"][0]["name"], "Weakened Reality")

    def test_number_span_carries_scenario_binding(self) -> None:
        span = units.num(0.59, "factor", sc="stat:multFire")
        self.assertIn('data-sc="stat:multFire"', span)
        self.assertIn('data-kind="factor"', span)
        span = units.num(1234.5, "damage", sc="group:ignite")
        self.assertIn('data-sc="group:ignite"', span)
        self.assertIn('data-value="1234.5"', span)
        plain = units.num(223.2, "damage")
        self.assertNotIn("data-sc=", plain)

    def test_sensitivity_current_row_matches_result(self) -> None:
        sensitivity = self.view["sensitivity"]
        current = next(row for row in sensitivity["rows"] if row["level"] == sensitivity["current_level"])
        self.assertAlmostEqual(current["single"], self.result["single_cast"]["totals"]["single_cast_total"])

    def test_conditions_view_contract(self) -> None:
        conditions = self.view["conditions"]
        self.assertTrue(conditions.get("available"), conditions.get("unavailable_reason"))
        for key in (
            "schema", "header", "counts", "filters", "states", "presets", "effects",
            "groups", "auto_notes", "checked", "dependency", "export", "client_rules",
        ):
            self.assertIn(key, conditions)
        counts = conditions["counts"]
        breakdown = counts.get("denominator_breakdown") or {}
        self.assertTrue(breakdown, "checked_objects must expose its denominator breakdown")
        self.assertEqual(counts["checked_objects"], sum(int(value) for value in breakdown.values()))
        self.assertGreaterEqual(counts["candidates"], 10)
        self.assertGreaterEqual(counts["relevant"], 5)
        # main-skill dependency table is exposed for the page/doc
        modules = conditions["dependency"]["modules"]
        self.assertGreaterEqual(len(modules), 8)
        for module in modules:
            self.assertIn("id", module)
            self.assertIn("label", module)
        # effect cards are unique and carry the user-facing fields the contract requires
        effects = conditions["effects"]
        self.assertEqual(len(effects), 17)
        effect_ids = [effect["effect_id"] for effect in effects]
        self.assertEqual(len(effect_ids), len(set(effect_ids)))
        for effect in effects:
            for key in (
                "effect_id", "name", "source_label", "summary", "impact_path",
                "relevance", "support", "status", "condition_checks", "evidence",
            ):
                self.assertIn(key, effect, effect.get("effect_id"))
            self.assertIn(effect["status"]["initial"], _CONDITIONAL_STATUS_IDS)
            self.assertTrue(effect["evidence"], effect["effect_id"])

    def test_conditions_evaluation_is_documentation_only(self) -> None:
        conditional = self.result.get("conditional") or {}
        self.assertEqual(conditional.get("status"), "ready")
        evaluation = conditional["evaluation"]
        self.assertFalse(evaluation["dps_recomputed"])
        self.assertEqual(evaluation["validation"]["errors"], [])
        self.assertFalse(evaluation["changed_from_baseline"])
        self.assertGreaterEqual(len(evaluation["proposed_contributions"]), 1)
        # proposals are exported for a later re-calculation, never applied to the engine result
        snapshot_contributions = {
            row["contribution_id"] for row in self.result["source_ledger"]
        }
        for proposal in evaluation["proposed_contributions"]:
            self.assertIn("effect_id", proposal)
            self.assertFalse(
                proposal.get("proposal_id") in snapshot_contributions,
                "proposals must not be injected into the existing source ledger",
            )
        # engine totals are the untouched baseline values
        self.assertAlmostEqual(
            self.result["single_cast"]["totals"]["single_cast_total"],
            10662227.469716422,
            places=3,
        )

    def test_conditions_client_rules_align_with_view(self) -> None:
        conditions = self.view["conditions"]
        rules = conditions["client_rules"]
        self.assertEqual(rules["schema"], "bh-conditional-rules-v1")
        state_ids = {state["id"] for state in rules["states"]}
        known = set(state_ids)
        for auto in rules["auto_states"]:
            known.add(auto["id"])
            for comparison in auto.get("comparisons") or []:
                known.add(comparison["state"])
        rule_effects = {effect["effect_id"]: effect for effect in rules["effects"]}
        view_effects = {effect["effect_id"]: effect for effect in conditions["effects"]}
        self.assertEqual(set(rule_effects), set(view_effects))
        for effect_id, rule in rule_effects.items():
            effect = view_effects[effect_id]
            control = effect["control"]
            self.assertIn(control["kind"], ("toggle", "stacks", "cast_state", "none"))
            for key in ("state_id", "stack_state_id"):
                if control.get(key):
                    self.assertIn(control[key], known, (effect_id, key))
            for group in ("all", "any"):
                for atom in (rule.get("condition") or {}).get(group) or []:
                    self.assertIn(atom["state"], known, (effect_id, atom["state"]))
            self.assertIn(
                rule["support"],
                ("engine_supported", "needs_rule_mapping",
                 "needs_action_or_time_simulation", "mechanism_unresolved"),
            )
        labels = rules["status_labels"]
        for status in _CONDITIONAL_STATUS_IDS:
            self.assertIn(status, labels)
        self.assertEqual(conditions["filters"]["statuses"][0]["id"], "all")
        filter_ids = {item["id"] for item in conditions["filters"]["statuses"]}
        self.assertTrue(_CONDITIONAL_STATUS_IDS <= filter_ids)

    def test_conditions_data_has_no_absolute_paths(self) -> None:
        payload = json.dumps(self.result["conditional"], ensure_ascii=False, sort_keys=True)
        # third-party reference URLs (https://...) are legitimate evidence locators;
        # only local host paths / file URLs are forbidden in the page data
        payload = re.sub(r"https?://\S+", "", payload)
        for pattern in (r"[A-Za-z]:\\\\", r"[A-Za-z]:/", "file://"):
            with self.subTest(pattern=pattern):
                self.assertNotRegex(payload, pattern)

    def test_source_catalog_is_player_readable(self) -> None:
        sources = self.view["sources"]
        self.assertGreaterEqual(len(sources), 240)
        for source in sources.values():
            self.assertNotRegex(source["name"] or "", r"\[(?:equipment|idols|blessings)\.")
            self.assertTrue(source["display_name"])
            self.assertNotIn("node:bh2:", source["name"])

    def test_each_page_module_renders_a_section(self) -> None:
        for renderer, page_id in (
            (page_overview.render_overview, "overview"),
            (page_composition.render_composition, "composition"),
            (page_ignite.render_ignite, "ignite"),
            (page_timeline.render_timeline, "timeline"),
            (page_validation.render_validation, "validation"),
            (page_conditions.render_conditions, "conditions"),
        ):
            with self.subTest(page=page_id):
                fragment = renderer(copy.deepcopy(self.view))
                self.assertIn(f'data-page-panel="{page_id}"', fragment)
                self.assertTrue(fragment.lstrip().startswith("<section"))
                self.assertEqual(fragment.count("<script"), 0)


if __name__ == "__main__":
    unittest.main()
