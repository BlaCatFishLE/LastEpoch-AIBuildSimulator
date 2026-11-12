"""P4-F1: different operations racing on the same target must not overwrite
or roll back each other's published artefacts."""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from le_calc.workflow import atomic_io, commit


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class TargetRaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-race-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.target = "reports/black-hole/race-target"
        self.candidate_a = _write_tree(
            self.root / "candidate" / "a", {"payload.txt": "winner-A\n"}
        )
        self.candidate_b = _write_tree(
            self.root / "candidate" / "b", {"payload.txt": "winner-B\n"}
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _fingerprint(self, label: str) -> str:
        return commit.compute_input_fingerprint(
            {"case": "black-hole", "target": self.target, "variant": label}
        )

    def test_file_target_race_has_one_committed_and_no_overwrite(self) -> None:
        file_a = _write_tree(self.root / "files" / "a", {"rule.txt": "A\n"}) / "rule.txt"
        file_b = _write_tree(self.root / "files" / "b", {"rule.txt": "B\n"}) / "rule.txt"
        target = "rules/race-target.txt"
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(label: str, source: Path) -> None:
            barrier.wait(timeout=10)
            results[label] = commit.publish_file(
                source,
                target,
                operation_id="op-file-" + label,
                attempt_id="attempt-" + label,
                input_fingerprint=self._fingerprint("file-" + label),
                operations_root=self.ops,
                root=self.root,
            )

        threads = [
            threading.Thread(target=worker, args=("a", file_a)),
            threading.Thread(target=worker, args=("b", file_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        committed = next(label for label, status in statuses.items() if status == "committed")
        loser = next(label for label, status in statuses.items() if status == "failed")
        target_path = self.root / target
        expected = (file_a if committed == "a" else file_b).read_text(encoding="utf-8")
        self.assertEqual(target_path.read_text(encoding="utf-8"), expected)
        self.assertTrue({"target_exists", "publish_failed"} & {item["code"] for item in results[loser].issues}, results[loser].issues)
        # The failed operation must not claim or publish the same target.
        self.assertTrue((self.ops / f"op-file-{committed}.json").is_file())
        self.assertFalse((self.ops / f"op-file-{loser}.json").exists())

    def test_directory_target_race_does_not_delete_winner(self) -> None:
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}

        def worker(label: str, candidate: Path) -> None:
            barrier.wait(timeout=10)
            results[label] = commit.publish_run_dir(
                candidate,
                self.target,
                operation_id="op-dir-" + label,
                attempt_id="attempt-" + label,
                input_fingerprint=self._fingerprint("dir-" + label),
                operations_root=self.ops,
                root=self.root,
            )

        threads = [
            threading.Thread(target=worker, args=("a", self.candidate_a)),
            threading.Thread(target=worker, args=("b", self.candidate_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        committed = next(label for label, status in statuses.items() if status == "committed")
        winner = self.candidate_a if committed == "a" else self.candidate_b
        target_path = self.root / self.target
        self.assertTrue((target_path / "payload.txt").is_file())
        self.assertEqual(atomic_io.tree_sha256(target_path), atomic_io.tree_sha256(winner))
        self.assertEqual((target_path / "payload.txt").read_text(encoding="utf-8"),
                         (winner / "payload.txt").read_text(encoding="utf-8"))

    def test_rollback_keeps_other_operation_bytes_and_skips_changed_target(self) -> None:
        other_target = self.root / "reports" / "other" / "kept.txt"
        other_target.parent.mkdir(parents=True, exist_ok=True)
        other_target.write_text("other-operation\n", encoding="utf-8")
        candidate_a = self.candidate_a
        candidate_b = self.candidate_b
        request = {
            "operation_id": "op-rollback",
            "attempt_id": "a1",
            "operation_kind": "write_run",
            "input_fingerprint": self._fingerprint("rollback"),
            "candidate_refs": [
                {
                    "candidate_id": "first",
                    "kind": "dir",
                    "candidate_path": str(candidate_a),
                    "path": "reports/black-hole/rollback-first",
                },
                {
                    "candidate_id": "second",
                    "kind": "dir",
                    "candidate_path": str(candidate_b),
                    "path": "reports/black-hole/rollback-second",
                },
            ],
        }
        original = atomic_io.atomic_publish_dir
        calls = {"count": 0}

        def flaky(source, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                # Simulate another operation's already-published file appearing
                # at the second target; rollback must not remove it.
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).mkdir()
                (Path(target) / "other.txt").write_text("other\n", encoding="utf-8")
                raise OSError("second publish collision")
            return original(source, target, **kwargs)

        with mock.patch.object(atomic_io, "atomic_publish_dir", side_effect=flaky):
            result = commit.commit_operation(
                request, operations_root=self.ops, root=self.root
            )
        self.assertEqual(result.status, "failed")
        self.assertIn("publish_failed", [item["code"] for item in result.issues])
        # First target was created by this op and still matches -> rolled back.
        self.assertFalse((self.root / "reports" / "black-hole" / "rollback-first").exists())
        # Second target contains foreign bytes -> left intact.
        self.assertTrue(
            (self.root / "reports" / "black-hole" / "rollback-second" / "other.txt").is_file()
        )
        self.assertEqual(
            (self.root / "reports" / "black-hole" / "rollback-second" / "other.txt").read_text(
                encoding="utf-8"
            ),
            "other\n",
        )
        # Pre-existing unrelated file untouched.
        self.assertEqual(other_target.read_text(encoding="utf-8"), "other-operation\n")
        self.assertFalse((self.ops / "op-rollback.json").exists())

    def test_same_content_existing_file_without_intent_is_rejected(self) -> None:
        source = _write_tree(self.root / "files" / "same", {"rule.txt": "same\n"}) / "rule.txt"
        target = "rules/same-content.txt"
        target_path = self.root / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("same\n", encoding="utf-8")
        before_stat = target_path.stat()
        result = commit.publish_file(
            source,
            target,
            operation_id="op-same-content",
            attempt_id="a1",
            input_fingerprint=self._fingerprint("same-content"),
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(result.status, "failed")
        self.assertIn("target_exists", [item["code"] for item in result.issues])
        self.assertEqual(target_path.read_text(encoding="utf-8"), "same\n")
        self.assertEqual(target_path.stat().st_ino, before_stat.st_ino)


class LockAndInjectedFailureTest(TargetRaceTest):
    def test_target_lock_timeout_returns_failed_and_releases_locks(self) -> None:
        target = "reports/black-hole/locked-target"
        lock_path = atomic_io.publish_lock_path(self.root / target)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text('{"holder": "test"}\n', encoding="utf-8")
        try:
            result = commit.publish_run_dir(
                self.candidate_a,
                target,
                operation_id="waiting-op",
                attempt_id="a1",
                input_fingerprint=self._fingerprint("locked"),
                operations_root=self.ops,
                root=self.root,
                lock_timeout_seconds=0.2,
                target_lock_timeout_seconds=0.2,
            )
        finally:
            lock_path.unlink(missing_ok=True)
        self.assertEqual(result.status, "failed")
        self.assertIn("publish_failed", [item["code"] for item in result.issues])
        # Both the operation lock and the internal publish lock are released.
        retry = commit.publish_run_dir(
            self.candidate_a,
            target,
            operation_id="waiting-op",
            attempt_id="a2",
            input_fingerprint=self._fingerprint("locked"),
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(retry.status, "committed")
        self.assertTrue((self.root / target / "payload.txt").is_file())
        self.assertFalse(lock_path.exists())

    def _run_injected_publish_race(self, directory: bool) -> dict:
        target = "reports/black-hole/injected-" + ("dir" if directory else "file")
        source_a = self.candidate_a
        source_b = self.candidate_b
        original = (
            atomic_io.atomic_publish_dir if directory else atomic_io.atomic_publish_file
        )
        calls = {"count": 0}
        count_lock = threading.Lock()
        barrier = threading.Barrier(2)

        def injected(source, destination, **kwargs):
            with count_lock:
                calls["count"] += 1
                index = calls["count"]
            if index == 1:
                # Review-script style: the lock holder waits for a second
                # publisher inside atomic publish; the second publisher is
                # queued behind the target lock, so this barrier times out.
                try:
                    barrier.wait(timeout=1.0)
                except threading.BrokenBarrierError as exc:
                    raise RuntimeError("injected barrier timeout") from exc
            return original(source, destination, **kwargs)

        results: dict[str, object] = {}
        start = threading.Barrier(2)

        def worker(label: str, candidate: Path) -> None:
            start.wait(timeout=10)
            if directory:
                results[label] = commit.publish_run_dir(
                    candidate,
                    target,
                    operation_id="injected-" + label,
                    attempt_id="a-" + label,
                    input_fingerprint=self._fingerprint("injected-" + label),
                    operations_root=self.ops,
                    root=self.root,
                )
            else:
                results[label] = commit.publish_file(
                    candidate / "payload.txt",
                    target + ".txt",
                    operation_id="injected-" + label,
                    attempt_id="a-" + label,
                    input_fingerprint=self._fingerprint("injected-" + label),
                    operations_root=self.ops,
                    root=self.root,
                )

        patch_target = (
            "atomic_publish_dir" if directory else "atomic_publish_file"
        )
        with mock.patch.object(atomic_io, patch_target, side_effect=injected):
            threads = [
                threading.Thread(target=worker, args=("a", source_a)),
                threading.Thread(target=worker, args=("b", source_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "failed"], statuses)
        if directory:
            target_path = self.root / target
            self.assertTrue((target_path / "payload.txt").is_file())
            committed = next(
                label for label, result in results.items() if result.status == "committed"
            )
            winner = source_a if committed == "a" else source_b
            self.assertEqual(
                atomic_io.tree_sha256(target_path), atomic_io.tree_sha256(winner)
            )
        else:
            target_path = self.root / (target + ".txt")
            self.assertTrue(target_path.is_file())
            committed = next(
                label for label, result in results.items() if result.status == "committed"
            )
            winner = source_a if committed == "a" else source_b
            self.assertEqual(
                target_path.read_text(encoding="utf-8"),
                (winner / "payload.txt").read_text(encoding="utf-8"),
            )
        loser = next(
            label for label, result in results.items() if result.status == "failed"
        )
        self.assertNotEqual(loser, committed)
        return {
            "statuses": statuses,
            "results": {label: result.to_dict() for label, result in results.items()},
        }

    def test_injected_publish_failure_file_race_one_committed(self) -> None:
        summary = self._run_injected_publish_race(directory=False)
        self.assertEqual(len(summary["results"]), 2)

    def test_injected_publish_failure_directory_race_one_committed(self) -> None:
        summary = self._run_injected_publish_race(directory=True)
        self.assertEqual(len(summary["results"]), 2)

    def _run_review_style_barrier_race(self, directory: bool) -> dict:
        target = "reports/black-hole/review-barrier-" + ("dir" if directory else "file")
        original = (
            atomic_io.atomic_publish_dir if directory else atomic_io.atomic_publish_file
        )
        barrier = threading.Barrier(2)

        def scheduled(*args, **kwargs):
            barrier.wait(timeout=5)
            return original(*args, **kwargs)

        results: dict[str, object] = {}
        start = threading.Barrier(2)

        def worker(label: str, candidate: Path) -> None:
            start.wait(timeout=10)
            if directory:
                results[label] = commit.publish_run_dir(
                    candidate,
                    target,
                    operation_id="review-barrier-" + label,
                    attempt_id=label,
                    input_fingerprint=self._fingerprint("review-barrier-" + label),
                    operations_root=self.ops,
                    root=self.root,
                )
            else:
                results[label] = commit.publish_file(
                    candidate / "payload.txt",
                    target + ".txt",
                    operation_id="review-barrier-" + label,
                    attempt_id=label,
                    input_fingerprint=self._fingerprint("review-barrier-" + label),
                    operations_root=self.ops,
                    root=self.root,
                )

        patch_target = "atomic_publish_dir" if directory else "atomic_publish_file"
        with mock.patch.object(atomic_io, patch_target, side_effect=scheduled):
            threads = [
                threading.Thread(target=worker, args=("a", self.candidate_a)),
                threading.Thread(target=worker, args=("b", self.candidate_b)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "failed"], statuses)
        if directory:
            target_path = self.root / target
            self.assertTrue((target_path / "payload.txt").is_file())
        else:
            target_path = self.root / (target + ".txt")
            self.assertTrue(target_path.is_file())
        # Exactly one operation record exists for the successful op.
        records = [path for path in self.ops.glob("review-barrier-*.json")]
        self.assertEqual(len(records), 1)
        return {
            "statuses": statuses,
            "results": {label: result.to_dict() for label, result in results.items()},
        }

    def test_review_style_barrier_file_race(self) -> None:
        summary = self._run_review_style_barrier_race(directory=False)
        self.assertEqual(summary["statuses"], ["committed", "failed"])

    def test_review_style_barrier_directory_race(self) -> None:
        summary = self._run_review_style_barrier_race(directory=True)
        self.assertEqual(summary["statuses"], ["committed", "failed"])


if __name__ == "__main__":
    unittest.main()
