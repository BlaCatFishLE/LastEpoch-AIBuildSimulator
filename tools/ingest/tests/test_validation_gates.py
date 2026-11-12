"""Negative/positive tests for the P1-3 validation gates.

The review requires that the validator actually executes the configured field
requirements, splits idols/blessings/season into separate required domains,
detects a silently dropped source family (weaver/season records) and fails when
semantic normalization results are missing:

* clearing buff relationships must fail;
* removing effect -> stat links must fail;
* deleting one required sub-domain must fail (even when the old combined
  idol+blessing count would still pass);
* dropping a source index mapping must fail.

The same suite also runs the configured rules against the immutable published
dataset ``phase1-dataset-001`` (read-only) and proves that the review's
"44 pass / 0 warn / 0 fail" result is no longer reproducible.
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_support as gates  # noqa: E402
from le_data.config import load_coverage_scope  # noqa: E402
from le_data.store import connect  # noqa: E402
from le_data.validation.checks import Validator  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PUBLISHED_DB = PROJECT_ROOT / "data" / "releases" / "phase1-dataset-001" / "data.sqlite"


def GATE_SCOPE() -> dict:
    """A tiny executable scope that mirrors the structure of coverage-scope.json."""
    return {
        "required_domains": [
            {"id": "skills", "label": "Skills", "priority": "A",
             "entity_kinds": ["skill"], "minimum_entities": 1},
            {"id": "buffs", "label": "Buffs", "priority": "A",
             "entity_kinds": ["buff"], "minimum_entities": 1},
            {"id": "idols", "label": "Idols", "priority": "B",
             "entity_kinds": ["idol"], "minimum_entities": 1},
            {"id": "blessings", "label": "Blessings", "priority": "B",
             "entity_kinds": ["blessing"], "minimum_entities": 1},
            {"id": "season", "label": "Season entities", "priority": "B",
             "entity_kinds": ["season_entity"], "minimum_entities": 1,
             "absence_issue_types": ["season_source_absent", "season_entity_unmapped"]},
        ],
        "field_requirements": [
            {
                "id": "skill_canonical_name",
                "label": "Skills have a canonical name",
                "entity_kind": "skill",
                "sql": "SELECT COUNT(*) FROM skills s JOIN entities e ON e.entity_id = s.entity_id "
                       "WHERE e.canonical_name IS NOT NULL AND TRIM(e.canonical_name) <> ''",
                "denominator_sql": "SELECT COUNT(*) FROM skills",
                "minimum_ratio": 1.0,
                "severity": "fail",
            }
        ],
        "source_index": [
            {
                "id": "buff_records_mapped",
                "domain": "buffs",
                "raw_entity_kinds": ["buff"],
                "minimum_raw_records": 1,
                "minimum_mapped_ratio": 1.0,
            },
            {
                "id": "season_weaver_records",
                "domain": "season",
                "raw_entity_kinds": ["weaver_node", "weaver_tree"],
                "minimum_raw_records": 1,
                "minimum_mapped_ratio": 1.0,
            },
        ],
        "buff_source_configs": {
            "source_record_patterns": ['%"buffs"%', '%"buffAbilities"%', '%"masteryBuffs"%'],
            "config_keys": ["buffs", "buffAbilities", "masteryBuffs"],
            "relationship_patterns": ["%buff%"],
            "issue_type_patterns": ["%buff%"],
        },
        "version_gate": {},
        "quality_thresholds": {},
    }


class ValidationGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = gates.build_gate_db(self.tmp / "gate.sqlite")

    # -- helpers -----------------------------------------------------------
    def run_validator(self, scope: dict | None = None, db_path: Path | None = None) -> dict:
        conn = connect(db_path or self.db_path, readonly=True)
        try:
            return Validator(conn, scope or GATE_SCOPE()).run_all()
        finally:
            conn.close()

    @staticmethod
    def statuses(result: dict) -> dict[str, str]:
        return {check["check_id"]: check["status"] for check in result["checks"]}

    def execute(self, sql: str, params: tuple = (), db_path: Path | None = None) -> None:
        conn = connect(db_path or self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    # -- positive baseline --------------------------------------------------
    def test_positive_gate_checks_pass(self):
        result = self.run_validator()
        statuses = self.statuses(result)
        for check_id in (
            "field_requirements_config",
            "field_skill_canonical_name",
            "source_index_buff_records_mapped",
            "source_index_season_weaver_records",
            "semantic_stat_links_present",
            "semantic_buff_relationships_present",
            "buff_source_configs",
            "domain_skills",
            "domain_buffs",
            "domain_idols",
            "domain_blessings",
            "domain_season",
        ):
            self.assertEqual(statuses.get(check_id), "pass", f"{check_id}: {statuses.get(check_id)}")

    def test_summary_separates_gate_stages(self):
        result = self.run_validator()
        stages = result["stages"]
        for stage_name in (
            "structural_integrity",
            "acquisition_completeness",
            "semantic_normalization",
            "version_compatibility",
            "phase2_import_readiness",
        ):
            self.assertIn(stage_name, stages)
        self.assertGreaterEqual(stages["semantic_normalization"]["pass"], 3)
        self.assertEqual(stages["version_compatibility"]["fail"], 0)
        self.assertEqual(stages["phase2_import_readiness"], {"pass": 0, "warn": 0, "fail": 0})

    def test_validation_report_writes_stage_table(self):
        from le_data.validation.checks import write_validation_reports

        result = self.run_validator()
        paths = write_validation_reports(result, self.tmp / "report")
        markdown = Path(paths["md"]).read_text(encoding="utf-8")
        self.assertIn("## Gate stages", markdown)
        self.assertIn("semantic_normalization", markdown)
        stored = json.loads(Path(paths["json"]).read_text(encoding="utf-8"))
        self.assertIn("stages", stored)

    # -- negative: buff relationships cleared ------------------------------
    def test_clearing_buff_relationships_fails(self):
        path = gates.build_gate_db(self.tmp / "no-buff-rel.sqlite", buff_relationship=False)
        result = self.run_validator(db_path=path)
        statuses = self.statuses(result)
        self.assertEqual(statuses.get("semantic_buff_relationships_present"), "fail")
        self.assertEqual(statuses.get("buff_source_configs"), "fail")
        self.assertFalse(result["ok"])

    # -- negative: effect -> stat links removed ----------------------------
    def test_removing_stat_links_fails(self):
        path = gates.build_gate_db(self.tmp / "no-stat-link.sqlite", stat_linked=False)
        result = self.run_validator(db_path=path)
        self.assertEqual(self.statuses(result).get("semantic_stat_links_present"), "fail")

    # -- negative: required sub-domain deleted -----------------------------
    def test_removing_required_blessing_subdomain_fails(self):
        self.execute("DELETE FROM entity_names WHERE entity_id = ?", ("blessing:gate:bl1",))
        self.execute("DELETE FROM source_id_map WHERE entity_id = ?", ("blessing:gate:bl1",))
        self.execute("DELETE FROM blessings WHERE entity_id = ?", ("blessing:gate:bl1",))
        self.execute("DELETE FROM entities WHERE entity_id = ?", ("blessing:gate:bl1",))
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("domain_blessings"), "fail")
        # The old combined idols+blessings domain would still have passed via idols.
        self.assertEqual(statuses.get("domain_idols"), "pass")

    # -- negative: source index mapping dropped ----------------------------
    def test_dropping_weaver_source_index_mapping_fails(self):
        self.execute("DELETE FROM source_id_map WHERE kind = 'weaver_node'")
        result = self.run_validator()
        statuses = self.statuses(result)
        self.assertEqual(statuses.get("source_index_season_weaver_records"), "fail")
        detail = next(
            check for check in result["checks"]
            if check["check_id"] == "source_index_season_weaver_records"
        )
        self.assertEqual(detail["detail"]["unmapped_records"], 1)
        self.assertEqual(detail["detail"]["raw_records"], 2)

    # -- season absence policy ---------------------------------------------
    def test_season_absence_without_issue_fails(self):
        path = gates.build_gate_db(self.tmp / "season-no-issue.sqlite", season_entity=False)
        statuses = self.statuses(self.run_validator(db_path=path))
        self.assertEqual(statuses.get("domain_season"), "fail")

    def test_season_absence_with_explicit_issue_is_only_a_warning(self):
        path = gates.build_gate_db(
            self.tmp / "season-issue.sqlite", season_entity=False, season_issue=True
        )
        statuses = self.statuses(self.run_validator(db_path=path))
        self.assertEqual(statuses.get("domain_season"), "warn")

    # -- executable field requirements -------------------------------------
    def test_field_requirement_detects_missing_field(self):
        self.execute("UPDATE entities SET canonical_name = NULL WHERE entity_id = ?", ("skill:gate:a1",))
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("field_skill_canonical_name"), "fail")

    def test_invalid_field_requirement_sql_fails_instead_of_skipping(self):
        scope = GATE_SCOPE()
        scope["field_requirements"] = [
            {
                "id": "broken_rule",
                "label": "Broken rule",
                "sql": "SELECT COUNT(*) FROM table_that_does_not_exist",
                "denominator_sql": "SELECT COUNT(*) FROM skills",
                "minimum_ratio": 1.0,
                "severity": "fail",
            }
        ]
        statuses = self.statuses(self.run_validator(scope=scope))
        self.assertEqual(statuses.get("field_broken_rule"), "fail")

    def test_legacy_prose_field_requirements_are_not_executed(self):
        scope = GATE_SCOPE()
        scope["field_requirements"] = {"skill": ["canonical_name"]}
        result = self.run_validator(scope=scope)
        statuses = self.statuses(result)
        self.assertEqual(statuses.get("field_requirements_config"), "warn")


class CoverageScopeConfigTests(unittest.TestCase):
    """The checked-in scope must stay executable: no prose-only gates."""

    def test_real_scope_has_executable_split_domains_and_gates(self):
        scope = load_coverage_scope()
        domain_ids = [domain["id"] for domain in scope["required_domains"]]
        self.assertNotIn("idols_blessings_season", domain_ids)
        for domain_id in ("idols", "blessings", "season"):
            self.assertIn(domain_id, domain_ids)
        season = next(domain for domain in scope["required_domains"] if domain["id"] == "season")
        self.assertTrue(season.get("absence_issue_types"))
        self.assertTrue(season.get("count_sql"), "season domain must count functional weaver coverage")

        requirements = scope["field_requirements"]
        self.assertIsInstance(requirements, list)
        self.assertGreaterEqual(len(requirements), 10)
        for requirement in requirements:
            self.assertIn("sql", requirement, requirement.get("id"))
            self.assertIn("denominator_sql", requirement, requirement.get("id"))
            self.assertIn("minimum_ratio", requirement, requirement.get("id"))

        semantic_ids = {item["id"] for item in scope["semantic_requirements"]}
        self.assertIn("stat_links_present", semantic_ids)
        self.assertIn("buff_relationships_present", semantic_ids)
        self.assertEqual(scope["version_gate"]["require_compatibility_conclusion"], True)
        self.assertEqual(scope["version_gate"]["require_version_evidence"], True)
        self.assertTrue(scope.get("source_index"))

    def test_real_scope_sql_is_executable_on_published_dataset(self):
        if not PUBLISHED_DB.exists():
            self.skipTest(f"published reference dataset not available: {PUBLISHED_DB}")
        scope = load_coverage_scope()
        conn = connect(PUBLISHED_DB, readonly=True)
        try:
            # Field requirements must stay executable against the published schema.
            # Semantic requirement SQL is allowed to fail here (the published dataset
            # predates the P1-2 stat_link_status column); those failures are exercised
            # by test_published_dataset_no_longer_passes_the_review_gates.
            for requirement in scope["field_requirements"]:
                conn.execute(requirement["sql"]).fetchone()
                conn.execute(requirement["denominator_sql"]).fetchone()
        finally:
            conn.close()

    def test_published_dataset_no_longer_passes_the_review_gates(self):
        if not PUBLISHED_DB.exists():
            self.skipTest(f"published reference dataset not available: {PUBLISHED_DB}")
        scope = load_coverage_scope()
        conn = connect(PUBLISHED_DB, readonly=True)
        try:
            result = Validator(conn, scope).run_all()
        finally:
            conn.close()
        self.assertFalse(result["ok"])
        statuses = {check["check_id"]: check["status"] for check in result["checks"]}
        for check_id in (
            "semantic_stat_links_present",
            "semantic_buff_relationships_present",
            "buff_source_configs",
            "version_compatibility_conclusion",
        ):
            self.assertEqual(statuses.get(check_id), "fail", f"{check_id}: {statuses.get(check_id)}")
        # season coverage is functional (weaver tree + 80 weaver nodes are mapped), so the
        # domain must not fail just because season_entities is empty; the raw weaver records
        # are still guarded by source_index.
        self.assertEqual(statuses.get("domain_season"), "pass")
        # Non-blocking by design: per-buff source relationship coverage is a warn-level
        # lead (many UIBuffs rows are only reachable through candidate references).
        self.assertEqual(statuses.get("field_buff_source_relationship"), "warn")
        # The review baseline was 44 pass / 0 warn / 0 fail; the gates must now fail.
        self.assertGreaterEqual(result["counts"]["fail"], 4)
        self.assertNotEqual(result["counts"], {"pass": 44, "warn": 0, "fail": 0})


class FieldRequirementConfigTests(unittest.TestCase):
    def test_copy_of_scope_is_independent(self):
        scope = GATE_SCOPE()
        clone = copy.deepcopy(scope)
        clone["field_requirements"][0]["sql"] = "SELECT 1"
        self.assertNotEqual(scope["field_requirements"][0]["sql"], clone["field_requirements"][0]["sql"])
        self.assertEqual(json.loads(json.dumps(scope))["required_domains"][0]["id"], "skills")


if __name__ == "__main__":
    unittest.main()
