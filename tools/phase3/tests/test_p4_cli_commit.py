# -*- coding: utf-8 -*-
"""P4 staged-publish CLI tests (``run --publish staged`` + commit_operation)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)
CALC_SRC = str(ROOT / "tools" / "phase3" / "src")
if CALC_SRC not in sys.path:
    sys.path.insert(0, CALC_SRC)

BH_CASE = "examples/black-hole/case.legacy.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_digest(directory: Path) -> str:
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rows.append([path.relative_to(directory).as_posix(), sha256_file(path)])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def run_cli(args: list[str], *, timeout: float = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = CALC_SRC + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "scripts/le_commands.py", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
    else:  # pragma: no cover - this repository targets Windows first
        try:
            os.kill(pid, 15)
        except OSError:
            pass


class StagedPublishTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="p4-cli-commit-", dir=SCRATCH)
        cls.root = Path(cls._tmp.name)
        cls.ops = cls.root / "ops"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _args(self, run_id: str, *extra: str) -> list[str]:
        return [
            "run",
            "--case", BH_CASE,
            "--out", str(self.root / run_id),
            "--run-id", run_id,
            "--publish", "staged",
            "--operations-root", str(self.ops),
            *extra,
        ]

    def _record_path(self, run_id: str) -> Path:
        candidates = sorted(self.ops.glob("run-black-hole-*.json")) if self.ops.is_dir() else []
        for path in candidates:
            if run_id.replace(".", "-") in path.stem:
                return path
        # operation id sanitizes some characters; fall back to a single record match
        return candidates[0]

    def _state(self, run_dir: Path) -> dict:
        return json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))

    def _manifest_files_valid(self, run_dir: Path) -> list[str]:
        manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
        problems = []
        for name, expected in (manifest.get("files") or {}).items():
            path = run_dir / name
            if not path.is_file() or sha256_file(path) != str(expected):
                problems.append(name)
        return problems

    def test_staged_success_and_path_semantics(self) -> None:
        run_id = "p4-commit-a"
        args = self._args(run_id)
        before_records = len(list(self.ops.glob("*.json"))) if self.ops.is_dir() else 0
        completed = run_cli(args)
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
        self.assertIn("staged 发布：committed", completed.stdout)
        final_dir = self.root / run_id
        self.assertTrue(final_dir.is_dir(), "正式目录必须存在")
        staging_root = final_dir.parent / ".staging"
        self.assertFalse(
            staging_root.exists()
            and any(path.name.startswith(run_id + "-") for path in staging_root.iterdir()),
            "staged 成功后不得残留本次候选目录",
        )
        # operation record + tree sha
        records = sorted(self.ops.glob("*.json"))
        self.assertEqual(len(records), before_records + 1)
        record = json.loads(records[0].read_text(encoding="utf-8"))
        self.assertEqual(record["status"], "committed")
        self.assertEqual(record["operation_id"], sorted(records)[0].stem)
        self.assertEqual(len(record["published_refs"]), 1)
        from le_calc.workflow import atomic_io

        self.assertEqual(record["published_refs"][0]["sha256"], atomic_io.tree_sha256(final_dir))
        # pipeline-state.last_commit consistent with the operation record
        state = self._state(final_dir)
        self.assertEqual(state["schema_version"], "le-pipeline-state-v1")
        last = state["last_commit"]
        self.assertEqual(last["operation_id"], record["operation_id"])
        self.assertEqual(last["status"], "committed")
        self.assertEqual(last["input_fingerprint"], record["input_fingerprint"])
        self.assertRegex(last["attempt_id"], "^" + run_id + r"-")
        self.assertTrue(last["committed_refs"])
        # manifest hashes + final-path semantics
        self.assertEqual(self._manifest_files_valid(final_dir), [])
        text_paths = [final_dir / "run-manifest.json", final_dir / "result.json",
                      final_dir / "black-hole-damage.html", final_dir / "README.zh-CN.md"]
        for path in text_paths:
            text = path.read_text(encoding="utf-8")
            self.assertNotIn(".staging", text, f"{path.name} 泄漏 staging 路径")
            self.assertIn(run_id, text)
        result = json.loads((final_dir / "result.json").read_text(encoding="utf-8"))
        out_dir = str((result.get("run") or {}).get("out_dir") or "")
        self.assertIn(run_id, out_dir)
        self.assertNotIn(".staging", out_dir)
        # default (non-staged) path is unchanged: direct output, no pipeline-state/operations
        default_dir = self.root / "p4-commit-default"
        default = run_cli(
            ["run", "--case", BH_CASE, "--out", str(default_dir), "--run-id", "p4-commit-default",
             "--no-cache"]
        )
        self.assertEqual(default.returncode, 0, default.stdout[-2000:] + default.stderr[-1000:])
        self.assertTrue((default_dir / "result.json").is_file())
        # G1 routes --publish none through the same stage graph in place; the
        # pipeline-state is written but intentionally not added to the manifest
        # (default manifest parity with staged is asserted by task-44 acceptance).
        self.assertTrue((default_dir / "pipeline-state.json").is_file())
        self.assertTrue((default_dir / "validation-result.json").is_file())
        default_manifest = json.loads((default_dir / "run-manifest.json").read_text(encoding="utf-8"))
        self.assertNotIn("pipeline-state.json", default_manifest.get("files") or {})
        # 默认路径不创建本 run 的 staging 候选（其他 staged 测试的候选互不影响）
        self.assertFalse(
            (default_dir.parent / ".staging").exists()
            and any(
                path.name.startswith("p4-commit-default-")
                for path in (default_dir.parent / ".staging").iterdir()
            )
        )

    def test_replay_same_input_does_not_duplicate_publish(self) -> None:
        run_id = "p4-commit-b"
        args = self._args(run_id)
        first = run_cli(args)
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-1000:])
        final_dir = self.root / run_id
        digest_before = tree_digest(final_dir)
        state_before = (final_dir / "pipeline-state.json").read_bytes()
        records_before = sorted(self.ops.glob("*.json"))
        second = run_cli(args)
        self.assertEqual(second.returncode, 0, second.stdout[-3000:] + second.stderr[-1000:])
        self.assertIn("staged 发布：replayed", second.stdout)
        self.assertEqual(tree_digest(final_dir), digest_before, "replay 不得覆盖/改写正式目录")
        self.assertEqual((final_dir / "pipeline-state.json").read_bytes(), state_before)
        records_after = sorted(self.ops.glob("*.json"))
        self.assertEqual([p.name for p in records_after], [p.name for p in records_before])
        self.assertEqual(self._state(final_dir)["last_commit"]["status"], "committed")

    def test_conflict_different_input_preserves_target(self) -> None:
        run_id = "p4-commit-c"
        first = run_cli(self._args(run_id))
        self.assertEqual(first.returncode, 0, first.stdout[-2000:] + first.stderr[-1000:])
        final_dir = self.root / run_id
        digest_before = tree_digest(final_dir)
        rec_before = (sorted(self.ops.glob("*.json"))[0]).read_bytes()
        conflict = run_cli(self._args(run_id, "--port", "9999"))
        self.assertNotEqual(conflict.returncode, 0, "不同输入必须是非 0")
        combined = conflict.stdout + conflict.stderr
        self.assertIn("conflict", combined.lower())
        self.assertIn("input_fingerprint_conflict", combined)
        self.assertEqual(tree_digest(final_dir), digest_before, "conflict 不得改写正式目录")
        self.assertEqual((sorted(self.ops.glob("*.json"))[0]).read_bytes(), rec_before)
        self.assertIn(run_id, (final_dir / "result.json").read_text(encoding="utf-8"))
        # failed attempt retains its candidate for diagnostics; retrying the
        # original command reclaims exactly that same-target leftover and replays.
        self.assertTrue(self._candidates(final_dir), "冲突失败应保留候选供诊断")
        retry = run_cli(self._args(run_id))
        self.assertEqual(retry.returncode, 0, retry.stdout[-2000:] + retry.stderr[-1000:])
        self.assertIn("staged 发布：replayed", retry.stdout)
        self.assertEqual(self._candidates(final_dir), [])
        self.assertEqual(tree_digest(final_dir), digest_before)

    def test_interrupted_candidate_has_no_final_and_can_resume(self) -> None:
        run_id = "p4-commit-d"
        args = self._args(run_id)
        env = dict(os.environ)
        env["PYTHONPATH"] = CALC_SRC + os.pathsep + env.get("PYTHONPATH", "")
        proc = subprocess.Popen(
            [sys.executable, "scripts/le_commands.py", *args],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        final_dir = self.root / run_id
        staging_root = final_dir.parent / ".staging"
        killed = False
        deadline = time.time() + 90
        try:
            while time.time() < deadline:
                if staging_root.is_dir() and any(staging_root.iterdir()):
                    time.sleep(0.2)
                    kill_tree(proc.pid)
                    killed = True
                    break
                time.sleep(0.02)
            self.assertTrue(killed, "未能在 candidate 阶段中断 run")
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
            self.assertFalse(final_dir.exists(), "中断后正式目录不得存在（无半成品）")
            self.assertTrue(any(staging_root.iterdir()), "staging 候选应保留供恢复/清理")
            retry = run_cli(args)
            self.assertEqual(retry.returncode, 0, retry.stdout[-3000:] + retry.stderr[-1000:])
            self.assertIn("已清理", retry.stdout)
            self.assertTrue(final_dir.is_dir())
            self.assertEqual(self._manifest_files_valid(final_dir), [])
            self.assertTrue(self._state(final_dir)["last_commit"])
            self.assertEqual(
                self._candidates(final_dir),
                [],
                "恢复完成后本次 run 的 stale candidate 应被清理",
            )
        finally:
            if proc.poll() is None:
                kill_tree(proc.pid)

    def test_staged_hit_model_fr(self) -> None:
        run_id = "p4-commit-fr"
        final_dir = self.root / run_id
        completed = run_cli([
            "run", "--case", "examples/flame-reave/case.legacy-hit.json",
            "--out", str(final_dir), "--run-id", run_id,
            "--publish", "staged", "--operations-root", str(self.ops),
        ])
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
        self.assertIn("staged 发布：committed", completed.stdout)
        self.assertTrue(final_dir.is_dir())
        self.assertEqual(self._manifest_files_valid(final_dir), [])
        state = self._state(final_dir)
        self.assertEqual(state["last_commit"]["status"], "committed")
        html = (final_dir / "flame-reave-damage.html").read_text(encoding="utf-8")
        self.assertNotIn(".staging", html)
        self.assertIn(run_id, html)
        result = json.loads((final_dir / "result.json").read_text(encoding="utf-8"))
        self.assertNotIn(".staging", json.dumps(result, ensure_ascii=False))

    def test_no_cache_composes_with_staged(self) -> None:
        run_id = "p4-commit-e"
        completed = run_cli(self._args(run_id, "--no-cache"))
        self.assertEqual(completed.returncode, 0, completed.stdout[-3000:] + completed.stderr[-1000:])
        final_dir = self.root / run_id
        self.assertIn("staged 发布：committed", completed.stdout)
        self.assertEqual(self._manifest_files_valid(final_dir), [])
        self.assertTrue((final_dir / "pipeline-state.json").is_file())


    # -- task-46: --out without --run-id must have a stable logical identity -----
    def _auto_args(self, out_dir: Path, *extra: str) -> list[str]:
        return [
            "run",
            "--case", BH_CASE,
            "--out", str(out_dir),
            "--publish", "staged",
            "--operations-root", str(self.ops),
            *extra,
        ]

    def test_same_command_without_run_id_replays(self) -> None:
        final_dir = self.root / "p4-commit-auto"
        args = self._auto_args(final_dir)
        first = run_cli(args)
        self.assertEqual(first.returncode, 0, first.stdout[-3000:] + first.stderr[-1000:])
        self.assertIn("逻辑 run-id 取输出目录名：p4-commit-auto", first.stdout)
        self.assertIn("staged 发布：committed", first.stdout)
        self.assertTrue(final_dir.is_dir())
        digest_before = tree_digest(final_dir)
        state_before = (final_dir / "pipeline-state.json").read_bytes()
        records_before = sorted(path.name for path in self.ops.glob("*.json"))
        state = self._state(final_dir)
        op_id = state["last_commit"]["operation_id"]
        self.assertEqual(op_id, "run-black-hole-p4-commit-auto")
        self.assertEqual(state["last_commit"]["status"], "committed")
        result = json.loads((final_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(str((result.get("run") or {}).get("run_id")), "p4-commit-auto")
        second = run_cli(args)
        self.assertEqual(second.returncode, 0, second.stdout[-3000:] + second.stderr[-1000:])
        self.assertIn("staged 发布：replayed", second.stdout)
        self.assertEqual(tree_digest(final_dir), digest_before, "replay 不得覆盖/改写正式目录")
        self.assertEqual((final_dir / "pipeline-state.json").read_bytes(), state_before)
        self.assertEqual(sorted(path.name for path in self.ops.glob("*.json")), records_before)
        # same logical id + changed input => conflict, target preserved
        conflict = run_cli(self._auto_args(final_dir, "--port", "9999"))
        self.assertNotEqual(conflict.returncode, 0)
        combined = conflict.stdout + conflict.stderr
        self.assertIn("input_fingerprint_conflict", combined)
        self.assertEqual(tree_digest(final_dir), digest_before)

    def test_without_run_id_different_out_is_independent(self) -> None:
        out_a = self.root / "p4-commit-auto-a"
        out_b = self.root / "p4-commit-auto-b"
        first = run_cli(self._auto_args(out_a))
        self.assertEqual(first.returncode, 0, first.stdout[-3000:] + first.stderr[-1000:])
        second = run_cli(self._auto_args(out_b))
        self.assertEqual(second.returncode, 0, second.stdout[-3000:] + second.stderr[-1000:])
        self.assertIn("staged 发布：committed", first.stdout)
        self.assertIn("staged 发布：committed", second.stdout)
        state_a = self._state(out_a)
        state_b = self._state(out_b)
        self.assertNotEqual(
            state_a["last_commit"]["operation_id"], state_b["last_commit"]["operation_id"]
        )
        self.assertEqual(state_a["last_commit"]["operation_id"], "run-black-hole-p4-commit-auto-a")
        self.assertEqual(state_b["last_commit"]["operation_id"], "run-black-hole-p4-commit-auto-b")
        # replaying one output leaves the other untouched
        digest_b = tree_digest(out_b)
        replay = run_cli(self._auto_args(out_a))
        self.assertEqual(replay.returncode, 0, replay.stdout[-2000:] + replay.stderr[-1000:])
        self.assertIn("staged 发布：replayed", replay.stdout)
        self.assertEqual(tree_digest(out_b), digest_b)
        self.assertEqual(
            [p.name for p in sorted(self.ops.glob("*.json"))].count("run-black-hole-p4-commit-auto-a.json"),
            1,
        )


    # -- P4-F4: candidate lifecycle lease (active attempts must not be deleted) --
    def _pop_args(self, final_dir: Path) -> list[str]:
        return [
            sys.executable, "scripts/le_commands.py",
            *self._auto_args(final_dir),
        ]

    def _popen(self, final_dir: Path) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = CALC_SRC + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.Popen(
            self._pop_args(final_dir), cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
        )

    def _wait_candidate(self, final_dir: Path, prefix: str, timeout: float = 90) -> Path:
        staging = final_dir.parent / ".staging"
        deadline = time.time() + timeout
        while time.time() < deadline:
            if staging.is_dir():
                for path in staging.iterdir():
                    if path.is_dir() and path.name.startswith(prefix):
                        return path
            time.sleep(0.01)
        self.fail("未观察到 staged 候选目录")

    def _candidates(self, final_dir: Path) -> list[Path]:
        """Only candidates owned by this exact final target (not other tests)."""
        staging = final_dir.parent / ".staging"
        if not staging.is_dir():
            return []
        try:
            target = final_dir.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            target = str(final_dir.resolve())
        owned: list[Path] = []
        for path in staging.iterdir():
            if not path.is_dir():
                continue
            marker = staging / f"{path.name}.owner.json"
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(payload.get("target") or "") == target:
                owned.append(path)
        return owned

    def test_staged_plan_is_pure_and_active_owner_is_preserved(self) -> None:
        """Review-compat: planning must not create candidates or crash on active owner."""
        import argparse
        import sys as _sys

        scripts = str(ROOT / "scripts")
        if scripts not in _sys.path:
            _sys.path.insert(0, scripts)
        import le_commands  # noqa: PLC0415

        case = le_commands._load_case(argparse.Namespace(case=BH_CASE, save=None, planner=None, stash=None))
        args = argparse.Namespace(
            publish="staged",
            operations_root=str(self.root / "plan-ops"),
            no_report_cache=False,
            no_cache=False,
        )
        final_dir = self.root / "plan-final"
        first = le_commands._staged_plan(case, args, run_id="plan-run", final_dir=final_dir)
        try:
            self.assertIsNotNone(first)
            self.assertFalse(first["candidate"].exists(), "规划阶段不得创建候选目录")
            first["candidate"].mkdir(parents=True)
            (first["candidate"] / "in-progress.json").write_text("{}\n", encoding="utf-8")
            second = le_commands._staged_plan(case, args, run_id="plan-run", final_dir=final_dir)
            self.assertTrue(second.get("wait_required"))
            self.assertTrue(second.get("lease_owner"))
            self.assertTrue(first["candidate"].is_dir(), "活跃候选被误删")
        finally:
            le_commands._release_staging_plan(first)

    def test_active_attempt_is_not_deleted_and_second_waits(self) -> None:
        final_dir = self.root / "p4-commit-concurrent"
        args = self._auto_args(final_dir)
        first = self._popen(final_dir)
        try:
            candidate = self._wait_candidate(final_dir, "p4-commit-concurrent-")
            second = self._popen(final_dir)
            try:
                time.sleep(0.4)
                self.assertIsNone(first.poll(), "第一个进程应仍在执行")
                self.assertIsNone(second.poll(), "第二个进程应等待活跃租约，而不是删除候选")
                self.assertTrue(candidate.is_dir(), "活跃候选被第二个进程删除")
                self.assertFalse(final_dir.exists(), "提交完成前最终目录不得出现")
                first_output, _ = first.communicate(timeout=120)
                second_output, _ = second.communicate(timeout=120)
                if first.stdout is not None:
                    first.stdout.close()
                if second.stdout is not None:
                    second.stdout.close()
            finally:
                if second.poll() is None:
                    kill_tree(second.pid)
        finally:
            if first.poll() is None:
                kill_tree(first.pid)
        self.assertEqual(first.returncode, 0, first_output[-2000:])
        self.assertEqual(second.returncode, 0, second_output[-2000:])
        self.assertIn("staged 发布：committed", first_output)
        self.assertIn("staged 发布：replayed", second_output)
        self.assertTrue(final_dir.is_dir())
        self.assertEqual(self._manifest_files_valid(final_dir), [])
        self.assertEqual(len(operation_files := sorted(self.ops.glob("*.json"))), 1)
        self.assertEqual(self._state(final_dir)["last_commit"]["operation_id"], operation_files[0].stem)
        # 成功完成后不得残留该目标的候选/租约目录
        self.assertEqual(self._candidates(final_dir), [])

    def test_crashed_attempt_is_reclaimed_by_retry(self) -> None:
        final_dir = self.root / "p4-commit-crash"
        args = self._auto_args(final_dir)
        first = self._popen(final_dir)
        try:
            candidate = self._wait_candidate(final_dir, "p4-commit-crash-")
            kill_tree(first.pid)
            try:
                first.wait(timeout=20)
            except subprocess.TimeoutExpired:
                kill_tree(first.pid)
        finally:
            if first.poll() is None:
                kill_tree(first.pid)
            if first.stdout is not None:
                first.stdout.close()
        self.assertFalse(final_dir.exists(), "崩溃后正式目录不得存在")
        self.assertTrue(candidate.is_dir(), "崩溃候选应保留供回收/诊断")
        second = run_cli(args)
        self.assertEqual(second.returncode, 0, second.stdout[-3000:] + second.stderr[-1000:])
        self.assertIn("已清理", second.stdout)
        self.assertIn("staged 发布：committed", second.stdout)
        self.assertTrue(final_dir.is_dir())
        self.assertEqual(self._manifest_files_valid(final_dir), [])
        self.assertEqual(self._candidates(final_dir), [])
        self.assertTrue(self._state(final_dir)["last_commit"])


if __name__ == "__main__":
    unittest.main()
