"""FileStateStore tests (``le-pipeline-state-v1`` / P3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import contract, statestore

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"


def make_result(stage_id: str, outputs: list[Path], *, root: Path) -> contract.StageResult:
    refs = [
        contract.make_artifact_ref(path, root=root, artifact_id=f"{stage_id}:{path.name}")
        for path in outputs
    ]
    return contract.StageResult(
        stage_id=stage_id,
        execution_status=contract.EXECUTION_COMPLETED,
        readiness=contract.READY,
        output_refs=refs,
        metrics={"elapsed_ms": 1.0},
        cache={"status": contract.CACHE_DISABLED, "reason": "test"},
    )


class FileStateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="p3-state-", dir=SCRATCH)
        self.run_dir = Path(self.tmp.name) / "run"
        self.run_dir.mkdir()
        self.store = statestore.FileStateStore(self.run_dir, root=ROOT)
        self.state = contract.PipelineState(case_id="black-hole", run_id="run-1")

    def tearDown(self):
        self.tmp.cleanup()

    def _stage(self, name: str, content: str) -> Path:
        path = self.run_dir / f"{name}.json"
        path.write_text(content, encoding="utf-8")
        return path

    def test_roundtrip_and_atomic_save(self):
        output = self._stage("a", "{}")
        self.store.record(self.state, make_result("freeze_input", [output], root=ROOT))
        ref = self.store.save(self.state)
        self.assertTrue(self.store.path.is_file())
        self.assertEqual(ref["schema_version"], contract.SCHEMA_PIPELINE_STATE)
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.to_dict(), self.state.to_dict())
        self.assertEqual(loaded.stages[0].stage_id, "freeze_input")

    def test_load_missing_returns_none(self):
        self.assertIsNone(self.store.load())

    def test_corrupt_json_raises(self):
        self.store.path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(statestore.StateStoreError):
            self.store.load()

    def test_unknown_schema_raises(self):
        self.store.path.write_text(
            json.dumps({"schema_version": "le-pipeline-state-v2", "case_id": "x", "run_id": "r"}),
            encoding="utf-8",
        )
        with self.assertRaises(statestore.StateStoreError):
            self.store.load()

    def test_verify_detects_missing_output_and_invalidates_downstream(self):
        first = self._stage("freeze", "{}")
        second = self._stage("parse", "{}")
        third = self._stage("snapshot", "{}")
        self.store.record(self.state, make_result("freeze_input", [first], root=ROOT))
        self.store.record(self.state, make_result("parse_input", [second], root=ROOT))
        self.store.record(self.state, make_result("prepare_snapshot", [third], root=ROOT))
        second.unlink()
        report = self.store.resume_plan(self.state)
        self.assertEqual(report["valid"], ["freeze_input"])
        self.assertEqual([item["stage_id"] for item in report["invalidated"]], ["parse_input"])
        self.assertEqual(report["downstream_invalidated"], ["prepare_snapshot"])
        self.assertEqual(report["restart_from"], "parse_input")

    def test_verify_detects_sha_mismatch(self):
        output = self._stage("freeze", "{}")
        self.store.record(self.state, make_result("freeze_input", [output], root=ROOT))
        output.write_text('{"tampered":true}', encoding="utf-8")
        report = self.store.resume_plan(self.state)
        self.assertEqual(report["valid"], [])
        self.assertEqual(report["invalidated"][0]["reason"], "sha256_mismatch")

    def test_record_replaces_existing_stage(self):
        output = self._stage("a", "{}")
        self.store.record(self.state, make_result("freeze_input", [output], root=ROOT))
        output2 = self._stage("a2", "{}")
        self.store.record(self.state, make_result("freeze_input", [output2], root=ROOT))
        self.assertEqual(len(self.state.stages), 1)
        self.assertTrue(self.state.stages[0].output_refs[0]["path"].endswith("a2.json"))

    def test_invalidate_removes_listed_stages(self):
        first = self._stage("freeze", "{}")
        second = self._stage("parse", "{}")
        self.store.record(self.state, make_result("freeze_input", [first], root=ROOT))
        self.store.record(self.state, make_result("parse_input", [second], root=ROOT))
        removed = self.store.invalidate(self.state, ["freeze_input", "parse_input"])
        self.assertEqual(removed, ["freeze_input", "parse_input"])
        self.assertEqual(self.state.stages, [])

    def test_non_completed_stage_invalidates(self):
        output = self._stage("a", "{}")
        result = make_result("freeze_input", [output], root=ROOT)
        result.execution_status = contract.EXECUTION_WAITING
        self.store.record(self.state, result)
        report = self.store.resume_plan(self.state)
        self.assertEqual(report["valid"], [])
        self.assertEqual(report["invalidated"][0]["stage_id"], "freeze_input")


if __name__ == "__main__":
    unittest.main()
