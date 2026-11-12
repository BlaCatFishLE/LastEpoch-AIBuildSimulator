"""task-36 F1/F2/F3/P2: page-side policy, canonical scenario and validation alignment."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from le_calc import hit_report
from le_calc.report_ui import document

import test_hit_report


class HitPolicyCanonicalScenarioTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = hit_report._hit_report_js()
        cls.actions = document.asset("report_actions.js")
        cls.examples_path = Path(__file__).resolve().parents[3] / "tools" / "phase3" / "schemas" / "le-report-config-v1.examples.json"

    def test_hit_policy_expands_by_skill_policy_not_truncated_list(self):
        for token in (
            "hit_policy",
            "first_weight",
            "subsequent_weight",
            "policyWeights",
            "weightsForHits",
            "customBaseWeights",
        ):
            self.assertIn(token, self.js)
        self.assertIn("index === 0 ? HIT_POLICY.firstWeight : HIT_POLICY.additionalWeight", self.js)
        self.assertNotIn("currentWeights", self.js)

    def test_custom_weights_are_explicit_and_survive_reloads(self):
        for token in (
            "initialCustomWeights",
            "le-hit-custom-weights",
            "le-hit-generation-state",
            "persistCustomWeights",
            "updateWeightsFromScenario",
        ):
            self.assertIn(token, self.js)

    def test_canonical_scenario_round_trips_declared_sections(self):
        for token in (
            "canonicalScenario",
            "deepMerge",
            "filterScenario",
            "single_hit",
            "le-hit-canonical-scenario",
            "SCENARIO_ALLOWED",
        ):
            self.assertIn(token, self.js)
        # save/request path uses the canonical scenario
        self.assertIn("function readScenario()", self.js)
        self.assertIn("function applyScenario(scenario)", self.js)

    def test_reset_uses_immutable_generation_scenario_not_current_weights(self):
        reset_start = self.js.index('reset.addEventListener("click"')
        reset_block = self.js[reset_start:reset_start + 900]
        self.assertIn("initialCanonical", reset_block)
        self.assertIn("initialCustomWeights", reset_block)
        self.assertNotIn("currentWeights", reset_block)

    def test_weight_copy_is_data_driven(self):
        module = Path(hit_report.__file__).read_text(encoding="utf-8")
        self.assertIn("def _weights_note", module)
        self.assertNotIn("本 BD 为 4 击（权重 1 / 0.5 / 0.5 / 0.5）", module)
        self.assertNotIn("后续命中按 50% 权重计入", module)

    def test_shared_validator_has_capability_checks_and_codes(self):
        for token in (
            "validateScenarioPayload",
            "target_fields",
            "buff_controls",
            "out_of_range",
            "invalid_scenario_value",
            "unknown_scenario_field",
            "invalid_baseline_profile",
            "config_report_ref_mismatch",
            'typeof value !== "number"',
            "report_ref.input_fingerprints",
        ):
            self.assertIn(token, self.actions)
        # strict type checks must not coerce strings to numbers
        self.assertNotIn("Number(value)", self.actions)

    def test_shared_examples_cover_f1_f2_f3_cases(self):
        examples = json.loads(self.examples_path.read_text(encoding="utf-8"))
        ids = {case["id"] for case in examples["cases"]}
        for case_id in (
            "valid_complete_config",
            "valid_roundtrip_non_control_fields",
            "valid_custom_weights_override",
            "invalid_target_fire_resistance",
            "invalid_ambition_string",
            "nested_report_ref_fingerprint",
            "invalid_nested_single_hit_state",
            "invalid_baseline_profile",
        ):
            self.assertIn(case_id, ids)
        self.assertGreaterEqual(len(examples["cases"]), 16)


if __name__ == "__main__":
    unittest.main()
