# -*- coding: utf-8 -*-
"""P5 独立验收：update_pointer / 变体复用 / 知识二次复用 / 打包交接。

真实 CLI/子进程/文件系统/库 API；不修改实现、正式案例/expected/rules/知识源、历史 run。
证据：``reports/p5-acceptance/**``。
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
import threading
import time
import unittest
import uuid
import zipfile
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[3]
P5_ROOT = ROOT / "reports" / "p5-acceptance"
CALC_SRC = ROOT / "tools" / "phase3" / "src"
KNOWLEDGE_SRC = ROOT / "tools" / "knowledge" / "src"
for source in (CALC_SRC, KNOWLEDGE_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

STAMP = time.strftime("%Y%m%dT%H%M%S")
SESSION = P5_ROOT / "work" / ("p5-" + STAMP)
LOG_DIR = P5_ROOT / "logs"
FR_CASE = "examples/flame-reave/case.legacy-hit.json"
BH_CASE = "examples/black-hole/case.legacy.json"
VARIANT_CASE = "examples/flame-reave-variant/case.json"
VARIANT_EXPECTED = "examples/flame-reave-variant/expected/expected-summary.json"
FR_EXPECTED = "examples/flame-reave/expected/expected-summary.json"
BH_EXPECTED = "examples/black-hole/expected/expected-summary.json"
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


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def work_dir(name: str) -> pathlib.Path:
    directory = SESSION / name
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def save_log(name: str, completed: subprocess.CompletedProcess, extra: str = "") -> pathlib.Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{name}.log"
    path.write_text(
        f"{extra}exit: {completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
        encoding="utf-8",
    )
    return path


def run_cli(
    args: list[str],
    *,
    cwd: pathlib.Path = ROOT,
    env: dict | None = None,
    timeout: float = 1800,
    log: str | None = None,
) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged["PYTHONPATH"] = str(CALC_SRC) + os.pathsep + merged.get("PYTHONPATH", "")
    if env:
        merged.update(env)
    completed = subprocess.run(
        args, cwd=str(cwd), env=merged, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )
    if log:
        save_log(log, completed)
    return completed


def parse_stage_statuses(output: str) -> dict[str, str]:
    rows = {}
    for line in output.splitlines():
        match = re.match(r"\s+(simulate|validate_result|render_report|commit)\s+\S+\s+cache=(\w+)", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def result_without_run(result: dict) -> dict:
    copy = json.loads(json.dumps(result, ensure_ascii=False))
    copy.pop("run", None)
    return copy


def external_urls(html: str) -> list[str]:
    urls = re.findall(r"https?://[^\s\"'<>]+", html)
    return [url for url in urls if "127.0.0.1" not in url and "localhost" not in url]


# ---------------------------------------------------------------------------
# P5-1 update_pointer contract
# ---------------------------------------------------------------------------
class UpdatePointerAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        from le_calc.workflow import commit, atomic_io

        self.commit = commit
        self.atomic_io = atomic_io
        self.root = work_dir("pointer-" + self._testMethodName)
        self.ops = self.root / "ops"
        self.ops.mkdir(parents=True, exist_ok=True)
        pointer = self.root / "data" / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        self.old = {"dataset_id": "phase1-dataset-003", "database": "data/releases/old/data.sqlite",
                    "file_sha256": "a" * 64}
        pointer.write_text(json.dumps(self.old, ensure_ascii=False), encoding="utf-8")
        self.pointer = pointer
        self.old_sha = atomic_io.sha256_file(pointer)
        self.candidate = self.root / "candidate" / "current.json"
        self.candidate.parent.mkdir(parents=True, exist_ok=True)
        self.new = {"dataset_id": "p5-acceptance-004", "database": "data/releases/new/data.sqlite",
                    "file_sha256": "b" * 64}
        self.candidate.write_text(json.dumps(self.new, ensure_ascii=False), encoding="utf-8")
        self.fp = commit.compute_input_fingerprint(
            {"operation": "update_pointer", "old": self.old_sha, "new": self.new}
        )
        self.target = "data/current.json"

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "pointer::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"pointer-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def update(self, *, operation_id: str = "pointer-op", fingerprint: str | None = None,
               candidate: pathlib.Path | None = None, expected_sha: str | None = None,
               target: str | None = None, expected_revision: str | None = None):
        return self.commit.update_pointer(
            candidate or self.candidate,
            target or self.target,
            operation_id=operation_id,
            attempt_id="attempt-1",
            input_fingerprint=fingerprint or self.fp,
            expected_current_sha256=self.old_sha if expected_sha is None else expected_sha,
            expected_current_revision=expected_revision,
            operations_root=self.ops,
            root=self.root,
        )

    def records(self) -> list[pathlib.Path]:
        return sorted(path for path in self.ops.glob("*.json") if path.is_file())

    def test_success_whitelist_and_rejections(self) -> None:
        result = self.update()
        self.assertEqual(result.status, "committed", result.to_dict())
        self.assertEqual(read_json(self.pointer), self.new)
        self.assertEqual([p for p in self.pointer.parent.glob("*.tmp")], [], "出现临时半成品文件")
        record = self.commit.read_operation_record("pointer-op", self.ops)
        self.assertEqual(record["operation_kind"], "update_pointer")
        self.assertEqual(record["published_refs"][0]["sha256"], self.atomic_io.sha256_file(self.pointer))
        # 白名单 build pointer
        build_target = "data/builds/letools/p5accept/current.json"
        build_pointer = self.root / build_target
        build_pointer.parent.mkdir(parents=True, exist_ok=True)
        build_pointer.write_text(json.dumps({"revision_id": "rev-old"}), encoding="utf-8")
        build_sha = self.atomic_io.sha256_file(build_pointer)
        build_result = self.commit.update_pointer(
            self.candidate, build_target, operation_id="build-pointer-op", attempt_id="a1",
            input_fingerprint=self.commit.compute_input_fingerprint({"build": "p5"}),
            expected_current_sha256=build_sha,
            operations_root=self.ops, root=self.root,
        )
        self.assertEqual(build_result.status, "committed", build_result.to_dict())
        self.assertEqual(read_json(build_pointer), self.new)
        records_before = [p.name for p in self.records()]
        new_sha = self.atomic_io.sha256_file(self.pointer)
        bad = self.root / "candidate" / "bad.json"
        bad.write_text("{not-json", encoding="utf-8")
        rejection_cases = [
            ("non_whitelist", lambda: self.update(
                operation_id="rej-nonwhitelist", fingerprint=self.commit.compute_input_fingerprint({"nv": 1}),
                expected_sha=new_sha, target="data/not-a-pointer.json")),
            ("traversal", lambda: self.update(
                operation_id="rej-traversal", fingerprint=self.commit.compute_input_fingerprint({"tr": 1}),
                expected_sha=new_sha, target="../outside.json")),
            ("invalid_candidate", lambda: self.update(
                operation_id="rej-invalid", fingerprint=self.commit.compute_input_fingerprint({"iv": 1}),
                expected_sha=new_sha, candidate=bad)),
            ("expected_mismatch", lambda: self.update(
                operation_id="rej-expected", fingerprint=self.commit.compute_input_fingerprint({"em": 1}),
                expected_sha="0" * 64)),
        ]
        failure_results = {}
        for name, call in rejection_cases:
            try:
                outcome = call()
                failure_results[name] = outcome.status
                if outcome.status == "committed":
                    self.fail(f"{name}: 不应 committed")
            except Exception as exc:  # 非法路径允许明确抛错，但不得写指针
                failure_results[name] = f"raised:{type(exc).__name__}"
        # 目标缺失/损坏（使用无既有 owner 的独立 build pointer 目标）
        missing_path = self.root / "data/builds/letools/missing/current.json"
        missing = self.update(operation_id="rej-missing",
                              fingerprint=self.commit.compute_input_fingerprint({"mi": 1}),
                              expected_sha="0" * 64,
                              target="data/builds/letools/missing/current.json")
        self.assertEqual(missing.status, "failed", missing.to_dict())
        failure_results["missing"] = missing.status
        corrupt_path = self.root / "data/builds/letools/corrupt/current.json"
        corrupt_path.parent.mkdir(parents=True, exist_ok=True)
        corrupt_path.write_text("{bad-pointer", encoding="utf-8")
        corrupt = self.update(operation_id="rej-corrupt",
                              fingerprint=self.commit.compute_input_fingerprint({"co": 1}),
                              expected_sha=self.atomic_io.sha256_file(corrupt_path),
                              target="data/builds/letools/corrupt/current.json")
        self.assertEqual(corrupt.status, "failed", corrupt.to_dict())
        failure_results["corrupt"] = corrupt.status
        self.assertIn("pointer_target_missing", [i["code"] for i in missing.issues])
        self.assertIn("pointer_target_corrupt", [i["code"] for i in corrupt.issues])
        self.assertEqual([p.name for p in self.records()], records_before,
                         "失败的操作不得产生 operation record")
        self.assertEqual(list(self.ops.glob(".intents/*.json")), [], "失败的操作不得残留 intent")
        self.record(success="committed", build_pointer="committed", failures=failure_results)

    def test_idempotency_conflict_and_record_failure_recovery(self) -> None:
        first = self.update()
        self.assertEqual(first.status, "committed")
        pointer_bytes = self.pointer.read_bytes()
        inode = self.pointer.stat().st_ino
        replay = self.update()
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertEqual(self.pointer.read_bytes(), pointer_bytes)
        self.assertEqual(self.pointer.stat().st_ino, inode, "replayed 重写了指针文件")
        conflict = self.update(fingerprint=self.commit.compute_input_fingerprint({"other": 1}))
        self.assertEqual(conflict.status, "conflict", conflict.to_dict())
        self.assertEqual(self.pointer.read_bytes(), pointer_bytes)
        # 记录写失败 → build pointer 已替换、无 record、intent 保留；不同候选重试 → replayed，字节不变
        target = "data/builds/letools/p5accept/current.json"
        target_path = self.root / target
        target_path.parent.mkdir(parents=True, exist_ok=True)
        old = {"dataset_id": "record-old"}
        target_path.write_text(json.dumps(old), encoding="utf-8")
        target_sha = self.atomic_io.sha256_file(target_path)
        candidate2 = self.root / "candidate-record.json"
        candidate2.write_text(json.dumps({"dataset_id": "record-new"}), encoding="utf-8")
        fp2 = self.commit.compute_input_fingerprint({"op": "record-failure"})
        with mock.patch.object(self.commit, "_write_operation_record",
                               side_effect=OSError("injected pointer record failure")):
            failed = self.commit.update_pointer(
                candidate2, target, operation_id="pointer-record-failure", attempt_id="a1",
                input_fingerprint=fp2, expected_current_sha256=target_sha,
                operations_root=self.ops, root=self.root,
            )
        self.assertEqual(failed.status, "failed", failed.to_dict())
        self.assertIn("record_write_failed", [i["code"] for i in failed.issues])
        published_bytes = target_path.read_bytes()
        intent = self.commit._intent_path(self.ops, "pointer-record-failure")
        self.assertTrue(intent.is_file(), "记录失败后应保留 intent")
        retry_candidate = self.root / "retry-candidate.json"
        retry_candidate.write_text(json.dumps({"dataset_id": "retry-different"}), encoding="utf-8")
        retry = self.commit.update_pointer(
            retry_candidate, target, operation_id="pointer-record-failure", attempt_id="a2",
            input_fingerprint=fp2, expected_current_sha256=target_sha,
            operations_root=self.ops, root=self.root,
        )
        self.assertEqual(retry.status, "replayed", retry.to_dict())
        self.assertIn("recovered_record_after_publish", [i["code"] for i in retry.issues])
        self.assertEqual(target_path.read_bytes(), published_bytes)
        record = self.commit.read_operation_record("pointer-record-failure", self.ops)
        self.assertIsNotNone(record)
        self.assertFalse(intent.exists(), "恢复后 intent 应清理")
        # stale intent（替换前崩溃）应丢弃并继续发布
        stale_target = "data/builds/letools/stale/current.json"
        stale_path = self.root / stale_target
        stale_path.parent.mkdir(parents=True, exist_ok=True)
        stale_path.write_text(json.dumps(old), encoding="utf-8")
        stale_sha = self.atomic_io.sha256_file(stale_path)
        stale_candidate = self.root / "stale-candidate.json"
        stale_candidate.write_text(json.dumps({"dataset_id": "stale"}), encoding="utf-8")
        stale_intent = {
            "schema_version": "le-operation-intent-v1", "operation_id": "pointer-stale",
            "attempt_id": "stale", "operation_kind": "update_pointer", "input_fingerprint": fp2,
            "candidate_refs": [{"candidate_id": "pointer", "kind": "file",
                                "path": stale_target,
                                "sha256": self.atomic_io.sha256_file(stale_candidate)}],
            "previous_sha256": stale_sha, "created_at": "2026-01-01T00:00:00+00:00",
        }
        stale_intent_path = self.commit._intent_path(self.ops, "pointer-stale")
        stale_intent_path.parent.mkdir(parents=True, exist_ok=True)
        stale_intent_path.write_text(json.dumps(stale_intent), encoding="utf-8")
        stale_result = self.commit.update_pointer(
            stale_candidate, stale_target, operation_id="pointer-stale", attempt_id="fresh",
            input_fingerprint=fp2, expected_current_sha256=stale_sha,
            operations_root=self.ops, root=self.root,
        )
        self.assertEqual(stale_result.status, "committed", stale_result.to_dict())
        self.assertEqual(read_json(stale_path), {"dataset_id": "stale"})
        self.assertFalse(stale_intent_path.exists())
        self.record(replay=replay.status, conflict=conflict.status, record_failure=failed.status,
                    recovered=retry.status, stale_intent="committed")

    def test_concurrency_and_atomic_failure(self) -> None:
        barrier = threading.Barrier(2)
        results = {}

        def same_op_worker(label: str) -> None:
            barrier.wait(timeout=15)
            results[label] = self.update(operation_id="pointer-concurrent")

        threads = [threading.Thread(target=same_op_worker, args=("a",)),
                   threading.Thread(target=same_op_worker, args=("b",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        statuses = sorted(result.status for result in results.values())
        self.assertEqual(statuses, ["committed", "replayed"], statuses)
        self.assertEqual(read_json(self.pointer), self.new)
        self.assertTrue((self.ops / "pointer-concurrent.json").is_file())
        # 不同 op 竞争同一 build pointer：最多一个 committed，失败方不破坏成功方
        target = "data/builds/letools/race/current.json"
        race_path = self.root / target
        race_path.parent.mkdir(parents=True, exist_ok=True)
        race_old = {"dataset_id": "race-old"}
        race_path.write_text(json.dumps(race_old), encoding="utf-8")
        race_sha = self.atomic_io.sha256_file(race_path)
        candidate_b = self.root / "candidate-b.json"
        candidate_b.write_text(json.dumps({"dataset_id": "race-b"}), encoding="utf-8")
        race_barrier = threading.Barrier(2)
        race_results = {}

        def race_worker(label: str, candidate: pathlib.Path) -> None:
            race_barrier.wait(timeout=15)
            race_results[label] = self.commit.update_pointer(
                candidate, target, operation_id="pointer-race-" + label, attempt_id=label,
                input_fingerprint=self.commit.compute_input_fingerprint({"candidate": label}),
                expected_current_sha256=race_sha, operations_root=self.ops, root=self.root,
            )

        race_threads = [threading.Thread(target=race_worker, args=("a", self.candidate)),
                        threading.Thread(target=race_worker, args=("b", candidate_b))]
        for thread in race_threads:
            thread.start()
        for thread in race_threads:
            thread.join(timeout=60)
        race_statuses = sorted(result.status for result in race_results.values())
        self.assertEqual(race_statuses, ["committed", "failed"], race_statuses)
        winner = next(label for label, result in race_results.items() if result.status == "committed")
        expected_winner = self.new if winner == "a" else {"dataset_id": "race-b"}
        self.assertEqual(read_json(race_path), expected_winner)
        loser = next(label for label, result in race_results.items() if result.status == "failed")
        self.assertFalse((self.ops / f"pointer-race-{loser}.json").exists())
        # 原子替换异常 → failed、指针不变、无 intent/record
        target3 = "data/builds/letools/atomic/current.json"
        atomic_path = self.root / target3
        atomic_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_old = {"dataset_id": "atomic-old"}
        atomic_path.write_text(json.dumps(atomic_old), encoding="utf-8")
        atomic_sha = self.atomic_io.sha256_file(atomic_path)
        before = atomic_path.read_bytes()
        with mock.patch.object(self.atomic_io, "atomic_replace_file",
                               side_effect=OSError("injected pointer replace failure")):
            atomic_result = self.commit.update_pointer(
                self.candidate, target3, operation_id="pointer-atomic", attempt_id="a1",
                input_fingerprint=self.commit.compute_input_fingerprint({"atomic": 1}),
                expected_current_sha256=atomic_sha, operations_root=self.ops, root=self.root,
            )
        self.assertEqual(atomic_result.status, "failed", atomic_result.to_dict())
        self.assertIn("publish_failed", [i["code"] for i in atomic_result.issues])
        self.assertEqual(atomic_path.read_bytes(), before)
        self.assertFalse(self.commit._intent_path(self.ops, "pointer-atomic").exists())
        self.assertIsNone(self.commit.read_operation_record("pointer-atomic", self.ops))
        self.assertTrue(callable(getattr(self.commit, "update_pointer", None)), "update_pointer 未实现")
        unsupported = ["new_revision", "publish_dataset", "publish_rule", "register_mechanism"]
        self.record(same_op=statuses, race=race_statuses, winner=winner,
                    atomic_failure=atomic_result.status, unsupported_kinds=unsupported)


# ---------------------------------------------------------------------------
# P5-2 variant case
# ---------------------------------------------------------------------------
class VariantAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = work_dir("variant-" + self._testMethodName)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "variant::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"variant-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def run_variant(self, tag: str, run_id: str, extra: list[str] | None = None) -> tuple[dict, dict[str, str], pathlib.Path]:
        out = self.out / tag
        args = [sys.executable, "scripts/le_commands.py", "run", "--case", VARIANT_CASE,
                "--out", rel(out), "--run-id", run_id, "--publish", "staged",
                "--operations-root", rel(self.ops), "--no-report-cache"]
        args.extend(extra or [])
        completed = run_cli(args, env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
                            log=f"variant-{tag}")
        self.assertEqual(completed.returncode, 0,
                         f"variant {tag}: {completed.stdout[-4000:]}{completed.stderr[-2000:]}")
        return read_json(out / "result.json"), parse_stage_statuses(completed.stdout + completed.stderr), out

    def test_variant_expected_source_diff_and_offline_report(self) -> None:
        variant = read_json(ROOT / VARIANT_CASE)
        expected = read_json(ROOT / VARIANT_EXPECTED)
        fr_expected = read_json(ROOT / FR_EXPECTED)
        bh_expected_before = sha256_file(ROOT / BH_EXPECTED)
        fr_expected_before = sha256_file(ROOT / FR_EXPECTED)
        self.assertEqual(expected["case_id"], "flame-reave-variant")
        self.assertEqual(expected["source_run"]["run_id"], "p5-variant-calibration")
        self.assertIn("reports/p5-drill/work/calibration", expected["source_run"]["path"])
        fr_hit = next(c["expected"] for c in fr_expected["checks"] if c["pointer"] == "/single_hit/totals/damage")
        variant_hit = next(c["expected"] for c in expected["checks"] if c["pointer"] == "/single_hit/totals/damage")
        self.assertNotEqual(variant_hit, fr_hit, "variant expected 不得照抄 FR 数值")
        self.assertAlmostEqual(variant_hit / fr_hit, 0.75, places=12)
        scenario = read_json(ROOT / variant["inputs"]["scenario"])
        self.assertEqual(scenario["target"]["fire_resistance"], 0.25)
        self.assertTrue((ROOT / "examples/flame-reave-variant/expected/derivations.zh-CN.md").is_file())
        # validate
        validate = run_cli([sys.executable, "scripts/le_commands.py", "validate", "--case", VARIANT_CASE],
                           log="variant-validate")
        self.assertEqual(validate.returncode, 0, validate.stdout[-3000:] + validate.stderr[-1000:])
        cold_result, cold_statuses, cold_dir = self.run_variant("cold", "p5-acceptance-variant-cold")
        warm_result, warm_statuses, warm_dir = self.run_variant("warm", "p5-acceptance-variant-warm")
        self.assertEqual(warm_statuses.get("simulate"), "hit", warm_statuses)
        self.assertEqual(warm_statuses.get("validate_result"), "hit", warm_statuses)
        self.assertEqual(result_without_run(cold_result), result_without_run(warm_result),
                         "variant 冷/热 result（去 run）不一致")
        for out in (cold_dir, warm_dir):
            self.assertTrue((out / "result.json").is_file())
            self.assertTrue((out / "run-manifest.json").is_file())
            self.assertTrue((out / "flame-reave-damage.html").is_file())
            manifest = read_json(out / "run-manifest.json")
            self.assertTrue((manifest.get("consistency") or {}).get("all_pass"), out)
            html = (out / "flame-reave-damage.html").read_text(encoding="utf-8", errors="replace")
            self.assertEqual(external_urls(html), [], f"{out.name} 报告包含外部 URL")
        # expected files 未被改写
        self.assertEqual(sha256_file(ROOT / BH_EXPECTED), bh_expected_before)
        self.assertEqual(sha256_file(ROOT / FR_EXPECTED), fr_expected_before)
        self.record(validate="PASS", cold_statuses=cold_statuses, warm_statuses=warm_statuses,
                    fr_single=fr_hit, variant_single=variant_hit,
                        ratio=variant_hit / fr_hit, expected_source=expected["source_run"]["run_id"])


# ---------------------------------------------------------------------------
# P5-3 knowledge second reuse
# ---------------------------------------------------------------------------
class KnowledgeReuseAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        from le_knowledge import api
        self.api = api
        self.root = work_dir("knowledge-" + self._testMethodName)
        self.knowledge_dir = self.root / "knowledge"
        shutil.copytree(ROOT / "knowledge", self.knowledge_dir,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.mechanisms = self.knowledge_dir / "mechanisms"
        source = ROOT / "reports/p5-drill/knowledge/mechanisms/fr-hit-variant-target-resistance.json"
        self.assertNotEqual(source, self.mechanisms / source.name)
        payload = read_json(source)
        payload["mechanism_id"] = "p5-acceptance-variant-target-resistance"
        payload["title"] = {"zh": "P5 验收隔离知识机制", "en": "P5 acceptance isolated knowledge mechanism"}
        payload["case_scope"] = ["flame-reave-variant"]
        payload["aliases"] = ["p5 acceptance variant", "target fire resistance", "火抗"]
        destination = self.mechanisms / (payload["mechanism_id"] + ".json")
        destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.index = self.root / "knowledge.sqlite"
        self.index_md = self.root / "INDEX.generated.md"
        self.mechanism_id = payload["mechanism_id"]

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "knowledge::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"knowledge-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def test_isolated_rebuild_search_show_and_api_reuse(self) -> None:
        rebuild = self.api.rebuild(
            knowledge_dir=self.knowledge_dir, index_path=self.index, index_md_path=self.index_md,
            root=ROOT,
        )
        self.assertTrue(rebuild.get("ok"), rebuild)
        self.assertTrue(self.index.is_file(), "未生成隔离索引")
        search_kwargs = {"knowledge_dir": self.knowledge_dir, "index_path": self.index, "root": ROOT}
        first = self.api.search({"queries": [{"query_id": "mech", "term": "mechanism:" + self.mechanism_id,
                                              "kind": "id"}],
                                 "filters": {}, "context": {}, "limit": 20}, **search_kwargs)
        second = self.api.search({"queries": [{"query_id": "mech", "term": "mechanism:" + self.mechanism_id,
                                               "kind": "id"}],
                                 "filters": {}, "context": {}, "limit": 20}, **search_kwargs)
        first_bytes = json.dumps(first, ensure_ascii=False, sort_keys=True)
        second_bytes = json.dumps(second, ensure_ascii=False, sort_keys=True)
        self.assertEqual(first_bytes, second_bytes, "两次 search 字节/语义不一致")
        self.assertIn(self.mechanism_id, first_bytes, "新机制未出现在 search 结果")
        show = self.api.show(self.mechanism_id, knowledge_dir=self.knowledge_dir,
                             index_path=self.index, root=ROOT)
        self.assertIn(self.mechanism_id, json.dumps(show, ensure_ascii=False), "show 不可读")
        # API 级二次复用：domain.resolve_build 注入隔离知识检索，两次语义一致且绑定新机制
        from le_calc.workflow import domain
        variant = read_json(ROOT / VARIANT_CASE)
        main_skill = variant.get("main_skill") or {}
        request = {
            "schema_version": "le-resolve-build-request-v1",
            "case_id": "flame-reave-variant",
            "case_ref": VARIANT_CASE,
            "main_skill": {"ability_id": main_skill.get("ability_id"), "name": main_skill.get("name")},
            "knowledge": {
                "knowledge_dir": rel(self.knowledge_dir),
                "index_path": rel(self.index),
            },
        }
        first_resolve = domain.resolve_build(request, root=ROOT)
        second_resolve = domain.resolve_build(request, root=ROOT)
        def strip_elapsed(value):
            if isinstance(value, dict):
                return {key: strip_elapsed(item) for key, item in value.items() if key != "elapsed_ms"}
            if isinstance(value, list):
                return [strip_elapsed(item) for item in value]
            return value
        self.assertEqual(strip_elapsed(first_resolve), strip_elapsed(second_resolve),
                         "两次 resolve 语义不一致")
        self.assertIn(self.mechanism_id, json.dumps(first_resolve, ensure_ascii=False),
                      "resolve 未绑定隔离知识新机制")
        self.record(rebuild_counts=rebuild.get("counts") or rebuild.get("index") or {},
                    search_bytes_equal=True, show_readable=True, resolve_twice_equal=True,
                    new_mechanism_bound=True,
                    cli_limitation="prepare/run CLI 无 knowledge-dir 参数；本验收使用 le_knowledge/domain API 注入")


# ---------------------------------------------------------------------------
# P5-4 package / clean handover
# ---------------------------------------------------------------------------
class PackageAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = work_dir("package-" + self._testMethodName)
        self.bundle = self.root / "bundle"
        self.clean = self.root / "clean"

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "package::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"package-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # 打包目录/zip/解压副本很大；保留 manifest 副本与日志，删除临时包
        manifest = self.bundle / "package-manifest.json"
        if manifest.is_file():
            shutil.copyfile(manifest, self.root / "package-manifest.json")
        for directory in (self.bundle, self.clean):
            shutil.rmtree(directory, ignore_errors=True)
        for zip_path in self.root.glob("*.zip"):
            zip_path.unlink()

    def record(self, **details) -> None:
        self._details = details

    def test_package_check_and_clean_variant_session(self) -> None:
        package = run_cli([sys.executable, "scripts/le_commands.py", "package",
                           "--out", rel(self.bundle)], log="package-build", timeout=3600)
        self.assertEqual(package.returncode, 0, package.stdout[-5000:] + package.stderr[-3000:])
        self.assertTrue(self.bundle.is_dir(), "package 输出目录不存在")
        manifest = read_json(self.bundle / "package-manifest.json")
        files = manifest.get("files") or []
        variant_files = [row for row in files if str(row.get("path") or "").startswith("examples/flame-reave-variant/")]
        self.assertGreaterEqual(len(variant_files), 20, f"包内变体文件不足 20：{len(variant_files)}")
        # package.py 显式携带 4 个冻结规范 run 作为 provenance；除此之外不得含历史 run/scratch
        allowed_reports = (
            "reports/phase-2/",
            "reports/discovery/phase-2-o3ZRm9my/",
            "reports/black-hole/save-build-bh-affix-db/",
            "reports/flame-reave/save-build-fr-hit-stage16/",
        )
        frozen_reports = []
        for row in files:
            path = str(row.get("path") or "")
            if path.startswith("reports/"):
                if path.startswith(allowed_reports):
                    frozen_reports.append(path)
                else:
                    self.fail(f"包内含非显式白名单历史 run：{path}")
            self.assertFalse(path.startswith(".scratch/"), f"包内含 .scratch：{path}")
            self.assertNotRegex(path, r"^[A-Za-z]:[\\/]", f"包内绝对路径：{path}")
        check = run_cli([sys.executable, "scripts/check_package.py", str(self.bundle)],
                        log="package-check", timeout=1200)
        self.assertEqual(check.returncode, 0, check.stdout[-5000:] + check.stderr[-3000:])
        self.assertIn("PASS", check.stdout)
        # 解压 zip（默认与输出目录同名 .zip）到干净会话
        zip_candidates = sorted(self.root.glob("*.zip"))
        self.assertTrue(zip_candidates, "未生成 zip")
        self.clean.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_candidates[0]) as archive:
            archive.extractall(self.clean)
        clean_scripts = self.clean / "scripts" / "le_commands.py"
        self.assertTrue(clean_scripts.is_file(), "干净包缺少 scripts/le_commands.py")
        clean_env = {"PYTHONPATH": str(self.clean / "tools/phase3/src"),
                     "LE_PIPELINE_CACHE_ROOT": str(self.clean / ".clean-cache")}
        validate = run_cli([sys.executable, str(clean_scripts), "validate", "--case", VARIANT_CASE],
                           cwd=self.clean, env=clean_env, log="package-clean-validate", timeout=600)
        self.assertEqual(validate.returncode, 0, validate.stdout[-4000:] + validate.stderr[-2000:])
        run = run_cli([sys.executable, str(clean_scripts), "run", "--case", VARIANT_CASE,
                       "--out", ".clean-runs/cold", "--run-id", "p5-package-cold"],
                      cwd=self.clean, env=clean_env, log="package-clean-run", timeout=1200)
        self.assertEqual(run.returncode, 0, run.stdout[-5000:] + run.stderr[-3000:])
        clean_result = self.clean / ".clean-runs" / "cold"
        self.assertTrue((clean_result / "result.json").is_file())
        self.assertTrue((clean_result / "run-manifest.json").is_file())
        self.assertTrue((clean_result / "flame-reave-damage.html").is_file())
        reproduce = run_cli([sys.executable, str(clean_scripts), "reproduce", "--case", VARIANT_CASE,
                             "--out", ".clean-runs/repro", "--run-id", "p5-package-repro"],
                            cwd=self.clean, env=clean_env, log="package-clean-reproduce", timeout=1800)
        self.assertEqual(reproduce.returncode, 0, reproduce.stdout[-5000:] + reproduce.stderr[-3000:])
        report = read_json(self.clean / ".clean-runs" / "repro" / "p5-package-repro" / "reproduce-report.json")
        self.assertTrue(report.get("ok"), report)
        self.assertEqual(len(report.get("checks") or []), 41, "干净包 reproduce 不是 41 项")
        self.record(package="PASS", files=len(files), variant_files=len(variant_files),
                    frozen_report_files=len(frozen_reports),
                    frozen_report_allowlist=list(allowed_reports),
                    check="PASS", clean_validate="PASS", clean_run="PASS",
                    clean_reproduce=41)


# ---------------------------------------------------------------------------
# P5-R1/R2: pointer progression + expected CAS interleaving (review fixes)
# ---------------------------------------------------------------------------
POINTER_EVIDENCE = P5_ROOT / "pointer-r1r2"


class PointerProgressionAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        from le_calc.workflow import commit, atomic_io

        self.commit = commit
        self.atomic_io = atomic_io
        self.root = work_dir("pointer-progression-" + self._testMethodName)
        self.ops = self.root / "ops"

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "pointer-r1::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        POINTER_EVIDENCE.mkdir(parents=True, exist_ok=True)
        (POINTER_EVIDENCE / f"{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def make_pointer(self, target: str, field: str) -> tuple[pathlib.Path, str]:
        pointer = self.root / target
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({field: "old"}), encoding="utf-8")
        return pointer, self.atomic_io.sha256_file(pointer)

    def update(self, candidate: pathlib.Path, target: str, operation_id: str, *,
               expected_sha: str | None = None, expected_revision: str | None = None):
        return self.commit.update_pointer(
            candidate, target, operation_id=operation_id, attempt_id="a1",
            input_fingerprint=self.commit.compute_input_fingerprint(
                {"op": operation_id, "expected": expected_sha or expected_revision}),
            expected_current_sha256=expected_sha,
            expected_current_revision=expected_revision,
            operations_root=self.ops, root=self.root,
        )

    def assert_audit_records(self, operation_ids: list[str]) -> None:
        names = {path.stem for path in self.ops.glob("*.json")}
        for operation_id in operation_ids:
            self.assertIn(operation_id, names, f"缺少审计记录 {operation_id}: {sorted(names)}")

    def test_dataset_and_build_sha_progression_old_expected_and_replay(self) -> None:
        results = {}
        for kind, target, field in (
            ("dataset", "data/current.json", "dataset_id"),
            ("build", "data/builds/letools/r1r2/current.json", "revision_id"),
        ):
            pointer, old_sha = self.make_pointer(target, field)
            candidate_a = self.root / f"{kind}-a.json"
            candidate_b = self.root / f"{kind}-b.json"
            candidate_a.write_text(json.dumps({field: "next-a"}), encoding="utf-8")
            candidate_b.write_text(json.dumps({field: "next-b"}), encoding="utf-8")
            op_a, op_b = f"{kind}-op-a", f"{kind}-op-b"
            first = self.update(candidate_a, target, op_a, expected_sha=old_sha)
            self.assertEqual(first.status, "committed", first.to_dict())
            a_sha = self.atomic_io.sha256_file(pointer)
            second = self.update(candidate_b, target, op_b, expected_sha=a_sha)
            self.assertEqual(second.status, "committed", second.to_dict())
            self.assertEqual(read_json(pointer), {field: "next-b"})
            # 旧 expected 被拒绝
            stale = self.update(candidate_a, target, f"{kind}-stale", expected_sha=old_sha)
            self.assertEqual(stale.status, "failed", stale.to_dict())
            self.assertIn("current_revision_mismatch", [i["code"] for i in stale.issues])
            # 旧 op1 重试：replayed 语义，当前指针仍 B，不误判历史损坏
            old_replay = self.update(candidate_a, target, op_a, expected_sha=old_sha)
            self.assertEqual(old_replay.status, "replayed", old_replay.to_dict())
            self.assertIn("pointer_advanced_after_commit", [i["code"] for i in old_replay.issues])
            self.assertEqual(read_json(pointer), {field: "next-b"}, "旧 op 重试回退了指针")
            self.assert_audit_records([op_a, op_b])
            self.assertFalse((self.ops / f"{kind}-stale.json").exists())
            results[kind] = {"first": first.status, "second": second.status,
                             "stale": stale.status, "old_replay": old_replay.status,
                             "final": read_json(pointer),
                             "records": sorted(p.name for p in self.ops.glob("*.json"))}
        self.record(progression=results)

    def test_dataset_and_build_revision_progression(self) -> None:
        results = {}
        for kind, target, field in (
            ("dataset", "data/current.json", "dataset_id"),
            ("build", "data/builds/letools/r1r2rev/current.json", "revision_id"),
        ):
            pointer, _ = self.make_pointer(target, field)
            candidate_a = self.root / f"{kind}-ra.json"
            candidate_b = self.root / f"{kind}-rb.json"
            candidate_a.write_text(json.dumps({field: "rev-a"}), encoding="utf-8")
            candidate_b.write_text(json.dumps({field: "rev-b"}), encoding="utf-8")
            rev_a = f"{kind}-rev-a"
            first = self.update(candidate_a, target, f"{kind}-rev-op-a", expected_revision="old")
            self.assertEqual(first.status, "committed", first.to_dict())
            second = self.update(candidate_b, target, f"{kind}-rev-op-b", expected_revision="rev-a")
            self.assertEqual(second.status, "committed", second.to_dict())
            self.assertEqual(read_json(pointer), {field: "rev-b"})
            stale = self.update(candidate_a, target, f"{kind}-rev-stale", expected_revision="old")
            self.assertEqual(stale.status, "failed", stale.to_dict())
            replay = self.update(candidate_a, target, f"{kind}-rev-op-a", expected_revision="old")
            self.assertEqual(replay.status, "replayed", replay.to_dict())
            self.assertEqual(read_json(pointer), {field: "rev-b"})
            results[kind] = {"first": first.status, "second": second.status,
                             "stale": stale.status, "replay": replay.status,
                             "final": read_json(pointer)}
        self.record(progression=results)


class PointerInterleavingAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        from le_calc.workflow import commit, atomic_io

        self.commit = commit
        self.atomic_io = atomic_io

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "pointer-r2::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        POINTER_EVIDENCE.mkdir(parents=True, exist_ok=True)
        (POINTER_EVIDENCE / f"{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def race(self, target_type: str, expected_mode: str) -> dict:
        """固定交错：slow 在真实 expected 校验后暂停，fast 提交，slow 恢复替换。"""
        root = work_dir(f"pointer-r2-{target_type}-{expected_mode}")
        ops = root / "ops"
        target = "data/current.json" if target_type == "dataset" else "data/builds/letools/r2/current.json"
        field = "dataset_id" if target_type == "dataset" else "revision_id"
        pointer = root / target
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps({field: "old"}), encoding="utf-8")
        old_sha = self.atomic_io.sha256_file(pointer)
        candidates = {}
        for label in ("slow", "fast"):
            path = root / f"{label}.json"
            path.write_text(json.dumps({field: label}), encoding="utf-8")
            candidates[label] = path
        expected_value = old_sha if expected_mode == "sha" else "old"
        checked = threading.Event()
        fast_done = threading.Event()
        results: dict[str, object] = {}
        errors: list[str] = []
        original_matches = self.commit._pointer_expected_matches

        def hook(expected, target_path):
            matched = original_matches(expected, target_path)  # 真实比较，不 mock 结果
            if threading.current_thread().name == "slow-writer":
                checked.set()
                if not fast_done.wait(15):
                    raise TimeoutError("fast writer did not finish")
            return matched

        def update(label: str):
            return self.commit.update_pointer(
                candidates[label], target, operation_id=f"r2-{target_type}-{expected_mode}-{label}",
                attempt_id=label,
                input_fingerprint=self.commit.compute_input_fingerprint(
                    {"race": target_type, "mode": expected_mode, "label": label}),
                expected_current_sha256=(expected_value if expected_mode == "sha" else None),
                expected_current_revision=(expected_value if expected_mode == "revision" else None),
                operations_root=ops, root=root,
            )

        def slow():
            try:
                results["slow"] = update("slow")
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"slow: {type(exc).__name__}: {exc}")
            finally:
                fast_done.set()

        def fast():
            try:
                if not checked.wait(15):
                    raise TimeoutError("slow never checked")
                results["fast"] = update("fast")
            except BaseException as exc:  # noqa: BLE001
                errors.append(f"fast: {type(exc).__name__}: {exc}")
            finally:
                fast_done.set()

        with mock.patch.object(self.commit, "_pointer_expected_matches", side_effect=hook):
            slow_thread = threading.Thread(target=slow, name="slow-writer")
            fast_thread = threading.Thread(target=fast, name="fast-writer")
            slow_thread.start()
            fast_thread.start()
            slow_thread.join(timeout=30)
            fast_thread.join(timeout=30)
        self.assertFalse(slow_thread.is_alive() or fast_thread.is_alive(), "竞态线程未结束")
        self.assertEqual(errors, [], errors)
        statuses = {label: result.status for label, result in results.items()}
        self.assertEqual(sorted(statuses.values()), ["committed", "failed"], statuses)
        winner = next(label for label, status in statuses.items() if status == "committed")
        loser = next(label for label, status in statuses.items() if status == "failed")
        self.assertEqual(read_json(pointer), {field: winner}, "最终指针不是赢家内容")
        loser_codes = [issue["code"] for issue in results[loser].issues]
        self.assertIn("current_revision_mismatch", loser_codes, loser_codes)
        winner_record = ops / f"r2-{target_type}-{expected_mode}-{winner}.json"
        self.assertTrue(winner_record.is_file())
        self.assertFalse((ops / f"r2-{target_type}-{expected_mode}-{loser}.json").exists(),
                         "失败方产生了 operation record")
        # 赢家 replay 不被失败方影响
        replay = self.commit.update_pointer(
            candidates[winner], target,
            operation_id=f"r2-{target_type}-{expected_mode}-{winner}", attempt_id="replay",
            input_fingerprint=self.commit.compute_input_fingerprint(
                {"race": target_type, "mode": expected_mode, "label": winner}),
            expected_current_sha256=(expected_value if expected_mode == "sha" else None),
            expected_current_revision=(expected_value if expected_mode == "revision" else None),
            operations_root=ops, root=root,
        )
        self.assertEqual(replay.status, "replayed", replay.to_dict())
        self.assertEqual(read_json(pointer), {field: winner})
        return {"target_type": target_type, "expected_mode": expected_mode,
                "statuses": statuses, "winner": winner, "loser": loser,
                "loser_codes": loser_codes, "final": read_json(pointer),
                "winner_record": winner_record.name,
                "replay": replay.status}

    def test_fixed_interleaving_four_combinations(self) -> None:
        matrix = []
        for target_type in ("dataset", "build"):
            for expected_mode in ("sha", "revision"):
                matrix.append(self.race(target_type, expected_mode))
        for row in matrix:
            self.assertEqual(sorted(row["statuses"].values()), ["committed", "failed"], row)
        self.record(matrix=matrix,
                    method="真实线程/Event 固定交错；hook 仅包装真实 _pointer_expected_matches 后暂停，不 mock 比较/I/O")


class PointerReviewScriptsAcceptanceTests(unittest.TestCase):
    def test_review_scripts_rerun_with_isolated_output(self) -> None:
        source_dir = ROOT / "reports/p5-lead-review-20261109"
        scripts = ["pointer_boundary_checks.py", "pointer_matrix_checks.py"]
        out_dir = POINTER_EVIDENCE / "review-scripts"
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        generated = {}
        for name in scripts:
            source = source_dir / name
            self.assertTrue(source.is_file(), f"缺少审查脚本：{source}")
            text = source.read_text(encoding="utf-8")
            marker = "OUT=ROOT/'reports/p5-lead-review-20261109'"
            self.assertIn(marker, text, f"{name} 输出路径格式变化")
            text = text.replace(marker, f"OUT=ROOT/{str(out_dir.relative_to(ROOT))!r}")
            copy_path = out_dir / name
            copy_path.write_text(text, encoding="utf-8")
            completed = run_cli([sys.executable, str(copy_path)],
                                env={"PYTHONPATH": str(CALC_SRC)},
                                log=f"pointer-review-{name}")
            self.assertEqual(completed.returncode, 0,
                             f"{name}: {completed.stdout[-4000:]}{completed.stderr[-2000:]}")
            generated[name] = completed.stdout
        boundary = read_json(out_dir / "pointer-boundary-summary.json")
        self.assertEqual(boundary["sequential"]["first"]["status"], "committed", boundary)
        self.assertEqual(boundary["sequential"]["second"]["status"], "committed", boundary)
        self.assertEqual(boundary["sequential"]["final_pointer"], {"dataset_id": "next-b"})
        race_statuses = sorted(result["status"] for result in boundary["cas_race"]["results"].values())
        self.assertEqual(race_statuses, ["committed", "failed"], boundary["cas_race"])
        matrix = read_json(out_dir / "pointer-matrix-summary.json")
        rows = []
        def walk(node):
            if isinstance(node, dict):
                if "results" in node and isinstance(node["results"], dict):
                    rows.append(node)
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(matrix)
        self.assertGreaterEqual(len(rows), 4, f"matrix 行不足 4：{rows}")
        for row in rows:
            statuses = sorted(result["status"] for result in row["results"].values())
            self.assertEqual(statuses, ["committed", "failed"], row)
        RECORDS.append({"test": "pointer-r1r2::review-scripts", "status": "pass",
                        "details": {"scripts": scripts,
                                    "sequential": [boundary["sequential"]["first"]["status"],
                                                   boundary["sequential"]["second"]["status"]],
                                    "race": race_statuses, "matrix_rows": len(rows)}})
        POINTER_EVIDENCE.mkdir(parents=True, exist_ok=True)
        (POINTER_EVIDENCE / "review-scripts.json").write_text(
            json.dumps({"sequential": boundary["sequential"],
                        "race": boundary["cas_race"],
                        "matrix_rows": len(rows)}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")


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


def tearDownModule() -> None:  # noqa: N802
    passed = sum(1 for record in RECORDS if record["status"] == "pass")
    failed = sum(1 for record in RECORDS if record["status"] == "fail")
    summary = {
        "schema": "p5-acceptance-v1",
        "session": STAMP,
        "started_at": MODULE_STATE.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "work_root": "reports/p5-acceptance/work/p5-" + STAMP,
        "summary": {"pass": passed, "fail": failed, "total": len(RECORDS)},
        "tests": RECORDS,
    }
    (P5_ROOT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
