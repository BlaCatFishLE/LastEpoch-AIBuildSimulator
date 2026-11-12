"""Analysis artifact writer tests (analysis-plan / coverage / research-queue)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import artifacts, contract

ROOT = Path(__file__).resolve().parents[3]


def synthetic_resolve_result(case_id: str = "black-hole") -> dict:
    checklist_items = []
    for item in __import__("le_calc.workflow.checklist", fromlist=["checklist"]).checklist():
        checklist_items.append(
            {**item, "status": "supported", "reason": "test", "mechanisms": [],
             "model_blocks": []}
        )
    plan = {
        "schema_version": contract.SCHEMA_ANALYSIS_PLAN,
        "plan_id": contract.deterministic_id("analysis_plan", {"case_id": case_id}),
        "case_id": case_id,
        "main_skill": {"ability_id": "bh2", "name": "Black Hole", "model_id": "bh-event"},
        "checklist": checklist_items,
        "components": ["bh-body-fire (fire/body)"],
        "skill_template": None,
        "knowledge_index": {"knowledge_dir": "knowledge", "index_path": "data/cache/knowledge.sqlite",
                            "index_digest": None},
        "scope_sources": {"skill_trees": []},
        "counts": {"checklist_items": 9, "bound_mechanisms": 0, "gaps": 2},
    }
    gap = {
        "gap_id": contract.deterministic_id("gap", {"kind": "missing_mechanism_mapping"}),
        "kind": "missing_mechanism_mapping",
        "severity": "warning",
        "blocking": False,
        "question": "查询项 'node:bh2:99' 在知识库中没有匹配机制条目",
        "related_ids": ["node:bh2:99"],
        "source_refs": [{"kind": "case_input", "locator": "examples/black-hole/inputs/calculation-input.affix-db.json"}],
        "related_mechanism_refs": [],
        "researched_already": [],
        "affected": {"components": ["body"], "blocks_target": False},
        "deliverable_kind": "mapping",
        "verification_required": ["正向：重跑 resolve_build 后该 gap 消失"],
    }
    coverage = {
        "schema_version": contract.SCHEMA_COVERAGE,
        "case_id": case_id,
        "scope": {
            "case_id": case_id,
            "main_skill": {"ability_id": "bh2", "model_id": "bh-event"},
            "selected_inputs": [{"role": "calculation_input",
                                 "path": "examples/black-hole/inputs/calculation-input.affix-db.json",
                                 "sha256": "a" * 64}],
            "calculation_input": "examples/black-hole/inputs/calculation-input.affix-db.json",
            "snapshot": "examples/black-hole/snapshot/input-snapshot.affix-db.json",
            "skill_trees": [],
            "passive_tree": {},
            "equipment_items": 29,
            "idols": 0,
            "blessings": 0,
            "excluded_features": [],
        },
        "included": [{"id": "bh-body-component", "entry_kind": "mechanism",
                      "reason": "test", "evidence_state": "confirmed",
                      "implementation_state": "validated", "components": ["body"]}],
        "excluded_with_reason": [],
        "unresolved": [],
        "out_of_scope": [],
        "counts": {"included": 1, "excluded_with_reason": 0, "unresolved": 0, "out_of_scope": 0},
    }
    return {
        "schema_version": contract.SCHEMA_RESOLVE_BUILD_RESULT,
        "case_id": case_id,
        "main_skill": {"ability_id": "bh2", "name": "Black Hole"},
        "model_id": "bh-event",
        "compatibility_baseline": {},
        "analysis_plan": plan,
        "bound_mechanisms": [],
        "coverage": coverage,
        "gaps": [gap],
        "issues": [],
        "readiness": contract.PARTIAL,
        "input_fingerprint": "b" * 64,
        "metrics": {"gaps": 1},
    }


class ArtifactsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        scratch = ROOT / ".scratch"
        scratch.mkdir(exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(prefix="p2-artifacts-", dir=scratch)
        cls.out = Path(cls.tmp.name) / "analysis"
        cls.manifest = artifacts.write_analysis_artifacts(
            synthetic_resolve_result(), cls.out, root=ROOT
        )

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_writes_three_artifacts_with_schema_versions(self):
        self.assertEqual(set(self.manifest["files"]), set(artifacts.ARTIFACT_NAMES))
        payloads = artifacts.read_analysis_artifacts(self.out, root=ROOT)
        self.assertEqual(payloads["analysis-plan.json"]["schema_version"],
                         contract.SCHEMA_ANALYSIS_PLAN)
        self.assertEqual(payloads["coverage.json"]["schema_version"], contract.SCHEMA_COVERAGE)
        self.assertEqual(payloads["research-queue.json"]["schema_version"],
                         contract.SCHEMA_RESEARCH_QUEUE)
        self.assertEqual(len(self.manifest["artifact_refs"]), 3)
        for ref in self.manifest["artifact_refs"]:
            self.assertRegex(ref["sha256"], r"^[0-9a-f]{64}$")
            self.assertFalse(Path(ref["path"]).is_absolute())
            self.assertTrue(contract.verify_artifact_ref(ref, root=ROOT)["ok"])

    def test_research_queue_maps_gaps_to_contract_tasks(self):
        payloads = artifacts.read_analysis_artifacts(self.out, root=ROOT)
        queue = payloads["research-queue.json"]
        self.assertEqual(len(queue["entries"]), 1)
        entry = queue["entries"][0]
        self.assertEqual(entry["schema_version"], contract.SCHEMA_RESEARCH_TASK)
        self.assertTrue(entry["task_id"].startswith("research_task-"))
        self.assertEqual(entry["input_fingerprint"], "b" * 64)
        task = contract.ResearchTask.from_dict(entry)
        self.assertTrue(task.to_dict()["question"])
        self.assertEqual(queue["summary"]["tasks"], 1)

    def test_no_machine_absolute_paths(self):
        payloads = artifacts.read_analysis_artifacts(self.out, root=ROOT)
        for payload in payloads.values():
            self.assertEqual(artifacts.find_absolute_paths(payload), [])

    def test_refuses_overwrite_by_default(self):
        with self.assertRaises(contract.ContractError):
            artifacts.write_analysis_artifacts(synthetic_resolve_result(), self.out, root=ROOT)

    def test_rewrite_is_deterministic_with_overwrite(self):
        first = {
            name: (self.out / name).read_bytes() for name in artifacts.ARTIFACT_NAMES
        }
        manifest = artifacts.write_analysis_artifacts(
            synthetic_resolve_result(), self.out, root=ROOT, overwrite=True
        )
        second = {name: (self.out / name).read_bytes() for name in artifacts.ARTIFACT_NAMES}
        self.assertEqual(first, second)
        self.assertEqual(manifest["counts"]["research_tasks"], 1)

    def test_missing_plan_raises(self):
        with self.assertRaises(contract.ContractError):
            artifacts.write_analysis_artifacts({"case_id": "x"}, Path(self.tmp.name) / "bad", root=ROOT)

    def test_find_absolute_paths_detects_machine_paths(self):
        payload = {"a": ["C:\\Users\\x\\y", "/home/user/x", "reports/x/result.json"]}
        found = artifacts.find_absolute_paths(payload)
        self.assertEqual(len(found), 2)


if __name__ == "__main__":
    unittest.main()
