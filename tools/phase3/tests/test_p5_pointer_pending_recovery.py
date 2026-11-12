"""P5-R3: pending pointer intent recovery before advancing A -> B."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from le_calc.workflow import atomic_io, commit


class PointerPendingRecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p5-pending-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.counter = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, rel: str, payload: dict[str, object]) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _candidate(self, label: str, payload: dict[str, object]) -> Path:
        self.counter += 1
        return self._write(f"candidate/{self.counter}-{label}.json", payload)

    def _fingerprint(self, label: str) -> str:
        return commit.compute_input_fingerprint({"pending-test": label})

    def _update(self, candidate: Path, target: str, operation_id: str,
                expected_sha: str | None = None, fingerprint: str | None = None):
        return commit.update_pointer(
            candidate,
            target,
            operation_id=operation_id,
            attempt_id=operation_id + "-attempt",
            input_fingerprint=fingerprint or self._fingerprint(operation_id),
            expected_current_sha256=expected_sha,
            operations_root=self.ops,
            root=self.root,
        )

    def _scenario(self, target: str, old: dict, a_payload: dict, b_payload: dict):
        pointer = self._write(target, old)
        old_sha = atomic_io.sha256_file(pointer)
        a = self._candidate("a", a_payload)
        b = self._candidate("b", b_payload)
        with mock.patch.object(
            commit,
            "_write_operation_record",
            side_effect=OSError("injected A record failure"),
        ):
            first_a = self._update(a, target, "op-A", old_sha)
        self.assertEqual(first_a.status, "failed", first_a.to_dict())
        self.assertIn("record_write_failed", [i["code"] for i in first_a.issues])
        self.assertTrue(commit._intent_path(self.ops, "op-A").is_file())
        self.assertIsNone(commit.read_operation_record("op-A", self.ops))
        sha_a = atomic_io.sha256_file(pointer)
        b_result = self._update(b, target, "op-B", sha_a)
        return pointer, a, b, old_sha, sha_a, b_result

    def _assert_recovered(self, pointer, target, a, old_sha, sha_a, b_result, b_payload):
        self.assertEqual(b_result.status, "committed", b_result.to_dict())
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), b_payload)
        a_record = commit.read_operation_record("op-A", self.ops)
        self.assertIsNotNone(a_record, "A record should be recovered before B advances")
        self.assertEqual(a_record["published_refs"][0]["sha256"], sha_a)
        self.assertFalse(commit._intent_path(self.ops, "op-A").exists())
        b_record = commit.read_operation_record("op-B", self.ops)
        self.assertIsNotNone(b_record)
        # Old A operation replay must not revert the pointer to A.
        replay = self._update(a, target, "op-A", old_sha)
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertIn("pointer_advanced_after_commit", [i["code"] for i in replay.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), b_payload)

    def test_dataset_record_failure_then_b_advances_recovers_a(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old"}
        a_payload = {"dataset_id": "dataset-A"}
        b_payload = {"dataset_id": "dataset-B"}
        pointer, a, _b, old_sha, sha_a, b_result = self._scenario(
            target, old, a_payload, b_payload
        )
        self._assert_recovered(pointer, target, a, old_sha, sha_a, b_result, b_payload)

    def test_build_record_failure_then_b_advances_recovers_a(self) -> None:
        target = "data/builds/letools/o3ZRm9my/current.json"
        old = {"build_id": "letools:o3ZRm9my", "revision_id": "rev-old"}
        a_payload = {"build_id": "letools:o3ZRm9my", "revision_id": "rev-A"}
        b_payload = {"build_id": "letools:o3ZRm9my", "revision_id": "rev-B"}
        pointer, a, _b, old_sha, sha_a, b_result = self._scenario(
            target, old, a_payload, b_payload
        )
        self._assert_recovered(pointer, target, a, old_sha, sha_a, b_result, b_payload)

    def test_stale_intent_blocks_b_advance(self) -> None:
        target = "data/current.json"
        pointer = self._write(target, {"dataset_id": "dataset-old"})
        current_sha = atomic_io.sha256_file(pointer)
        stale = {
            "schema_version": "le-operation-intent-v1",
            "operation_id": "op-stale",
            "attempt_id": "stale",
            "operation_kind": "update_pointer",
            "input_fingerprint": self._fingerprint("stale"),
            "candidate_refs": [
                {"candidate_id": "pointer", "kind": "file", "path": target,
                 "sha256": "0" * 64}
            ],
            "previous_sha256": "1" * 64,
            "created_at": "2026-01-01T00:00:00+00:00",
        }
        path = commit._intent_path(self.ops, "op-stale")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(stale), encoding="utf-8")
        b = self._candidate("stale-b", {"dataset_id": "dataset-B"})
        result = self._update(b, target, "op-B", current_sha)
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pending_pointer_recovery", [i["code"] for i in result.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "dataset-old"})
        self.assertFalse(commit.read_operation_record("op-B", self.ops))

    def test_corrupt_intent_blocks_b_advance(self) -> None:
        target = "data/current.json"
        pointer = self._write(target, {"dataset_id": "dataset-old"})
        current_sha = atomic_io.sha256_file(pointer)
        path = commit._intent_path(self.ops, "op-corrupt")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not-json", encoding="utf-8")
        b = self._candidate("corrupt-b", {"dataset_id": "dataset-B"})
        result = self._update(b, target, "op-B", current_sha)
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("pending_pointer_recovery", [i["code"] for i in result.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "dataset-old"})

    def test_recovery_write_failure_is_retryable_and_does_not_advance(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old"}
        pointer = self._write(target, old)
        old_sha = atomic_io.sha256_file(pointer)
        a = self._candidate("a", {"dataset_id": "dataset-A"})
        b = self._candidate("b", {"dataset_id": "dataset-B"})
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected A failure")
        ):
            self._update(a, target, "op-A", old_sha)
        sha_a = atomic_io.sha256_file(pointer)
        self.assertTrue(commit._intent_path(self.ops, "op-A").is_file())
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected recovery failure")
        ):
            blocked = self._update(b, target, "op-B", sha_a)
        self.assertEqual(blocked.status, "failed", blocked.to_dict())
        self.assertIn("pending_pointer_recovery", [i["code"] for i in blocked.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "dataset-A"})
        self.assertTrue(commit._intent_path(self.ops, "op-A").is_file())
        self.assertFalse(commit.read_operation_record("op-B", self.ops))
        # Retry now succeeds: A record repaired, B advanced.
        retry = self._update(b, target, "op-B", sha_a)
        self.assertEqual(retry.status, "committed", retry.to_dict())
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "dataset-B"})
        self.assertIsNotNone(commit.read_operation_record("op-A", self.ops))

    def test_concurrent_b_c_only_one_advances_and_a_is_recovered_once(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old"}
        pointer = self._write(target, old)
        old_sha = atomic_io.sha256_file(pointer)
        a = self._candidate("a", {"dataset_id": "dataset-A"})
        b = self._candidate("b", {"dataset_id": "dataset-B"})
        c = self._candidate("c", {"dataset_id": "dataset-C"})
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected A failure")
        ):
            self._update(a, target, "op-A", old_sha)
        sha_a = atomic_io.sha256_file(pointer)
        barrier = threading.Barrier(2)
        results = {}

        def worker(label: str, candidate: Path) -> None:
            barrier.wait(timeout=10)
            results[label] = self._update(candidate, target, "op-" + label, sha_a)

        threads = [
            threading.Thread(target=worker, args=("B", b)),
            threading.Thread(target=worker, args=("C", c)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "failed"], statuses)
        winner = next(label for label, result in results.items() if result.status == "committed")
        winner_payload = {"dataset_id": "dataset-" + winner}
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), winner_payload)
        self.assertIsNotNone(commit.read_operation_record("op-A", self.ops))
        self.assertFalse(commit._intent_path(self.ops, "op-A").exists())

    def _tamper_redirect(self, target: str, old: dict, a_payload: dict,
                         mode: str) -> tuple:
        pointer = self._write(target, old)
        old_sha = atomic_io.sha256_file(pointer)
        a = self._candidate("a", a_payload)
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected A failure")
        ):
            first = self._update(a, target, "op-A", old_sha)
        self.assertEqual(first.status, "failed", first.to_dict())
        intent_path = commit._intent_path(self.ops, "op-A")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        self.assertIn("target_path", intent, "新 pointer intent 必须绑定 target_path")
        if mode == "candidate_path":
            intent["candidate_refs"][0]["path"] = "data/other-pointer.json"
        elif mode == "top_target":
            intent["target_path"] = "data/other-pointer.json"
        else:  # pragma: no cover
            raise AssertionError(mode)
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
        sha_a = atomic_io.sha256_file(pointer)
        b = self._candidate("b", {"dataset_id": "B"} if "current.json" in target else {"revision_id": "B"})
        blocked = self._update(b, target, "op-B", sha_a)
        return pointer, a, old_sha, sha_a, blocked, intent_path

    def test_dataset_redirected_candidate_path_fail_closed(self) -> None:
        target = "data/current.json"
        pointer, a, old_sha, sha_a, blocked, intent_path = self._tamper_redirect(
            target, {"dataset_id": "old"}, {"dataset_id": "A"}, "candidate_path"
        )
        self.assertEqual(blocked.status, "failed", blocked.to_dict())
        self.assertIn("pending_pointer_recovery", [i["code"] for i in blocked.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "A"})
        self.assertFalse(commit.read_operation_record("op-B", self.ops))
        self.assertFalse((self.ops / "op-B.json").exists())
        self.assertTrue(intent_path.is_file(), "tampered intent 必须保留待人工处理")
        # A retry must explicitly report corrupt intent, not silently advance.
        retry = self._update(a, target, "op-A", old_sha)
        self.assertEqual(retry.status, "failed", retry.to_dict())
        self.assertIn("intent_corrupt", [i["code"] for i in retry.issues])
        self.assertFalse(commit.read_operation_record("op-A", self.ops))
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "A"})

    def test_build_redirected_top_target_fail_closed(self) -> None:
        target = "data/builds/letools/o3ZRm9my/current.json"
        pointer, a, old_sha, sha_a, blocked, intent_path = self._tamper_redirect(
            target,
            {"build_id": "letools:o3ZRm9my", "revision_id": "old"},
            {"build_id": "letools:o3ZRm9my", "revision_id": "A"},
            "top_target",
        )
        self.assertEqual(blocked.status, "failed", blocked.to_dict())
        self.assertIn("pending_pointer_recovery", [i["code"] for i in blocked.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8"))["revision_id"], "A")
        self.assertFalse(commit.read_operation_record("op-B", self.ops))
        retry = self._update(a, target, "op-A", old_sha)
        self.assertEqual(retry.status, "failed", retry.to_dict())
        self.assertIn("intent_corrupt", [i["code"] for i in retry.issues])
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8"))["revision_id"], "A")

    def test_legacy_intent_without_target_path_is_compatible(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old"}
        pointer = self._write(target, old)
        old_sha = atomic_io.sha256_file(pointer)
        a = self._candidate("a", {"dataset_id": "dataset-A"})
        b = self._candidate("b", {"dataset_id": "dataset-B"})
        with mock.patch.object(
            commit, "_write_operation_record", side_effect=OSError("injected A failure")
        ):
            self._update(a, target, "op-A", old_sha)
        intent_path = commit._intent_path(self.ops, "op-A")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        intent.pop("target_path", None)  # simulate pre-R3b legacy intent
        intent_path.write_text(json.dumps(intent), encoding="utf-8")
        sha_a = atomic_io.sha256_file(pointer)
        b_result = self._update(b, target, "op-B", sha_a)
        self.assertEqual(b_result.status, "committed", b_result.to_dict())
        self.assertEqual(json.loads(pointer.read_text(encoding="utf-8")), {"dataset_id": "dataset-B"})
        self.assertIsNotNone(commit.read_operation_record("op-A", self.ops))
        retry = self._update(a, target, "op-A", old_sha)
        self.assertEqual(retry.status, "replayed", retry.to_dict())
        self.assertIn("pointer_advanced_after_commit", [i["code"] for i in retry.issues])



if __name__ == "__main__":
    unittest.main()
