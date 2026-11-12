# -*- coding: utf-8 -*-
"""P4 行为验收（缓存/失效/恢复 + commit 幂等/原子/冲突），独立验证方。

契约：``docs/contracts/workflow-interface.md`` §6/§7/§10/§11；实施方案 §10 P4 与 §11 第 9/14 条。
本文件只做验收：真实 CLI/子进程 + 真实库 API；不修改实现、正式 rules/examples/expected/docs，
不覆盖历史 run，不 git commit。证据写入 ``reports/p4-acceptance/**``。

运行：
    $env:PYTHONPATH='tools\\phase3\\src'
    & .\\.venv\\Scripts\\python.exe -m unittest discover -s tools\\phase3\\tests -p 'test_p4_acceptance.py' -v
"""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
P4_ROOT = ROOT / "reports" / "p4-acceptance"
WORK_ROOT = P4_ROOT / "work"
CALC_SRC = ROOT / "tools" / "phase3" / "src"
if str(CALC_SRC) not in sys.path:
    sys.path.insert(0, str(CALC_SRC))

STAMP = time.strftime("%Y%m%dT%H%M%S")
SESSION = WORK_ROOT / STAMP
OPS_ROOT = SESSION / "ops"
CAND_ROOT = SESSION / "candidates"
PUBLISH_ROOT = SESSION / "published"
LOG_ROOT = P4_ROOT / "logs"

MODULE_STATE: dict = {}
RECORDS: list = []


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(directory: pathlib.Path) -> str:
    """Deterministic directory-tree digest (relative posix path + file sha)."""
    rows = []
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            rows.append([path.relative_to(directory).as_posix(), sha256_file(path)])
    return sha256_bytes(json.dumps(rows, ensure_ascii=False).encode("utf-8"))


def write_candidate(name: str, files: dict[str, str] | None = None) -> pathlib.Path:
    directory = CAND_ROOT / name
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True, exist_ok=True)
    payload = files or {
        "manifest.json": json.dumps({"run": name, "schema_version": "p4-candidate-v1"}, ensure_ascii=False, indent=2) + "\n",
        "nested/result.json": json.dumps({"value": 42, "name": name}, ensure_ascii=False, indent=2) + "\n",
        "notes.txt": "candidate for p4 acceptance\n",
    }
    for relative, content in payload.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    return directory


def reject_absolute_paths(payload: object) -> list[str]:
    import re

    pattern = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
    text = json.dumps(payload, ensure_ascii=False, default=str)
    return pattern.findall(text)


def run_python(args: list[str], *, timeout: float = 300, env: dict | None = None, cwd=ROOT):
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + merged.get("PYTHONPATH", "")
    if env:
        merged.update(env)
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd),
        env=merged,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def commit_module():
    module = MODULE_STATE.get("commit")
    if module is None:
        module = __import__("le_calc.workflow.commit", fromlist=["commit_operation"])
        MODULE_STATE["commit"] = module
    return module


def input_fingerprint(payload: object) -> str:
    commit = commit_module()
    if hasattr(commit, "compute_input_fingerprint"):
        return commit.compute_input_fingerprint(payload)
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def make_request(
    *,
    operation_id: str,
    candidate: pathlib.Path,
    target_rel: str,
    fingerprint: str | None = None,
    candidate_id: str = "run",
    operation_kind: str = "write_run",
    attempt_id: str = "attempt-1",
    preconditions: dict | None = None,
) -> dict:
    reference = {
        "candidate_id": candidate_id,
        "kind": "dir" if candidate.is_dir() else "file",
        "candidate_path": str(candidate),
        "path": target_rel,
        "artifact_id": candidate_id,
    }
    return {
        "schema_version": "le-commit-request-v1",
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "operation_kind": operation_kind,
        "input_fingerprint": fingerprint or input_fingerprint({"operation_id": operation_id, "candidate_sha": tree_sha256(candidate) if candidate.is_dir() else sha256_file(candidate)}),
        "candidate_refs": [reference],
        "preconditions": preconditions or {},
    }


