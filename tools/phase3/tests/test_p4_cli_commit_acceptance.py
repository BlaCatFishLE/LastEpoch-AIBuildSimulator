# -*- coding: utf-8 -*-
"""P4-C2 独立验收：CLI ``run --publish staged`` 的原子/幂等/冲突/中断/路径语义。

对象：``le.ps1 run --publish staged``（candidate → commit.publish_run_dir → 最终目录）。
本文件只做独立验收：真实 CLI 进程 + 真实 commit 记录；不修改实现/别人测试，
不覆盖历史 run、不 commit。证据写入 ``reports/p4-acceptance/**``。

运行：
    $env:PYTHONPATH='tools\\phase3\\src'
    & .\\.venv\\Scripts\\python.exe -m unittest discover -s tools\\phase3/tests -p 'test_p4_cli_commit_acceptance.py' -v
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[3]
P4_ROOT = ROOT / "reports" / "p4-acceptance"
CALC_SRC = ROOT / "tools" / "phase3" / "src"
if str(CALC_SRC) not in sys.path:
    sys.path.insert(0, str(CALC_SRC))

STAMP = time.strftime("%Y%m%dT%H%M%S")
SESSION = P4_ROOT / "work" / ("cli-" + STAMP)
LOG_DIR = P4_ROOT / "logs"

FR_CASE = "examples/flame-reave/case.legacy-hit.json"
BH_CASE = "examples/black-hole/case.legacy.json"

RECORDS: list[dict] = []
MODULE_STATE: dict = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha(path: pathlib.Path) -> str:
    rows = []
    for item in sorted(path.rglob("*")):
        if item.is_file():
            rows.append([item.relative_to(path).as_posix(), sha256_file(item)])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def tree_sha_record(path: pathlib.Path) -> str:
    """Use the implementation's canonical tree digest for record comparison."""
    from le_calc.workflow import atomic_io

    return atomic_io.tree_sha256(path)


def rel(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def absolute_paths(payload: object) -> list[str]:
    pattern = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
    return pattern.findall(json.dumps(payload, ensure_ascii=False, default=str))


def operation_files(ops_root: pathlib.Path) -> list[pathlib.Path]:
    if not ops_root.is_dir():
        return []
    return sorted(path for path in ops_root.glob("*.json") if path.is_file())


def operation_record(ops_root: pathlib.Path, operation_id: str) -> dict:
    path = ops_root / (operation_id + ".json")
    if not path.is_file():
        raise AssertionError(f"缺少操作记录：{path}")
    return read_json(path)


def staged_operation_id(case_id: str, run_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"run-{case_id}-{run_id}").strip("-")
    return safe or "run"


def normalise_run_paths(payload: object, *run_dirs: pathlib.Path) -> object:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"reports/p4-acceptance/work/[^/\\\"]+/cli-[^/\\\"]*/out/[^/\\\"]+/[^/\\\"]+/", "<run>/", text)
    text = re.sub(r"reports\\\\p4-acceptance\\\\work\\\\[^\\\\\"]+\\\\cli-[^\\\\\"]*\\\\out\\\\[^\\\\\"]+\\\\[^\\\\\"]+\\\\", "<run>\\\\", text)
    for run_dir in run_dirs:
        forms = {
            str(run_dir),
            run_dir.as_posix(),
            run_dir.resolve().as_posix(),
            str(run_dir.resolve()),
            run_dir.relative_to(ROOT).as_posix(),
        }
        for form in sorted((item for item in forms if item), key=len, reverse=True):
            text = text.replace(form.replace("\\", "/"), "<run>")
            text = text.replace(form, "<run>")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return payload


VOLATILE_KEYS = {
    "run_id", "generated_at", "out_dir", "attempt_id", "recorded_at", "created_at",
    "elapsed_ms", "cache", "cache_status", "cache_key", "run_key",
}


