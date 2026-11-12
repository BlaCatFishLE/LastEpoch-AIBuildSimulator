"""Workflow contract round-trip tests (``le-workflow-v1`` / P2)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import contract

ROOT = Path(__file__).resolve().parents[3]
SCHEMA_DIR = ROOT / "tools" / "phase3" / "schemas" / "workflow-v1"
SHA = "a" * 64


def stage_request_payload() -> dict:
    return {
        "schema_version": "le-stage-request-v1",
        "stage_id": "resolve_skills",
        "case_id": "black-hole",
        "run_id": "run-1",
        "input_refs": [{"artifact_id": "calc", "path": "examples/x.json", "sha256": SHA}],
        "dependency_fingerprints": {"input": SHA},
        "parameters": {"main_skill": "bh2"},
    }


class StagePayloadRoundTripTest(unittest.TestCase):
    def test_stage_request_roundtrip(self):
        request = contract.StageRequest.from_dict(stage_request_payload())
        self.assertEqual(request.stage_id, "resolve_skills")
        self.assertEqual(request.to_dict()["input_refs"][0]["sha256"], SHA)

    def test_stage_request_unknown_version(self):
        payload = stage_request_payload()
        payload["schema_version"] = "le-stage-request-v2"
        with self.assertRaises(contract.ContractError):
            contract.StageRequest.from_dict(payload)

    def test_stage_result_roundtrip(self):
        result = contract.StageResult(
            stage_id="simulate",
            execution_status=contract.EXECUTION_COMPLETED,
            readiness=contract.PARTIAL,
            output_refs=[{"artifact_id": "result", "path": "reports/x/result.json", "sha256": SHA}],
            issues=[],
            metrics={"elapsed_ms": 1.5},
            cache=contract.make_cache_info(contract.CACHE_MISS, reason="first_run"),
        )
        payload = result.to_dict()
        again = contract.StageResult.from_dict(payload)
        self.assertEqual(again.execution_status, "completed")
        self.assertEqual(again.readiness, "partial")
        self.assertEqual(again.to_dict(), payload)

    def test_stage_result_waiting_requires_required_input(self):
        result = contract.StageResult(
            stage_id="freeze_input",
            execution_status=contract.EXECUTION_WAITING,
            readiness=contract.NEEDS_INPUT,
        )
        with self.assertRaises(contract.ContractError):
            result.to_dict()

    def test_stage_result_waiting_with_required_input_ok(self):
        result = contract.StageResult(
            stage_id="freeze_input",
            execution_status=contract.EXECUTION_WAITING,
            readiness=contract.NEEDS_INPUT,
            issues=[contract.make_issue("missing_input", "需要存档", required_input={"kind": "save"})],
        )
        payload = result.to_dict()
        self.assertEqual(payload["execution_status"], "waiting")

    def test_stage_result_invalid_execution_status(self):
        result = contract.StageResult(stage_id="x", execution_status="done", readiness=None)
        with self.assertRaises(contract.ContractError):
            result.to_dict()


class ResearchAndCommitRoundTripTest(unittest.TestCase):
    def test_research_task_roundtrip(self):
        task = contract.ResearchTask(
            task_id=contract.deterministic_id("research_task", {"gap": "g1"}),
            gap_id=contract.deterministic_id("gap", {"kind": "missing_mapping"}),
            input_fingerprint=SHA,
            question="节点 22 的交互未登记",
            source_refs=[{"kind": "dataset", "locator": "data/releases/x/data.sqlite#nodes"}],
            related_mechanism_refs=["bh-body-component"],
            researched_already=["ruleset v4"],
            affected={"components": ["hit"], "blocks_target": False},
            deliverable_kind="mapping",
            verification_required=["正向：重跑 resolve_build"],
        )
        payload = task.to_dict()
        again = contract.ResearchTask.from_dict(payload)
        self.assertEqual(again.to_dict(), payload)
        self.assertTrue(again.task_id.startswith("research_task-"))

    def test_research_task_invalid_fingerprint(self):
        task = contract.ResearchTask(
            task_id="research_task-1", gap_id="gap-1", input_fingerprint="short",
            question="q",
        )
        with self.assertRaises(contract.ContractError):
            task.to_dict()

    def test_research_proposal_roundtrip(self):
        proposal = contract.ResearchProposal(
            proposal_id="proposal-1", task_id="research_task-1",
            based_on_fingerprint=SHA, outcome=contract.OUTCOME_UNRESOLVED,
            mechanism_refs=["bh-body-component"],
        )
        payload = proposal.to_dict()
        self.assertEqual(contract.ResearchProposal.from_dict(payload).to_dict(), payload)

    def test_research_proposal_invalid_outcome(self):
        proposal = contract.ResearchProposal(
            proposal_id="p", task_id="t", based_on_fingerprint=SHA, outcome="maybe"
        )
        with self.assertRaises(contract.ContractError):
            proposal.to_dict()

    def test_commit_request_and_result_roundtrip(self):
        request = contract.CommitRequest(
            operation_id="op-1", attempt_id="attempt-1", operation_kind="write_run",
            input_fingerprint=SHA,
            candidate_refs=[{"artifact_id": "result", "path": "reports/x/result.json", "sha256": SHA}],
        )
        payload = request.to_dict()
        self.assertEqual(contract.CommitRequest.from_dict(payload).to_dict(), payload)
        result = contract.CommitResult(
            operation_id="op-1", attempt_id="attempt-1", status=contract.COMMIT_COMMITTED,
            committed_refs=payload["candidate_refs"], current_revision="rev-1",
        )
        result_payload = result.to_dict()
        self.assertEqual(contract.CommitResult.from_dict(result_payload).to_dict(), result_payload)

    def test_commit_invalid_operation_kind(self):
        request = contract.CommitRequest(
            operation_id="o", attempt_id="a", operation_kind="explode", input_fingerprint=SHA
        )
        with self.assertRaises(contract.ContractError):
            request.to_dict()

    def test_validation_result_roundtrip(self):
        result = contract.ValidationResult(
            kind="result", passed=False, accepted=["a"],
            errors=[{"id": "x", "message": "bad"}],
            checks=[{"id": "x", "status": "FAIL"}],
            next_actions=["re-run"],
        )
        payload = result.to_dict()
        self.assertEqual(contract.ValidationResult.from_dict(payload).to_dict(), payload)

    def test_validation_result_unknown_version(self):
        payload = contract.ValidationResult(kind="result", passed=True).to_dict()
        payload["schema_version"] = "le-validation-result-v2"
        with self.assertRaises(contract.ContractError):
            contract.ValidationResult.from_dict(payload)

    def test_pipeline_state_roundtrip(self):
        state = contract.PipelineState(
            case_id="black-hole", run_id="run-1",
            stages=[contract.StageState(stage_id="simulate",
                                        execution_status="completed", readiness="ready")],
        )
        payload = state.to_dict()
        self.assertEqual(contract.PipelineState.from_dict(payload).to_dict(), payload)


class HelperTest(unittest.TestCase):
    def test_dumps_loads_and_schema_check(self):
        payload = {"schema_version": "le-stage-result-v1", "value": 1}
        text = contract.dumps(payload)
        self.assertEqual(contract.loads(text, expected_schemas=[contract.SCHEMA_STAGE_RESULT]), payload)
        with self.assertRaises(contract.ContractError):
            contract.loads(text, expected_schemas=[contract.SCHEMA_STAGE_REQUEST])

    def test_fingerprint_is_deterministic(self):
        left = contract.fingerprint({"b": 2, "a": [1]})
        right = contract.fingerprint({"a": [1], "b": 2})
        self.assertEqual(left, right)
        self.assertRegex(left, r"^[0-9a-f]{64}$")

    def test_artifact_ref_helpers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "reports" / "x" / "result.json"
            target.parent.mkdir(parents=True)
            target.write_text("{}", encoding="utf-8")
            ref = contract.make_artifact_ref(target, root=root, schema_version="x")
            self.assertEqual(ref["path"], "reports/x/result.json")
            self.assertTrue(contract.verify_artifact_ref(ref, root=root)["ok"])
            target.write_text('{"changed": true}', encoding="utf-8")
            self.assertEqual(
                contract.verify_artifact_ref(ref, root=root)["reason"], "sha256_mismatch"
            )

    def test_normalize_relative_rejects_outside_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(contract.ContractError):
                contract.normalize_relative("C:/outside/x.json", root=root)


class SchemaFilesTest(unittest.TestCase):
    EXPECTED = {
        "resolve-build-request-v1.json",
        "resolve-build-result-v1.json",
        "simulate-request-v1.json",
        "simulate-result-v1.json",
        "validation-result-v1.json",
        "analysis-plan-v1.json",
        "coverage-v1.json",
        "research-queue-v1.json",
    }

    def test_schema_files_present_and_valid(self):
        files = {path.name for path in SCHEMA_DIR.glob("*.json")}
        # other P2 workstreams may add sibling schemas (research hand-off); only
        # require the eight workflow-domain schemas owned by this task.
        missing = self.EXPECTED - files
        self.assertEqual(missing, set())
        for name in sorted(files):
            schema = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
            self.assertEqual(schema.get("$schema"), "https://json-schema.org/draft/2020-12/schema")
            self.assertTrue(str(schema.get("$id", "")).startswith("le-workflow/"))
            self.assertEqual(schema.get("type"), "object")
            self.assertIn("required", schema)


if __name__ == "__main__":
    unittest.main()
