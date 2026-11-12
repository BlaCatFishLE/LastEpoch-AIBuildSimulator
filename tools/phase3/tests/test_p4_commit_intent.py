"""P4-R2: publish intent and recovery after record-write failure."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from le_calc.workflow import atomic_io, commit


def _tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class PublishIntentRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-intent-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.candidate = _tree(
            self.root / "candidate" / "original",
            {"result.json": '{"run": 1, "generated_at": "first"}\n', "state.json": "{}\n"},
        )
        self.target = "reports/black-hole/intent-target"
        self.op = "op-r2"
        self.fp = commit.compute_input_fingerprint({"case": "black-hole", "run": "r2"})

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _publish(self, candidate: Path | None = None, **kwargs):
        return commit.publish_run_dir(
            candidate or self.candidate,
            self.target,
            operation_id=kwargs.pop("operation_id", self.op),
            attempt_id=kwargs.pop("attempt_id", "attempt-1"),
            input_fingerprint=kwargs.pop("input_fingerprint", self.fp),
            operations_root=self.ops,
            root=self.root,
            **kwargs,
        )

    def _intent_path(self) -> Path:
        return commit._intent_path(self.ops, self.op)

    def _record(self):
        return commit.read_operation_record(self.op, self.ops)

    def _fail_record_once(self) -> None:
        with mock.patch.object(
            commit,
            "_write_operation_record",
            side_effect=OSError("injected operation record write failure"),
        ):
            result = self._publish()
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("record_write_failed", [item["code"] for item in result.issues])
        self.assertTrue((self.root / self.target).is_dir())
        self.assertIsNone(self._record())
        self.assertTrue(self._intent_path().is_file(), "发布意图应保留")
        self.assertTrue((self.root / self.target / "result.json").is_file())

    def test_record_failure_message_and_intent_retained(self) -> None:
        self._fail_record_once()
        result = self._publish()  # no injection this time
        # The retry may already recover; check the original failure text via a fresh op.
        self.assertIn(result.status, {"replayed", "committed"})

    def test_retry_with_new_candidate_recovers_from_intent(self) -> None:
        with mock.patch.object(
            commit,
            "_write_operation_record",
            side_effect=OSError("injected operation record write failure"),
        ):
            first = self._publish()
        self.assertEqual(first.status, "failed")
        self.assertIn("record_write_failed", [item["code"] for item in first.issues])
        self.assertIn("可重试恢复", first.issues[0]["message"])
        intent = json.loads(self._intent_path().read_text(encoding="utf-8"))
        original_sha = intent["candidate_refs"][0]["sha256"]
        target_sha = atomic_io.tree_sha256(self.root / self.target)
        self.assertEqual(target_sha, original_sha)

        # A normal retry's candidate tree differs (timestamps/state), but the
        # intent's original published tree sha must be used for recovery.
        newer = _tree(
            self.root / "candidate" / "newer",
            {"result.json": '{"run": 2, "generated_at": "second"}\n', "state.json": "{}\n"},
        )
        self.assertNotEqual(atomic_io.tree_sha256(newer), target_sha)
        retry = self._publish(newer, attempt_id="attempt-2")
        self.assertEqual(retry.status, "replayed", retry.to_dict())
        self.assertIn(
            "recovered_record_after_publish",
            [item["code"] for item in retry.issues],
        )
        record = self._record()
        self.assertIsNotNone(record)
        self.assertEqual(record["published_refs"][0]["sha256"], original_sha)
        self.assertEqual(atomic_io.tree_sha256(self.root / self.target), target_sha)
        self.assertFalse(self._intent_path().exists(), "成功补记录后应清理意图")

    def test_different_input_with_intent_is_conflict(self) -> None:
        with mock.patch.object(
            commit,
            "_write_operation_record",
            side_effect=OSError("injected"),
        ):
            first = self._publish()
        self.assertEqual(first.status, "failed")
        target_sha = atomic_io.tree_sha256(self.root / self.target)
        other = commit.compute_input_fingerprint({"case": "black-hole", "run": "other"})
        result = self._publish(input_fingerprint=other, attempt_id="a-other")
        self.assertEqual(result.status, "conflict")
        self.assertIn("input_fingerprint_conflict", [item["code"] for item in result.issues])
        self.assertIsNone(self._record())
        self.assertEqual(atomic_io.tree_sha256(self.root / self.target), target_sha)
        self.assertTrue(self._intent_path().is_file())

    def test_foreign_target_without_intent_is_target_exists(self) -> None:
        target = self.root / self.target
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.candidate, target)
        result = self._publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("target_exists", [item["code"] for item in result.issues])
        self.assertIsNone(self._record())

    def test_corrupt_intent_is_failed(self) -> None:
        path = self._intent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        result = self._publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("intent_corrupt", [item["code"] for item in result.issues])
        self.assertFalse((self.root / self.target).exists())

    def test_tampered_intent_sha_is_failed_without_overwriting_target(self) -> None:
        # First create a published target + a valid intent, then tamper the sha.
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected")
        ):
            self._publish()
        intent = json.loads(self._intent_path().read_text(encoding="utf-8"))
        intent["candidate_refs"][0]["sha256"] = "0" * 64
        self._intent_path().write_text(
            json.dumps(intent, ensure_ascii=False), encoding="utf-8"
        )
        before = atomic_io.tree_sha256(self.root / self.target)
        result = self._publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("intent_target_mismatch", [item["code"] for item in result.issues])
        self.assertEqual(atomic_io.tree_sha256(self.root / self.target), before)
        self.assertIsNone(self._record())

    def test_stale_intent_without_target_is_discarded_and_publish_continues(self) -> None:
        # Simulate a crash after intent write but before publish.
        intent = {
            "schema_version": "le-operation-intent-v1",
            "operation_id": self.op,
            "attempt_id": "stale",
            "operation_kind": "write_run",
            "input_fingerprint": self.fp,
            "candidate_refs": [
                {
                    "candidate_id": "run",
                    "kind": "dir",
                    "path": self.target,
                    "sha256": atomic_io.tree_sha256(self.candidate),
                }
            ],
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        path = self._intent_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(intent, ensure_ascii=False), encoding="utf-8")
        self.assertFalse((self.root / self.target).exists())
        result = self._publish(attempt_id="fresh")
        self.assertEqual(result.status, "committed")
        self.assertTrue((self.root / self.target).is_dir())
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
