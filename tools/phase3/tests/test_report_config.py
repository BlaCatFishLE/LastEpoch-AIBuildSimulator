# -*- coding: utf-8 -*-
"""R3/F1/F2/F3 shared report-config contract tests (``le-report-config-v1``).

The same schema file, examples file and capability declaration are used by the
page JS validator; Python must yield the same conclusion/codes for every shared
example (``tools/phase3/schemas/le-report-config-v1.examples.json``).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc import recalc, report_capabilities

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = REPO / "tools" / "phase3" / "schemas"
EXAMPLES = json.loads(
    (SCHEMAS / "le-report-config-v1.examples.json").read_text(encoding="utf-8")
)
FIXTURES = EXAMPLES["fixtures"]


def _codes(result: dict) -> list[str]:
    return [str(issue.get("code")) for issue in result["issues"]]


class SharedExamplesTests(unittest.TestCase):
    """Every shared example is the JS/Python alignment contract (F3)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = FIXTURES["capabilities"]

    def test_every_shared_example_matches_expected_conclusion(self) -> None:
        cases = EXAMPLES["cases"]
        self.assertGreaterEqual(len(cases), 12)
        for case in cases:
            with self.subTest(case=case["id"]):
                result = recalc.validate_report_config(
                    case["payload"],
                    current_bindings=case.get("current_binding"),
                    capabilities=self.capabilities,
                )
                self.assertEqual(
                    result["ok"], case["expected_ok"], f"{case['id']}: {result['issues']}"
                )
                self.assertEqual(_codes(result), list(case["expected_codes"]))
                expect = case.get("expect") or {}
                if "legacy" in expect:
                    self.assertEqual(result["legacy"], expect["legacy"])
                if expect.get("legacy_reason"):
                    self.assertEqual(result["legacy_reason"], expect["legacy_reason"])
                if "binding_checked" in expect:
                    self.assertEqual(result["binding_checked"], expect["binding_checked"])

    def test_shared_issue_codes_are_known(self) -> None:
        self.assertTrue(set(recalc.CONFIG_ISSUE_CODES) <= set(EXAMPLES["issue_codes"]))
        for code in EXAMPLES["issue_codes"]:
            if code.startswith("config_") or code.startswith("legacy_config_"):
                self.assertIn(code, recalc.CONFIG_ISSUE_CODES)

    def test_review_counterexamples_are_present(self) -> None:
        ids = {case["id"] for case in EXAMPLES["cases"]}
        for expected in (
            "invalid_target_fire_resistance",
            "invalid_ambition_string",
            "nested_report_ref_fingerprint",
            "valid_roundtrip_non_control_fields",
        ):
            self.assertIn(expected, ids)
        by_id = {case["id"]: case for case in EXAMPLES["cases"]}
        self.assertEqual(
            by_id["invalid_target_fire_resistance"]["expected_codes"], ["out_of_range"]
        )
        self.assertEqual(
            by_id["invalid_ambition_string"]["expected_codes"], ["invalid_scenario_value"]
        )
        self.assertEqual(
            by_id["nested_report_ref_fingerprint"]["expected_codes"],
            ["config_report_ref_mismatch"],
        )
        self.assertTrue(by_id["valid_roundtrip_non_control_fields"]["expected_ok"])

    def test_schema_file_declares_the_contract(self) -> None:
        schema = json.loads((SCHEMAS / "le-report-config-v1.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["$id"], "le-calc/le-report-config-v1.json")
        self.assertEqual(schema["properties"]["schema_version"]["const"], recalc.SCHEMA_REPORT_CONFIG)
        self.assertIn("binding", schema["properties"])
        self.assertIn("scenario", schema["$defs"])
        required = schema["$defs"]["binding"]["required"]
        for name in ("case_id", "model_id", "capabilities_version", "report_ref", "input_fingerprints"):
            self.assertIn(name, required)


class ValidatorBehaviourTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.capabilities = FIXTURES["capabilities"]
        cls.current = FIXTURES["current_binding"]
        cls.valid_payload = {
            "schema_version": recalc.SCHEMA_REPORT_CONFIG,
            "binding": json.loads(json.dumps(FIXTURES["binding"])),
            "scenario": json.loads(json.dumps(FIXTURES["scenario"])),
        }

    def test_never_raises_for_garbage_inputs(self) -> None:
        for payload in (None, [], "text", 42, {}, {"schema_version": 1}):
            with self.subTest(payload=payload):
                result = recalc.validate_report_config(
                    payload, current_bindings=self.current, capabilities=self.capabilities
                )
                self.assertFalse(result["ok"])
                self.assertTrue(result["issues"])
                self.assertIsInstance(result["issues"][0]["code"], str)

    def test_file_validation_reads_json_and_reports_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / "good.json"
            good.write_text(json.dumps(self.valid_payload), encoding="utf-8")
            result = recalc.validate_report_config_file(
                good, current_bindings=self.current, capabilities=self.capabilities
            )
            self.assertTrue(result["ok"], result["issues"])
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            result = recalc.validate_report_config_file(
                bad, current_bindings=self.current, capabilities=self.capabilities
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["issues"][0]["code"], "config_invalid_json")
            missing = recalc.validate_report_config_file(
                Path(tmp) / "missing.json", current_bindings=self.current
            )
            self.assertFalse(missing["ok"])

    def test_without_current_binding_reports_unchecked(self) -> None:
        result = recalc.validate_report_config(self.valid_payload, capabilities=self.capabilities)
        self.assertTrue(result["ok"])
        self.assertFalse(result["binding_checked"])
        self.assertIn("config_binding_not_checked", _codes(result))

    def test_without_capabilities_reports_unavailable(self) -> None:
        result = recalc.validate_report_config(self.valid_payload, current_bindings=self.current)
        self.assertTrue(result["ok"])
        self.assertIn("config_capabilities_unavailable", _codes(result))

    def test_unknown_binding_field_rejected(self) -> None:
        payload = json.loads(json.dumps(self.valid_payload))
        payload["binding"]["debug_note"] = "x"
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])
        self.assertEqual(_codes(result), ["config_binding_unknown_field"])

    def test_binding_schema_version_alias_accepted(self) -> None:
        payload = json.loads(json.dumps(self.valid_payload))
        payload["binding"]["schema_version"] = recalc.SCHEMA_RECALC_BINDINGS
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertTrue(result["ok"], result["issues"])
        payload["binding"]["schema_version"] = "other-bindings"
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])

    def test_valid_result_preserves_full_normalized_scenario(self) -> None:
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["single_hit"] = {"states": {"frenzy_active": True}}
        payload["scenario"]["states"]["enchant_weapon_active"] = False
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertTrue(result["ok"], result["issues"])
        self.assertIsInstance(result["scenario"], dict)
        self.assertEqual(
            result["scenario"]["single_hit"]["states"]["frenzy_active"], True
        )
        self.assertEqual(result["scenario"]["states"]["enchant_weapon_active"], False)
        self.assertEqual(result["scenario"]["hit_override"]["hits_per_cast"], 4)

    def test_target_and_states_rules_come_from_capabilities(self) -> None:
        # target range declared by capabilities.target_fields
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["target"]["fire_resistance"] = 2.0
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])
        self.assertEqual(_codes(result), ["out_of_range"])
        # buff_controls type
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["states"]["ambition_stacks"] = "5"
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])
        self.assertEqual(_codes(result), ["invalid_scenario_value"])
        # scenario_fields allowness
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["unexpected_top"] = 1
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])
        self.assertEqual(_codes(result), ["unknown_scenario_field"])
        # baseline_profiles
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["baseline_profile"] = "ghost_profile"
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=self.capabilities
        )
        self.assertFalse(result["ok"])
        self.assertEqual(_codes(result), ["invalid_baseline_profile"])

    def test_without_declared_range_type_check_still_applies(self) -> None:
        capabilities = json.loads(json.dumps(self.capabilities))
        for item in capabilities["target_fields"]:
            if item["field"] == "fire_resistance":
                item.pop("min", None)
                item.pop("max", None)
        payload = json.loads(json.dumps(self.valid_payload))
        payload["scenario"]["target"]["fire_resistance"] = 2.0
        result = recalc.validate_report_config(
            payload, current_bindings=self.current, capabilities=capabilities
        )
        # no declared max => no out_of_range issue (the declaration is the source of truth)
        self.assertTrue(result["ok"], result["issues"])