def call_commit(request: dict, *, operations_root: pathlib.Path, name: str) -> dict:
    commit = commit_module()
    result = commit.commit_operation(request, operations_root=operations_root, root=ROOT)
    payload = result.to_dict() if hasattr(result, "to_dict") else dict(result)
    (LOG_ROOT / f"commit-{name}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def issue_codes(result: dict) -> list[str]:
    return [str(issue.get("code") or "") for issue in result.get("issues") or []]


def read_operation_record(operation_id: str, operations_root: pathlib.Path) -> dict:
    commit = commit_module()
    record = commit.read_operation_record(operation_id, operations_root)
    if record is None:
        return {}
    if hasattr(record, "to_dict"):
        record = record.to_dict()
    return dict(record)


# ---------------------------------------------------------------------------
# commit_operation 行为验收
# ---------------------------------------------------------------------------
class P4CommitAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        P4_ROOT.mkdir(parents=True, exist_ok=True)
        for directory in (SESSION, OPS_ROOT, CAND_ROOT, PUBLISH_ROOT, LOG_ROOT, P4_ROOT / "evidence"):
            directory.mkdir(parents=True, exist_ok=True)

    def setUp(self) -> None:
        self.name = self._testMethodName
        self.ops = OPS_ROOT / self.name
        if self.ops.exists():
            shutil.rmtree(self.ops)
        self.ops.mkdir(parents=True, exist_ok=True)
        self.targets: list[pathlib.Path] = []

    def tearDown(self) -> None:
        record = {
            "test": f"commit::{self.name}",
            "status": "pass",
            "details": getattr(self, "_details", {}),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        if result is not None:
            failures = [msg for test, msg in (result.failures + result.errors) if test is self]
            if failures:
                record["status"] = "fail"
                record["message"] = failures[-1][-4000:]
        RECORDS.append(record)

    def record(self, **details) -> None:
        self._details = details

    def _target(self, suffix: str) -> tuple[str, pathlib.Path]:
        relative = f"reports/p4-acceptance/work/{STAMP}/published/{self.name}-{suffix}"
        absolute = ROOT / relative
        if absolute.exists():
            shutil.rmtree(absolute)
        self.targets.append(absolute)
        absolute.parent.mkdir(parents=True, exist_ok=True)
        return relative, absolute

    def test_idempotent_replay_no_duplicate_publish(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("idempotent")
        request = make_request(operation_id="op-replay", candidate=candidate, target_rel=target_rel)
        first = call_commit(request, operations_root=self.ops, name="replay-first")
        self.assertEqual(first["status"], "committed", first)
        self.assertTrue(target.is_dir(), "首次提交未发布正式产物")
        tree_before = tree_sha256(target)
        files_before = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
        second_request = dict(request)
        second_request["attempt_id"] = "attempt-2"
        second = call_commit(second_request, operations_root=self.ops, name="replay-second")
        self.assertEqual(second["status"], "replayed", second)
        self.assertEqual(tree_sha256(target), tree_before, "replayed 不得改动已发布产物")
        files_after = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
        self.assertEqual(files_after, files_before)
        record = read_operation_record("op-replay", self.ops)
        self.assertEqual(record.get("operation_id"), "op-replay")
        self.assertEqual(record.get("input_fingerprint"), request["input_fingerprint"])
        self.assertEqual(record.get("status"), "committed")
        self.record(first=first["status"], second=second["status"], target_tree_sha=tree_before)

    def test_conflict_on_different_input(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("conflict")
        request = make_request(operation_id="op-conflict", candidate=candidate, target_rel=target_rel)
        first = call_commit(request, operations_root=self.ops, name="conflict-first")
        self.assertEqual(first["status"], "committed", first)
        tree_before = tree_sha256(target)
        changed_candidate = write_candidate(self.name + "-changed", {"different.txt": "different input\n"})
        changed = dict(request)
        changed["attempt_id"] = "attempt-2"
        changed["input_fingerprint"] = input_fingerprint({"different": True})
        changed["candidate_refs"] = [
            {
                "candidate_id": "run",
                "kind": "dir",
                "candidate_path": str(changed_candidate),
                "path": target_rel,
            }
        ]
        second = call_commit(changed, operations_root=self.ops, name="conflict-second")
        self.assertEqual(second["status"], "conflict", second)
        self.assertEqual(tree_sha256(target), tree_before, "conflict 不得覆盖已发布产物")
        self.assertEqual(read_operation_record("op-conflict", self.ops)["input_fingerprint"], request["input_fingerprint"])
        self.record(first=first["status"], second=second["status"], codes=issue_codes(second))

    def test_precondition_existing_target_rejected(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("precondition")
        target.mkdir(parents=True, exist_ok=True)
        (target / "occupied.txt").write_text("existing target\n", encoding="utf-8")
        request = make_request(operation_id="op-precondition", candidate=candidate, target_rel=target_rel)
        result = call_commit(request, operations_root=self.ops, name="precondition")
        self.assertIn(result["status"], {"failed", "conflict", "rejected"}, result)
        codes = issue_codes(result)
        self.assertTrue(codes, "前置条件失败应给出明确 issue code：%r" % result)
        self.assertTrue(
            any(code in {"target_exists", "precondition_failed", "conflict", "target_not_empty"} for code in codes),
            codes,
        )
        self.assertEqual((target / "occupied.txt").read_text(encoding="utf-8"), "existing target\n")
        self.assertFalse((target / "manifest.json").exists(), "前置条件失败不得发布候选产物")
        self.record(status=result["status"], codes=codes)

    def test_atomic_publish_no_partial_artifacts(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("atomic")
        request = make_request(operation_id="op-atomic", candidate=candidate, target_rel=target_rel)
        result = call_commit(request, operations_root=self.ops, name="atomic")
        self.assertEqual(result["status"], "committed", result)
        self.assertEqual(tree_sha256(target), tree_sha256(candidate), "发布产物必须与候选逐字节一致")
        leftovers = [path for path in target.parent.rglob(".tmp-*") if path.exists()]
        self.assertEqual(leftovers, [], "发布后不得留下临时半成品：%r" % leftovers)
        self.assertFalse((target / ".tmp-incomplete").exists())
        self.record(target_sha=tree_sha256(target), leftover_count=len(leftovers))

    def test_crash_before_publish_recovers(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("crash-before")
        temp_leftover = target.parent / (".tmp-op-crash-before-stale")
        temp_leftover.mkdir(parents=True, exist_ok=True)
        (temp_leftover / "half.txt").write_text("partial\n", encoding="utf-8")
        request = make_request(operation_id="op-crash-before", candidate=candidate, target_rel=target_rel)
        result = call_commit(request, operations_root=self.ops, name="crash-before")
        self.assertIn(result["status"], {"committed", "replayed"}, result)
        self.assertEqual(tree_sha256(target), tree_sha256(candidate), "恢复后正式产物必须完整")
        self.assertFalse(temp_leftover.exists(), "孤儿临时目录必须被清理")
        self.record(status=result["status"], codes=issue_codes(result), target_sha=tree_sha256(target))

    def test_crash_after_publish_before_record_recovers(self) -> None:
        """P4-R2：发布后记录写失败留下 intent；不同候选树重试可补记录 replayed。"""
        commit = commit_module()
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("crash-after")
        request = make_request(operation_id="op-crash-after", candidate=candidate, target_rel=target_rel)
        with mock.patch.object(commit, "_write_operation_record",
                               side_effect=OSError("injected operation record write failure")):
            first = call_commit(request, operations_root=self.ops, name="crash-after-first")
        self.assertEqual(first["status"], "failed", first)
        self.assertIn("record_write_failed", issue_codes(first), first)
        self.assertTrue(target.is_dir(), "失败前正式产物已发布")
        self.assertEqual(read_operation_record("op-crash-after", self.ops), {}, "记录写入失败时应无 record")
        intent_path = commit._intent_path(self.ops, "op-crash-after")
        self.assertTrue(intent_path.is_file(), "发布意图 intent 应保留供恢复")
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        original_sha = intent["candidate_refs"][0]["sha256"]
        from le_calc.workflow import atomic_io
        self.assertEqual(atomic_io.tree_sha256(target), original_sha, "正式产物应与意图绑定")
        tree_before = tree_sha256(target)
        # 真实重试：不同的候选树（时间/状态不同），同 op+同 input 按意图原发布 sha 恢复。
        newer = write_candidate(self.name + "-candidate-newer",
                                {"manifest.json": '{"run": "newer"}\n',
                                 "nested/result.json": '{"value": 99}\n',
                                 "notes.txt": "newer candidate tree\n"})
        retry_request = make_request(operation_id="op-crash-after", candidate=newer, target_rel=target_rel,
                                     fingerprint=request["input_fingerprint"], attempt_id="attempt-2")
        retry = call_commit(retry_request, operations_root=self.ops, name="crash-after-retry")
        self.assertEqual(retry["status"], "replayed", retry)
        self.assertIn("recovered_record_after_publish", issue_codes(retry), retry)
        self.assertEqual(tree_sha256(target), tree_before, "恢复不得重建/覆盖已发布产物")
        record = read_operation_record("op-crash-after", self.ops)
        self.assertEqual(record.get("input_fingerprint"), request["input_fingerprint"])
        self.assertEqual(record.get("published_refs", [{}])[0].get("sha256"), original_sha)
        self.assertFalse(intent_path.exists(), "补记录成功后应清理 intent")
        self.record(first=first["status"], retry=retry["status"],
                    codes=issue_codes(retry), target_sha=tree_before)

    def test_foreign_target_without_intent_is_rejected(self) -> None:
        """P4-R2：无意图的既有目标不得被泛化接受（failed target_exists）。"""
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("foreign-target")
        shutil.copytree(candidate, target)
        tree_before = tree_sha256(target)
        request = make_request(operation_id="op-foreign-target", candidate=candidate, target_rel=target_rel)
        result = call_commit(request, operations_root=self.ops, name="foreign-target")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("target_exists", issue_codes(result), result)
        self.assertEqual(tree_sha256(target), tree_before, "无意图的外部目标不得被改动")
        self.assertEqual(read_operation_record("op-foreign-target", self.ops), {})
        self.record(status=result["status"], codes=issue_codes(result))

    def test_record_corrupt_is_reported_not_overwritten(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("record-corrupt")
        self.ops.mkdir(parents=True, exist_ok=True)
        (self.ops / "op-record-corrupt.json").write_text("{not valid json", encoding="utf-8")
        request = make_request(operation_id="op-record-corrupt", candidate=candidate, target_rel=target_rel)
        result = call_commit(request, operations_root=self.ops, name="record-corrupt")
        self.assertIn(result["status"], {"failed"}, result)
        self.assertIn("operation_record_corrupt", issue_codes(result), result)
        self.assertFalse(target.exists(), "记录损坏时不得发布正式产物")
        record_path = self.ops / "op-record-corrupt.json"
        self.assertEqual(record_path.read_text(encoding="utf-8"), "{not valid json", "不得覆盖损坏记录")
        self.record(status=result["status"], codes=issue_codes(result))

    def test_published_artifact_missing_detected(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("missing-published")
        request = make_request(operation_id="op-missing-published", candidate=candidate, target_rel=target_rel)
        first = call_commit(request, operations_root=self.ops, name="missing-published-first")
        self.assertEqual(first["status"], "committed", first)
        shutil.rmtree(target)
        retry = dict(request)
        retry["attempt_id"] = "attempt-2"
        second = call_commit(retry, operations_root=self.ops, name="missing-published-retry")
        self.assertEqual(second["status"], "failed", second)
        codes = issue_codes(second)
        self.assertTrue(any(code in {"published_artifact_missing", "published_artifact_sha_mismatch"} for code in codes), codes)
        self.assertFalse(target.exists(), "不得在产物缺失时静默重新发布")
        self.record(status=second["status"], codes=codes)

    def test_target_path_traversal_rejected(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        request = make_request(
            operation_id="op-traversal",
            candidate=candidate,
            target_rel="../../outside-p4-acceptance",
        )
        with self.assertRaises(ValueError):
            call_commit(request, operations_root=self.ops, name="traversal")
        self.assertFalse((ROOT.parent / "outside-p4-acceptance").exists())
        self.record(result="ValueError")

    def test_concurrent_same_operation_single_publish(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("concurrent")
        request = make_request(operation_id="op-concurrent", candidate=candidate, target_rel=target_rel)
        runner = SESSION / "commit_runner.py"
        runner.write_text(
            "import json, sys\n"
            "from le_calc.workflow import commit\n"
            "spec = json.loads(open(sys.argv[1], encoding='utf-8').read())\n"
            "result = commit.commit_operation(spec['request'], operations_root=spec['ops'], root=spec['root'])\n"
            "payload = result.to_dict() if hasattr(result, 'to_dict') else dict(result)\n"
            "open(sys.argv[2], 'w', encoding='utf-8').write(json.dumps(payload, ensure_ascii=False))\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + env.get("PYTHONPATH", "")
        processes = []
        results = []
        barrier = SESSION / "concurrent-go"
        if barrier.exists():
            barrier.unlink()
        for index in range(2):
            spec = SESSION / f"commit-spec-{index}.json"
            spec.write_text(json.dumps({"request": request, "ops": str(self.ops), "root": str(ROOT)}), encoding="utf-8")
            out = SESSION / f"commit-result-{index}.json"
            if out.exists():
                out.unlink()
            code = (
                "import json, sys, time, pathlib\n"
                f"barrier = pathlib.Path({str(barrier)!r})\n"
                "deadline = time.time() + 20\n"
                "while not barrier.exists() and time.time() < deadline:\n"
                "    time.sleep(0.01)\n"
                f"sys.argv = ['runner', {str(spec)!r}, {str(out)!r}]\n"
                f"exec(compile(open({str(runner)!r}, encoding='utf-8').read(), {str(runner)!r}, 'exec'))\n"
            )
            proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), env=env)
            processes.append((proc, out))
        time.sleep(0.2)
        barrier.write_text("go", encoding="utf-8")
        for proc, out in processes:
            proc.wait(timeout=120)
            self.assertEqual(proc.returncode, 0, "并发 commit 子进程失败")
            results.append(json.loads(out.read_text(encoding="utf-8")))
        statuses = sorted(result["status"] for result in results)
        self.assertIn("committed", statuses)
        self.assertTrue(all(status in {"committed", "replayed"} for status in statuses), statuses)
        self.assertEqual(statuses.count("committed"), 1, "同一操作最多一次 committed 发布：%r" % statuses)
        self.assertEqual(tree_sha256(target), tree_sha256(candidate))
        leftovers = [path for path in target.parent.rglob(".tmp-*") if path.exists()]
        self.assertEqual(leftovers, [], "并发后不得有半成品")
        self.record(statuses=statuses, target_sha=tree_sha256(target))

    def test_no_absolute_paths_in_records(self) -> None:
        candidate = write_candidate(self.name + "-candidate")
        target_rel, target = self._target("no-abs")
        request = make_request(operation_id="op-no-abs", candidate=candidate, target_rel=target_rel)
        first = call_commit(request, operations_root=self.ops, name="no-abs-first")
        self.assertEqual(first["status"], "committed", first)
        record = read_operation_record("op-no-abs", self.ops)
        payload = {"result": first, "record": record}
        hits = reject_absolute_paths(payload)
        self.assertEqual(hits, [], "commit 记录/结果不得泄漏机器绝对路径：%r" % hits[:3])
        self.record(absolute_path_hits=hits)


# ---------------------------------------------------------------------------
# 阶段缓存/失效/恢复行为验收（真实 CLI 子进程）
# ---------------------------------------------------------------------------
VOLATILE_KEYS = {
    "run_id", "run_key", "generated_at", "created_at", "started_at", "finished_at",
    "elapsed_ms", "cache_status", "cache_key", "cache", "output_dir", "run_dir",
    "parameters_fingerprint", "implementation_fingerprint", "input_fingerprint",
}


def strip_volatile(value: object) -> object:
    if isinstance(value, dict):
        return {key: strip_volatile(item) for key, item in value.items() if key not in VOLATILE_KEYS}
    if isinstance(value, list):
        return [strip_volatile(item) for item in value]
    return value


def run_files(run_dir: pathlib.Path) -> dict[str, str]:
    rows = {}
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative in {"pipeline-state.json", "prepare-context.json"}:
            continue
        if relative.startswith("pipeline-state.corrupt"):
            continue
        if ".tmp" in relative or "__pycache__" in relative:
            continue
        rows[relative] = sha256_file(path)
    return rows


class P4CacheAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.runs = SESSION / "runs"
        cls.runs.mkdir(parents=True, exist_ok=True)

    def setUp(self) -> None:
        self.name = self._testMethodName
        self.cache_root = SESSION / "cache" / self.name
        if self.cache_root.exists():
            shutil.rmtree(self.cache_root)
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.run_root = self.runs / self.name
        self.run_root.mkdir(parents=True, exist_ok=True)
        self.started = time.perf_counter()

    def tearDown(self) -> None:
        record = {
            "test": f"cache::{self.name}",
            "status": "pass",
            "details": getattr(self, "_details", {}),
            "elapsed_s": round(time.perf_counter() - self.started, 2),
            "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        if result is not None:
            failures = [msg for test, msg in (result.failures + result.errors) if test is self]
            if failures:
                record["status"] = "fail"
                record["message"] = failures[-1][-4000:]
        RECORDS.append(record)

    def record(self, **details) -> None:
        self._details = details

    # -- command helpers -------------------------------------------------
    def prepare(
        self,
        run_id: str,
        *,
        model: str = "fr",
        extra: list[str] | None = None,
        env: dict | None = None,
        timeout: float = 300,
        allow_partial: bool = False,
        main_skill: str | None = None,
    ) -> tuple[dict, float]:
        case = "examples/flame-reave/case.legacy-hit.json" if model == "fr" else "examples/black-hole/case.legacy.json"
        main_skill = main_skill or ("fr11mv" if model == "fr" else "bh2")
        args = [
            "scripts/le_commands.py",
            "prepare",
            "--case",
            case,
            "--out",
            str(self.run_root),
            "--run-id",
            run_id,
            "--cache-root",
            str(self.cache_root),
            "--main-skill",
            main_skill,
        ]
        args.extend(extra or [])
        merged = dict(os.environ)
        if env:
            merged.update(env)
        began = time.perf_counter()
        completed = subprocess.run(
            [sys.executable, *args],
            cwd=str(ROOT),
            env=merged,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        elapsed = time.perf_counter() - began
        log = LOG_ROOT / f"prepare-{self.name}-{run_id}.log"
        log.write_text(
            f"command: {args}\nreturncode: {completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )
        refused = completed.returncode != 0 and not (allow_partial and completed.returncode == 3)
        if refused:
            raise AssertionError(
                f"prepare 失败（run_id={run_id}，exit={completed.returncode}），日志 {log}\n"
                f"stdout:\n{completed.stdout[-3000:]}\nstderr:\n{completed.stderr[-3000:]}"
            )
        import re as _re

        match = _re.search(r"子进程调用：(\d+) 次", completed.stdout or "")
        if not hasattr(self, "_child_calls"):
            self._child_calls = {}
        self._child_calls[run_id] = int(match.group(1)) if match else None
        partial = completed.returncode == 3
        if partial:
            self._partial_runs = getattr(self, "_partial_runs", set()) | {run_id}
        return self.state(run_id), elapsed

    def child_calls(self, run_id: str) -> int | None:
        return getattr(self, "_child_calls", {}).get(run_id)

    def is_partial(self, run_id: str) -> bool:
        return run_id in getattr(self, "_partial_runs", set())

    def state(self, run_id: str) -> dict:
        path = self.run_root / run_id / "pipeline-state.json"
        if not path.is_file():
            raise AssertionError(f"缺少 pipeline-state.json：{path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def run_dir(self, run_id: str) -> pathlib.Path:
        return self.run_root / run_id

    def stages(self, state: dict) -> dict[str, dict]:
        return {str(entry["stage_id"]): entry for entry in state.get("stages") or []}

    def cache_statuses(self, state: dict) -> dict[str, str]:
        rows = {}
        for stage_id, entry in self.stages(state).items():
            cache = entry.get("cache") if isinstance(entry.get("cache"), dict) else {}
            status = entry.get("cache_status") or cache.get("status") or "unknown"
            rows[stage_id] = str(status)
        return rows

    def hit_stages(self, state: dict) -> list[str]:
        return [stage for stage, status in self.cache_statuses(state).items() if status == "hit"]

    @staticmethod
    def _normalise_run_paths(payload: object, run_dir: pathlib.Path) -> object:
        import re

        text = json.dumps(payload, ensure_ascii=False)
        # 任意 run 目录前缀归一化（缓存命中可能 materialize 自其他 run）
        text = re.sub(r"reports/p4-acceptance/work/[^/\"]+/runs/[^/\"]+/[^/\"]+/", "<run>/", text)
        text = re.sub(r"reports/p4-acceptance/work/[^/\\\"]+/runs/[^/\\\"]+/[^/\\\"]+/", "<run>/", text)
        prefixes = {
            str(run_dir),
            run_dir.as_posix(),
            run_dir.resolve().as_posix(),
            str(run_dir.resolve()),
        }
        for prefix in sorted((item for item in prefixes if item), key=len, reverse=True):
            text = text.replace(prefix.replace("\\", "/"), "<run>")
            text = text.replace(prefix, "<run>")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return payload

    def compare_outputs(self, left_run: pathlib.Path, right_run: pathlib.Path) -> dict:
        left = run_files(left_run)
        right = run_files(right_run)
        exact = []
        metadata_only = []
        semantic = []
        for relative in sorted(set(left) | set(right)):
            if relative not in left or relative not in right:
                semantic.append({"path": relative, "reason": "missing_on_one_side"})
                continue
            if left[relative] == right[relative]:
                exact.append(relative)
                continue
            if relative.endswith(".json"):
                try:
                    left_payload = strip_volatile(self._normalise_run_paths(
                        json.loads((left_run / relative).read_text(encoding="utf-8")), left_run))
                    right_payload = strip_volatile(self._normalise_run_paths(
                        json.loads((right_run / relative).read_text(encoding="utf-8")), right_run))
                except json.JSONDecodeError:
                    semantic.append({"path": relative, "reason": "invalid_json"})
                    continue
                if left_payload == right_payload:
                    metadata_only.append(relative)
                else:
                    semantic.append({"path": relative, "reason": "json_value_diff"})
            else:
                semantic.append({"path": relative, "reason": "binary_diff"})
        return {"exact": exact, "metadata_only": metadata_only, "semantic": semantic}

    def cache_entry_path(self, stage_id: str, cache_key: str) -> pathlib.Path:
        return self.cache_root / stage_id / (cache_key + ".json")

    # -- tests -----------------------------------------------------------
    def test_warm_cache_all_hit_and_outputs_match(self) -> None:
        cold_state, cold_elapsed = self.prepare("cold-1")
        cold_hits = self.hit_stages(cold_state)
        self.assertEqual(cold_hits, [], "冷跑（空缓存）不应有 hit：%r" % cold_hits)
        warm_state, warm_elapsed = self.prepare("warm-1")
        warm_hits = self.hit_stages(warm_state)
        expected = list(self.stages(warm_state))
        self.assertGreaterEqual(len(warm_hits), max(1, len(expected) - 1),
                                "warm 二跑应全部适用阶段 hit：%r" % self.cache_statuses(warm_state))
        comparison = self.compare_outputs(self.run_dir("cold-1"), self.run_dir("warm-1"))
        self.assertEqual(comparison["semantic"], [], "缓存命中与完整重算输出语义不一致：%r" % comparison["semantic"][:5])
        ratio = warm_elapsed / cold_elapsed if cold_elapsed else 1.0
        self.assertLess(ratio, 0.8, "warm 二跑耗时应显著下降：cold=%.2fs warm=%.2fs" % (cold_elapsed, warm_elapsed))
        cold_calls = self.child_calls("cold-1")
        warm_calls = self.child_calls("warm-1")
        self.assertIsNotNone(cold_calls, "prepare 输出缺少子进程调用计数")
        self.assertIsNotNone(warm_calls, "prepare 输出缺少子进程调用计数")
        self.assertEqual(warm_calls, 0, "warm 全 hit 不应启动子进程：%r" % warm_calls)
        self.assertGreater(cold_calls, warm_calls, "cache 命中后子进程调用数应下降")
        state_doc = json.loads((self.run_dir("warm-1") / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertEqual(reject_absolute_paths(state_doc), [], "pipeline-state 泄漏绝对路径")
        self.record(cold_elapsed_s=round(cold_elapsed, 3), warm_elapsed_s=round(warm_elapsed, 3),
                    cold_hits=len(cold_hits), warm_hits=len(warm_hits), stage_count=len(expected),
                    cold_child_calls=cold_calls, warm_child_calls=warm_calls,
                    exact_files=len(comparison["exact"]), metadata_only=comparison["metadata_only"],
                    speedup=round(ratio, 3))

    def test_no_cache_forces_execution_then_warm_again(self) -> None:
        self.prepare("cold-1")
        disabled_state, disabled_elapsed = self.prepare("no-cache-1", extra=["--no-cache"])
        statuses = self.cache_statuses(disabled_state)
        self.assertTrue(all(status in {"disabled", "miss"} for status in statuses.values()),
                        "--no-cache 强制全部重算：%r" % statuses)
        self.assertIn("disabled", statuses.values(), "--no-cache 阶段应标记 disabled：%r" % statuses)
        warm_state, _ = self.prepare("warm-after-no-cache")
        self.assertGreaterEqual(len(self.hit_stages(warm_state)), max(1, len(self.stages(warm_state)) - 1))
        comparison = self.compare_outputs(self.run_dir("no-cache-1"), self.run_dir("warm-after-no-cache"))
        self.assertEqual(comparison["semantic"], [], comparison["semantic"][:5])
        self.record(no_cache_statuses=statuses, no_cache_elapsed_s=round(disabled_elapsed, 3),
                    warm_hits=len(self.hit_stages(warm_state)))

    def test_implementation_override_invalidates_downstream_only(self) -> None:
        self.prepare("warm-base")
        override = json.dumps({"resolve_skills": "acceptance-token-1"})
        changed_state, _ = self.prepare("impl-change", env={"LE_PIPELINE_IMPL_OVERRIDE": override})
        statuses = self.cache_statuses(changed_state)
        stage_ids = list(statuses)
        self.assertIn("resolve_skills", stage_ids)
        index = stage_ids.index("resolve_skills")
        upstream = stage_ids[:index]
        downstream = stage_ids[index:]
        for stage in upstream:
            self.assertEqual(statuses[stage], "hit", "无关上游阶段应保持 hit：%r" % statuses)
        for stage in downstream:
            self.assertIn(statuses[stage], {"miss", "disabled"}, "下游阶段必须 miss：%r" % statuses)
        comparison = self.compare_outputs(self.run_dir("warm-base"), self.run_dir("impl-change"))
        self.assertEqual(comparison["semantic"], [], comparison["semantic"][:5])
        self.record(statuses=statuses, upstream=upstream, downstream=downstream)

    def test_cache_entry_deleted_or_corrupt_rebuilds(self) -> None:
        state, _ = self.prepare("warm-base")
        stage_entry = self.stages(state).get("export_calc_input") or next(iter(self.stages(state).values()))
        cache_key = stage_entry.get("cache_key") or (stage_entry.get("cache") or {}).get("key")
        self.assertTrue(cache_key, "pipeline-state 缺少 cache_key：%r" % stage_entry)
        entry_path = self.cache_entry_path(str(stage_entry["stage_id"]), str(cache_key))
        self.assertTrue(entry_path.is_file(), "缓存条目不存在：%s" % entry_path)
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        # 删除缓存条目 → 该阶段 miss，其他阶段仍 hit
        entry_path.unlink()
        deleted_state, _ = self.prepare("after-delete")
        deleted_statuses = self.cache_statuses(deleted_state)
        self.assertEqual(deleted_statuses[str(stage_entry["stage_id"])], "miss",
                         "删除缓存条目后应 miss：%r" % deleted_statuses)
        # 另一阶段写入损坏 JSON → 该阶段 miss/重建，其余阶段 hit
        state2, _ = self.prepare("warm-2")
        other_entry = next(entry for key, entry in self.stages(state2).items()
                           if key != str(stage_entry["stage_id"]) and (entry.get("cache_key")))
        other_path = self.cache_entry_path(str(other_entry["stage_id"]), str(other_entry["cache_key"]))
        other_path.write_text("{broken", encoding="utf-8")
        corrupt_state, _ = self.prepare("after-corrupt")
        corrupt_statuses = self.cache_statuses(corrupt_state)
        self.assertIn(corrupt_statuses[str(other_entry["stage_id"])], {"miss", "disabled"},
                      "损坏缓存应失效重建：%r" % corrupt_statuses)
        comparison = self.compare_outputs(self.run_dir("warm-base"), self.run_dir("after-corrupt"))
        self.assertEqual(comparison["semantic"], [], comparison["semantic"][:5])
        self.record(deleted_stage=str(stage_entry["stage_id"]), corrupt_stage=str(other_entry["stage_id"]),
                    statuses=corrupt_statuses)

    def test_bad_pipeline_state_recovery(self) -> None:
        self.prepare("base")
        self.prepare("base-ref")
        run_dir = self.run_dir("base")
        state_path = run_dir / "pipeline-state.json"
        state_path.write_text("{broken state", encoding="utf-8")
        # --resume 应备份坏 state 并按可恢复方式重建，不混用半成品
        result = subprocess.run(
            [sys.executable, "scripts/le_commands.py", "prepare",
             "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
             "--run-id", "base", "--cache-root", str(self.cache_root),
             "--main-skill", "fr11mv", "--resume"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        log = LOG_ROOT / "prepare-bad-state-resume.log"
        log.write_text((result.stdout or "") + "\n--- stderr ---\n" + (result.stderr or ""), encoding="utf-8")
        self.assertEqual(result.returncode, 0, "坏 pipeline-state 的 --resume 应可恢复：%s" % (result.stdout[-3000:] + result.stderr[-1000:]))
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired.get("schema_version"), "le-pipeline-state-v1")
        backups = sorted(run_dir.glob("pipeline-state.corrupt*.json"))
        self.assertTrue(backups, "应保留损坏 state 备份（pipeline-state.corrupt*.json）")
        self.assertIn("broken state", backups[0].read_text(encoding="utf-8"))
        comparison = self.compare_outputs(self.run_dir("base"), self.run_dir("base-ref"))
        self.assertEqual(comparison["semantic"], [], "坏 state 恢复后输出与完整执行不一致：%r" % comparison["semantic"][:5])
        self.record(recovered=True, backup=backups[0].relative_to(ROOT).as_posix())

    def test_deleted_output_resumed_and_restored(self) -> None:
        self.prepare("output-base")
        self.prepare("output-ref")
        state = self.state("output-base")
        target_ref = None
        target_stage = None
        for entry in state["stages"]:
            for ref in entry.get("output_refs") or []:
                path_value = str(ref.get("path") or "")
                if path_value.startswith(f"reports/p4-acceptance/work/{STAMP}/runs/"):
                    absolute = ROOT / path_value
                    if absolute.is_file() and ".json" in absolute.name:
                        target_ref = absolute
                        target_stage = entry["stage_id"]
                        break
            if target_ref:
                break
        self.assertIsNotNone(target_ref, "未找到可删除的 state 引用产物")
        target_ref.unlink()
        resumed = subprocess.run(
            [sys.executable, "scripts/le_commands.py", "prepare",
             "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
             "--run-id", "output-base", "--cache-root", str(self.cache_root),
             "--main-skill", "fr11mv", "--resume"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        log = LOG_ROOT / "prepare-deleted-output-resume.log"
        log.write_text((resumed.stdout or "") + "\n--- stderr ---\n" + (resumed.stderr or ""), encoding="utf-8")
        self.assertEqual(resumed.returncode, 0, "删除已引用产物后 --resume 应重建：%s" % (resumed.stdout[-2500:] + resumed.stderr[-1000:]))
        self.assertTrue(target_ref.is_file(), "resume 后应恢复被删除的产物：%s" % target_ref)
        comparison = self.compare_outputs(self.run_dir("output-base"), self.run_dir("output-ref"))
        self.assertEqual(comparison["semantic"], [], "删除产物恢复后与完整执行不一致：%r" % comparison["semantic"][:5])
        self.record(stage=target_stage, restored=target_ref.relative_to(ROOT).as_posix())

    def test_missing_pipeline_state_resume_rebuilds(self) -> None:
        self.prepare("missing-base")
        self.prepare("missing-ref")
        state_path = self.run_dir("missing-base") / "pipeline-state.json"
        state_path.unlink()
        resumed = subprocess.run(
            [sys.executable, "scripts/le_commands.py", "prepare",
             "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
             "--run-id", "missing-base", "--cache-root", str(self.cache_root),
             "--main-skill", "fr11mv", "--resume"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        log = LOG_ROOT / "prepare-missing-state-resume.log"
        log.write_text((resumed.stdout or "") + "\n--- stderr ---\n" + (resumed.stderr or ""), encoding="utf-8")
        self.assertEqual(resumed.returncode, 0, "删除 pipeline-state 后 --resume 应重建：%s" % (resumed.stdout[-2500:] + resumed.stderr[-1000:]))
        rebuilt = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(rebuilt.get("schema_version"), "le-pipeline-state-v1")
        self.assertTrue(rebuilt.get("stages"), "重建后的 state 必须包含阶段记录")
        comparison = self.compare_outputs(self.run_dir("missing-base"), self.run_dir("missing-ref"))
        self.assertEqual(comparison["semantic"], [], "删 state 恢复后与完整执行不一致：%r" % comparison["semantic"][:5])
        self.record(rebuilt_stages=len(rebuilt["stages"]))

    def test_cached_prepare_downstream_engine_numbers_match(self) -> None:
        self.prepare("engine-cold", model="bh")
        self.prepare("engine-warm", model="bh")
        case = json.loads((ROOT / "examples" / "black-hole" / "case.json").read_text(encoding="utf-8"))
        ruleset = str((ROOT / case["ruleset"]["path"]).resolve())
        outputs = {}
        for run_id in ("engine-cold", "engine-warm"):
            out = SESSION / "engine" / run_id
            if out.exists():
                shutil.rmtree(out)
            out.mkdir(parents=True, exist_ok=True)
            snapshot = self.run_dir(run_id) / "steps" / "05-input-snapshot.json"
            self.assertTrue(snapshot.is_file(), "BH prepare 缺少输入快照：%s" % snapshot)
            merged = dict(os.environ)
            merged["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + merged.get("PYTHONPATH", "")
            completed = subprocess.run(
                [sys.executable, "-m", "le_calc", "run", "--snapshot", str(snapshot),
                 "--ruleset", ruleset, "--out", str(out), "--no-html", "--json"],
                cwd=str(ROOT), env=merged, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=300,
            )
            self.assertEqual(completed.returncode, 0, "下游引擎运行失败：%s" % completed.stderr[-2000:])
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            recompute = result.get("conditional", {}).get("recompute", {})
            outputs[run_id] = {
                "single_cast_total": result["single_cast"]["totals"]["single_cast_total"],
                "ignite_total": result["single_cast"]["totals"]["ignite_total"],
                "body_total": result["single_cast"]["totals"]["body_total"],
                "baseline_loops": [
                    {
                        "cast_count": loop.get("cast_count"),
                        "windows": [
                            {"dps": window.get("dps"), "total_damage": window.get("total_damage"),
                             "window_s": window.get("window_s")}
                            for window in loop.get("windows") or []
                        ],
                    }
                    for loop in (recompute.get("baseline") or {}).get("loops") or []
                ],
            }
        cold = outputs["engine-cold"]
        warm = outputs["engine-warm"]
        self.assertEqual(cold, warm, "缓存命中准备路线的下游引擎关键数值必须一致")
        self.assertGreater(cold["single_cast_total"], 0)
        window_values = [window["dps"] for loop in cold["baseline_loops"] for window in loop["windows"]]
        self.assertTrue(any(value and value > 0 for value in window_values),
                        "BH 下游结果缺少有效 DPS 窗口数值：%r" % window_values)
        self.record(cold_engine=cold, values_equal=True)

    def test_interrupted_prepare_resume_completes(self) -> None:
        run_id = "interrupted"
        run_dir = self.run_dir(run_id)
        cache_root = SESSION / "cache" / self.name
        args = [
            sys.executable, "scripts/le_commands.py", "prepare",
            "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
            "--run-id", run_id, "--cache-root", str(cache_root), "--main-skill", "fr11mv",
        ]
        proc = subprocess.Popen(args, cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        state_path = run_dir / "pipeline-state.json"
        deadline = time.time() + 60
        killed = False
        while time.time() < deadline:
            if state_path.is_file():
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    state = {}
                if len(state.get("stages") or []) >= 1:
                    self._kill_tree(proc.pid)
                    killed = True
                    break
            time.sleep(0.02)
        if not killed:
            self._kill_tree(proc.pid)
            self.fail("未能在第一个阶段后中断 prepare（阶段过短/未写 state）")
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self._kill_tree(proc.pid)
        state_after_kill = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state_after_kill.get("schema_version"), "le-pipeline-state-v1", "中断后 state 必须完整可读")
        resumed = subprocess.run(
            [sys.executable, "scripts/le_commands.py", "prepare",
             "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
             "--run-id", run_id, "--cache-root", str(cache_root), "--main-skill", "fr11mv", "--resume"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        log = LOG_ROOT / "prepare-interrupted-resume.log"
        log.write_text((resumed.stdout or "") + "\n--- stderr ---\n" + (resumed.stderr or ""), encoding="utf-8")
        self.assertEqual(resumed.returncode, 0, "中断后 --resume 应完成：%s" % (resumed.stdout[-2000:] + resumed.stderr[-1000:]))
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        stage_ids = [entry["stage_id"] for entry in final_state.get("stages") or []]
        self.assertEqual(len(stage_ids), len(set(stage_ids)), "state 不应有重复阶段")
        self.assertFalse((run_dir / "pipeline-state.corrupt.json").exists())
        # 与完整重算的输出比较（缓存/恢复路线与完整路线一致）
        clean_state, _ = self.prepare("clean-reference")
        comparison = self.compare_outputs(run_dir, self.run_dir("clean-reference"))
        self.assertEqual(comparison["semantic"], [], "中断恢复结果与完整重算语义不一致：%r" % comparison["semantic"][:5])
        self.record(stages_after_kill=len(state_after_kill.get("stages") or []),
                    stages_final=len(stage_ids), resumed=True)

    def test_cli_parameter_change_invalidates_downstream_only(self) -> None:
        self.prepare("param-base")
        changed_state, _ = self.prepare("param-alt", allow_partial=True,
                                        main_skill="fr11mv-acceptance-alt")
        statuses = self.cache_statuses(changed_state)
        ordered = [stage for stage in self.stages(changed_state)]
        self.assertIn("resolve_skills", ordered)
        index = ordered.index("resolve_skills")
        upstream = ordered[:index]
        for stage in upstream:
            self.assertEqual(statuses[stage], "hit", "仅改 main_skill 时上游必须 hit：%r" % statuses)
        self.assertEqual(statuses.get("resolve_skills"), "miss", "resolve_skills 参数变化必须 miss：%r" % statuses)
        self.assertIn(statuses.get("prepare_snapshot"), {"miss", "disabled"},
                      "下游 prepare_snapshot 必须 miss：%r" % statuses)
        self.assertIsNotNone(self.child_calls("param-base"))
        self.record(statuses=statuses, partial=self.is_partial("param-alt"),
                    base_child_calls=self.child_calls("param-base"),
                    alt_child_calls=self.child_calls("param-alt"))

    def test_cached_payload_deleted_or_tampered_triggers_miss(self) -> None:
        self.prepare("cold-base")
        state, _ = self.prepare("warm-base")
        hit_ids = self.hit_stages(state)
        self.assertTrue(hit_ids, "warm-base 应有 hit：%r" % self.cache_statuses(state))
        stage_entry = self.stages(state)[hit_ids[0]]
        stage_id = str(stage_entry["stage_id"])
        cache_key = str(stage_entry["cache_key"])
        entry_path = self.cache_entry_path(stage_id, cache_key)
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        outputs = entry.get("outputs") or []
        self.assertTrue(outputs, "缓存条目缺少 outputs：%r" % entry)
        payload_dir = entry_path.with_suffix("")
        payload = payload_dir / str(outputs[0]["cached_path"])
        self.assertTrue(payload.is_file(), "缓存 payload 不存在：%s" % payload)
        payload.unlink()
        deleted_state, _ = self.prepare("after-payload-delete")
        deleted_status = self.cache_statuses(deleted_state)[stage_id]
        self.assertIn(deleted_status, {"miss", "disabled"}, "payload 缺失应触发 miss：%r" % deleted_status)
        # 篡改另一个阶段的 payload 字节（sha 变化）→ miss 重建，结果仍正确
        state2, _ = self.prepare("warm-2")
        other = next(entry for key, entry in self.stages(state2).items()
                     if key != stage_id and entry.get("cache_key"))
        other_id = str(other["stage_id"]); other_key = str(other["cache_key"])
        other_entry_path = self.cache_entry_path(other_id, other_key)
        other_entry = json.loads(other_entry_path.read_text(encoding="utf-8"))
        other_payload = other_entry_path.with_suffix("") / str(other_entry["outputs"][0]["cached_path"])
        other_payload.write_bytes(b"tampered-cache-payload")
        tampered_state, _ = self.prepare("after-payload-tamper")
        tampered_status = self.cache_statuses(tampered_state)[other_id]
        self.assertIn(tampered_status, {"miss", "disabled"}, "payload sha 变化应触发 miss：%r" % tampered_status)
        comparison = self.compare_outputs(self.run_dir("warm-base"), self.run_dir("after-payload-tamper"))
        self.assertEqual(comparison["semantic"], [], comparison["semantic"][:5])
        # 缓存条目 JSON 与 payload 内不得出现机器绝对路径
        for path in self.cache_root.rglob("*.json"):
            payload_doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(reject_absolute_paths(payload_doc), [], "缓存条目泄漏绝对路径：%s" % path)
        self.record(stage=stage_id, deleted_status=deleted_status,
                    tampered_stage=other_id, tampered_status=tampered_status)

    def test_bh_warm_cache_hit_and_outputs_match(self) -> None:
        cold_state, cold_elapsed = self.prepare("bh-cold", model="bh")
        self.assertEqual(self.hit_stages(cold_state), [])
        warm_state, warm_elapsed = self.prepare("bh-warm", model="bh")
        self.assertGreaterEqual(len(self.hit_stages(warm_state)), max(1, len(self.stages(warm_state)) - 1))
        comparison = self.compare_outputs(self.run_dir("bh-cold"), self.run_dir("bh-warm"))
        self.assertEqual(comparison["semantic"], [], comparison["semantic"][:5])
        self.assertLess(warm_elapsed, cold_elapsed * 0.8,
                        "BH warm 耗时应显著下降：cold=%.2fs warm=%.2fs" % (cold_elapsed, warm_elapsed))
        cold_calls = self.child_calls("bh-cold")
        warm_calls = self.child_calls("bh-warm")
        self.assertIsNotNone(cold_calls)
        self.assertIsNotNone(warm_calls)
        self.assertEqual(warm_calls, 0, "BH warm 全 hit 不应启动子进程：%r" % warm_calls)
        self.assertGreater(cold_calls, 0)
        self.record(cold_elapsed_s=round(cold_elapsed, 3), warm_elapsed_s=round(warm_elapsed, 3),
                    cold_hits=len(self.hit_stages(cold_state)), warm_hits=len(self.hit_stages(warm_state)),
                    cold_child_calls=cold_calls, warm_child_calls=warm_calls)

    def test_stage_parameter_and_input_fingerprint_cascade(self) -> None:
        """补充 API 级级联语义：某阶段参数/输入变化只改变该阶段+下游 cache key。"""
        sys.path.insert(0, str(CALC_SRC))
        from le_calc.workflow import contract as workflow_contract
        from le_calc.workflow import stage as stage_module
        from le_calc.workflow import stage_cache as cache_module

        case_path = ROOT / "examples" / "flame-reave" / "case.json"
        case = json.loads(case_path.read_text(encoding="utf-8"))
        input_refs = [
            {"artifact_id": "case", "path": "examples/flame-reave/case.legacy-hit.json", "sha256": sha256_file(case_path)},
        ]
        base_params = {"case_ref": "examples/flame-reave/case.legacy-hit.json", "main_skill": "fr11mv"}
        changed_params = dict(base_params)
        changed_params["main_skill"] = "fr11mv-acceptance-salt"
        stage_ids = ["freeze_input", "parse_input", "normalize_build", "export_calc_input",
                     "apply_affix_ranges", "resolve_skills", "prepare_snapshot", "min_validate"]
        target = "resolve_skills"

        def fingerprints(parameters_by_stage: dict[str, dict], input_by_stage: dict[str, list]) -> dict[str, dict]:
            keys: dict[str, str] = {}
            result: dict[str, dict] = {}
            for stage_id in stage_ids:
                request = workflow_contract.StageRequest(
                    stage_id=stage_id,
                    case_id=str(case.get("case_id")),
                    run_id="acceptance-fingerprint",
                    input_refs=input_by_stage.get(stage_id, input_refs),
                    parameters=parameters_by_stage.get(stage_id, base_params),
                    dependency_fingerprints={"dependencies": dict(keys)},
                )
                handler = stage_module.STAGES[stage_id]["handler"]
                row = cache_module.request_fingerprints(request, ROOT, handler=handler)
                result[stage_id] = row
                keys[stage_id] = row["cache_key"]
            return result

        base = fingerprints({}, {})
        param_changed = fingerprints({target: changed_params}, {})
        changed_stages = [stage_id for stage_id in stage_ids
                          if param_changed[stage_id]["cache_key"] != base[stage_id]["cache_key"]]
        index = stage_ids.index(target)
        self.assertEqual(changed_stages, stage_ids[index:],
                         "参数变化应只改变该阶段及下游：%r" % changed_stages)
        changed_input = [dict(ref) for ref in input_refs]
        changed_input[0] = dict(changed_input[0], sha256="0" * 64)
        input_changed = fingerprints({}, {target: changed_input})
        changed_by_input = [stage_id for stage_id in stage_ids
                            if input_changed[stage_id]["cache_key"] != base[stage_id]["cache_key"]]
        self.assertEqual(changed_by_input, stage_ids[index:],
                         "输入变化应只改变该阶段及下游：%r" % changed_by_input)
        self.record(parameter_changed=changed_stages, input_changed=changed_by_input,
                    target=target)

    def test_partial_artifacts_do_not_yield_incomplete_cache(self) -> None:
        """确定性模拟“阶段执行中断留下部分产物后 --resume”：缓存输出集必须完整。

        场景：已完整产出的 run 目录中保留阶段产物，但把 pipeline-state 截断到
        freeze_input（等价于在 parse_input 中途中断），并删除 parse_input 及下游
        缓存条目；--resume 重跑 parse_input 时这些文件已存在且字节相同。缓存条目
        仍必须记录该阶段全部产物；后续 cache hit 不得缺文件。
        """
        self.prepare("reference")
        self.prepare("partial")
        partial_dir = self.run_dir("partial")
        state_path = partial_dir / "pipeline-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        stage_ids = [entry["stage_id"] for entry in state.get("stages") or []]
        self.assertGreaterEqual(len(stage_ids), 2)
        state["stages"] = [entry for entry in state["stages"] if entry["stage_id"] == "freeze_input"]
        state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 删除 parse_input 及下游缓存，强制 resume 重跑这阶段（模拟中断时尚未写缓存）
        for stage_id in stage_ids[1:]:
            directory = self.cache_root / stage_id
            if directory.is_dir():
                shutil.rmtree(directory)
        resumed = subprocess.run(
            [sys.executable, "scripts/le_commands.py", "prepare",
             "--case", "examples/flame-reave/case.legacy-hit.json", "--out", str(self.run_root),
             "--run-id", "partial", "--cache-root", str(self.cache_root),
             "--main-skill", "fr11mv", "--resume"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        log = LOG_ROOT / "prepare-partial-artifacts-resume.log"
        log.write_text((resumed.stdout or "") + "\n--- stderr ---\n" + (resumed.stderr or ""), encoding="utf-8")
        self.assertEqual(resumed.returncode, 0, "部分产物 + --resume 应完成：%s" % (resumed.stdout[-2500:] + resumed.stderr[-1000:]))
        final_state = json.loads(state_path.read_text(encoding="utf-8"))
        parse_entry = next(entry for entry in final_state["stages"] if entry["stage_id"] == "parse_input")
        self.assertTrue(parse_entry.get("cache_key"), parse_entry)
        entry_path = self.cache_entry_path("parse_input", str(parse_entry["cache_key"]))
        self.assertTrue(entry_path.is_file(), "resume 后应写缓存条目：%s" % entry_path)
        entry = json.loads(entry_path.read_text(encoding="utf-8"))
        cached_paths = sorted(item.get("relative_path") for item in entry.get("outputs") or [])
        reference_files = run_files(self.run_dir("reference"))
        expected_parse = sorted(
            relative for relative in reference_files
            if relative.startswith("steps/01-save-import/")
        )
        self.assertEqual(cached_paths, expected_parse,
                         "阶段缓存输出集必须覆盖完整执行的全部产物：%r != %r" % (cached_paths, expected_parse))
        # 后续 cache hit 的输出文件也必须与完整执行逐文件一致
        self.prepare("after-partial")
        comparison = self.compare_outputs(self.run_dir("reference"), self.run_dir("after-partial"))
        self.assertEqual(comparison["semantic"], [],
                         "部分产物恢复后的缓存命中与完整执行不一致：%r" % comparison["semantic"][:5])
        self.record(cached_outputs=cached_paths, expected=expected_parse,
                    after_files=len(run_files(self.run_dir("after-partial"))))

    @staticmethod
    def _kill_tree(pid: int) -> None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        else:  # pragma: no cover
            try:
                os.kill(pid, 15)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 模块入口
# ---------------------------------------------------------------------------
def setUpModule() -> None:  # noqa: N802 - unittest hook
    from _author_assets import require_author_assets
    require_author_assets()
    P4_ROOT.mkdir(parents=True, exist_ok=True)
    for directory in (SESSION, OPS_ROOT, CAND_ROOT, PUBLISH_ROOT, LOG_ROOT, P4_ROOT / "evidence"):
        directory.mkdir(parents=True, exist_ok=True)
    RECORDS.clear()
    MODULE_STATE["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")


def tearDownModule() -> None:  # noqa: N802 - unittest hook
    summary = {
        "schema": "p4-acceptance-summary-v1",
        "session": STAMP,
        "started_at": MODULE_STATE.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "repo_root": ".",
        "commands": {
            "suite": "python -m unittest discover -s tools/phase3/tests -p 'test_p4_acceptance.py' -v",
            "work_root": f"reports/p4-acceptance/work/{STAMP}",
        },
        "tests": RECORDS,
        "summary": {
            "pass": sum(1 for record in RECORDS if record["status"] == "pass"),
            "fail": sum(1 for record in RECORDS if record["status"] == "fail"),
            "total": len(RECORDS),
        },
    }
    path = P4_ROOT / "summary.json"
    path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
