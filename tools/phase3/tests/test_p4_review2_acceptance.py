# -*- coding: utf-8 -*-
"""P4 审查反例长期回归（F1–F4 / G1）：独立行为验收。

来源：``docs/reviews/p4-lead-review-2026-11-08.zh-CN.md`` 的 P4-F1/F2/F3/F4 与 G1。
本文件只做独立验收：真实 CLI 子进程 / 文件系统 / commit 库 API；不修改实现、
别人测试、正式 rules/examples/expected/knowledge/docs，不覆盖历史 run、不 commit。
证据：``reports/p4-acceptance/{logs,work}/review2-*``。
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
P4_ROOT = ROOT / "reports" / "p4-acceptance"
CALC_SRC = ROOT / "tools" / "phase3" / "src"
if str(CALC_SRC) not in sys.path:
    sys.path.insert(0, str(CALC_SRC))

STAMP = time.strftime("%Y%m%dT%H%M%S")
SESSION = P4_ROOT / "work" / ("review2-" + STAMP)
LOG_DIR = P4_ROOT / "logs"
FR_CASE = "examples/flame-reave/case.legacy-hit.json"
BH_CASE = "examples/black-hole/case.legacy.json"
RECORDS: list[dict] = []
MODULE_STATE: dict = {}


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------
def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def normalise_abs(payload: object) -> list[str]:
    return re.findall(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|,;)]*",
                       json.dumps(payload, ensure_ascii=False, default=str))


def work_dir(test: str) -> pathlib.Path:
    directory = SESSION / test
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run_process(args: list[str], *, env: dict | None = None, timeout: float = 600) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + merged.get("PYTHONPATH", "")
    if env:
        merged.update(env)
    return subprocess.run(
        args, cwd=str(ROOT), env=merged, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def save_log(name: str, completed: subprocess.CompletedProcess) -> pathlib.Path:
    path = LOG_DIR / f"review2-{name}.log"
    path.write_text(
        f"exit: {completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
        encoding="utf-8",
    )
    return path


def parse_stage_statuses(output: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\s+(simulate|validate_result|render_report|commit)\s+\S+\s+cache=(\w+)", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def parse_prepare_cache(output: str) -> dict[str, int]:
    match = re.search(r"阶段缓存：hit=(\d+) miss=(\d+) disabled=(\d+) state_cached=(\d+)", output)
    if not match:
        return {}
    return {"hit": int(match.group(1)), "miss": int(match.group(2)),
            "disabled": int(match.group(3)), "state_cached": int(match.group(4))}


def parse_child_calls(output: str) -> int:
    match = re.search(r"子进程调用：(\d+) 次", output)
    return int(match.group(1)) if match else -1


def prepare_command(
    case: str,
    *,
    run_id: str,
    out_root: pathlib.Path,
    cache_root: pathlib.Path,
    extra: list[str] | None = None,
    main_skill: str,
) -> list[str]:
    args = [
        sys.executable, "scripts/le_commands.py", "prepare",
        "--case", case, "--out", str(out_root), "--run-id", run_id,
        "--cache-root", str(cache_root), "--main-skill", main_skill,
    ]
    args.extend(extra or [])
    return args


def plan_states_for(state: dict) -> dict[str, dict]:
    return {entry["stage_id"]: entry for entry in state.get("stages") or []}


def stage_cache(state: dict) -> dict[str, str]:
    return {stage: (entry.get("cache_status") or (entry.get("cache") or {}).get("status") or "unknown")
            for stage, entry in plan_states_for(state).items()}


# ---------------------------------------------------------------------------
# P4-F1: concurrent different operations on the same target
# ---------------------------------------------------------------------------
class F1ConcurrencyAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from le_calc.workflow import atomic_io, commit

        cls.commit = commit
        cls.atomic_io = atomic_io

    def setUp(self) -> None:
        self.name = self._testMethodName
        self.root = work_dir("f1-" + self.name)
        self.ops = self.root / "ops"
        self.candidates = self.root / "candidates"
        self.candidates.mkdir(parents=True, exist_ok=True)
        self.targets = self.root / "targets"
        self.targets.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        path = LOG_DIR / f"review2-f1-{self.name}.json"
        path.write_text(json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record = {"test": "F1::" + self.name, "status": self._status, "details": getattr(self, "_details", {})}
        RECORDS.append(record)

    @property
    def _status(self) -> str:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        if result and any(test is self for test, _ in result.failures + result.errors):
            return "fail"
        return "pass"

    def record(self, **details) -> None:
        self._details = details

    def _candidate_dir(self, label: str, text: str) -> pathlib.Path:
        directory = self.candidates / label
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "payload.txt").write_text(text, encoding="utf-8")
        (directory / "nested").mkdir(exist_ok=True)
        (directory / "nested" / "data.json").write_text(json.dumps({"label": label}) + "\n", encoding="utf-8")
        return directory

    def _candidate_file(self, label: str, text: str) -> pathlib.Path:
        path = self.candidates / (label + ".txt")
        path.write_text(text, encoding="utf-8")
        return path

    def _fp(self, label: str) -> str:
        return self.commit.compute_input_fingerprint({"label": label})

    def _target_rel(self, name: str) -> str:
        return rel(self.targets / name)

    def test_different_operations_same_file_target(self) -> None:
        file_a = self._candidate_file("a", "winner-A\n")
        file_b = self._candidate_file("b", "winner-B\n")
        target = self._target_rel("same-target.txt")
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: dict[str, str] = {}

        def worker(label: str, source: pathlib.Path) -> None:
            try:
                barrier.wait(timeout=15)
                results[label] = self.commit.publish_file(
                    source, target,
                    operation_id="f1-file-" + label,
                    attempt_id="attempt-" + label,
                    input_fingerprint=self._fp("file-" + label),
                    operations_root=self.ops, root=ROOT,
                )
            except BaseException as exc:  # noqa: BLE001 - convert to assertion evidence
                errors[label] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=("a", file_a)),
                   threading.Thread(target=worker, args=("b", file_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, {}, errors)
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        winner = next(label for label, status in statuses.items() if status == "committed")
        loser = next(label for label, status in statuses.items() if status == "failed")
        winner_source = file_a if winner == "a" else file_b
        target_abs = ROOT / target
        self.assertEqual(target_abs.read_text(encoding="utf-8"), winner_source.read_text(encoding="utf-8"))
        loser_codes = [issue["code"] for issue in results[loser].issues]
        self.assertTrue(any(code in {"target_exists", "target_locked", "publish_failed"} for code in loser_codes),
                        loser_codes)
        # winner replay must remain replayed and byte-identical
        replay = self.commit.publish_file(
            winner_source, target, operation_id="f1-file-" + winner,
            attempt_id="attempt-replay", input_fingerprint=self._fp("file-" + winner),
            operations_root=self.ops, root=ROOT,
        )
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertEqual(target_abs.read_text(encoding="utf-8"), winner_source.read_text(encoding="utf-8"))
        # loser retry must not delete/alter the winner
        retry = self.commit.publish_file(
            file_b if loser == "b" else file_a, target, operation_id="f1-file-" + loser,
            attempt_id="attempt-retry", input_fingerprint=self._fp("file-" + loser),
            operations_root=self.ops, root=ROOT,
        )
        self.assertIn(retry.status, ("failed", "conflict"), retry.to_dict())
        self.assertEqual(target_abs.read_text(encoding="utf-8"), winner_source.read_text(encoding="utf-8"))
        self.record(statuses=statuses, loser_codes=loser_codes, winner=winner,
                    replay=replay.status, retry=retry.status)

    def test_different_operations_same_directory_target(self) -> None:
        candidate_a = self._candidate_dir("dir-a", "A\n")
        candidate_b = self._candidate_dir("dir-b", "B\n")
        target = self._target_rel("same-dir")
        barrier = threading.Barrier(2)
        results: dict[str, object] = {}
        errors: dict[str, str] = {}

        def worker(label: str, candidate: pathlib.Path) -> None:
            try:
                barrier.wait(timeout=15)
                results[label] = self.commit.publish_run_dir(
                    candidate, target,
                    operation_id="f1-dir-" + label,
                    attempt_id="attempt-" + label,
                    input_fingerprint=self._fp("dir-" + label),
                    operations_root=self.ops, root=ROOT,
                )
            except BaseException as exc:  # noqa: BLE001
                errors[label] = f"{type(exc).__name__}: {exc}"

        threads = [threading.Thread(target=worker, args=("a", candidate_a)),
                   threading.Thread(target=worker, args=("b", candidate_b))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(errors, {}, errors)
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        winner = next(label for label, status in statuses.items() if status == "committed")
        loser = next(label for label, status in statuses.items() if status == "failed")
        winner_dir = candidate_a if winner == "a" else candidate_b
        target_abs = ROOT / target
        self.assertTrue(target_abs.is_dir(), "冲突后成功方目录消失")
        winner_sha = self.atomic_io.tree_sha256(winner_dir)
        self.assertEqual(self.atomic_io.tree_sha256(target_abs), winner_sha, "成功方目录被失败方改变")
        replay = self.commit.publish_run_dir(
            winner_dir, target, operation_id="f1-dir-" + winner,
            attempt_id="attempt-replay", input_fingerprint=self._fp("dir-" + winner),
            operations_root=self.ops, root=ROOT,
        )
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertEqual(self.atomic_io.tree_sha256(target_abs), winner_sha)
        loser_codes = [issue["code"] for issue in results[loser].issues]
        self.record(statuses=statuses, winner=winner, loser_codes=loser_codes,
                    replay=replay.status, tree_sha=winner_sha)

    def test_lock_wait_and_publish_exception_return_failed_without_raise(self) -> None:
        file_a = self._candidate_file("lock", "locked\n")
        target = self._target_rel("locked.txt")
        # operation lock held by a foreign lock file
        lock_dir = self.ops / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        op_lock = lock_dir / "f1-lock-op.lock"
        op_lock.write_text("held\n", encoding="utf-8")
        try:
            result = self.commit.publish_file(
                file_a, target, operation_id="f1-lock-op", attempt_id="a1",
                input_fingerprint=self._fp("lock-op"), operations_root=self.ops, root=ROOT,
                lock_timeout_seconds=0.3,
            )
        finally:
            op_lock.unlink(missing_ok=True)
        self.assertEqual(result.status, "failed", result.to_dict())
        self.assertIn("operation_locked", [issue["code"] for issue in result.issues])
        # publish target lock held by a foreign lock file: commit must return
        # a failed result (publish_failed/target_locked), never raise.
        target_lock = self.atomic_io.publish_lock_path(ROOT / target)
        target_lock.parent.mkdir(parents=True, exist_ok=True)
        target_lock.write_text("held\n", encoding="utf-8")
        try:
            target_result = self.commit.publish_file(
                file_a, target, operation_id="f1-target-lock-op", attempt_id="a1",
                input_fingerprint=self._fp("target-lock-op"), operations_root=self.ops, root=ROOT,
                target_lock_timeout_seconds=0.3,
            )
        finally:
            target_lock.unlink(missing_ok=True)
        self.assertEqual(target_result.status, "failed", target_result.to_dict())
        self.assertTrue(any(code in {"publish_failed", "target_locked"}
                            for code in [issue["code"] for issue in target_result.issues]),
                        target_result.to_dict())
        # publish exception is converted to failed result, not raised
        exception_target = self._target_rel("exception.txt")
        with mock.patch.object(self.atomic_io, "atomic_publish_file", side_effect=OSError("injected publish failure")):
            exception_result = self.commit.publish_file(
                file_a, exception_target, operation_id="f1-exception-op", attempt_id="a1",
                input_fingerprint=self._fp("exception-op"), operations_root=self.ops, root=ROOT,
            )
        self.assertEqual(exception_result.status, "failed", exception_result.to_dict())
        self.assertIn("publish_failed", [issue["code"] for issue in exception_result.issues])
        self.assertFalse((ROOT / exception_target).exists(), "发布异常后不得留下正式产物")
        self.record(operation_lock=result.status, target_lock=target_result.status,
                    publish_exception=exception_result.status)


# ---------------------------------------------------------------------------
# P4-F2: same-path override input content changes
# ---------------------------------------------------------------------------
class F2OverrideInputAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = self._testMethodName
        self.root = work_dir("f2-" + self.name)
        self.runs = self.root / "runs"
        self.cache = self.root / "cache"
        self.inputs = self.root / "inputs"
        for directory in (self.runs, self.cache, self.inputs):
            directory.mkdir(parents=True, exist_ok=True)
        self._records: list[dict] = []

    def tearDown(self) -> None:
        path = LOG_DIR / f"review2-f2-{self.name}.json"
        path.write_text(json.dumps({"test": self.name, "records": self._records}, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "F2::" + self.name, "status": status, "details": self._records})

    def prepare(self, *, run_id: str, case: str, main_skill: str, extra: list[str],
                expect_success: bool = True) -> tuple[subprocess.CompletedProcess, dict, dict]:
        args = prepare_command(case, run_id=run_id, out_root=self.runs, cache_root=self.cache,
                               extra=extra, main_skill=main_skill)
        completed = run_process(args, timeout=300)
        save_log(f"f2-{self.name}-{run_id}", completed)
        state_path = self.runs / run_id / "pipeline-state.json"
        state = read_json(state_path) if state_path.is_file() else {}
        statuses = stage_cache(state) if state else {}
        cache_counts = parse_prepare_cache(completed.stdout)
        child_calls = parse_child_calls(completed.stdout)
        if expect_success:
            self.assertEqual(completed.returncode, 0,
                             f"{run_id} 期望成功：{completed.stdout[-3000:]}{completed.stderr[-1000:]}")
        else:
            self.assertNotEqual(completed.returncode, 0,
                                f"{run_id} 期望失败：{completed.stdout[-2000:]}{completed.stderr[-1000:]}")
        return completed, statuses, {"cache": cache_counts, "child_calls": child_calls, "state": state}

    def _assert_no_stale_hit(self, statuses: dict, *, must_miss: tuple[str, ...], details: dict) -> None:
        for stage in must_miss:
            self.assertIn(statuses.get(stage), {"miss", "disabled"},
                          f"{stage} 必须失效重跑，实际 {statuses}: {details}")
        self.assertGreaterEqual(details["child_calls"], 1, f"应有实际子进程执行：{details}")

    def test_same_path_save_content_change_invalid_delete(self) -> None:
        save_copy = self.inputs / "save-copy"
        shutil.copyfile(ROOT / "data/raw/saves/2026-11-07/1CHARACTERSLOT_BETA_1", save_copy)
        cold, cold_statuses, cold_details = self.prepare(
            run_id="cold", case=FR_CASE, main_skill="fr11mv",
            extra=["--save", rel(save_copy)],
        )
        self.assertTrue(cold_statuses, "cold run 缺少阶段状态")
        self.assertGreaterEqual(cold_details["child_calls"], 1)
        # 同一路径替换为另一份有效存档 → 对应阶段+下游 miss，并得到新结果
        shutil.copyfile(ROOT / "data/raw/saves/2026-11-05/1CHARACTERSLOT_BETA_2", save_copy)
        changed, changed_statuses, changed_details = self.prepare(
            run_id="valid-change", case=FR_CASE, main_skill="fr11mv",
            extra=["--save", rel(save_copy)],
        )
        self._assert_no_stale_hit(changed_statuses, must_miss=("freeze_input", "parse_input"),
                                  details=changed_details)
        for stage, status in changed_statuses.items():
            if stage in ("freeze_input", "parse_input"):
                continue
            self.assertIn(status, {"miss", "disabled", "cached"},
                          f"内容变化后下游阶段 {stage} 不应 hit 旧内容：{changed_statuses}")
        cold_input = cold_details["state"]["stages"][0]["input_fingerprint"]
        changed_input = changed_details["state"]["stages"][0]["input_fingerprint"]
        self.assertNotEqual(cold_input, changed_input, "同一路径内容变化未改变 freeze_input 指纹")
        bd_before = sha256_file(self.runs / "cold" / "steps/01-save-import/bd.json")
        bd_after = sha256_file(self.runs / "valid-change" / "steps/01-save-import/bd.json")
        self.assertNotEqual(bd_before, bd_after, "有效内容变更仍复用旧 bd.json")
        # 同一路径无效内容 → 正常失败，不得全部 hit
        save_copy.write_text("not-a-valid-save", encoding="utf-8")
        invalid, invalid_statuses, invalid_details = self.prepare(
            run_id="invalid", case=FR_CASE, main_skill="fr11mv",
            extra=["--save", rel(save_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(invalid_statuses, must_miss=("freeze_input",), details=invalid_details)
        self.assertNotEqual(invalid_statuses.get("parse_input"), "hit")
        # --no-cache 对照：同样失败且不 hit
        invalid_nc, invalid_nc_statuses, invalid_nc_details = self.prepare(
            run_id="invalid-nocache", case=FR_CASE, main_skill="fr11mv",
            extra=["--save", rel(save_copy), "--no-cache"], expect_success=False,
        )
        self.assertGreaterEqual(invalid_nc_details["child_calls"], 1)
        self.assertNotEqual(invalid_nc_statuses.get("parse_input"), "hit")
        # 删除同一路径 → 正常失败，不复用旧结果
        save_copy.unlink()
        deleted, deleted_statuses, deleted_details = self.prepare(
            run_id="deleted", case=FR_CASE, main_skill="fr11mv",
            extra=["--save", rel(save_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(deleted_statuses, must_miss=("freeze_input",), details=deleted_details)
        self.assertNotEqual(deleted_statuses.get("parse_input"), "hit")
        self._records.append({
            "cold": cold_statuses, "valid_change": changed_statuses,
            "invalid": invalid_statuses, "invalid_nocache": invalid_nc_statuses,
            "deleted": deleted_statuses,
            "freeze_fingerprints": {"cold": cold_input, "changed": changed_input},
            "bd_sha": {"cold": bd_before, "changed": bd_after},
        })

    def test_same_path_planner_content_change_invalid_delete(self) -> None:
        planner_copy = self.inputs / "planner-copy.json"
        shutil.copyfile(ROOT / "examples/black-hole/inputs/planner-build-response.json", planner_copy)
        cold, cold_statuses, cold_details = self.prepare(
            run_id="cold", case=BH_CASE, main_skill="bh2",
            extra=["--planner", rel(planner_copy)],
        )
        payload = read_json(planner_copy)
        payload["_f2_note"] = "same-path planner content change"
        planner_copy.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed, changed_statuses, changed_details = self.prepare(
            run_id="valid-change", case=BH_CASE, main_skill="bh2",
            extra=["--planner", rel(planner_copy)],
        )
        self._assert_no_stale_hit(changed_statuses, must_miss=("freeze_input", "parse_input"),
                                  details=changed_details)
        cold_input = cold_details["state"]["stages"][0]["input_fingerprint"]
        changed_input = changed_details["state"]["stages"][0]["input_fingerprint"]
        self.assertNotEqual(cold_input, changed_input)
        planner_copy.write_text("{not valid json", encoding="utf-8")
        invalid, invalid_statuses, invalid_details = self.prepare(
            run_id="invalid", case=BH_CASE, main_skill="bh2",
            extra=["--planner", rel(planner_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(invalid_statuses, must_miss=("freeze_input",), details=invalid_details)
        self.assertNotEqual(invalid_statuses.get("parse_input"), "hit")
        invalid_nc, invalid_nc_statuses, invalid_nc_details = self.prepare(
            run_id="invalid-nocache", case=BH_CASE, main_skill="bh2",
            extra=["--planner", rel(planner_copy), "--no-cache"], expect_success=False,
        )
        self.assertGreaterEqual(invalid_nc_details["child_calls"], 1)
        planner_copy.unlink()
        deleted, deleted_statuses, deleted_details = self.prepare(
            run_id="deleted", case=BH_CASE, main_skill="bh2",
            extra=["--planner", rel(planner_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(deleted_statuses, must_miss=("freeze_input",), details=deleted_details)
        self._records.append({"cold": cold_statuses, "valid_change": changed_statuses,
                              "invalid": invalid_statuses, "invalid_nocache": invalid_nc_statuses,
                              "deleted": deleted_statuses,
                              "freeze_fingerprints": {"cold": cold_input, "changed": changed_input}})

    def test_same_path_stash_content_change_invalid_delete(self) -> None:
        stash_copy = self.inputs / "stash-copy"
        shutil.copyfile(ROOT / "data/raw/saves/2026-11-05/STASH_0", stash_copy)
        cold, cold_statuses, cold_details = self.prepare(
            run_id="cold", case=BH_CASE, main_skill="bh2",
            extra=["--stash", rel(stash_copy)],
        )
        original = stash_copy.read_bytes()
        stash_copy.write_bytes(original + b"\n")
        try:
            changed, changed_statuses, changed_details = self.prepare(
                run_id="valid-change", case=BH_CASE, main_skill="bh2",
                extra=["--stash", rel(stash_copy)], expect_success=False,
            )
            changed_ok = True
        except AssertionError:
            changed_ok = False
            # 允许内容变化导致正常失败；但仍必须失效且不得全部 hit
            changed = None
            changed_statuses = stage_cache(read_json(self.runs / "valid-change" / "pipeline-state.json")) \
                if (self.runs / "valid-change" / "pipeline-state.json").is_file() else {}
            changed_details = {"cache": {}, "child_calls": -1, "state": {}}
        if changed_ok:
            self._assert_no_stale_hit(changed_statuses, must_miss=("freeze_input", "parse_input"),
                                      details=changed_details)
        else:
            self.assertNotEqual(changed_statuses.get("parse_input"), "hit",
                                "stash 内容变化后 parse_input 仍 hit 旧结果")
        # 无效内容 → 正常失败
        stash_copy.write_text("not-a-valid-stash", encoding="utf-8")
        invalid, invalid_statuses, invalid_details = self.prepare(
            run_id="invalid", case=BH_CASE, main_skill="bh2",
            extra=["--stash", rel(stash_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(invalid_statuses, must_miss=("freeze_input",), details=invalid_details)
        self.assertNotEqual(invalid_statuses.get("parse_input"), "hit")
        invalid_nc, invalid_nc_statuses, invalid_nc_details = self.prepare(
            run_id="invalid-nocache", case=BH_CASE, main_skill="bh2",
            extra=["--stash", rel(stash_copy), "--no-cache"], expect_success=False,
        )
        self.assertGreaterEqual(invalid_nc_details["child_calls"], 1)
        self.assertNotEqual(invalid_nc_statuses.get("parse_input"), "hit")
        # 删除 → 正常失败
        stash_copy.unlink()
        deleted, deleted_statuses, deleted_details = self.prepare(
            run_id="deleted", case=BH_CASE, main_skill="bh2",
            extra=["--stash", rel(stash_copy)], expect_success=False,
        )
        self._assert_no_stale_hit(deleted_statuses, must_miss=("freeze_input",), details=deleted_details)
        self._records.append({"cold": cold_statuses, "changed": changed_statuses,
                              "changed_outcome": "success" if changed_ok else "expected-failure",
                              "invalid": invalid_statuses, "invalid_nocache": invalid_nc_statuses,
                              "deleted": deleted_statuses})


# ---------------------------------------------------------------------------
# P4-F3: --resume --no-cache forces actual execution
# ---------------------------------------------------------------------------
class F3ResumeNoCacheAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = self._testMethodName
        self.root = work_dir("f3-" + self.name)
        self.runs = self.root / "runs"
        self.cache = self.root / "cache"
        for directory in (self.runs, self.cache):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "F3::" + self.name, "status": status, "details": getattr(self, "_details", {})})

    def record(self, **details) -> None:
        self._details = details

    def prepare(self, run_id: str, *, extra: list[str] | None = None, env: dict | None = None,
                expect_ok: bool = True) -> tuple[subprocess.CompletedProcess, dict, str]:
        args = prepare_command(FR_CASE, run_id=run_id, out_root=self.runs, cache_root=self.cache,
                               extra=extra, main_skill="fr11mv")
        completed = run_process(args, env=env, timeout=300)
        save_log(f"f3-{self.name}-{run_id}", completed)
        if expect_ok:
            self.assertEqual(completed.returncode, 0, f"{run_id}: {completed.stdout[-3000:]} {completed.stderr[-1000:]}")
        else:
            self.assertNotEqual(completed.returncode, 0, f"{run_id} 应失败")
        state_path = self.runs / run_id / "pipeline-state.json"
        state = read_json(state_path) if state_path.is_file() else {}
        return completed, state, completed.stdout + completed.stderr

    def test_resume_no_cache_executes_and_refreshes(self) -> None:
        cold, cold_state, cold_output = self.prepare("cold")
        self.assertEqual(parse_prepare_cache(cold_output).get("miss"), 8, cold_output[-2000:])
        cold_artifacts = {
            stage: [
                (ref.get("path"), ref.get("sha256"))
                for ref in (entry.get("output_refs") or [])
            ]
            for stage, entry in plan_states_for(cold_state).items()
        }
        cold_copies = {}
        for stage, refs in cold_artifacts.items():
            for path_value, _ in refs:
                source = ROOT / path_value
                if source.is_file():
                    cold_copies[path_value] = source.read_bytes()
        resume, resume_state, resume_output = self.prepare("cold", extra=["--resume", "--no-cache"])
        cache_counts = parse_prepare_cache(resume_output)
        child_calls = parse_child_calls(resume_output)
        self.assertEqual(cache_counts.get("disabled"), 8, f"--resume --no-cache 应 disabled=8：{cache_counts}")
        self.assertEqual(cache_counts.get("state_cached"), 0, f"不得走 state 快速路径：{cache_counts}")
        self.assertGreater(child_calls, 0, "--resume --no-cache 应实际执行子进程")
        resume_artifacts = {
            stage: [(ref.get("path"), ref.get("sha256")) for ref in (entry.get("output_refs") or [])]
            for stage, entry in plan_states_for(resume_state).items()
        }
        self.assertEqual({stage: [path for path, _ in refs] for stage, refs in resume_artifacts.items()},
                         {stage: [path for path, _ in refs] for stage, refs in cold_artifacts.items()},
                         "强制重算后阶段产物文件集应与 cold 一致")
        self.assertEqual(set(cold_copies), {path for refs in resume_artifacts.values() for path, _ in refs},
                         "强制重算后缺少/新增产物文件")
        volatile = {"generated_at", "created_at", "updated_at", "timestamp", "exported_at",
                    "elapsed_ms", "cache_key", "cache_status", "cache", "run_id", "out_dir",
                    "recorded_at"}
        def strip(value):
            if isinstance(value, dict):
                return {key: strip(item) for key, item in value.items() if key not in volatile}
            if isinstance(value, list):
                return [strip(item) for item in value]
            return value
        semantic_diffs = []
        for path_value, original in cold_copies.items():
            current = (ROOT / path_value).read_bytes()
            if original == current:
                continue
            if path_value.endswith(".json"):
                try:
                    left = strip(json.loads(original.decode("utf-8")))
                    right = strip(json.loads(current.decode("utf-8")))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    semantic_diffs.append(path_value)
                    continue
                if left != right:
                    semantic_diffs.append(path_value)
            else:
                semantic_diffs.append(path_value)
        self.assertEqual(semantic_diffs, [], "强制重算后产物语义与 cold 不一致：%r" % semantic_diffs[:5])
        # 刷新之后默认运行恢复 hit
        warm, warm_state, warm_output = self.prepare("warm-after-resume")
        warm_counts = parse_prepare_cache(warm_output)
        self.assertEqual(warm_counts.get("hit"), 8, f"刷新后默认运行应 8/8 hit：{warm_counts}")
        self.assertEqual(parse_child_calls(warm_output), 0)
        self.record(cold_cache=parse_prepare_cache(cold_output), resume_cache=cache_counts,
                    resume_child_calls=child_calls, warm_cache=warm_counts,
                    warm_child_calls=parse_child_calls(warm_output))

    def test_env_disable_resume_equivalent(self) -> None:
        self.prepare("cold")
        env_result, env_state, env_output = self.prepare(
            "cold", extra=["--resume"], env={"LE_PIPELINE_CACHE_DISABLE": "1"}
        )
        counts = parse_prepare_cache(env_output)
        self.assertEqual(counts.get("disabled"), 8, f"env 禁用应 disabled=8：{counts}")
        self.assertEqual(counts.get("state_cached"), 0)
        self.assertGreater(parse_child_calls(env_output), 0)
        warm, _, warm_output = self.prepare("warm-after-env")
        self.assertEqual(parse_prepare_cache(warm_output).get("hit"), 8)
        self.record(env_cache=counts, env_child_calls=parse_child_calls(env_output),
                    warm_cache=parse_prepare_cache(warm_output))


# ---------------------------------------------------------------------------
# P4-F4: staged lifecycle (active attempt protected / crash reclaim)
# ---------------------------------------------------------------------------
class F4StagedLifecycleAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = self._testMethodName
        self.root = work_dir("f4-" + self.name)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "F4::" + self.name, "status": status, "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review2-f4-{self.name}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def args(self, final_dir: pathlib.Path, run_id: str) -> list[str]:
        return [
            sys.executable, "scripts/le_commands.py", "run", "--case", BH_CASE,
            "--out", str(final_dir), "--run-id", run_id,
            "--publish", "staged", "--operations-root", str(self.ops), "--no-report-cache",
        ]

    def popen(self, final_dir: pathlib.Path, run_id: str) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["LE_PIPELINE_CACHE_ROOT"] = str(self.cache)
        return subprocess.Popen(
            self.args(final_dir, run_id), cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

    @staticmethod
    def kill_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:  # pragma: no cover
            try:
                os.kill(pid, 15)
            except OSError:
                pass

    def owned_candidates(self, final_dir: pathlib.Path) -> list[pathlib.Path]:
        staging = final_dir.parent / ".staging"
        if not staging.is_dir():
            return []
        target = rel(final_dir)
        owned = []
        for candidate in staging.iterdir():
            if not candidate.is_dir():
                continue
            marker = staging / f"{candidate.name}.owner.json"
            try:
                payload = read_json(marker)
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("target") or "") == target:
                owned.append(candidate)
        return owned

    def wait_candidate(self, final_dir: pathlib.Path, timeout: float = 90) -> pathlib.Path:
        deadline = time.time() + timeout
        while time.time() < deadline:
            candidates = self.owned_candidates(final_dir)
            if candidates:
                return candidates[0]
            time.sleep(0.01)
        self.fail("未观察到 staged 生命周期候选")

    def assert_valid_final(self, final_dir: pathlib.Path) -> dict:
        self.assertTrue(final_dir.is_dir(), "staged 正式目录不存在")
        manifest = read_json(final_dir / "run-manifest.json")
        files = manifest.get("files") or {}
        self.assertTrue(files, "run-manifest 缺少 files")
        for name, expected in files.items():
            path = final_dir / name
            self.assertTrue(path.is_file(), f"发布缺文件：{name}")
            self.assertEqual(sha256_file(path), str(expected), f"发布文件 sha 不一致：{name}")
        self.assertTrue((manifest.get("consistency") or {}).get("all_pass"))
        state = read_json(final_dir / "pipeline-state.json")
        self.assertEqual(state.get("schema_version"), "le-pipeline-state-v1")
        self.assertIn(str((state.get("last_commit") or {}).get("status") or ""), ("committed", "replayed"))
        return manifest

    def test_active_attempt_is_not_deleted_and_second_waits(self) -> None:
        final_dir = self.out / "live"
        first = self.popen(final_dir, "f4-live")
        first_output = ""
        try:
            candidate = self.wait_candidate(final_dir)
            second = self.popen(final_dir, "f4-live")
            second_output = ""
            try:
                time.sleep(0.6)
                self.assertIsNone(first.poll(), "A 应仍在运行")
                self.assertIsNone(second.poll(), "B 应等待活跃租约，而不是删除 A 的候选")
                self.assertTrue(candidate.is_dir(), "A 的候选被 B 删除")
                self.assertFalse(final_dir.exists(), "提交完成前正式目录不得出现")
                first_output, _ = first.communicate(timeout=180)
                second_output, _ = second.communicate(timeout=180)
            finally:
                if second.poll() is None:
                    self.kill_tree(second.pid)
        finally:
            if first.poll() is None:
                self.kill_tree(first.pid)
        self.assertEqual(first.returncode, 0, first_output[-3000:])
        self.assertEqual(second.returncode, 0, second_output[-3000:])
        self.assertIn("staged 发布：committed", first_output)
        self.assertIn("另一个 staged run 正在执行同一目标", second_output)
        self.assertRegex(second_output, r"staged 发布：(replayed|committed)")
        self.assert_valid_final(final_dir)
        self.assertEqual(self.owned_candidates(final_dir), [], "成功后不得残留本目标候选")
        records = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        self.assertEqual(len(records), 1, "同一 run 只应有一份操作记录")
        self.record(candidate=candidate.name, first="committed", second=second_output[-200:] if len(second_output) < 200 else "waited",
                    records=len(records))

    def test_crashed_attempt_reclaimed_by_waiter_and_retry(self) -> None:
        final_dir = self.out / "crash"
        first = self.popen(final_dir, "f4-crash")
        first_output = ""
        try:
            candidate = self.wait_candidate(final_dir)
            second = self.popen(final_dir, "f4-crash")
            second_output = ""
            try:
                time.sleep(0.5)
                self.assertIsNone(first.poll(), "A 应在运行")
                self.assertIsNone(second.poll(), "B 应等待活跃租约")
                self.kill_tree(first.pid)
                try:
                    first_output, _ = first.communicate(timeout=30)
                except subprocess.TimeoutExpired:
                    self.kill_tree(first.pid)
                self.assertFalse(final_dir.exists(), "A 崩溃后正式目录不得存在半成品")
                self.assertTrue(candidate.is_dir(), "崩溃候选应保留供回收")
                second_output, _ = second.communicate(timeout=180)
            finally:
                if second.poll() is None:
                    self.kill_tree(second.pid)
        finally:
            if first.poll() is None:
                self.kill_tree(first.pid)
        self.assertEqual(second.returncode, 0, second_output[-4000:])
        self.assertIn("staged 发布：committed", second_output)
        self.assertIn("已清理", second_output)
        self.assert_valid_final(final_dir)
        self.assertEqual(self.owned_candidates(final_dir), [], "回收后不得残留候选")
        records = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        self.assertEqual(len(records), 1)
        self.record(crashed_candidate=candidate.name, waiter_result="committed",
                    records=len(records), second_tail=second_output[-300:])


# ---------------------------------------------------------------------------
# P4-G1: formal run stage graph (simulate/validate/render/commit)
# ---------------------------------------------------------------------------
class G1RunStageAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = self._testMethodName
        self.root = work_dir("g1-" + self.name)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "G1::" + self.name, "status": status, "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review2-g1-{self.name}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def run_cli(self, case: str, final_dir: pathlib.Path, run_id: str, *extra: str,
                expect_ok: bool = True) -> tuple[subprocess.CompletedProcess, dict[str, str], str]:
        args = [sys.executable, "scripts/le_commands.py", "run", "--case", case,
                "--out", str(final_dir), "--run-id", run_id, "--publish", "staged",
                "--operations-root", str(self.ops), *extra]
        completed = run_process(args, env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)}, timeout=600)
        save_log(f"g1-{self.name}-{run_id}", completed)
        output = completed.stdout + "\n" + completed.stderr
        statuses = parse_stage_statuses(output)
        if expect_ok:
            self.assertEqual(completed.returncode, 0,
                             f"{run_id} 失败：{completed.stdout[-4000:]}{completed.stderr[-2000:]}")
        else:
            self.assertNotEqual(completed.returncode, 0, f"{run_id} 应失败：{output[-3000:]}")
        return completed, statuses, output

    def assert_valid_final(self, final_dir: pathlib.Path) -> dict:
        self.assertTrue(final_dir.is_dir(), f"正式目录不存在：{final_dir}")
        manifest = read_json(final_dir / "run-manifest.json")
        files = manifest.get("files") or {}
        for name, expected in files.items():
            path = final_dir / name
            self.assertTrue(path.is_file(), f"发布缺文件：{name}")
            self.assertEqual(sha256_file(path), str(expected), f"发布文件 sha 不一致：{name}")
        self.assertTrue((manifest.get("consistency") or {}).get("all_pass"))
        validation = read_json(final_dir / "validation-result.json")
        self.assertTrue(validation.get("passed"), validation)
        self.assertTrue(validation.get("accepted"), validation)
        return {"manifest": manifest, "validation": validation}

    @staticmethod
    def no_run_metadata(result: dict) -> dict:
        copy = json.loads(json.dumps(result, ensure_ascii=False))
        copy.pop("run", None)
        return copy

    def test_bh_same_input_reuses_stages_and_no_cache_executes_all(self) -> None:
        final_dir = self.out / "bh-replay"
        first, first_statuses, first_output = self.run_cli(BH_CASE, final_dir, "g1-bh-replay",
                                                           "--no-report-cache")
        self.assertIn("staged 发布：committed", first_output)
        valid = self.assert_valid_final(final_dir)
        state_bytes = (final_dir / "pipeline-state.json").read_bytes()
        second, second_statuses, second_output = self.run_cli(BH_CASE, final_dir, "g1-bh-replay",
                                                              "--no-report-cache")
        self.assertIn("staged 发布：replayed", second_output)
        self.assertEqual(second_statuses.get("simulate"), "hit", second_statuses)
        self.assertEqual(second_statuses.get("validate_result"), "hit", second_statuses)
        self.assertEqual(second_statuses.get("render_report"), "hit", second_statuses)
        self.assertEqual((final_dir / "pipeline-state.json").read_bytes(), state_bytes,
                         "replayed 不得改变最终 pipeline-state")
        # --no-cache 全部执行
        nocache_dir = self.out / "bh-nocache"
        nocache, nocache_statuses, nocache_output = self.run_cli(
            BH_CASE, nocache_dir, "g1-bh-nocache", "--no-cache"
        )
        for stage in ("simulate", "validate_result", "render_report", "commit"):
            self.assertEqual(nocache_statuses.get(stage), "disabled", f"{stage}: {nocache_statuses}")
        self.assertIn("staged 发布：committed", nocache_output)
        self.assert_valid_final(nocache_dir)
        self.record(first_statuses=first_statuses, replay_statuses=second_statuses,
                    nocache_statuses=nocache_statuses,
                    validation_accepted=len(valid["validation"].get("accepted") or []))

    def test_fr_endpoint_only_rerenders_and_numbers_stay(self) -> None:
        base_dir = self.out / "fr-base"
        base, base_statuses, base_output = self.run_cli(
            FR_CASE, base_dir, "g1-fr-base", "--no-report-cache", "--port", "8765"
        )
        self.assertIn("staged 发布：committed", base_output)
        self.assert_valid_final(base_dir)
        changed_dir = self.out / "fr-endpoint-8899"
        changed, changed_statuses, changed_output = self.run_cli(
            FR_CASE, changed_dir, "g1-fr-endpoint", "--no-report-cache", "--port", "8899"
        )
        self.assertIn("staged 发布：committed", changed_output)
        self.assertEqual(changed_statuses.get("simulate"), "hit", changed_statuses)
        self.assertEqual(changed_statuses.get("validate_result"), "hit", changed_statuses)
        self.assertEqual(changed_statuses.get("render_report"), "miss", changed_statuses)
        self.assert_valid_final(changed_dir)
        base_result = read_json(base_dir / "result.json")
        changed_result = read_json(changed_dir / "result.json")
        self.assertEqual(self.no_run_metadata(base_result), self.no_run_metadata(changed_result),
                         "只改端点不应改变数值/结果")
        base_html = (base_dir / "flame-reave-damage.html").read_text(encoding="utf-8")
        changed_html = (changed_dir / "flame-reave-damage.html").read_text(encoding="utf-8")
        self.assertNotEqual(base_html, changed_html, "端点变化后 HTML 必须重渲染")
        self.assertIn("8899", changed_html)
        # FR 同输入 replay 也应在 stage 命中后 replayed
        replay, replay_statuses, replay_output = self.run_cli(
            FR_CASE, base_dir, "g1-fr-base", "--no-report-cache", "--port", "8765"
        )
        self.assertIn("staged 发布：replayed", replay_output)
        self.assertIn(replay_statuses.get("simulate"), ("hit",), replay_statuses)
        self.record(base_statuses=base_statuses, endpoint_statuses=changed_statuses,
                    replay_statuses=replay_statuses)

    def test_scenario_change_invalidates_simulate_and_downstream(self) -> None:
        base_dir = self.out / "bh-base"
        self.run_cli(BH_CASE, base_dir, "g1-bh-base", "--no-report-cache")
        base_result = read_json(base_dir / "result.json")
        scenario = read_json(ROOT / "examples/black-hole/scenario/conditional-scenario.json")
        effects = scenario.setdefault("effects", {})
        key = "idol.throne_of_ambition"
        effects.setdefault(key, {})["stacks"] = 3
        scenario_path = self.root / "scenario-changed.json"
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case = read_json(ROOT / BH_CASE)
        case["inputs"]["scenario"] = rel(scenario_path)
        case_path = self.root / "case-scenario-changed.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        changed_dir = self.out / "bh-scenario"
        changed, statuses, output = self.run_cli(
            rel(case_path), changed_dir, "g1-bh-scenario", "--no-report-cache", expect_ok=False
        )
        # 场景内容变化使 simulate 失效重算；案例 expected 仍绑定旧场景，
        # validation 门禁因此失败并拒绝发布（下游 render/commit 不执行）。
        self.assertEqual(statuses.get("simulate"), "miss", statuses)
        self.assertIn(statuses.get("validate_result"), ("miss", "failed"), statuses)
        self.assertNotIn("render_report", statuses, statuses)
        self.assertNotIn("commit", statuses, statuses)
        self.assertFalse(changed_dir.exists(), "validation 未通过不得发布正式目录")
        self.assertIn("validation", output.lower())
        staging = changed_dir.parent / ".staging"
        self.assertTrue(staging.is_dir() and any(staging.iterdir()), "门禁失败应保留候选")
        self.record(statuses=statuses, exit=changed.returncode)

    def test_interrupted_run_retries_and_recovers(self) -> None:
        final_dir = self.out / "bh-interrupted"
        args = [sys.executable, "scripts/le_commands.py", "run", "--case", BH_CASE,
                "--out", str(final_dir), "--run-id", "g1-bh-interrupted",
                "--publish", "staged", "--operations-root", str(self.ops), "--no-report-cache"]
        env = dict(os.environ)
        env["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        env["LE_PIPELINE_CACHE_ROOT"] = str(self.cache)
        process = subprocess.Popen(args, cwd=str(ROOT), env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        try:
            deadline = time.time() + 90
            candidate = None
            while time.time() < deadline and process.poll() is None:
                staging = final_dir.parent / ".staging"
                if staging.is_dir():
                    entries = [path for path in staging.iterdir()
                               if path.is_dir() and path.name.startswith("g1-bh-interrupted-")]
                    if entries:
                        candidate = entries[0]
                        break
                time.sleep(0.01)
            self.assertIsNotNone(candidate, "未观察到中断前的 candidate")
            self.assertFalse(final_dir.exists(), "提交前不得出现正式目录")
            F4StagedLifecycleAcceptanceTests.kill_tree(process.pid)
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                F4StagedLifecycleAcceptanceTests.kill_tree(process.pid)
        finally:
            if process.poll() is None:
                F4StagedLifecycleAcceptanceTests.kill_tree(process.pid)
        self.assertFalse(final_dir.exists(), "中断后不得留下正式目录")
        self.assertTrue(candidate is not None and candidate.is_dir(), "中断候选应保留供回收")
        retry, statuses, output = self.run_cli(BH_CASE, final_dir, "g1-bh-interrupted", "--no-report-cache")
        self.assertIn("已清理", output)
        self.assertIn("staged 发布：committed", output)
        self.assert_valid_final(final_dir)
        self.assertEqual(F4StagedLifecycleAcceptanceTests.owned_candidates(self, final_dir), [],
                         "恢复成功后不得残留候选")
        self.record(statuses=statuses, recovered=True)

    def test_validation_gate_blocks_publish(self) -> None:
        case = read_json(ROOT / BH_CASE)
        expected = read_json(ROOT / case["expected"])
        expected["checks"][0]["expected"] = float(expected["checks"][0]["expected"]) + 1000.0
        bad_expected = self.root / "expected-bad.json"
        bad_expected.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["expected"] = rel(bad_expected)
        bad_case = self.root / "case-bad.json"
        bad_case.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        final_dir = self.out / "bh-gate"
        records_before = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        completed, statuses, output = self.run_cli(
            rel(bad_case), final_dir, "g1-bh-gate", "--no-report-cache", expect_ok=False
        )
        self.assertFalse(final_dir.exists(), "validation 未通过不得发布正式目录")
        staged = final_dir.parent / ".staging"
        self.assertTrue(staged.is_dir() and any(staged.iterdir()), "validation 门禁应保留候选供排查")
        records_after = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        self.assertEqual(records_after, records_before, "validation 未通过不得产生提交记录")
        self.assertIn(statuses.get("validate_result"), ("failed", "miss", "disabled"), statuses)
        self.assertIn("validation", output.lower())
        self.record(statuses=statuses, exit=completed.returncode, records=len(records_after))


# ---------------------------------------------------------------------------
# module entry
# ---------------------------------------------------------------------------
def setUpModule() -> None:  # noqa: N802
    from _author_assets import require_author_assets
    require_author_assets()
    SESSION.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS.clear()
    MODULE_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def write_summary() -> None:
    passed = sum(1 for record in RECORDS if record["status"] == "pass")
    failed = sum(1 for record in RECORDS if record["status"] == "fail")
    summary = {
        "schema": "p4-review2-acceptance-v1",
        "session": STAMP,
        "started_at": MODULE_STATE.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "work_root": "reports/p4-acceptance/work/review2-" + STAMP,
        "summary": {"pass": passed, "fail": failed, "total": len(RECORDS)},
        "tests": RECORDS,
    }
    (P4_ROOT / "review2-acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def tearDownModule() -> None:  # noqa: N802
    write_summary()


if __name__ == "__main__":
    unittest.main()