class HitPolicyTests(unittest.TestCase):
    """F1: capabilities.hit_policy drives default weight expansion."""

    def test_hit_policy_declared_for_both_models(self) -> None:
        hit = report_capabilities.capabilities_for_model("hit", repo_root=REPO)
        policy = hit["hit_policy"]
        self.assertEqual(policy["kind"], "first_full_then_fraction")
        self.assertEqual(policy["first_weight"], 1.0)
        self.assertEqual(policy["subsequent_weight"], 0.5)
        self.assertEqual(policy["hits"], {"min": 1, "max": 8, "default": 4})
        self.assertIs(policy["custom_weights_supported"], True)
        bh = report_capabilities.capabilities_for_model("black-hole", repo_root=REPO)
        self.assertEqual(bh["hit_policy"], {"kind": "not_applicable"})

    def test_hit_policy_hits_follow_model_parameters(self) -> None:
        capabilities = report_capabilities.capabilities_for_model("hit", repo_root=REPO)
        declaration = next(
            item for item in capabilities["model_parameters"] if item["key"] == "hits_per_cast"
        )
        self.assertEqual(capabilities["hit_policy"]["hits"]["min"], declaration["min"])
        self.assertEqual(capabilities["hit_policy"]["hits"]["max"], declaration["max"])
        self.assertEqual(capabilities["hit_policy"]["hits"]["default"], declaration["default"])

    def test_hit_policy_in_capabilities_artifact_and_validated(self) -> None:
        result = {
            "issues": [],
            "buffs": {"applied": [], "screening": {"candidates": []}},
            "dps": {},
        }
        document = report_capabilities.build_report_capabilities(
            case_id="flame-reave",
            model_id="hit",
            repo_root=REPO,
            run_id="hit-policy-run",
            report_path="reports/flame-reave/hit-policy-run/flame-reave-damage.html",
            result=result,
        )
        self.assertEqual(
            document["capabilities"]["hit_policy"]["kind"], "first_full_then_fraction"
        )
        self.assertEqual(
            report_capabilities.validate_report_capabilities(document, repo_root=REPO), document
        )
        broken = json.loads(json.dumps(document))
        broken["capabilities"]["hit_policy"]["kind"] = "unknown"
        broken["capabilities_digest"] = report_capabilities.capabilities_digest(
            broken["capabilities"]
        )
        broken["capabilities"]["recalculation"]["capabilities_version"] = (
            report_capabilities.capabilities_version(broken["capabilities"])
        )
        with self.assertRaises(ValueError):
            report_capabilities.validate_report_capabilities(broken, repo_root=REPO)

    def test_shared_example_fixture_declares_hit_policy(self) -> None:
        policy = FIXTURES["capabilities"]["hit_policy"]
        self.assertEqual(policy["kind"], "first_full_then_fraction")
        self.assertEqual(policy["hits"]["default"], 4)


if __name__ == "__main__":
    unittest.main()
