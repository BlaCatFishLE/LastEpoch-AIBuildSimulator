"""Tests for the result assembly and run artefacts."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import support

from le_calc import model
from le_calc.report import build_result, consistency_checks, write_run
from le_calc.ruleset import load_ruleset

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "result-v1.json"


class BuildResultTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.snapshot = support.make_snapshot()
        cls.ruleset = load_ruleset()
        cls.result = build_result(
            cls.snapshot,
            cls.ruleset,
            run_id="test-run",
            generated_at="2026-11-04T00:00:00+00:00",
        )

    def test_required_top_level_keys(self):
        for key in (
            "schema", "run", "input", "scope", "scenario", "action_policy", "snapshot",
            "source_ledger", "rule_ledger", "modifier_graph", "single_cast", "loops",
            "issues", "excluded_sources",
        ):
            self.assertIn(key, self.result)
        self.assertEqual(self.result["schema"], "black-hole-stage1-result-v1")

    def test_schema_required_properties_present(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        for key in schema["required"]:
            self.assertIn(key, self.result)

    def test_run_metadata(self):
        run = self.result["run"]
        self.assertEqual(run["run_id"], "test-run")
        self.assertEqual(run["engine"]["id"], "le_calc")
        self.assertEqual(run["ruleset"]["id"], "bh-stage1-v1")
        self.assertEqual(run["generated_at"], "2026-11-04T00:00:00+00:00")
        self.assertIn(run["status"], ("complete", "assumption", "incomplete"))
        self.assertEqual(run["status"], "assumption")

    def test_rule_ledger_contains_confirmed_and_derived(self):
        ledger = self.result["rule_ledger"]
        ids = {rule["rule_id"] for rule in ledger}
        for required in (
            "rule-bh-component",
            "rule-bh-total-damage",
            "rule-bh-binary-system",
            "rule-bh-duration",
            "rule-ignite-heat-wave",
            "rule-ignite-heat-wave-timing",
            "rule-penetration-dot",
            "rule-cooldown-base",
            "rule-cooldown-icr",
            "rule-derived-heat-wave-chance",
            "rule-derived-cooldown",
            "rule-derived-ignite-penetration",
            "rule-bh-binary-more-interpretation",
            "rule-fire-penetration-with-ignite",
            "rule-black-hole-recast",
        ):
            self.assertIn(required, ids)
        statuses = {rule["status"] for rule in ledger}
        self.assertTrue({"confirmed", "derived", "assumed", "unresolved"} <= statuses)
        for rule in ledger:
            self.assertTrue(rule["evidence"])

    def test_source_ledger_matches_snapshot(self):
        ledger = self.result["source_ledger"]
        self.assertEqual(len(ledger), len(self.snapshot.contributions))
        ids = [row["contribution_id"] for row in ledger]
        self.assertEqual(ids, sorted(ids))
        for row in ledger:
            self.assertIn("included", row)
            self.assertIn("status", row)

    def test_consistency_checks_pass(self):
        checks = self.result["consistency"]
        self.assertTrue(checks["all_pass"], json.dumps(checks, indent=2))
        for key, value in checks.items():
            if key == "all_pass":
                continue
            self.assertIsInstance(value, bool)

    def test_scenario_and_scope(self):
        scenario = self.result["scenario"]
        self.assertTrue(scenario["target"]["boss_tag"])
        self.assertEqual(scenario["target"]["resistances"]["fire"], 0.0)
        self.assertTrue(scenario["coverage"]["both_stars_cover_target"])
        self.assertTrue(scenario["coverage"]["center_condition_met"])
        self.assertTrue(scenario["coverage"]["theoretical"])
        self.assertEqual(len(self.result["loops"]), 2)
        self.assertEqual(len(self.result["excluded_sources"]), 1)

    def test_issues_are_explicit(self):
        issue_ids = {issue["issue_id"] for issue in self.result["issues"]}
        self.assertIn("engine-heat-wave-expected", issue_ids)
        self.assertIn("engine-penetration-ignite", issue_ids)
        self.assertIn("engine-user-confirmed-elemental-int", issue_ids)
        self.assertIn("engine-binary-more-uncertainty", issue_ids)
        self.assertIn("engine-fire-penetration-with-ignite", issue_ids)
        self.assertIn("engine-multi-black-hole-overlap", issue_ids)
        self.assertIn("engine-reference-tick-conflict", issue_ids)
        self.assertIn("engine-cooldown-mapping", issue_ids)
        self.assertIn("engine-crescendoing-rage-deferred", issue_ids)
        self.assertIn("engine-policy-difference", issue_ids)

    def test_single_cast_payload_uses_expression_trees(self):
        body = self.result["single_cast"]["body"]
        for component in body["components"]:
            self.assertIn("expression", component)
            self.assertEqual(component["expression"]["kind"], "mul")
        self.assertIn("expression", self.result["single_cast"]["ignite"])

    def test_deterministic(self):
        again = build_result(
            self.snapshot,
            self.ruleset,
            run_id="test-run",
            generated_at="2026-11-04T00:00:00+00:00",
        )
        self.assertEqual(
            json.dumps(self.result, sort_keys=True),
            json.dumps(again, sort_keys=True),
        )


class WriteRunTest(unittest.TestCase):
    def test_write_run_artefacts(self):
        snapshot = support.make_snapshot()
        ruleset = load_ruleset()
        result = build_result(
            snapshot,
            ruleset,
            run_id="test-run",
            generated_at="2026-11-04T00:00:00+00:00",
        )
        with tempfile.TemporaryDirectory() as tmp:
            written = write_run(result, tmp, render_html=False)
            for name in (
                "run-manifest.json",
                "input-snapshot.json",
                "source-ledger.json",
                "rule-ledger.json",
                "result.json",
                "README.zh-CN.md",
            ):
                path = Path(tmp) / name
                self.assertTrue(path.is_file(), name)
                if name.endswith(".json"):
                    json.loads(path.read_text(encoding="utf-8"))
            readme = (Path(tmp) / "README.zh-CN.md").read_text(encoding="utf-8")
            self.assertIn("Black Hole", readme)
            self.assertIn("不是完整 BD", readme)
            self.assertIn("assumed", readme)
            self.assertEqual(written["run-manifest.json"], str(Path(tmp) / "run-manifest.json"))
            stored = json.loads((Path(tmp) / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(stored["run"]["run_id"], "test-run")
            manifest = json.loads((Path(tmp) / "run-manifest.json").read_text(encoding="utf-8"))
            self.assertIn("result.json", manifest["files"])


class ConsistencyHelperTest(unittest.TestCase):
    def test_consistency_helper_flags_broken_totals(self):
        snapshot = support.make_snapshot()
        ruleset = load_ruleset()
        result = build_result(
            snapshot,
            ruleset,
            run_id="test-run",
            generated_at="2026-11-04T00:00:00+00:00",
        )
        single = json.loads(json.dumps(result["single_cast"]))
        single["totals"]["single_cast_total"] *= 1.01
        checks = consistency_checks(single, result["loops"])
        self.assertFalse(checks["all_pass"])
        self.assertFalse(checks["single_total_equals_body_plus_ignite"])


if __name__ == "__main__":
    unittest.main()
