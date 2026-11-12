"""P5: update_pointer atomic whitelisted pointer publication."""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from le_calc.workflow import atomic_io, commit, contract


class UpdatePointerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p5-pointer-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.pointer_target = "data/current.json"
        self.pointer = self.root / self.pointer_target
        self.pointer.parent.mkdir(parents=True, exist_ok=True)
        self.old_pointer = {
            "dataset_id": "phase1-dataset-003",
            "database": "data/releases/old/data.sqlite",
            "file_sha256": "a" * 64,
        }
        self.pointer.write_text(
            json.dumps(self.old_pointer, ensure_ascii=False), encoding="utf-8"
        )
        self.candidate = self.root / "candidate" / "current.json"
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        self.new_pointer = {
            "dataset_id": "phase1-dataset-004",
            "database": "data/releases/new/data.sqlite",
            "file_sha256": "b" * 64,
        }
        self.candidate.write_text(
            json.dumps(self.new_pointer, ensure_ascii=False), encoding="utf-8"
        )
        self.old_sha = atomic_io.sha256_file(self.pointer)
        self.input_fp = commit.compute_input_fingerprint(
            {"operation": "update_pointer", "old": self.old_sha, "new": self.new_pointer}
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _update(self, *, operation_id: str = "pointer-op", fingerprint: str | None = None,
                candidate: Path | None = None, expected_sha: str | None = None,
                target: str | None = None, expected_revision: str | None = None):
        return commit.update_pointer(
            candidate or self.candidate,
            target or self.pointer_target,
            operation_id=operation_id,
            attempt_id="attempt-1",
            input_fingerprint=fingerprint or self.input_fp,
            expected_current_sha256=expected_sha if expected_sha is not None else self.old_sha,
            expected_current_revision=expected_revision,
            operations_root=self.ops,
            root=self.root,
        )


class UpdatePointerLifecycleTest(UpdatePointerTestBase):
    def test_pointer_committed_atomically_and_recorded(self) -> None:
        result = self._update()
        self.assertEqual(result.status, "committed", result.to_dict())
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.new_pointer)
        self.assertFalse((self.pointer.parent / (self.pointer.name + ".tmp")).exists())
        record = commit.read_operation_record("pointer-op", self.ops)
        self.assertEqual(record["operation_kind"], "update_pointer")
        self.assertEqual(record["published_refs"][0]["sha256"], atomic_io.sha256_file(self.pointer))
        self.assertTrue(self.pointer.is_file())

    def test_same_operation_same_input_replayed_without_rewrite(self) -> None:
        first = self._update()
        self.assertEqual(first.status, "committed")
        inode_before = self.pointer.stat().st_ino
        content_before = self.pointer.read_bytes()
        second = self._update()
        self.assertEqual(second.status, "replayed", second.to_dict())
        self.assertEqual(self.pointer.read_bytes(), content_before)
        self.assertEqual(self.pointer.stat().st_ino, inode_before)

    def test_same_operation_different_input_conflict(self) -> None:
        self._update()
        content = self.pointer.read_bytes()
        second = self._update(fingerprint=commit.compute_input_fingerprint({"other": 1}))
        self.assertEqual(second.status, "conflict", second.to_dict())
        self.assertEqual(self.pointer.read_bytes(), content)

    def test_expected_current_sha_mismatch_rejected_before_write(self) -> None:
        original = self.pointer.read_bytes()
        result = self._update(expected_sha="0" * 64)
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("current_revision_mismatch", [i["code"] for i in result.issues])
        self.assertEqual(self.pointer.read_bytes(), original)
        self.assertIsNone(commit.read_operation_record("pointer-op", self.ops))
        self.assertFalse(commit._intent_path(self.ops, "pointer-op").exists())

    def test_expected_revision_identity_is_accepted(self) -> None:
        result = self._update(expected_sha="", expected_revision="phase1-dataset-003")
        self.assertEqual(result.status, "committed", result.to_dict())
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.new_pointer)

    def test_missing_expected_current_rejected(self) -> None:
        result = commit.commit_operation(
            {
                "operation_id": "pointer-op",
                "attempt_id": "a1",
                "operation_kind": "update_pointer",
                "input_fingerprint": self.input_fp,
                "candidate_refs": [
                    {"kind": "file", "candidate_path": str(self.candidate),
                     "path": self.pointer_target}
                ],
            },
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pointer_expected_current_required",
                      [i["code"] for i in result.issues])
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.old_pointer)

    def test_target_missing_rejected(self) -> None:
        self.pointer.unlink()
        result = self._update()
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pointer_target_missing", [i["code"] for i in result.issues])
        self.assertFalse(self.pointer.exists())

    def test_non_whitelist_target_rejected(self) -> None:
        result = self._update(target="data/not-a-pointer.json")
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pointer_target_not_allowed", [i["code"] for i in result.issues])
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.old_pointer)

    def test_invalid_pointer_document_rejected(self) -> None:
        self.candidate.write_text("{not-json", encoding="utf-8")
        result = self._update(candidate=self.candidate, expected_sha=self.old_sha,
                              fingerprint=self.input_fp)
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("invalid_pointer_document", [i["code"] for i in result.issues])
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.old_pointer)

    def test_build_pointer_whitelist(self) -> None:
        build_target = "data/builds/letools/o3ZRm9my/current.json"
        build_pointer = self.root / build_target
        build_pointer.parent.mkdir(parents=True, exist_ok=True)
        build_pointer.write_text(json.dumps({"revision_id": "rev-old"}), encoding="utf-8")
        old_sha = atomic_io.sha256_file(build_pointer)
        result = commit.update_pointer(
            self.candidate,
            build_target,
            operation_id="build-pointer-op",
            attempt_id="a1",
            input_fingerprint=commit.compute_input_fingerprint({"build": "new"}),
            expected_current_sha256=old_sha,
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(result.status, "committed", result.to_dict())
        self.assertEqual(json.loads(build_pointer.read_text(encoding="utf-8")), self.new_pointer)

    def test_record_failure_then_retry_with_new_candidate_recovers(self) -> None:
        with mock.patch.object(
            commit, "_write_operation_record",
            side_effect=OSError("injected pointer record failure"),
        ):
            first = self._update()
        self.assertEqual(first.status, "failed", first.to_dict())
        self.assertIn("record_write_failed", [i["code"] for i in first.issues])
        replaced_bytes = self.pointer.read_bytes()
        self.assertTrue(commit._intent_path(self.ops, "pointer-op").is_file())
        self.assertIsNone(commit.read_operation_record("pointer-op", self.ops))
        # New retry candidate differs from the originally published pointer.
        retry_candidate = self.root / "candidate" / "retry.json"
        retry_candidate.write_text(
            json.dumps({"dataset_id": "retry-different", "file_sha256": "c" * 64}),
            encoding="utf-8",
        )
        retry = self._update(candidate=retry_candidate, fingerprint=self.input_fp)
        self.assertEqual(retry.status, "replayed", retry.to_dict())
        self.assertIn("recovered_record_after_publish", [i["code"] for i in retry.issues])
        self.assertEqual(self.pointer.read_bytes(), replaced_bytes)
        record = commit.read_operation_record("pointer-op", self.ops)
        self.assertEqual(record["published_refs"][0]["sha256"],
                         hashlib.sha256(replaced_bytes).hexdigest())
        self.assertFalse(commit._intent_path(self.ops, "pointer-op").exists())

    def test_stale_pointer_intent_before_publish_is_discarded(self) -> None:
        # Simulate crash after intent write but before the pointer replacement:
        # target is still the previous pointer, intent describes the new one.
        intent = {
            "schema_version": "le-operation-intent-v1",
            "operation_id": "pointer-op",
            "attempt_id": "stale",
            "operation_kind": "update_pointer",
            "input_fingerprint": self.input_fp,
            "candidate_refs": [
                {
                    "candidate_id": "pointer",
                    "kind": "file",
                    "path": self.pointer_target,
                    "sha256": atomic_io.sha256_file(self.candidate),
                    "artifact_id": "pointer",
                }
            ],
            "previous_sha256": self.old_sha,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        intent_path = commit._intent_path(self.ops, "pointer-op")
        intent_path.parent.mkdir(parents=True, exist_ok=True)
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
        result = self._update()
        self.assertEqual(result.status, "committed", result.to_dict())
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.new_pointer)
        self.assertFalse(intent_path.exists())


    def test_same_operation_concurrent_pointer_one_committed_one_replayed(self) -> None:
        barrier = threading.Barrier(2)
        results = {}

        def worker(label: str) -> None:
            barrier.wait(timeout=10)
            results[label] = self._update(operation_id="pointer-concurrent")

        threads = [
            threading.Thread(target=worker, args=("a",)),
            threading.Thread(target=worker, args=("b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "replayed"], statuses)
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), self.new_pointer)
        self.assertTrue((self.ops / "pointer-concurrent.json").is_file())

    def test_atomic_replace_exception_leaves_pointer_unchanged(self) -> None:
        before = self.pointer.read_bytes()
        with mock.patch.object(
            atomic_io, "atomic_replace_file", side_effect=OSError("injected replace failure")
        ):
            result = self._update()
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("publish_failed", [i["code"] for i in result.issues])
        self.assertEqual(self.pointer.read_bytes(), before)
        self.assertFalse(commit._intent_path(self.ops, "pointer-op").exists())
        self.assertIsNone(commit.read_operation_record("pointer-op", self.ops))


    def test_target_corrupt_pointer_rejected(self) -> None:
        self.pointer.write_text("{not-json", encoding="utf-8")
        result = self._update()
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pointer_target_corrupt", [i["code"] for i in result.issues])
        self.assertEqual(self.pointer.read_text(encoding="utf-8"), "{not-json")

    def test_pointer_lose_race_only_one_committed(self) -> None:
        candidate_b = self.root / "candidate" / "b.json"
        candidate_b.write_text(json.dumps({"dataset_id": "phase1-dataset-005"}), encoding="utf-8")
        barrier = threading.Barrier(2)
        results = {}

        def worker(label: str, candidate: Path) -> None:
            barrier.wait(timeout=10)
            results[label] = commit.update_pointer(
                candidate,
                self.pointer_target,
                operation_id="pointer-race-" + label,
                attempt_id=label,
                input_fingerprint=commit.compute_input_fingerprint({"candidate": label}),
                expected_current_sha256=self.old_sha,
                operations_root=self.ops,
                root=self.root,
            )

        threads = [
            threading.Thread(target=worker, args=("a", self.candidate)),
            threading.Thread(target=worker, args=("b", candidate_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "failed"], statuses)
        winner = next(label for label, result in results.items() if result.status == "committed")
        expected = self.new_pointer if winner == "a" else {"dataset_id": "phase1-dataset-005"}
        self.assertEqual(json.loads(self.pointer.read_text(encoding="utf-8")), expected)
        loser = next(label for label, result in results.items() if result.status == "failed")
        self.assertTrue((self.ops / f"pointer-race-{winner}.json").is_file())
        self.assertFalse((self.ops / f"pointer-race-{loser}.json").exists())


if __name__ == "__main__":
    unittest.main()
