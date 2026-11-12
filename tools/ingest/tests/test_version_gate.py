"""P1-4 version gate tests: unknown/conflicting versions must fail.

The validator compares dataset_meta.target_game_version,
source_snapshots.claimed_version and the page/module versions inside
source_snapshots.version_evidence with dataset_meta.compatibility_conclusion.
Only an explicit entry in a version-compat approval file (version pair +
evidence + approver) may allow a mismatch.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import gate_support as gates  # noqa: E402
from le_data.store import connect  # noqa: E402
from le_data.validation.checks import Validator  # noqa: E402

SCOPE = {"required_domains": [], "quality_thresholds": {}, "version_gate": {}}


class VersionGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db_path = gates.build_gate_db(self.tmp / "versions.sqlite")
        self.approval_path = self.tmp / "version-compat.json"

    # -- helpers -----------------------------------------------------------
    def run_validator(self, db_path: Path | None = None, approval_path: Path | None = None) -> dict:
        conn = connect(db_path or self.db_path, readonly=True)
        try:
            return Validator(
                conn, SCOPE, version_compat_path=approval_path or self.approval_path
            ).run_all()
        finally:
            conn.close()

    @staticmethod
    def statuses(result: dict) -> dict[str, str]:
        return {check["check_id"]: check["status"] for check in result["checks"]}

    @staticmethod
    def messages(result: dict) -> dict[str, str]:
        return {check["check_id"]: check["message"] for check in result["checks"]}

    def execute(self, sql: str, params: tuple = (), db_path: Path | None = None) -> None:
        conn = connect(db_path or self.db_path)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def update_snapshot(self, source: str, **fields) -> None:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.execute(
            f"UPDATE source_snapshots SET {assignments} WHERE source = ?",
            tuple(fields.values()) + (source,),
        )

    def update_meta(self, **fields) -> None:
        assignments = ", ".join(f"{column} = ?" for column in fields)
        self.execute(f"UPDATE dataset_meta SET {assignments}", tuple(fields.values()))

    def write_approvals(self, approvals: list[dict], *, key: str = "approvals") -> Path:
        self.approval_path.write_text(
            json.dumps({"schema_version": 1, key: approvals}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.approval_path

    @staticmethod
    def approval(**overrides) -> dict:
        approval = {
            "target_game_version": "1.5.2",
            "observed_version": "1.5.1",
            "source": "tunklab",
            "decision": "compatible",
            "evidence": "reports/discovery/tunklab-shape.md#version-evidence",
            "approved_by": "phase-1 review",
            "approved_at": "2026-11-01",
        }
        approval.update(overrides)
        return approval

    # -- baseline ----------------------------------------------------------
    def test_matching_versions_pass(self):
        result = self.run_validator()
        statuses = self.statuses(result)
        for check_id in (
            "version_target_present",
            "version_compat_config",
            "version_compatibility_conclusion",
            "version_claimed_versions",
            "version_evidence_versions",
        ):
            self.assertEqual(statuses.get(check_id), "pass", f"{check_id}: {statuses.get(check_id)}")

    # -- claimed version injection (review fault injection) ----------------
    def test_claimed_version_999_fails(self):
        self.update_snapshot("tunklab", claimed_version="9.9.9")
        result = self.run_validator()
        statuses = self.statuses(result)
        self.assertEqual(statuses.get("version_claimed_versions"), "fail")
        self.assertIn("9.9.9", self.messages(result)["version_claimed_versions"])
        self.assertFalse(result["ok"])

    def test_missing_claimed_version_fails(self):
        self.update_snapshot("letools", claimed_version=None)
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_claimed_versions"), "fail")

    # -- dataset_meta.compatibility_conclusion ----------------------------
    def test_missing_compatibility_conclusion_fails(self):
        self.update_meta(compatibility_conclusion=None)
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_compatibility_conclusion"), "fail")

    def test_plain_compatible_text_is_not_an_approval(self):
        self.update_meta(compatibility_conclusion="compatible")
        result = self.run_validator()
        self.assertEqual(self.statuses(result).get("version_compatibility_conclusion"), "fail")
        self.assertIn("not JSON", self.messages(result)["version_compatibility_conclusion"])

    def test_unverified_decision_fails(self):
        conclusion = gates.compatibility_conclusion(decision="unverified")
        self.update_meta(compatibility_conclusion=json.dumps(conclusion))
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_compatibility_conclusion"), "fail")

    def test_conclusion_source_mismatch_without_approval_fails(self):
        conclusion = gates.compatibility_conclusion(
            sources={"letools": "1.5.2", "tunklab": "1.5.1"}
        )
        self.update_meta(compatibility_conclusion=json.dumps(conclusion))
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_compatibility_conclusion"), "fail")

    # -- page/module evidence ---------------------------------------------
    def test_module_version_conflict_fails(self):
        evidence = [
            {"kind": "js_global_variable", "name": "gameVersion", "raw_value": "Version 1.5.2"},
            {"kind": "module", "module_versions": {"1.5.1": 4}},
        ]
        self.update_snapshot("letools", version_evidence=json.dumps(evidence))
        result = self.run_validator()
        self.assertEqual(self.statuses(result).get("version_evidence_versions"), "fail")
        self.assertIn("1.5.1", self.messages(result)["version_evidence_versions"])

    def test_evidence_without_game_version_fails(self):
        evidence = [{"kind": "config_claim", "value": "nightly"}]
        self.update_snapshot("letools", version_evidence=json.dumps(evidence))
        result = self.run_validator()
        self.assertEqual(self.statuses(result).get("version_evidence_versions"), "fail")
        self.assertIn("no page/module game version", self.messages(result)["version_evidence_versions"])

    def test_missing_version_evidence_fails(self):
        self.update_snapshot("tunklab", version_evidence=None)
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_evidence_versions"), "fail")

    def test_invalid_json_version_evidence_fails(self):
        self.update_snapshot("letools", version_evidence="{not-json")
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_evidence_versions"), "fail")

    # -- explicit approval path -------------------------------------------
    def test_approved_version_pair_allows_mismatch(self):
        self.update_snapshot("tunklab", claimed_version="1.5.1")
        conclusion = gates.compatibility_conclusion(
            sources={"letools": "1.5.2", "tunklab": "1.5.1"}
        )
        self.update_meta(compatibility_conclusion=json.dumps(conclusion))
        self.update_snapshot(
            "tunklab",
            version_evidence=json.dumps(
                {
                    "claimed_version": "1.5.1",
                    "game_version_banners": [{"page": "/", "text": "1.5.1"}],
                    "module_versions": {},
                }
            ),
        )
        self.write_approvals([self.approval()])
        result = self.run_validator()
        statuses = self.statuses(result)
        for check_id in (
            "version_compatibility_conclusion",
            "version_claimed_versions",
            "version_evidence_versions",
        ):
            self.assertEqual(statuses.get(check_id), "pass", f"{check_id}: {statuses.get(check_id)}")
        detail = next(
            check for check in result["checks"] if check["check_id"] == "version_claimed_versions"
        )
        self.assertTrue(detail["detail"]["sources"]["tunklab"]["claimed_approved"])

    def test_approval_without_evidence_is_rejected(self):
        self.update_snapshot("tunklab", claimed_version="1.5.1")
        self.write_approvals([self.approval(evidence="")])
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_claimed_versions"), "fail")

    def test_approval_for_other_target_version_is_rejected(self):
        self.update_snapshot("tunklab", claimed_version="1.5.1")
        self.write_approvals([self.approval(target_game_version="1.5.0")])
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_claimed_versions"), "fail")

    def test_approval_file_with_invalid_json_fails(self):
        self.approval_path.write_text("{broken", encoding="utf-8")
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_compat_config"), "fail")

    def test_approved_pairs_key_is_supported(self):
        self.update_snapshot("tunklab", claimed_version="1.5.1")
        self.write_approvals([self.approval()], key="approved_pairs")
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_compat_config"), "pass")
        self.assertEqual(statuses.get("version_claimed_versions"), "pass")

    def test_checked_in_version_compat_file_is_loadable(self):
        project_root = Path(__file__).resolve().parents[3]
        compat_file = project_root / "data" / "mappings" / "version-compat.json"
        if not compat_file.exists():
            self.skipTest(f"no checked-in version compat file: {compat_file}")
        statuses = self.statuses(self.run_validator(approval_path=compat_file))
        self.assertEqual(statuses.get("version_compat_config"), "pass")

    # -- structural --------------------------------------------------------
    def test_no_snapshots_fails(self):
        conn = connect(self.tmp / "no-snapshots.sqlite")
        try:
            gates.apply_migrations(conn)
            from le_data.store import upsert_dataset_meta

            upsert_dataset_meta(conn, "gate-dataset", status="valid", target_game_version="1.5.2")
            conn.commit()
        finally:
            conn.close()
        statuses = self.statuses(self.run_validator(db_path=self.tmp / "no-snapshots.sqlite"))
        self.assertEqual(statuses.get("version_claimed_versions"), "fail")

    def test_missing_target_version_fails(self):
        self.update_meta(target_game_version=None)
        statuses = self.statuses(self.run_validator())
        self.assertEqual(statuses.get("version_target_present"), "fail")


if __name__ == "__main__":
    unittest.main()
