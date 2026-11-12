"""task-27 R1-R8/O-A/B/C behavior and contract tests (client side)."""

from __future__ import annotations

import copy
import json
import re
import unittest
from pathlib import Path

from le_calc import hit_report, recalc
from le_calc.report_ui import document

import test_hit_report
from test_recalc_contract_page import synthetic_bindings


def minimal_capabilities() -> dict:
    return {
        "schema_version": "le-model-capabilities-v1",
        "model_id": "hit",
        "ui_contract_version": document.UI_CONTRACT_VERSION,
        "renderer_fingerprint": "e" * 64,
        "recalculation": {"capabilities_version": "le-model-capabilities-v1:fixture"},
        "scenario_fields": {
            "required": ["baseline_profile", "target", "states"],
            "allowed": ["baseline_profile", "target", "states", "hit_override", "schema"],
        },
        "baseline_profiles": ["legacy_single_skill", "build_base"],
        "profile_states": {"legacy_single_skill": {"enemy_high_health": False}},
        "buff_controls": [
            {"state_key": "enemy_high_health", "kind": "toggle", "default": True},
            {"state_key": "consume_rhythm_of_fire", "kind": "toggle", "default": False},
            {"state_key": "rhythm_of_fire_stacks", "kind": "stacks", "min": 0, "max": 12},
        ],
        "target_fields": [
            {"field": "monster_level", "kind": "int", "min": 1, "max": 100},
            {"field": "fire_resistance", "kind": "float", "min": -1.0, "max": 1.0},
            {"field": "boss_or_rare", "kind": "bool"},
        ],
        "model_parameters": [{"key": "hits_per_cast", "kind": "int", "min": 1, "max": 8}],
    }


class R2HitNormalizationTest(unittest.TestCase):
    def test_simulated_hits_override_static_block(self):
        derived = {"per_cast": 4, "damage_weights": [1.0, 0.5, 0.5, 0.5], "returns": 3}
        raw = {"per_cast": 4, "damage_weights": [1.0, 0.5, 0.5, 0.5], "effective_hits_per_cast": 2.5}
        scenario = {"hit_override": {"hits_per_cast": 1, "damage_weights": [1.0]}}
        hits = hit_report._normalize_hits_per_cast(raw, derived, scenario)
        self.assertEqual(hits["per_cast"], 1)
        self.assertEqual(hits["damage_weights"], [1.0])
        self.assertEqual(hits["effective_hits_per_cast"], 1.0)
        self.assertEqual(hits["returns"], 0)

    def test_equal_weights_are_preserved(self):
        derived = {"per_cast": 4}
        scenario = {"hit_override": {"hits_per_cast": 4, "damage_weights": [1.0, 1.0, 1.0, 1.0]}}
        hits = hit_report._normalize_hits_per_cast(None, derived, scenario)
        self.assertEqual(hits["damage_weights"], [1.0, 1.0, 1.0, 1.0])
        self.assertEqual(hits["effective_hits_per_cast"], 4.0)

    def test_html_has_single_canonical_hits_control(self):
        result = copy.deepcopy(test_hit_report.build_result())
        result["scenario"]["hit_override"] = {"hits_per_cast": 1, "damage_weights": [1.0]}
        result["hits_per_cast"] = {
            "per_cast": 1,
            "returns": 0,
            "damage_weights": [1.0],
            "effective_hits_per_cast": 1.0,
            "sources": [],
        }
        html = hit_report.render_hit_html(result)
        inputs = re.findall(r'<input[^>]*data-cond-hits[^>]*>', html)
        self.assertEqual(len(inputs), 1, inputs)
        self.assertIn('value="1"', inputs[0])