def strip_volatile(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: strip_volatile(value) for key, value in payload.items() if key not in VOLATILE_KEYS}
    if isinstance(payload, list):
        return [strip_volatile(item) for item in payload]
    return payload


def key_numbers(result: dict) -> dict:
    dps = result.get("dps") or {}
    single = (result.get("single_hit") or {}).get("totals") or {}
    hits = result.get("hits_per_cast") or {}
    values = {
        "dps.damage_per_second": dps.get("damage_per_second"),
        "dps.damage_per_cast": dps.get("damage_per_cast"),
        "dps.casts_per_second": dps.get("casts_per_second"),
        "single_hit.totals.damage": single.get("damage"),
        "single_hit.totals.single_hit_damage": single.get("single_hit_damage"),
        "hits_per_cast.effective_hits_per_cast": hits.get("effective_hits_per_cast"),
    }
    return {key: value for key, value in values.items() if value is not None}


class StagedCliAcceptanceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        P4_ROOT.mkdir(parents=True, exist_ok=True)

    def setUp(self) -> None:
        self.name = self._testMethodName
        self.test_root = SESSION / self.name
        if self.test_root.exists():
            shutil.rmtree(self.test_root)
        (self.test_root / "out").mkdir(parents=True, exist_ok=True)
        self.ops_root = self.test_root / "ops"
        self.ops_root.mkdir(parents=True, exist_ok=True)
        self._commands: list[str] = []

    def tearDown(self) -> None:
        record = {
            "test": self.name,
            "status": "pass",
            "details": getattr(self, "_details", {}),
            "commands": self._commands,
        }
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        if result is not None:
            failures = [message for test, message in (result.failures + result.errors) if test is self]
            if failures:
                record["status"] = "fail"
                record["message"] = failures[-1][-5000:]
        RECORDS.append(record)

    def detail(self, **values) -> None:
        self._details = values

    # -- command helpers -------------------------------------------------
    def command(
        self,
        case: str,
        out_dir: pathlib.Path,
        run_id: str,
        *,
        publish: str = "staged",
        extra: list[str] | None = None,
    ) -> list[str]:
        args = [
            sys.executable,
            "scripts/le_commands.py",
            "run",
            "--case",
            case,
            "--out",
            rel(out_dir),
            "--run-id",
            run_id,
            "--publish",
            publish,
            "--operations-root",
            rel(self.ops_root),
        ]
        args.extend(extra or [])
        return args

    def run_cli(
        self,
        case: str,
        out_dir: pathlib.Path,
        run_id: str,
        *,
        publish: str = "staged",
        extra: list[str] | None = None,
        timeout: float = 300,
        expect_ok: bool = True,
    ) -> tuple[subprocess.CompletedProcess, float, str]:
        args = self.command(case, out_dir, run_id, publish=publish, extra=extra)
        log_path = LOG_DIR / f"cli-staged-{self.name}-{run_id}-{int(time.time() * 1000)}.log"
        began = time.perf_counter()
        completed = subprocess.run(
            args, cwd=str(ROOT), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        elapsed = time.perf_counter() - began
        output = (completed.stdout or "") + "\n--- stderr ---\n" + (completed.stderr or "")
        log_path.write_text(f"command: {' '.join(args)}\nexit: {completed.returncode}\n{output}", encoding="utf-8")
        self._commands.append(" ".join(args))
        if expect_ok and completed.returncode != 0:
            raise AssertionError(
                f"CLI 运行失败（run_id={run_id}, exit={completed.returncode}），日志 {rel(log_path)}\n"
                f"{output[-4000:]}"
            )
        if not expect_ok and completed.returncode == 0:
            raise AssertionError(f"CLI 应失败但 exit=0（run_id={run_id}），日志 {rel(log_path)}\n{output[-3000:]}")
        return completed, elapsed, output

    # -- staged directory checks -----------------------------------------
    def assert_staged_tree(self, final_dir: pathlib.Path, run_id: str, *, case_id: str) -> dict:
        self.assertTrue(final_dir.is_dir(), f"staged 正式目录不存在：{final_dir}")
        staging = final_dir.parent / ".staging"
        candidates = sorted(staging.iterdir()) if staging.is_dir() else []
        leftovers = [path.name for path in candidates if path.is_dir()]
        self.assertEqual(leftovers, [], f"staged 成功/重放后仍有候选目录：{leftovers}")
        manifest = read_json(final_dir / "run-manifest.json")
        consistency = manifest.get("consistency") or {}
        self.assertTrue(consistency.get("all_pass"), f"run-manifest.all_pass 不为 true：{consistency}")
        files = manifest.get("files") or {}
        self.assertTrue(files, "run-manifest 缺少 files 表")
        for name, expected in files.items():
            path = final_dir / name
            self.assertTrue(path.is_file(), f"发布目录缺文件：{name}")
            self.assertEqual(sha256_file(path), str(expected), f"发布文件 sha 不一致：{name}")
        run_block = manifest.get("run") or {}
        out_field = str(run_block.get("out_dir") or "").replace("\\", "/")
        self.assertEqual(out_field, rel(final_dir), f"run-manifest.out_dir 不是最终目录：{out_field!r}")
        state = read_json(final_dir / "pipeline-state.json")
        last_commit = state.get("last_commit") or {}
        operation_id = staged_operation_id(case_id, run_id)
        self.assertEqual(state.get("schema_version"), "le-pipeline-state-v1")
        self.assertEqual(str(last_commit.get("operation_id") or ""), operation_id)
        self.assertIn(str(last_commit.get("status") or ""), ("committed", "replayed"))
        record = operation_record(self.ops_root, operation_id)
        self.assertIn(record.get("status"), ("committed", "replayed"), record)
        self.assertEqual(record.get("operation_id"), operation_id)
        self.assertEqual(str(record.get("input_fingerprint") or ""), str(last_commit.get("input_fingerprint") or ""))
        published = record.get("published_refs") or []
        matched = [ref for ref in published if str(ref.get("path") or "").replace("\\", "/") == rel(final_dir)]
        self.assertTrue(matched, f"操作记录缺少最终目录引用：{published}")
        self.assertEqual(matched[0].get("sha256"), tree_sha_record(final_dir),
                         "操作记录 published tree sha 与最终目录不一致")
        # 路径语义：最终文本产物不得泄漏 staging/candidate 路径
        staging_hits = []
        for path in final_dir.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".html", ".md", ".txt", ".csv"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            if ".staging" in text or "\\staging\\" in text:
                staging_hits.append(path.relative_to(final_dir).as_posix())
        self.assertEqual(staging_hits, [], f"最终目录文本产物泄漏 staging 路径：{staging_hits}")
        # 同一 operation 只应有一份操作记录（其他 run 的记录不计入）
        same_operation = [path for path in operation_files(self.ops_root)
                          if path.stem == operation_id]
        self.assertEqual(len(same_operation), 1, "同一 staged run 只应有一份操作记录：%r" % same_operation)
        return {"manifest": manifest, "state": state, "record": record, "file_count": len(files),
                "tree_sha": tree_sha_record(final_dir)}

    def assert_no_absolute_paths(self, payload: dict, label: str) -> None:
        hits = absolute_paths(payload)
        self.assertEqual(hits, [], f"{label} 泄漏机器绝对路径：{hits[:3]}")


class P4StagedCliAcceptanceTests(StagedCliAcceptanceBase):
    def test_staged_success_atomic_and_default_parity(self) -> None:
        final_dir = self.test_root / "out" / "staged-fr"
        default_dir = self.test_root / "out" / "default-fr"
        staged, staged_elapsed, staged_output = self.run_cli(
            FR_CASE, final_dir, "cli-staged-fr-1", extra=["--no-report-cache"]
        )
        self.assertIn("staged 发布：committed", staged_output)
        details = self.assert_staged_tree(final_dir, "cli-staged-fr-1", case_id="flame-reave")
        operations_before_default = set(operation_files(self.ops_root))
        default, default_elapsed, default_output = self.run_cli(
            FR_CASE, default_dir, "cli-default-fr-1", publish="none", extra=["--no-report-cache"]
        )
        self.assertIn("运行目录", default_output)
        self.assertEqual(set(operation_files(self.ops_root)), operations_before_default,
                         "默认 run 不应新增操作记录")
        # 默认直写：不能出现 .staging，不应有操作记录
        self.assertFalse((default_dir.parent / ".staging").exists() and any((default_dir.parent / ".staging").iterdir()),
                         "默认 run 不应产生 staging 候选")
        # 关键产物与关键数值一致（路径字段归一后比较）
        staged_result = normalise_run_paths(read_json(final_dir / "result.json"), final_dir, default_dir)
        default_result = normalise_run_paths(read_json(default_dir / "result.json"), final_dir, default_dir)
        staged_cmp = strip_volatile(staged_result)
        default_cmp = strip_volatile(default_result)
        staged_cmp.pop("run", None)
        default_cmp.pop("run", None)
        self.assertEqual(staged_cmp, default_cmp,
                         "staged 与默认 run 的 result.json（除 run 元数据外）归一后不一致")
        self.assertEqual(key_numbers(read_json(final_dir / "result.json")),
                         key_numbers(read_json(default_dir / "result.json")),
                         "staged 与默认 run 的关键数值不一致")
        staged_manifest = read_json(final_dir / "run-manifest.json")
        default_manifest = read_json(default_dir / "run-manifest.json")
        staged_files = set((staged_manifest.get("files") or {}).keys())
        default_files = set((default_manifest.get("files") or {}).keys())
        self.assertEqual(staged_files - default_files, {"pipeline-state.json"},
                         "staged 相对默认 run 只应额外有 pipeline-state.json：%r" % (staged_files - default_files))
        self.assertEqual(default_files - staged_files, set(),
                         "staged 缺少默认 run 的产物：%r" % (default_files - staged_files))
        self.assertTrue((final_dir / "flame-reave-damage.html").is_file())
        self.assertTrue(read_json(final_dir / "result.json").get("consistency", {}).get("all_pass", True) is not False)
        self.assert_no_absolute_paths({"record": details["record"], "state": details["state"]}, "staged 记录/state")
        self.detail(staged_elapsed_s=round(staged_elapsed, 3), default_elapsed_s=round(default_elapsed, 3),
                    file_count=details["file_count"], tree_sha=details["tree_sha"],
                    key_numbers=key_numbers(read_json(final_dir / "result.json")))

    def test_staged_final_dir_not_visible_before_publish(self) -> None:
        """候选阶段运行期间最终目录不应出现；退出后才一次性可见。"""
        final_dir = self.test_root / "out" / "atomic-fr"
        args = self.command(FR_CASE, final_dir, "cli-atomic-fr", extra=["--no-report-cache"])
        process = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        staging_root = final_dir.parent / ".staging"
        candidate_seen = False
        final_seen_while_candidate = False
        deadline = time.time() + 60
        while time.time() < deadline and process.poll() is None:
            if staging_root.is_dir() and any(path.is_dir() for path in staging_root.iterdir()):
                candidate_seen = True
                if final_dir.exists():
                    final_seen_while_candidate = True
                break
            time.sleep(0.01)
        self.assertTrue(candidate_seen, "未观察到候选阶段，无法验证发布原子性")
        self.assertFalse(final_seen_while_candidate, "候选阶段尚未完成时最终目录已出现")
        output = process.communicate(timeout=300)[0] or ""
        self.assertEqual(process.returncode, 0, f"staged run 失败：{output[-3000:]}")
        self.assertTrue(final_dir.is_dir(), "退出后最终目录不存在")
        self.assert_staged_tree(final_dir, "cli-atomic-fr", case_id="flame-reave")
        self.assertFalse((final_dir.parent / ".staging").exists()
                         and any((final_dir.parent / ".staging").iterdir()), "发布后仍有 staging 目录")
        self.detail(candidate_seen=True, final_absent_during_candidate=True, exit=process.returncode)

    def test_staged_replay_is_idempotent(self) -> None:
        final_dir = self.test_root / "out" / "replay-fr"
        first, _, first_output = self.run_cli(FR_CASE, final_dir, "cli-replay-fr", extra=["--no-report-cache"])
        self.assertIn("committed", first_output)
        before = self.assert_staged_tree(final_dir, "cli-replay-fr", case_id="flame-reave")
        state_before = sha256_file(final_dir / "pipeline-state.json")
        record_before = sha256_file(self.ops_root / (staged_operation_id("flame-reave", "cli-replay-fr") + ".json"))
        mtimes_before = {path.relative_to(final_dir).as_posix(): path.stat().st_mtime_ns
                         for path in sorted(final_dir.rglob("*")) if path.is_file()}
        second, replay_elapsed, second_output = self.run_cli(FR_CASE, final_dir, "cli-replay-fr",
                                                             extra=["--no-report-cache"])
        self.assertIn("staged 发布：replayed", second_output)
        after = self.assert_staged_tree(final_dir, "cli-replay-fr", case_id="flame-reave")
        self.assertEqual(after["tree_sha"], before["tree_sha"], "replayed 改变了最终目录 tree sha")
        self.assertEqual(sha256_file(final_dir / "pipeline-state.json"), state_before,
                         "replayed 改变了 pipeline-state 字节")
        self.assertEqual(sha256_file(self.ops_root / (staged_operation_id("flame-reave", "cli-replay-fr") + ".json")),
                         record_before, "replayed 改变了操作记录字节")
        mtimes_after = {path.relative_to(final_dir).as_posix(): path.stat().st_mtime_ns
                        for path in sorted(final_dir.rglob("*")) if path.is_file()}
        self.assertEqual(mtimes_after, mtimes_before, "replayed 改动了最终目录文件 mtime")
        self.assertEqual(len(operation_files(self.ops_root)), 1, "replayed 产生了重复操作记录")
        self.assertEqual(after["record"].get("input_fingerprint"), before["record"].get("input_fingerprint"))
        self.assertIn("replayed", second_output)
        self.detail(tree_sha=after["tree_sha"], replay_elapsed_s=round(replay_elapsed, 3),
                    input_fingerprint=after["record"].get("input_fingerprint"))

    def test_staged_conflict_same_runid_different_input(self) -> None:
        final_dir = self.test_root / "out" / "conflict-fr"
        first, _, _ = self.run_cli(FR_CASE, final_dir, "cli-conflict-fr", extra=["--no-report-cache"])
        before = self.assert_staged_tree(final_dir, "cli-conflict-fr", case_id="flame-reave")
        state_before = sha256_file(final_dir / "pipeline-state.json")
        record_before = sha256_file(self.ops_root / (staged_operation_id("flame-reave", "cli-conflict-fr") + ".json"))
        case_dir = self.test_root / "cases"
        case_dir.mkdir(parents=True, exist_ok=True)
        conflict_case = case_dir / "fr-conflict.json"
        payload = read_json(ROOT / FR_CASE)
        payload["notes"] = ["p4 cli conflict acceptance input"]
        conflict_case.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        failed, elapsed, output = self.run_cli(
            rel(conflict_case), final_dir, "cli-conflict-fr",
            extra=["--no-report-cache"], expect_ok=False,
        )
        self.assertIn("conflict", output.lower(), f"冲突输出缺少 conflict：{output[-2000:]}")
        self.assertIn("input_fingerprint_conflict", output)
        self.assertTrue(final_dir.is_dir(), "冲突后正式目录消失")
        self.assertEqual(tree_sha_record(final_dir), before["tree_sha"], "冲突破坏了已有正式目录")
        self.assertEqual(sha256_file(final_dir / "pipeline-state.json"), state_before)
        self.assertEqual(sha256_file(self.ops_root / (staged_operation_id("flame-reave", "cli-conflict-fr") + ".json")),
                         record_before)
        self.assertEqual(len(operation_files(self.ops_root)), 1)
        self.detail(conflict_exit=failed.returncode, conflict_elapsed_s=round(elapsed, 3),
                    tree_sha=before["tree_sha"],
                    staging_left=[p.name for p in (final_dir.parent / ".staging").iterdir()]
                    if (final_dir.parent / ".staging").is_dir() else [])

    def test_target_occupied_rejected_without_damage(self) -> None:
        final_dir = self.test_root / "out" / "occupied-fr"
        final_dir.mkdir(parents=True, exist_ok=True)
        marker = final_dir / "occupied.txt"
        marker.write_text("occupied by another run\n", encoding="utf-8")
        before = tree_sha(final_dir)
        failed, _, output = self.run_cli(FR_CASE, final_dir, "cli-occupied-fr",
                                         extra=["--no-report-cache"], expect_ok=False)
        self.assertIn("staged 发布未成功", output)
        self.assertIn("target_exists", output)
        self.assertEqual(tree_sha(final_dir), before, "目标被占用时正式目录被改动")
        self.assertEqual(marker.read_text(encoding="utf-8"), "occupied by another run\n")
        self.assertFalse((final_dir / "result.json").exists())
        self.detail(exit=failed.returncode, target_exists=True)

    def test_interrupted_staged_no_final_and_retry_cleans(self) -> None:
        parent = self.test_root / "out"
        neighbor_dir = parent / "neighbor-fr"
        neighbor, _, _ = self.run_cli(FR_CASE, neighbor_dir, "cli-neighbor-fr", extra=["--no-report-cache"])
        neighbor_sha = tree_sha_record(neighbor_dir)
        target_dir = parent / "interrupted-fr"
        args = self.command(FR_CASE, target_dir, "cli-interrupted-fr", extra=["--no-report-cache"])
        log_path = LOG_DIR / f"cli-staged-{self.name}-interrupted.log"
        process = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding="utf-8", errors="replace")
        staging_root = target_dir.parent / ".staging"
        candidate = None
        deadline = time.time() + 60
        while time.time() < deadline and process.poll() is None:
            if staging_root.is_dir():
                candidates = [path for path in sorted(staging_root.iterdir())
                              if path.is_dir() and path.name.startswith("cli-interrupted-fr-")]
                if candidates and any(candidates[0].rglob("*")):
                    candidate = candidates[0]
                    break
            time.sleep(0.01)
        self.assertIsNotNone(candidate, "未观察到 staged 候选阶段，无法验证中断")
        self.assertFalse(target_dir.exists(), "发布前正式目录已提前出现")
        self._kill_tree(process.pid)
        try:
            output = process.communicate(timeout=20)[0] or ""
        except subprocess.TimeoutExpired:
            self._kill_tree(process.pid)
            output = ""
        self.assertIsNotNone(process.returncode)
        self.assertFalse(target_dir.exists(), "中断后正式目录存在半成品")
        staging_left = sorted(path.name for path in staging_root.iterdir() if path.is_dir()) if staging_root.is_dir() else []
        self.assertTrue(staging_left, "实现声明：中断候选应保留以便排查/清理")
        log_path.write_text(f"interrupted output tail:\n{output[-3000:]}\nstaging_left={staging_left}\n", encoding="utf-8")
        # 同 run-id 重跑：清理 stale candidate 后成功发布
        retry, _, retry_output = self.run_cli(FR_CASE, target_dir, "cli-interrupted-fr",
                                              extra=["--no-report-cache"])
        self.assertIn("committed", retry_output)
        self.assertIn("已清理", retry_output)
        details = self.assert_staged_tree(target_dir, "cli-interrupted-fr", case_id="flame-reave")
        self.assertEqual(tree_sha_record(neighbor_dir), neighbor_sha, "重跑清理影响了其他 run")
        staging_remaining = sorted(path.name for path in staging_root.iterdir() if path.is_dir()) \
            if staging_root.is_dir() else []
        self.assertEqual(staging_remaining, [], "成功重跑后仍有 stale candidate：%r" % staging_remaining)
        self.detail(staging_left=staging_left, retry_committed=True,
                    target_tree_sha=details["tree_sha"], neighbor_sha=neighbor_sha)

    def test_staged_no_cache_combination_and_default_unchanged(self) -> None:
        final_dir = self.test_root / "out" / "nocache-fr"
        default_dir = self.test_root / "out" / "default-nocache-fr"
        staged, _, output = self.run_cli(FR_CASE, final_dir, "cli-nocache-fr", extra=["--no-cache"])
        self.assertIn("committed", output)
        details = self.assert_staged_tree(final_dir, "cli-nocache-fr", case_id="flame-reave")
        default, _, default_output = self.run_cli(
            FR_CASE, default_dir, "cli-default-nocache-fr", publish="none", extra=["--no-cache"]
        )
        self.assertTrue(default_dir.is_dir())
        self.assertIn("运行目录", default_output)
        self.assertEqual(key_numbers(read_json(final_dir / "result.json")),
                         key_numbers(read_json(default_dir / "result.json")),
                         "--no-cache 下 staged 与默认 run 关键数值不一致")
        self.assertFalse((default_dir.parent / ".staging").exists(), "默认 --no-cache run 产生了 staging")
        self.assert_no_absolute_paths({"record": details["record"], "state": details["state"]}, "--no-cache staged")
        self.detail(file_count=details["file_count"], tree_sha=details["tree_sha"])

    def test_staged_bh_non_hit_success(self) -> None:
        final_dir = self.test_root / "out" / "staged-bh"
        staged, elapsed, output = self.run_cli(BH_CASE, final_dir, "cli-staged-bh", extra=["--no-report-cache"])
        self.assertIn("staged 发布：committed", output)
        details = self.assert_staged_tree(final_dir, "cli-staged-bh", case_id="black-hole")
        self.assertTrue((final_dir / "black-hole-damage.html").is_file())
        result = read_json(final_dir / "result.json")
        self.assertTrue(result.get("consistency", {}).get("all_pass") is not False)
        self.detail(elapsed_s=round(elapsed, 3), file_count=details["file_count"], tree_sha=details["tree_sha"])

    @staticmethod
    def _kill_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:  # pragma: no cover
            try:
                os.kill(pid, 15)
            except OSError:
                pass


def setUpModule() -> None:  # noqa: N802
    from _author_assets import require_author_assets
    require_author_assets()
    SESSION.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    RECORDS.clear()
    MODULE_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def tearDownModule() -> None:  # noqa: N802
    summary = {
        "schema": "p4-cli-commit-acceptance-v1",
        "session": STAMP,
        "started_at": MODULE_STATE.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "commands": {
            "suite": "python -m unittest discover -s tools/phase3/tests -p 'test_p4_cli_commit_acceptance.py' -v",
            "work_root": f"reports/p4-acceptance/work/cli-{STAMP}",
            "cli": "python scripts/le_commands.py run --case <case> --out <dir> --run-id <id> --publish staged --operations-root <ops>",
        },
        "tests": RECORDS,
        "summary": {
            "pass": sum(1 for record in RECORDS if record["status"] == "pass"),
            "fail": sum(1 for record in RECORDS if record["status"] == "fail"),
            "total": len(RECORDS),
        },
    }
    (P4_ROOT / "cli-acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    unittest.main()
