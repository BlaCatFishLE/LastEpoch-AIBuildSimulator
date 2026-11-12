"""P4-B: idempotent commit, atomic publish, conflict and crash recovery."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from le_calc.workflow import atomic_io, commit, contract


def _write_tree(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for relative, text in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


class CommitTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-commit-")
        self.root = Path(self._tmp.name)
        self.ops = self.root / "data" / "cache" / "operations"
        self.candidate = _write_tree(
            self.root / "candidate" / "run-1",
            {"result.json": '{"answer": 42}\n', "run-manifest.json": '{"ok": true}\n'},
        )
        self.target = "reports/black-hole/commit-test"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def fingerprint(self, value: str = "input-1") -> str:
        return commit.compute_input_fingerprint({"scenario": value, "case": "black-hole"})

    def publish(
        self,
        *,
        operation_id: str = "op-1",
        fingerprint: str | None = None,
        attempt_id: str = "attempt-1",
        **kwargs,
    ):
        return commit.publish_run_dir(
            self.candidate,
            self.target,
            operation_id=operation_id,
            attempt_id=attempt_id,
            input_fingerprint=fingerprint or self.fingerprint(),
            operations_root=self.ops,
            root=self.root,
            **kwargs,
        )


class AtomicIoTest(CommitTestBase):
    def test_tree_and_file_sha_are_deterministic(self) -> None:
        first = atomic_io.tree_sha256(self.candidate)
        second = atomic_io.tree_sha256(self.candidate)
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertNotEqual(
            first,
            atomic_io.tree_sha256(
                _write_tree(self.root / "candidate" / "other", {"result.json": "{}\n"})
            ),
        )

    def test_atomic_write_json_has_no_partial_file(self) -> None:
        path = self.root / "cache" / "record.json"
        result = atomic_io.atomic_write_json(path, {"a": 1})
        self.assertEqual(result["sha256"], atomic_io.sha256_file(path))
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["a"], 1)
        self.assertEqual(list(path.parent.glob(".tmp-*")), [])


class CommitLifecycleTest(CommitTestBase):
    def test_first_commit_publishes_and_records(self) -> None:
        result = self.publish()
        self.assertEqual(result.status, contract.COMMIT_COMMITTED)
        self.assertEqual(result.operation_id, "op-1")
        target = self.root / self.target
        self.assertTrue((target / "result.json").is_file())
        self.assertEqual(result.committed_refs[0]["path"], self.target)
        self.assertEqual(result.committed_refs[0]["kind"], "dir")
        record = commit.read_operation_record("op-1", self.ops)
        self.assertEqual(record["schema_version"], "le-operation-record-v1")
        self.assertEqual(record["status"], "committed")
        self.assertEqual(record["input_fingerprint"], self.fingerprint())
        self.assertEqual(
            record["published_refs"][0]["sha256"],
            atomic_io.tree_sha256(target),
        )

    def test_same_operation_same_input_is_replayed_without_republish(self) -> None:
        first = self.publish()
        self.assertEqual(first.status, "committed")
        target = self.root / self.target
        before = atomic_io.tree_sha256(target)
        second = self.publish(attempt_id="attempt-2")
        self.assertEqual(second.status, "replayed")
        self.assertEqual(second.committed_refs, first.committed_refs)
        self.assertEqual(atomic_io.tree_sha256(target), before)
        self.assertEqual(len(list(target.rglob("result.json"))), 1)
        issues = [item["code"] for item in second.issues]
        self.assertIn("operation_replayed", issues)

    def test_same_operation_different_input_is_conflict_and_does_not_overwrite(self) -> None:
        self.publish()
        target = self.root / self.target
        before = atomic_io.tree_sha256(target)
        result = self.publish(fingerprint=self.fingerprint("changed"))
        self.assertEqual(result.status, "conflict")
        self.assertEqual(atomic_io.tree_sha256(target), before)
        self.assertIn("input_fingerprint_conflict", [item["code"] for item in result.issues])
        record = commit.read_operation_record("op-1", self.ops)
        self.assertEqual(record["input_fingerprint"], self.fingerprint())

    def test_existing_target_from_other_operation_is_rejected(self) -> None:
        self.publish()
        result = self.publish(operation_id="op-2", fingerprint=self.fingerprint("other"))
        self.assertEqual(result.status, "failed")
        self.assertIn("target_exists", [item["code"] for item in result.issues])
        self.assertFalse((self.ops / "op-2.json").exists())

    def test_precondition_failure_is_rejected(self) -> None:
        blocked = self.root / "blocked"
        blocked.write_text("x", encoding="utf-8")
        result = self.publish(preconditions={"must_not_exist": ["blocked"]})
        self.assertEqual(result.status, "failed")
        self.assertIn("precondition_failed", [item["code"] for item in result.issues])
        self.assertFalse((self.root / self.target).exists())
        self.assertFalse((self.ops / "op-1.json").exists())

    def test_replay_detects_deleted_published_artifact(self) -> None:
        self.publish()
        target = self.root / self.target
        shutil.rmtree(target)
        result = self.publish()
        self.assertEqual(result.status, "failed")
        codes = [item["code"] for item in result.issues]
        self.assertIn("published_artifact_missing", codes)
        self.assertTrue((self.ops / "op-1.json").is_file(), "记录应保留")
        record = commit.read_operation_record("op-1", self.ops)
        self.assertEqual(record["status"], "committed")

    def test_replay_detects_modified_published_artifact(self) -> None:
        self.publish()
        target = self.root / self.target
        (target / "result.json").write_text('{"answer": 0}\n', encoding="utf-8")
        result = self.publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("published_artifact_sha_mismatch", [item["code"] for item in result.issues])


class RecoveryTest(CommitTestBase):
    def test_foreign_target_without_intent_is_not_adopted(self) -> None:
        target = self.root / self.target
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.candidate, target)  # foreign/pre-existing target
        self.assertFalse((self.ops / ".intents" / "op-1.json").exists())
        result = self.publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("target_exists", [item["code"] for item in result.issues])
        self.assertFalse((self.ops / "op-1.json").exists())

    def test_crash_before_publish_cleans_orphan_temp_and_commits(self) -> None:
        target_parent = (self.root / self.target).parent
        target_parent.mkdir(parents=True, exist_ok=True)
        orphan = target_parent / ".tmp-op-1-deadbeef.dir"
        orphan.mkdir()
        (orphan / "partial.json").write_text("{", encoding="utf-8")
        result = self.publish()
        self.assertEqual(result.status, "committed")
        self.assertFalse(orphan.exists())
        self.assertFalse((self.root / self.target / "partial.json").exists())

    def test_target_exists_without_intent_or_record_is_rejected(self) -> None:
        target = self.root / self.target
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.candidate, target)
        result = self.publish(operation_id="op-no-intent")
        self.assertEqual(result.status, "failed")
        self.assertIn("target_exists", [item["code"] for item in result.issues])
        self.assertFalse(commit.read_operation_record("op-no-intent", self.ops))

    def test_corrupt_operation_record_is_failed_without_touching_target(self) -> None:
        target = self.root / self.target
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
        (target / "existing.txt").write_text("keep", encoding="utf-8")
        self.ops.mkdir(parents=True, exist_ok=True)
        (self.ops / "op-1.json").write_text("{not-json", encoding="utf-8")
        result = self.publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("operation_record_corrupt", [item["code"] for item in result.issues])
        self.assertEqual((target / "existing.txt").read_text(encoding="utf-8"), "keep")
        self.assertEqual((self.ops / "op-1.json").read_text(encoding="utf-8"), "{not-json")

    def test_unsupported_record_schema_is_corrupt(self) -> None:
        self.ops.mkdir(parents=True, exist_ok=True)
        (self.ops / "op-1.json").write_text(
            json.dumps({"schema_version": "other", "operation_id": "op-1"}),
            encoding="utf-8",
        )
        result = self.publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("operation_record_corrupt", [item["code"] for item in result.issues])

    def test_recover_operations_reports_problems_and_orphans(self) -> None:
        self.publish()
        target = self.root / self.target
        shutil.rmtree(target)
        parent = target.parent
        orphan = parent / ".tmp-op-1-orphan.dir"
        orphan.mkdir(exist_ok=True)
        report = commit.recover_operations(self.ops, root=self.root)
        self.assertTrue(report["records"])
        self.assertTrue(report["records"][0]["problems"])
        self.assertIn(str(orphan), report["orphan_temps"])


class AtomicFailureTest(CommitTestBase):
    def test_publish_failure_cleans_staging_and_leaves_no_target(self) -> None:
        from unittest import mock

        target = self.root / self.target

        def broken_publish(*args, **kwargs):
            staging = target.parent / ".tmp-op-1-partial.dir"
            staging.mkdir(parents=True, exist_ok=True)
            (staging / "half.json").write_text('{"partial": tru', encoding="utf-8")
            raise OSError("disk full")

        with mock.patch.object(
            commit.atomic_io, "atomic_publish_dir", side_effect=broken_publish
        ):
            result = self.publish()
        self.assertEqual(result.status, "failed")
        self.assertIn("publish_failed", [item["code"] for item in result.issues])
        self.assertFalse(target.exists(), "正式路径不得出现半成品")
        self.assertFalse((self.ops / "op-1.json").exists())
        self.assertEqual(list(target.parent.glob(".tmp-op-1-*")), [])

    def test_multiple_candidates_are_all_or_nothing_on_second_publish(self) -> None:
        second = _write_tree(
            self.root / "candidate" / "second", {"second.json": '{"second": 1}\n'}
        )
        request = {
            "operation_id": "op-multi",
            "attempt_id": "a1",
            "operation_kind": "write_run",
            "input_fingerprint": self.fingerprint("multi"),
            "candidate_refs": [
                {
                    "candidate_id": "first",
                    "kind": "dir",
                    "candidate_path": str(self.candidate),
                    "path": "multi/first",
                },
                {
                    "candidate_id": "second",
                    "kind": "dir",
                    "candidate_path": str(second),
                    "path": "multi/second",
                },
            ],
        }
        from unittest import mock

        original = commit.atomic_io.atomic_publish_dir
        calls = {"count": 0}

        def flaky(source, target, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                raise OSError("second publish failed")
            return original(source, target, **kwargs)

        with mock.patch.object(commit.atomic_io, "atomic_publish_dir", side_effect=flaky):
            result = commit.commit_operation(request, operations_root=self.ops, root=self.root)
        self.assertEqual(result.status, "failed")
        self.assertFalse((self.root / "multi" / "first").exists())
        self.assertFalse((self.root / "multi" / "second").exists())
        self.assertFalse((self.ops / "op-multi.json").exists())


class ConcurrencyTest(CommitTestBase):
    def test_concurrent_same_operation_commits_at_most_once(self) -> None:
        barrier = threading.Barrier(6)
        results: list[object] = []
        failures: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                results.append(
                    self.publish(operation_id="op-concurrent", fingerprint=self.fingerprint())
                )
            except BaseException as exc:  # pragma: no cover - surfaced below
                failures.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(failures, [])
        statuses = [result.status for result in results]
        self.assertEqual(statuses.count("committed"), 1, statuses)
        self.assertLessEqual(set(statuses), {"committed", "replayed"}, statuses)
        self.assertEqual(len(results), 6)
        self.assertTrue((self.root / self.target / "result.json").is_file())
        record = commit.read_operation_record("op-concurrent", self.ops)
        self.assertEqual(record["status"], "committed")


class GenericPublishTest(CommitTestBase):
    def test_publish_file_is_idempotent(self) -> None:
        candidate = self.root / "candidate" / "rule.json"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text('{"rule": 1}\n', encoding="utf-8")
        fingerprint = self.fingerprint("rule")
        first = commit.publish_file(
            candidate,
            "rules/commit-rule.json",
            operation_id="file-op",
            attempt_id="a1",
            input_fingerprint=fingerprint,
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(first.status, "committed")
        target = self.root / "rules" / "commit-rule.json"
        self.assertTrue(target.is_file())
        second = commit.publish_file(
            candidate,
            "rules/commit-rule.json",
            operation_id="file-op",
            attempt_id="a2",
            input_fingerprint=fingerprint,
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(second.status, "replayed")
        self.assertEqual(second.committed_refs, first.committed_refs)

    def test_target_path_traversal_is_rejected(self) -> None:
        with self.assertRaises(commit.CommitError):
            commit.publish_run_dir(
                self.candidate,
                "../outside",
                operation_id="bad",
                attempt_id="a",
                input_fingerprint=self.fingerprint(),
                operations_root=self.ops,
                root=self.root,
            )

    def test_input_fingerprint_is_deterministic(self) -> None:
        left = commit.compute_input_fingerprint({"b": [1, 2], "a": {"x": True}})
        right = commit.compute_input_fingerprint({"a": {"x": True}, "b": [1, 2]})
        self.assertEqual(left, right)
        self.assertNotEqual(left, commit.compute_input_fingerprint({"a": 1}))


def _subprocess_commit(
    root_text: str,
    candidate_text: str,
    target: str,
    operations_text: str,
    fingerprint: str,
    queue,
) -> None:
    """Multiprocessing entry point (must be importable on Windows spawn)."""

    from pathlib import Path as _Path

    from le_calc.workflow import commit as _commit

    try:
        result = _commit.publish_run_dir(
            _Path(candidate_text),
            target,
            operation_id="op-process",
            attempt_id="process-attempt",
            input_fingerprint=fingerprint,
            operations_root=_Path(operations_text),
            root=_Path(root_text),
        )
        queue.put((result.status, result.operation_id))
    except BaseException as exc:  # pragma: no cover - surfaced as failure tuple
        queue.put(("error", repr(exc)))


class ProcessConcurrencyTest(CommitTestBase):
    def test_two_processes_same_operation_commit_at_most_once(self) -> None:
        try:
            import multiprocessing as mp
        except ImportError:  # pragma: no cover
            self.skipTest("multiprocessing unavailable")
        context = mp.get_context("spawn")
        queue = context.Queue()
        processes = [
            context.Process(
                target=_subprocess_commit,
                args=(
                    str(self.root),
                    str(self.candidate),
                    self.target,
                    str(self.ops),
                    self.fingerprint(),
                    queue,
                ),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=60)
        statuses = [queue.get(timeout=20) for _ in processes]
        errors = [item for item in statuses if item[0] == "error"]
        self.assertEqual(errors, [])
        values = [item[0] for item in statuses]
        self.assertEqual(values.count("committed"), 1, values)
        self.assertLessEqual(set(values), {"committed", "replayed"}, values)
        self.assertTrue((self.root / self.target / "result.json").is_file())


class ExtraContractTest(CommitTestBase):
    def test_mapping_request_with_generic_file_kind_alias(self) -> None:
        candidate = self.root / "candidate" / "generic.txt"
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text("generic", encoding="utf-8")
        fingerprint = self.fingerprint("generic")
        request = {
            "operation_id": "generic-op",
            "attempt_id": "a1",
            "operation_kind": "write_file",
            "input_fingerprint": fingerprint,
            "candidate_refs": [
                {
                    "kind": "file",
                    "candidate_path": str(candidate),
                    "path": "generic/output.txt",
                    "artifact_id": "generic",
                }
            ],
            "preconditions": {"target_absent": True},
        }
        result = commit.commit_operation(
            request, operations_root=self.ops, root=self.root
        )
        self.assertEqual(result.status, "committed")
        self.assertTrue((self.root / "generic" / "output.txt").is_file())
        record = commit.read_operation_record("generic-op", self.ops)
        self.assertEqual(record["requested_operation_kind"], "write_file")
        self.assertEqual(record["operation_kind"], "write_run")

    def test_expected_current_revision_precondition(self) -> None:
        self.ops.mkdir(parents=True, exist_ok=True)
        (self.ops / "current-revision.json").write_text(
            json.dumps({"revision": "rev-2"}), encoding="utf-8"
        )
        result = self.publish(preconditions={"expected_current_revision": "rev-1"})
        self.assertEqual(result.status, "failed")
        self.assertIn("current_revision_mismatch", [item["code"] for item in result.issues])
        result_with_expected = self.publish(
            operation_id="op-rev-ok",
            preconditions={"expected_current_revision": "rev-2"},
        )
        self.assertEqual(result_with_expected.status, "committed")

    def test_relative_operations_root_is_resolved_against_repo_root(self) -> None:
        result = commit.publish_run_dir(
            self.candidate,
            self.target,
            operation_id="op-relative",
            attempt_id="a1",
            input_fingerprint=self.fingerprint("relative"),
            operations_root="data/cache/operations",
            root=self.root,
        )
        self.assertEqual(result.status, "committed")
        self.assertTrue((self.ops / "op-relative.json").is_file())
        serialized = result.to_dict()
        self.assertEqual(serialized["schema_version"], "le-commit-result-v1")
        self.assertEqual(serialized["status"], "committed")
        self.assertEqual(serialized["operation_id"], "op-relative")


class UnsafeOperationIdTest(CommitTestBase):
    SPECIAL_IDS = (
        "run:black-hole:2026-11-08T11:00:00",
        "a/b/c",
        r"a\b",
        "..",
        "../escape",
        "中文-操作",
        "x" * 300,
    )

    def _target(self, index: int) -> str:
        return f"reports/black-hole/unsafe-{index}"

    def test_special_operation_ids_commit_replay_and_conflict(self) -> None:
        for index, operation_id in enumerate(self.SPECIAL_IDS):
            target = self._target(index)
            fingerprint = self.fingerprint(operation_id)
            first = commit.publish_run_dir(
                self.candidate,
                target,
                operation_id=operation_id,
                attempt_id="a1",
                input_fingerprint=fingerprint,
                operations_root=self.ops,
                root=self.root,
            )
            self.assertEqual(first.status, "committed", operation_id)
            self.assertEqual(first.operation_id, operation_id)
            record_path = commit.operation_record_path(operation_id, self.ops)
            self.assertTrue(record_path.is_file(), operation_id)
            self.assertNotIn(":", record_path.name, operation_id)
            self.assertNotIn("/", record_path.name, operation_id)
            self.assertNotIn("\\", record_path.name, operation_id)
            self.assertNotIn("..", record_path.name, operation_id if operation_id != ".." else record_path.name)
            record = commit.read_operation_record(operation_id, self.ops)
            self.assertEqual(record["operation_id"], operation_id)
            self.assertTrue((self.root / target / "result.json").is_file())
            replay = commit.publish_run_dir(
                self.candidate,
                target,
                operation_id=operation_id,
                attempt_id="a2",
                input_fingerprint=fingerprint,
                operations_root=self.ops,
                root=self.root,
            )
            self.assertEqual(replay.status, "replayed", operation_id)
            conflict = commit.publish_run_dir(
                self.candidate,
                target,
                operation_id=operation_id,
                attempt_id="a3",
                input_fingerprint=self.fingerprint(operation_id + "-changed"),
                operations_root=self.ops,
                root=self.root,
            )
            self.assertEqual(conflict.status, "conflict", operation_id)
            lock_path = commit.OperationLock(self.ops, operation_id).path
            for forbidden in (":", "/", "\\"):
                self.assertNotIn(forbidden, lock_path.name, (operation_id, lock_path.name))

    def test_special_operation_ids_have_distinct_safe_paths(self) -> None:
        stems = [commit.operation_file_stem(operation_id) for operation_id in self.SPECIAL_IDS]
        self.assertEqual(len(stems), len(set(stems)))
        for stem in stems:
            self.assertRegex(stem, r"^[~A-Za-z0-9][A-Za-z0-9._-]*$")

    def test_unsafe_ids_do_not_escape_repo_or_operations_root(self) -> None:
        for index, operation_id in enumerate(("..", "../escape", "a/b", r"a\b")):
            result = commit.publish_run_dir(
                self.candidate,
                self._target(index),
                operation_id=operation_id,
                attempt_id="a1",
                input_fingerprint=self.fingerprint("escape-" + str(index)),
                operations_root=self.ops,
                root=self.root,
            )
            self.assertEqual(result.status, "committed", operation_id)
        self.assertFalse((self.root.parent / "operations").exists())
        self.assertEqual(len(list((self.ops).glob("*.json"))), 4)
        self.assertTrue((self.ops / ".locks").is_dir())

    def test_safe_staged_id_remains_backwards_compatible(self) -> None:
        operation_id = "run-black-hole-p4-stage"
        result = commit.publish_run_dir(
            self.candidate,
            "reports/black-hole/safe-id",
            operation_id=operation_id,
            attempt_id="a1",
            input_fingerprint=self.fingerprint("safe"),
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(result.status, "committed")
        self.assertEqual(
            commit.operation_file_stem(operation_id), operation_id,
            "安全 ID 应保持原文件名，兼容已部署记录",
        )
        self.assertTrue((self.ops / f"{operation_id}.json").is_file())

    def test_long_operation_id_maps_to_bounded_safe_stem(self) -> None:
        operation_id = "long-" + "x" * 400
        stem = commit.operation_file_stem(operation_id)
        self.assertLessEqual(len(stem), 128)
        self.assertRegex(stem, r"^[~A-Za-z0-9][A-Za-z0-9._-]*$")
        result = commit.publish_run_dir(
            self.candidate,
            "reports/black-hole/long-id",
            operation_id=operation_id,
            attempt_id="a1",
            input_fingerprint=self.fingerprint("long"),
            operations_root=self.ops,
            root=self.root,
        )
        self.assertEqual(result.status, "committed")
        self.assertEqual(commit.read_operation_record(operation_id, self.ops)["operation_id"], operation_id)


if __name__ == "__main__":
    unittest.main()