class R7ControlTypeTest(unittest.TestCase):
    def test_control_kind_comes_from_capabilities(self):
        result = copy.deepcopy(test_hit_report.build_result())
        screening = result["buffs"]["screening"]
        screening["candidates"] = [
            {
                "label": "Rhythm toggle",
                "source_kind": "skill_node",
                "coverage": "modeled",
                "condition": {"state": "consume_rhythm_of_fire"},
            },
            {
                "label": "Rhythm stacks",
                "source_kind": "skill_node",
                "coverage": "modeled",
                "condition": {"state": "rhythm_of_fire_stacks"},
            },
        ]
        html = hit_report._condition_parameters_block(result, minimal_capabilities())
        self.assertRegex(html, r'<input type="checkbox" data-cond data-state="consume_rhythm_of_fire"')
        self.assertRegex(html, r'<input type="number" data-cond data-state="rhythm_of_fire_stacks"[^>]*max="12"')
        self.assertNotRegex(html, r'<input type="number"[^>]*data-state="consume_rhythm_of_fire"')


class ClientContractSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = document.asset("recalc_contract.js")
        cls.fr_js = hit_report._hit_report_js()

    def test_R1_preset_does_not_override_user_edits(self):
        self.assertNotIn("PROFILE_STATES[baselineProfile]", self.fr_js)
        self.assertIn("preset.addEventListener", self.fr_js)
        self.assertIn("已切换预设", self.fr_js)

    def test_R3_config_is_validated_before_apply(self):
        for token in (
            "validateConfig",
            "validateBinding",
            'schema_version: "le-report-config-v1"',
            "未知配置字段",
            "配置绑定",
            "未修改当前配置",
        ):
            self.assertIn(token, self.fr_js)
        self.assertNotIn("parsed && parsed.scenario ? parsed.scenario : parsed", self.fr_js)

    def test_R4_target_inputs_mark_edits(self):
        self.assertIn('document.querySelector("[data-cond-fire-res]")', self.fr_js)
        self.assertIn("目标已更改，点击", self.fr_js)
        self.assertIn("markEdit(", self.fr_js)

    def test_R5_client_validates_complete_response_binding(self):
        for token in (
            "validateResponse",
            "client_edit_revision 不匹配",
            "case_id 不匹配",
            "model_id 不匹配",
            "capabilities_version 不匹配",
            "input_fingerprints 不匹配",
            "report_ref 不匹配",
            "recompute_endpoint 不匹配",
            "accepted_revision",
        ):
            self.assertIn(token, self.client)

    def test_R6_file_mode_explicit_compute_is_not_blocked(self):
        self.assertNotIn('location.protocol === "file:"', self.client)
        self.assertIn("本机重算未启用", self.client)
        self.assertIn("readBindings", self.client)

    def test_R8_session_scoped_requests(self):
        for token in ("client_session_id", "sessionId", "client_session_legacy"):
            self.assertIn(token, self.client)
        self.assertIn("request.client_session_id = sessionId", self.client)

    def test_O_A_state_machine_strings(self):
        for token in ("计算中…", "已应用", "计算失败", "已保留当前编辑与旧结果"):
            self.assertIn(token, self.client)
        for token in ("已更改，点击", "计算中…", "已应用", "data-status-state"):
            self.assertIn(token, self.fr_js)

    def test_O_B_audit_note_removed(self):
        page = hit_report.render_hit_html(test_hit_report.build_result())
        self.assertNotIn("完整审计数据保留在结果 JSON", page)

    def test_O_C_fingerprint_delegates_to_authoritative_module(self):
        from le_calc import report_fingerprint

        root = Path(__file__).resolve().parents[3]
        self.assertEqual(
            document.renderer_fingerprint(), report_fingerprint.renderer_fingerprint(root)
        )


