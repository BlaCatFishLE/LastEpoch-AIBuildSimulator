"""Validation and reporting tests on a small synthetic dataset."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.report.coverage import build_coverage, write_coverage_reports  # noqa: E402
from le_data.report.manifest import build_manifest  # noqa: E402
from le_data.report.quality import build_quality, write_quality_reports  # noqa: E402
from le_data.store import apply_migrations, connect, insert_row  # noqa: E402


def build_fixture_db(path: Path, *, valid_evidence: str = "known") -> Path:
    conn = connect(path)
    apply_migrations(conn)
    insert_row(conn, "dataset_meta", {
        "dataset_id": "fixture-dataset",
        "schema_version": "1",
        "normalizer_version": "0.1.0",
        "mapping_revision": "1",
        "status": "staging",
        "created_at": "2026-01-01T00:00:00+00:00",
        "target_game_version": "1.5.2",
    })
    insert_row(conn, "source_snapshots", {
        "source_snapshot_id": "fixture-snap",
        "source": "fixture",
        "claimed_version": "1.5.2",
        "status": "ok",
        "fetch_mode": "cache",
        "entry_url": "https://example.invalid/",
    })
    insert_row(conn, "raw_resources", {
        "resource_id": "fixture-snap:fixture.js",
        "source_snapshot_id": "fixture-snap",
        "source": "fixture",
        "resource_name": "fixture.js",
        "url": "https://example.invalid/fixture.js",
        "local_path": "data/raw/fixture/fixture-snap/responses/fixture.js.body",
        "sha256": "a" * 64,
        "size_bytes": 10,
        "http_status": 200,
        "fetch_status": "fetched",
        "role": "data",
    })
    conn.execute(
        "INSERT INTO dataset_source_snapshots VALUES (?,?,?)",
        ("fixture-dataset", "fixture-snap", "fixture"),
    )
    insert_row(conn, "source_records", {
        "record_id": "rec-skill",
        "source_snapshot_id": "fixture-snap",
        "source": "fixture",
        "resource_name": "fixture.js",
        "entity_kind": "skill",
        "source_scope": "abilities",
        "source_id": "aa1",
        "locator": "fixture.js#0",
        "raw_payload": "{}",
        "parse_status": "parsed",
    })
    insert_row(conn, "entities", {
        "entity_id": "skill:fixture:abilities:aa1",
        "kind": "skill",
        "canonical_name": "Fixture Skill",
        "record_status": "active",
        "primary_source": "fixture",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    insert_row(conn, "entity_names", {
        "entity_id": "skill:fixture:abilities:aa1",
        "language": "en",
        "name": "Fixture Skill",
        "is_alias": 0,
        "source": "fixture",
    })
    insert_row(conn, "source_id_map", {
        "mapping_id": "map-skill",
        "source_snapshot_id": "fixture-snap",
        "source": "fixture",
        "kind": "skill",
        "source_scope": "abilities",
        "source_id": "aa1",
        "entity_id": "skill:fixture:abilities:aa1",
        "mapping_method": "source_authoritative",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    insert_row(conn, "skills", {
        "entity_id": "skill:fixture:abilities:aa1",
        "skill_type": "active",
        "mana_cost": 5.0,
    })
    insert_row(conn, "field_evidence", {
        "entity_id": "skill:fixture:abilities:aa1",
        "field_path": "mana_cost",
        "source_record_id": "rec-skill",
        "source": "fixture",
        "source_snapshot_id": "fixture-snap",
        "raw_value": "5",
        "normalized_value": "5.0",
        "evidence_status": valid_evidence,
        "extraction_method": "parsed",
    })
    insert_row(conn, "data_issues", {
        "issue_id": "issue-1",
        "issue_type": "missing_field",
        "severity": "warning",
        "source": "fixture",
        "message": "cooldown not provided",
        "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
    })
    conn.commit()
    conn.close()
    return path


SMALL_SCOPE = {
    "required_domains": [
        {"id": "skills", "label": "Skills", "priority": "A",
         "entity_kinds": ["skill"], "minimum_entities": 1},
        {"id": "buffs", "label": "Buffs", "priority": "A",
         "entity_kinds": ["buff"], "minimum_entities": 1},
    ],
    "quality_thresholds": {"entities": 1},
}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.path = build_fixture_db(Path(tempfile.mkdtemp()) / "fixture.sqlite")

    def run_validator(self, scope=SMALL_SCOPE):
        from le_data.validation.checks import Validator

        conn = connect(self.path, readonly=True)
        try:
            return Validator(conn, scope).run_all()
        finally:
            conn.close()

    def test_passes_structure_but_fails_missing_domain(self):
        result = self.run_validator()
        self.assertFalse(result["ok"])
        failing = {check["check_id"] for check in result["checks"] if check["status"] == "fail"}
        self.assertIn("domain_buffs", failing)
        self.assertNotIn("sqlite_integrity", failing)
        self.assertNotIn("foreign_keys", failing)

    def test_invalid_evidence_status_detected(self):
        path = build_fixture_db(Path(tempfile.mkdtemp()) / "bad.sqlite", valid_evidence="bogus")
        from le_data.validation.checks import Validator

        conn = connect(path, readonly=True)
        try:
            result = Validator(conn, SMALL_SCOPE).run_all()
        finally:
            conn.close()
        failing = {check["check_id"] for check in result["checks"] if check["status"] == "fail"}
        self.assertIn("field_evidence_status", failing)

    def test_coverage_and_quality_reports(self):
        conn = connect(self.path, readonly=True)
        try:
            coverage = build_coverage(conn, SMALL_SCOPE)
            quality = build_quality(conn)
            manifest = build_manifest(conn, self.path, dataset_id="fixture-dataset")
        finally:
            conn.close()
        self.assertEqual(coverage["domains"]["skills"]["normalized_entities"], 1)
        self.assertEqual(coverage["domains"]["buffs"]["normalized_entities"], 0)
        self.assertEqual(quality["parse_status"][0]["n"], 1)
        self.assertEqual(manifest["dataset_id"], "fixture-dataset")
        self.assertTrue(manifest["database"]["logical_content_sha256"])
        out = Path(tempfile.mkdtemp())
        paths = write_coverage_reports(coverage, out)
        self.assertTrue(Path(paths["json"]).exists())
        self.assertTrue(Path(paths["md"]).exists())
        quality_paths = write_quality_reports(quality, out)
        self.assertTrue(Path(quality_paths["json"]).exists())


if __name__ == "__main__":
    unittest.main()
