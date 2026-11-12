"""P5 R1/R2: pointer progression, expected snapshot binding and interleaving."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from le_calc.workflow import atomic_io, commit


class PointerProgressionBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p5-pointer-progress-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.counter = 0

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_pointer(self, target_rel: str, payload: dict) -> Path:
        path = self.root / target_rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _candidate(self, name: str, payload: dict) -> Path:
        self.counter += 1
        path = self.root / "candidate" / f"{self.counter}-{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return path

    def _fingerprint(self, label: str) -> str:
        return commit.compute_input_fingerprint({"pointer-test": label})

    def _update(
        self,
        candidate: Path,
        target_rel: str,
        *,
        operation_id: str,
        expected_current_revision: str | None = None,
        expected_current_sha256: str | None = None,
        fingerprint: str | None = None,
    ):
        return commit.update_pointer(
            candidate,
            target_rel,
            operation_id=operation_id,
            attempt_id=operation_id + "-attempt",
            input_fingerprint=fingerprint or self._fingerprint(operation_id),
            expected_current_revision=expected_current_revision,
            expected_current_sha256=expected_current_sha256,
            operations_root=self.ops,
            root=self.root,
        )


class PointerProgressionTest(PointerProgressionBase):
    def test_dataset_sha_old_a_b_and_old_op_replay(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old", "file_sha256": "1" * 64}
        path = self._write_pointer(target, old)
        old_sha = atomic_io.sha256_file(path)
        a = self._candidate("a", {"dataset_id": "dataset-A", "file_sha256": "2" * 64})
        b = self._candidate("b", {"dataset_id": "dataset-B", "file_sha256": "3" * 64})
        first = self._update(a, target, operation_id="op-a", expected_current_sha256=old_sha)
        self.assertEqual(first.status, "committed", first.to_dict())
        sha_a = atomic_io.sha256_file(path)
        second = self._update(b, target, operation_id="op-b", expected_current_sha256=sha_a)
        self.assertEqual(second.status, "committed", second.to_dict())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(b.read_text(encoding="utf-8")))
        # old expected is rejected and never regresses the pointer
        stale = self._update(a, target, operation_id="op-stale", expected_current_sha256=old_sha)
        self.assertEqual(stale.status, "failed", stale.to_dict())
        self.assertIn("current_revision_mismatch", [i["code"] for i in stale.issues])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(b.read_text(encoding="utf-8")))
        # old op replay returns replayed and leaves the newer pointer untouched
        replay = self._update(a, target, operation_id="op-a", expected_current_sha256=old_sha)
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertIn("pointer_advanced_after_commit", [i["code"] for i in replay.issues])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), json.loads(b.read_text(encoding="utf-8")))
        self.assertTrue((self.ops / "op-a.json").is_file())
        self.assertTrue((self.ops / "op-b.json").is_file())

    def test_dataset_revision_identity_old_a_b(self) -> None:
        target = "data/current.json"
        old = {"dataset_id": "dataset-old"}
        path = self._write_pointer(target, old)
        a = self._candidate("a", {"dataset_id": "dataset-A"})
        b = self._candidate("b", {"dataset_id": "dataset-B"})
        first = self._update(a, target, operation_id="op-a", expected_current_revision="dataset-old")
        self.assertEqual(first.status, "committed", first.to_dict())
        second = self._update(b, target, operation_id="op-b", expected_current_revision="dataset-A")
        self.assertEqual(second.status, "committed", second.to_dict())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"dataset_id": "dataset-B"})

    def test_build_pointer_sha_and_revision_progression(self) -> None:
        target = "data/builds/letools/o3ZRm9my/current.json"
        old = {"build_id": "letools:o3ZRm9my", "revision_id": "rev-old"}
        path = self._write_pointer(target, old)
        old_sha = atomic_io.sha256_file(path)
        a = self._candidate("a", {"build_id": "letools:o3ZRm9my", "revision_id": "rev-A"})
        b = self._candidate("b", {"build_id": "letools:o3ZRm9my", "revision_id": "rev-B"})
        first = self._update(a, target, operation_id="build-a", expected_current_sha256=old_sha)
        self.assertEqual(first.status, "committed", first.to_dict())
        sha_a = atomic_io.sha256_file(path)
        second = self._update(b, target, operation_id="build-b", expected_current_sha256=sha_a)
        self.assertEqual(second.status, "committed", second.to_dict())
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["revision_id"], "rev-B")
        # revision identity path also advances (old -> C -> D)
        c = self._candidate("c", {"build_id": "letools:o3ZRm9my", "revision_id": "rev-C"})
        third = self._update(c, target, operation_id="build-c", expected_current_revision="rev-B")
        self.assertEqual(third.status, "committed", third.to_dict())
        replay = self._update(a, target, operation_id="build-a", expected_current_sha256=old_sha)
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertIn("pointer_advanced_after_commit", [i["code"] for i in replay.issues])
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["revision_id"], "rev-C")

    def _interleaved_race(self, target: str, expected_mode: str) -> dict:
        if target == "data/current.json":
            old = {"dataset_id": "dataset-old"}
            a_payload = {"dataset_id": "dataset-A"}
            b_payload = {"dataset_id": "dataset-B"}
            expected_old = "dataset-old"
        else:
            old = {"build_id": "letools:race", "revision_id": "rev-old"}
            a_payload = {"build_id": "letools:race", "revision_id": "rev-A"}
            b_payload = {"build_id": "letools:race", "revision_id": "rev-B"}
            expected_old = "rev-old"
        path = self._write_pointer(target, old)
        old_sha = atomic_io.sha256_file(path)
        a = self._candidate("race-a", a_payload)
        b = self._candidate("race-b", b_payload)
        expected_value = old_sha if expected_mode == "sha" else expected_old
        slow_checked = threading.Event()
        fast_done = threading.Event()
        results: dict[str, object] = {}
        errors: list[str] = []
        original = atomic_io.atomic_replace_file

        def wrapped(source, destination, **kwargs):
            if threading.current_thread().name == "slow-pointer":
                slow_checked.set()
                fast_done.wait(timeout=10)
            return original(source, destination, **kwargs)

        def worker(label: str, candidate: Path, operation_id: str) -> None:
            try:
                if label == "slow":
                    results[label] = self._update(
                        candidate, target,
                        operation_id=operation_id,
                        expected_current_sha256=(expected_value if expected_mode == "sha" else None),
                        expected_current_revision=(expected_value if expected_mode == "revision" else None),
                    )
                else:
                    # Slow has passed its expected check; fast commits B now.
                    self.assertTrue(slow_checked.wait(timeout=10))
                    results[label] = self._update(
                        candidate, target,
                        operation_id=operation_id,
                        expected_current_sha256=(expected_value if expected_mode == "sha" else None),
                        expected_current_revision=(expected_value if expected_mode == "revision" else None),
                    )
                    fast_done.set()
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                fast_done.set()

        with mock.patch.object(atomic_io, "atomic_replace_file", side_effect=wrapped):
            slow_thread = threading.Thread(
                target=worker, args=("slow", a, "race-slow"), name="slow-pointer"
            )
            fast_thread = threading.Thread(
                target=worker, args=("fast", b, "race-fast"), name="fast-pointer"
            )
            slow_thread.start()
            fast_thread.start()
            slow_thread.join(timeout=30)
            fast_thread.join(timeout=30)
        self.assertEqual(errors, [])
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        winner = next(label for label, status in statuses.items() if status == "committed")
        expected = a_payload if winner == "slow" else b_payload
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), expected)
        winner_op = "race-slow" if winner == "slow" else "race-fast"
        loser_op = "race-fast" if winner == "slow" else "race-slow"
        self.assertTrue((self.ops / f"{winner_op}.json").is_file())
        self.assertFalse((self.ops / f"{loser_op}.json").exists())
        loser = next(label for label, status in statuses.items() if status == "failed")
        loser_codes = [issue["code"] for issue in results[loser].issues]
        self.assertTrue(
            any(code in {"current_revision_mismatch", "publish_failed"} for code in loser_codes),
            loser_codes,
        )
        return {"statuses": statuses, "winner": winner, "loser_codes": loser_codes}

    def test_interleaved_dataset_sha(self) -> None:
        self._interleaved_race("data/current.json", "sha")

    def test_interleaved_dataset_revision(self) -> None:
        self._interleaved_race("data/current.json", "revision")

    def test_interleaved_build_sha(self) -> None:
        self._interleaved_race("data/builds/letools/o3ZRm9my/current.json", "sha")

    def test_interleaved_build_revision(self) -> None:
        self._interleaved_race("data/builds/letools/o3ZRm9my/current.json", "revision")


if __name__ == "__main__":
    unittest.main()