class ReportConfigContractTest(unittest.TestCase):
    def test_page_config_shape_is_accepted_by_authoritative_validator(self):
        capabilities = minimal_capabilities()
        bindings = synthetic_bindings("hit")
        scenario = {
            "schema": "le-scenario-v1",
            "baseline_profile": "build_base",
            "states": {"enemy_high_health": True, "consume_rhythm_of_fire": False, "rhythm_of_fire_stacks": 1},
            "hit_override": {"hits_per_cast": 4, "damage_weights": [1, 0.5, 0.5, 0.5]},
            "target": {"boss_or_rare": True, "monster_level": 100, "fire_resistance": 0.0},
        }
        result = recalc.validate_report_config(
            {
                "schema_version": "le-report-config-v1",
                "binding": bindings,
                "scenario": scenario,
            },
            current_bindings=bindings,
            capabilities=capabilities,
        )
        self.assertTrue(result["ok"], result["issues"])
        self.assertFalse(result["legacy"])
        self.assertEqual(result["scenario"]["hit_override"]["hits_per_cast"], 4)

    def test_foreign_binding_is_rejected(self):
        capabilities = minimal_capabilities()
        bindings = synthetic_bindings("hit")
        foreign = json.loads(json.dumps(bindings))
        foreign["case_id"] = "foreign-build"
        result = recalc.validate_report_config(
            {
                "schema_version": "le-report-config-v1",
                "binding": foreign,
                "scenario": {
                    "schema": "le-scenario-v1",
                    "baseline_profile": "build_base",
                    "states": {},
                    "target": {},
                },
            },
            current_bindings=bindings,
            capabilities=capabilities,
        )
        self.assertFalse(result["ok"])
        self.assertIn("config_case_mismatch", [issue["code"] for issue in result["issues"]])

    def test_legacy_config_without_binding_is_flagged(self):
        capabilities = minimal_capabilities()
        bindings = synthetic_bindings("hit")
        result = recalc.validate_report_config(
            {
                "schema": "le-report-config-v1",
                "scenario": {
                    "schema": "le-scenario-v1",
                    "baseline_profile": "build_base",
                    "states": {},
                    "target": {},
                },
            },
            current_bindings=bindings,
            capabilities=capabilities,
        )
        self.assertTrue(result["legacy"])
        self.assertIn("legacy_config_no_binding", [issue["code"] for issue in result["issues"]])


class ReportConfigExamplesAlignmentTest(unittest.TestCase):
    """F3/R3: page/config rules must agree with the shared examples schema."""

    def test_shared_examples_match_authoritative_validator(self):
        examples_path = (
            Path(__file__).resolve().parents[3]
            / "tools"
            / "phase3"
            / "schemas"
            / "le-report-config-v1.examples.json"
        )
        if not examples_path.is_file():
            self.skipTest("shared report-config examples missing")
        examples = json.loads(examples_path.read_text(encoding="utf-8"))
        capabilities = examples["fixtures"].get("capabilities")
        fixture_binding = examples["fixtures"].get("current_binding") or examples["fixtures"]["binding"]
        for case in examples["cases"]:
            payload = case["payload"]
            current_binding = case.get("current_binding") or fixture_binding
            result = recalc.validate_report_config(
                payload,
                current_bindings=current_binding,
                capabilities=capabilities,
            )
            expect = case.get("expect") or {}
            expected_ok = case.get("expected_ok", expect.get("ok"))
            expected_codes = case.get("expected_codes", expect.get("error_codes") or [])
            self.assertEqual(result["ok"], expected_ok, case["id"])
            if "legacy" in expect:
                self.assertEqual(result["legacy"], expect["legacy"], case["id"])
            if "binding_checked" in expect:
                self.assertEqual(result["binding_checked"], expect["binding_checked"], case["id"])
            if "legacy_reason" in expect:
                self.assertEqual(result["legacy_reason"], expect["legacy_reason"], case["id"])
            codes = [issue["code"] for issue in result["issues"]]
            for code in expected_codes:
                self.assertIn(code, codes, case["id"])
            if expected_ok and result["scenario"] is not None:
                self.assertIsInstance(result["scenario"], dict)


if __name__ == "__main__":
    unittest.main()
