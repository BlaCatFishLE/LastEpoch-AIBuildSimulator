# -*- coding: utf-8 -*-
"""P4-R1–R4 独立回归（第二轮审查反例）：重绑定/记录恢复/StateStore/失效边界。

真实 CLI/子进程/文件系统与库 API；不修改实现、工作树 UI、正式案例/规则/expected。
证据：``reports/p4-acceptance/{logs,work}/review3-*``。
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

from _dataset_asset import require_dataset_db

ROOT = pathlib.Path(__file__).resolve().parents[3]
P4_ROOT = ROOT / "reports" / "p4-acceptance"
CALC_SRC = ROOT / "tools" / "phase3" / "src"
if str(CALC_SRC) not in sys.path:
    sys.path.insert(0, str(CALC_SRC))

STAMP = time.strftime("%Y%m%dT%H%M%S")
SESSION = P4_ROOT / "work" / ("review3-" + STAMP)
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


def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def rel(path: pathlib.Path) -> str:
    return path.resolve().relative_to(ROOT).as_posix()


def work_dir(test: str) -> pathlib.Path:
    directory = SESSION / test
    if directory.exists():
        shutil.rmtree(directory, ignore_errors=True)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def run_args_record(args: list[str]) -> dict:
    return {"argv": args}


def run_cli(
    args: list[str],
    *,
    cwd: pathlib.Path = ROOT,
    env: dict | None = None,
    timeout: float = 900,
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
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / log).write_text(
            f"cwd: {cwd}\nexit: {completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )
    return completed


def parse_stage_statuses(output: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in output.splitlines():
        match = re.match(r"\s+(simulate|validate_result|render_report|commit)\s+\S+\s+cache=(\w+)", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


def cli_run_args(case: str, out: pathlib.Path, run_id: str, ops: pathlib.Path, extra: list[str] | None = None) -> list[str]:
    args = [
        sys.executable, "scripts/le_commands.py", "run", "--case", case,
        "--out", rel(out), "--run-id", run_id, "--publish", "staged",
        "--operations-root", rel(ops),
    ]
    args.extend(extra or [])
    return args


def operation_records(ops: pathlib.Path) -> list[pathlib.Path]:
    if not ops.is_dir():
        return []
    return sorted(path for path in ops.glob("*.json") if path.is_file())


def intent_files(ops: pathlib.Path) -> list[pathlib.Path]:
    directory = ops / ".intents"
    return sorted(path for path in directory.glob("*.json")) if directory.is_dir() else []


def subtree_sha(path: pathlib.Path) -> str:
    rows = []
    for file in sorted(path.rglob("*")):
        if file.is_file():
            rows.append([file.relative_to(path).as_posix(), sha256_file(file)])
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


def result_without_run(payload: dict) -> dict:
    copy = json.loads(json.dumps(payload, ensure_ascii=False))
    copy.pop("run", None)
    return copy


# ---------------------------------------------------------------------------
# R1: run-id collision / schema rebinding
# ---------------------------------------------------------------------------
class R1RebindAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = work_dir("r1-" + self._testMethodName)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "R1::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review3-r1-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def run_id_case(self, run_id: str, tag: str, *, cold: bool = False) -> tuple[dict, dict[str, str], dict]:
        final = self.out / tag
        completed = run_cli(
            cli_run_args(FR_CASE, final, run_id, self.ops, ["--no-report-cache"]),
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
            log=f"review3-r1-{self._testMethodName}-{tag}.log",
        )
        self.assertEqual(completed.returncode, 0,
                         f"{run_id}/{tag} 失败：{completed.stdout[-4000:]}{completed.stderr[-2000:]}")
        output = completed.stdout + completed.stderr
        statuses = parse_stage_statuses(output)
        result = read_json(final / "result.json")
        validation = read_json(final / "validation-result.json")
        state = read_json(final / "pipeline-state.json")
        record = operation_records(self.ops)
        return result, statuses, {"final": final, "output": output, "validation": validation,
                                  "state": state, "records": record, "cold": cold}

    def test_run_id_collisions_cold_warm_and_business_fields(self) -> None:
        base, base_statuses, base_meta = self.run_id_case("base", "cold-base", cold=True)
        self.assertEqual(base_statuses.get("simulate"), "miss")
        results = {"base": (base, base_statuses, base_meta)}
        warm_ids = [("copy", "warm-copy"), ("baseline", "warm-baseline"),
                    ("12345", "warm-numeric"),
                    ("p4-r1-normal-long-run-id-20261108", "warm-long")]
        for run_id, tag in warm_ids:
            result, statuses, meta = self.run_id_case(run_id, tag)
            self.assertEqual(statuses.get("simulate"), "hit", f"{run_id}: {statuses}")
            self.assertEqual(statuses.get("validate_result"), "hit", f"{run_id}: {statuses}")
            results[tag] = (result, statuses, meta)
        # 业务字段必须保留，且不得出现全文件替换痕迹
        base_serialized = json.dumps(base, ensure_ascii=False)
        base_build_base_count = base_serialized.count("build_base")
        for tag, (result, statuses, meta) in results.items():
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertIn("baselines", result, f"{tag}: 缺少 baselines")
            self.assertIn("build_base", result["baselines"].get("profiles") or {}, f"{tag}")
            self.assertIn("legacy_single_skill", result["baselines"].get("profiles") or {}, f"{tag}")
            candidates = ((result.get("buffs") or {}).get("screening") or {}).get("candidates") or []
            self.assertTrue(candidates, f"{tag}: 缺少 buffs.screening")
            self.assertTrue(all("baseline_condition_status" in c and "included_in_baseline" in c for c in candidates),
                            f"{tag}: baseline_condition_status/included_in_baseline 丢失")
            self.assertNotIn("copylines", serialized, f"{tag}: 出现 copylines 污染")
            self.assertEqual(serialized.count("build_base"), base_build_base_count,
                             f"{tag}: build_base 被改写/丢失")
            run_id = result["run"]["run_id"]
            expected_id = ("base" if tag == "base" else
                           ("p4-r1-normal-long-run-id-20261108" if tag == "warm-long" else
                            {"warm-copy": "copy", "warm-baseline": "baseline",
                             "warm-numeric": "12345"}[tag]))
            self.assertEqual(run_id, expected_id, tag)
            self.assertEqual(str(result["run"]["out_dir"]).replace("\\", "/"), rel(meta["final"]), tag)
            validation = meta["validation"]
            self.assertTrue(validation.get("passed"), tag)
            self.assertTrue(validation.get("accepted"), tag)
            self.assertEqual(str(validation.get("result_ref") or "").replace("\\", "/"),
                             rel(meta["final"] / "result.json"), tag)
            self.assertEqual(result_without_run(result), result_without_run(base),
                             f"{tag}: 冷热语义结果不同")
            records = meta["records"]
            current_record = [p for p in records if p.stem.endswith(expected_id)]
            self.assertTrue(current_record, f"{tag}: 缺少操作记录 {expected_id}: {[p.name for p in records]}")
            self.assertEqual(meta["state"]["last_commit"]["status"], "committed", tag)
            self.assertEqual(meta["state"]["last_commit"]["operation_id"], current_record[0].stem, tag)
        self.record(cold_statuses=base_statuses, warm_statuses={tag: r[1] for tag, r in results.items()},
                    business_fields_preserved=True,tests=len(results))

# ---------------------------------------------------------------------------
# R2: CLI record-write failure recovery
# ---------------------------------------------------------------------------
class R2RecordRecoveryAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = work_dir("r2-" + self._testMethodName)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)
        self.wrapper = self.root / "cli_record_inject.py"
        self.wrapper.write_text(
            "import sys, os\n"
            f"ROOT = {str(ROOT)!r}\n"
            "sys.path.insert(0, ROOT + '/scripts')\n"
            "sys.path.insert(0, ROOT + '/tools/phase3/src')\n"
            "import le_commands\n"
            "from le_calc.workflow import commit\n"
            "from unittest import mock\n"
            "inject = os.environ.get('R2_INJECT_RECORD_FAILURE') == '1'\n"
            "if inject:\n"
            "    with mock.patch.object(commit, '_write_operation_record', side_effect=OSError('injected operation record write failure')):\n"
            "        code = le_commands.main(sys.argv[1:])\n"
            "else:\n"
            "    code = le_commands.main(sys.argv[1:])\n"
            "sys.exit(int(code or 0))\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "R2::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review3-r2-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def cli_args(self, final: pathlib.Path, run_id: str, case: str = FR_CASE) -> list[str]:
        return [
            "run", "--case", case, "--out", rel(final), "--run-id", run_id,
            "--publish", "staged", "--operations-root", rel(self.ops), "--no-report-cache",
        ]

    def test_record_failure_recovery_foreign_conflict_and_intent_tamper(self) -> None:
        final = self.out / "recovered"
        run_id = "r2-window"
        args = self.cli_args(final, run_id)
        first = run_cli(
            [sys.executable, str(self.wrapper), *args],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache), "R2_INJECT_RECORD_FAILURE": "1"},
            log="review3-r2-first.log",
        )
        self.assertEqual(first.returncode, 1, first.stdout[-3000:] + first.stderr[-2000:])
        first_output = first.stdout + first.stderr
        self.assertIn("记录", first_output)
        self.assertTrue(final.is_dir(), "记录失败前正式目录应已发布")
        no_record = [p for p in operation_records(self.ops)]
        self.assertEqual(no_record, [], f"记录写入失败时不应有 record：{no_record}")
        intents = intent_files(self.ops)
        self.assertEqual(len(intents), 1, f"应保留 publish intent：{[p.name for p in intents]}")
        final_before = subtree_sha(final)
        state_before = sha256_file(final / "pipeline-state.json")
        # 正常 CLI 同命令重试：应 replayed、补记录、目录不变
        retry = run_cli(
            [sys.executable, "scripts/le_commands.py", *args],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
            log="review3-r2-retry.log",
        )
        self.assertEqual(retry.returncode, 0, retry.stdout[-4000:] + retry.stderr[-2000:])
        retry_output = retry.stdout + retry.stderr
        self.assertIn("staged 发布：replayed", retry_output)
        stages = parse_stage_statuses(retry_output)
        for stage in ("simulate", "validate_result", "render_report"):
            self.assertEqual(stages.get(stage), "hit", f"重试应命中阶段缓存：{stages}")
        records = operation_records(self.ops)
        self.assertEqual(len(records), 1, f"重试应补齐一条记录：{[p.name for p in records]}")
        self.assertEqual(intent_files(self.ops), [], "重试成功后 intent 应清理")
        self.assertEqual(subtree_sha(final), final_before, "恢复不得改变已发布正式目录")
        self.assertEqual(sha256_file(final / "pipeline-state.json"), state_before)
        # 不同输入 → conflict，目录不变
        case_dir = self.root / "cases"
        case_dir.mkdir(parents=True, exist_ok=True)
        conflict_case = case_dir / "fr-conflict.json"
        payload = read_json(ROOT / FR_CASE)
        payload["notes"] = ["r2 conflict probe"]
        conflict_case.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        conflict = run_cli(
            [sys.executable, "scripts/le_commands.py", *self.cli_args(final, run_id, rel(conflict_case))],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
            log="review3-r2-conflict.log",
        )
        self.assertEqual(conflict.returncode, 1, conflict.stdout[-3000:])
        self.assertIn("conflict", (conflict.stdout + conflict.stderr).lower())
        self.assertIn("input_fingerprint_conflict", conflict.stdout + conflict.stderr)
        self.assertEqual(subtree_sha(final), final_before)
        # 无 intent 的 foreign target → target_exists
        foreign = self.out / "foreign"
        shutil.copytree(final, foreign)
        foreign_before = subtree_sha(foreign)
        foreign_run = run_cli(
            [sys.executable, "scripts/le_commands.py",
             *self.cli_args(foreign, "r2-foreign")],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
            log="review3-r2-foreign.log",
        )
        self.assertEqual(foreign_run.returncode, 1, foreign_run.stdout[-3000:])
        self.assertIn("target_exists", foreign_run.stdout + foreign_run.stderr)
        self.assertEqual(subtree_sha(foreign), foreign_before)
        self.assertFalse([p for p in operation_records(self.ops) if "r2-foreign" in p.stem])
        # intent 篡改 → failed，不发布/不记录
        tamper_final = self.out / "tamper"
        tamper_args = self.cli_args(tamper_final, "r2-tamper")
        tamper_first = run_cli(
            [sys.executable, str(self.wrapper), *tamper_args],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache), "R2_INJECT_RECORD_FAILURE": "1"},
            log="review3-r2-tamper-first.log",
        )
        self.assertEqual(tamper_first.returncode, 1)
        tamper_intents = intent_files(self.ops)
        self.assertEqual(len(tamper_intents), 1)
        tamper_intents[0].write_text("{not-valid-intent", encoding="utf-8")
        tamper_retry = run_cli(
            [sys.executable, "scripts/le_commands.py", *tamper_args],
            env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
            log="review3-r2-tamper-retry.log",
        )
        self.assertEqual(tamper_retry.returncode, 1, tamper_retry.stdout[-3000:])
        tamper_output = tamper_retry.stdout + tamper_retry.stderr
        self.assertTrue("intent_corrupt" in tamper_output or "intent_target_mismatch" in tamper_output,
                        tamper_output[-2000:])
        self.assertEqual([p for p in operation_records(self.ops) if "r2-tamper" in p.stem], [])
        self.record(first_exit=first.returncode, retry_statuses=stages,
                    records=len(records), intent_cleared=True,
                    foreign_target="target_exists", conflict="conflict", tamper_failed=True)


# ---------------------------------------------------------------------------
# R3: FileStateStore verification of successful runs
# ---------------------------------------------------------------------------
class R3StateStoreAcceptanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = work_dir("r3-" + self._testMethodName)
        self.cache = self.root / "cache"
        self.ops = self.root / "ops"
        self.out = self.root / "out"
        for directory in (self.cache, self.ops, self.out):
            directory.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "R3::" + self._testMethodName, "status": status,
                        "details": getattr(self, "_details", {})})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review3-r3-{self._testMethodName}.json").write_text(
            json.dumps(getattr(self, "_details", {}), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def record(self, **details) -> None:
        self._details = details

    def run_case(self, case: str, out: pathlib.Path, run_id: str, *, publish: str = "staged",
                 extra: list[str] | None = None) -> dict[str, str]:
        args = [
            sys.executable, "scripts/le_commands.py", "run", "--case", case,
            "--out", rel(out), "--run-id", run_id, "--publish", publish,
            "--operations-root", rel(self.ops),
        ]
        args.extend(extra or [])
        completed = run_cli(args, env={"LE_PIPELINE_CACHE_ROOT": str(self.cache)},
                            log=f"review3-r3-{self._testMethodName}-{out.name}.log")
        self.assertEqual(completed.returncode, 0,
                         f"{run_id}: {completed.stdout[-4000:]}{completed.stderr[-2000:]}")
        return parse_stage_statuses(completed.stdout + completed.stderr)

    def verify_run(self, run_dir: pathlib.Path) -> dict:
        from le_calc.workflow import statestore, contract, stage

        store = statestore.FileStateStore(run_dir, root=ROOT)
        state = store.load()
        self.assertIsNotNone(state, f"缺少 pipeline-state：{run_dir}")
        report = store.verify(state)
        self.assertEqual(report["invalidated"], [], f"verify invalidated：{report}")
        self.assertEqual(report["downstream_invalidated"], [], f"verify downstream：{report}")
        expected_ids = ["simulate", "validate_result", "render_report", "commit"]
        self.assertEqual(report["valid"], expected_ids, report)
        seen_manifest = []
        for stage_state in state.stages:
            for ref in stage_state.output_refs or []:
                path_value = str(ref.get("path") or "")
                expected = str(ref.get("sha256") or "")
                self.assertTrue(path_value and expected, f"{stage_state.stage_id} ref 不完整")
                self.assertFalse(path_value.startswith("E:") or path_value.startswith("/"),
                                 f"绝对路径泄漏：{path_value}")
                absolute = pathlib.Path(path_value)
                if not absolute.is_absolute():
                    absolute = ROOT / absolute
                self.assertTrue(absolute.is_file(), f"ref 文件不存在：{path_value}")
                self.assertEqual(sha256_file(absolute), expected, f"ref sha 不一致：{path_value}")
                if absolute.name in {"run-manifest.json", "pipeline-state.json"}:
                    seen_manifest.append((stage_state.stage_id, path_value))
        self.assertEqual(seen_manifest, [], f"state↔manifest 循环引用：{seen_manifest}")
        return {"valid": report["valid"], "refs": sum(len(s.output_refs or []) for s in state.stages)}

    def test_bh_fr_cold_warm_staged_verify(self) -> None:
        outcomes = {}
        for tag, case in (("bh", BH_CASE), ("fr", FR_CASE)):
            cold_dir = self.out / f"{tag}-staged-cold"
            cold_statuses = self.run_case(case, cold_dir, f"r3-{tag}-cold", extra=["--no-report-cache"])
            cold_verify = self.verify_run(cold_dir)
            warm_dir = self.out / f"{tag}-staged-warm"
            warm_statuses = self.run_case(case, warm_dir, f"r3-{tag}-warm", extra=["--no-report-cache"])
            warm_verify = self.verify_run(warm_dir)
            default_dir = self.out / f"{tag}-default"
            default_statuses = self.run_case(case, default_dir, f"r3-{tag}-default",
                                             publish="none", extra=["--no-report-cache"])
            default_verify = self.verify_run(default_dir)
            outcomes[tag] = {"cold": cold_statuses, "warm": warm_statuses, "default": default_statuses,
                             "cold_verify": cold_verify, "warm_verify": warm_verify,
                             "default_verify": default_verify}
        self.record(runs=outcomes)


# ---------------------------------------------------------------------------
# R4: UI / engine / validation / renderer invalidation split (isolated tree)
# ---------------------------------------------------------------------------
class R4InvalidationAcceptanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.root = work_dir("r4-invalidation")
        cls.tree = cls.root / "tree"
        cls.tree.mkdir(parents=True, exist_ok=True)
        for relative in ("scripts", "config", "tools/phase3", "tools/build_import",
                         "tools/save_import", "examples", "data/builds", "data/raw/saves",
                         "data/affix-db"):
            source = ROOT / relative
            target = cls.tree / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # 小块数据复制，大 dataset 用同卷硬链接（只读，不修改原文件）
        for relative in ("data/current.json",):
            (cls.tree / "data").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, cls.tree / relative)
        dataset = cls.tree / "data/releases/phase1-dataset-003"
        dataset.mkdir(parents=True, exist_ok=True)
        for name in ("data.sqlite", "manifest.json"):
            source = ROOT / "data/releases/phase1-dataset-003" / name
            target = dataset / name
            try:
                os.link(source, target)
            except OSError:
                shutil.copyfile(source, target)
        cls.cache = cls.tree / "cache"
        cls.ops = cls.tree / "ops"
        cls.out = cls.tree / "reports" / "p4-r4" / "out"
        for directory in (cls.cache, cls.ops, cls.out):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls) -> None:
        # 隔离树是测试证据副本；工作树源码未被改动（测试后断言原文件 sha 不变）
        pass

    def setUp(self) -> None:
        self.name = self._testMethodName
        self.runs: dict[str, dict] = {}

    def tearDown(self) -> None:
        outcome = getattr(self, "_outcome", None)
        result = getattr(outcome, "result", None) if outcome else None
        status = "fail" if result and any(test is self for test, _ in result.failures + result.errors) else "pass"
        RECORDS.append({"test": "R4::" + self.name, "status": status, "details": self.runs})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        (LOG_DIR / f"review3-r4-{self.name}.json").write_text(
            json.dumps(self.runs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run_tree(self, tag: str, run_id: str, extra: list[str] | None = None) -> tuple[dict, dict, dict]:
        out = self.out / tag
        args = [
            sys.executable, str(self.tree / "scripts" / "le_commands.py"),
            "run", "--case", "examples/flame-reave/case.legacy-hit.json", "--out", rel_tree := str(out.relative_to(self.tree)).replace("\\", "/"),
            "--run-id", run_id, "--publish", "staged", "--operations-root", str(self.ops.relative_to(self.tree)).replace("\\", "/"),
            "--no-report-cache",
        ]
        args.extend(extra or [])
        env = dict(os.environ)
        env["LE_PIPELINE_CACHE_ROOT"] = str(self.cache)
        env["PYTHONPATH"] = str(self.tree / "tools/phase3/src") + os.pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            args, cwd=str(self.tree), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log = LOG_DIR / f"review3-r4-{tag}.log"
        log.write_text(
            f"exit: {completed.returncode}\n--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}\n",
            encoding="utf-8",
        )
        self.assertEqual(completed.returncode, 0,
                         f"{tag} isolated tree CLI 失败：{completed.stdout[-4000:]}{completed.stderr[-2000:]}")
        statuses = parse_stage_statuses(completed.stdout + completed.stderr)
        result = read_json(out / "result.json")
        self.runs[tag] = {"run_id": run_id, "statuses": statuses,
                          "result": result_without_run(result)}
        return result, statuses, {"out": out, "log": log}

    @staticmethod
    def fingerprint(stage_id: str, root: pathlib.Path) -> str:
        from le_calc.workflow import stage, stage_cache
        handler = stage.STAGES[stage_id]["handler"]
        return stage_cache.implementation_fingerprint(stage_id, root, handler=handler)["fingerprint"]

    def append(self, relative: str, marker: str) -> None:
        path = self.tree / relative
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n# " + marker + "\n")

    def test_ui_change_only_invalidates_render_real_run(self) -> None:
        original_ui_sha = sha256_file(ROOT / "tools/phase3/src/le_calc/report_ui/assets/app.js")
        base, base_statuses, base_meta = self.run_tree("base", "r4-base")
        before = {sid: self.fingerprint(sid, self.tree)
                  for sid in ("simulate", "validate_result", "render_report")}
        self.append("tools/phase3/src/le_calc/report_ui/assets/app.js", "r4 isolated UI change")
        after_ui = {sid: self.fingerprint(sid, self.tree)
                    for sid in ("simulate", "validate_result", "render_report")}
        self.assertEqual(before["simulate"], after_ui["simulate"], "UI 变化不应改变 simulate 指纹")
        self.assertEqual(before["validate_result"], after_ui["validate_result"], "UI 变化不应改变 validate 指纹")
        self.assertNotEqual(before["render_report"], after_ui["render_report"], "UI 变化应改变 render 指纹")
        ui_result, ui_statuses, _ = self.run_tree("ui-change", "r4-ui")
        self.assertEqual(ui_statuses.get("simulate"), "hit", ui_statuses)
        self.assertEqual(ui_statuses.get("validate_result"), "hit", ui_statuses)
        self.assertEqual(ui_statuses.get("render_report"), "miss", ui_statuses)
        self.assertEqual(result_without_run(ui_result), result_without_run(base), "UI 变化不应改变数值/结果")
        # engine 变化 → simulate 失效（真实 run）
        before_engine = {sid: self.fingerprint(sid, self.tree)
                         for sid in ("simulate", "validate_result", "render_report")}
        self.append("tools/phase3/src/le_calc/damage.py", "r4 isolated engine change")
        after_engine = {sid: self.fingerprint(sid, self.tree)
                        for sid in ("simulate", "validate_result", "render_report")}
        self.assertNotEqual(before_engine["simulate"], after_engine["simulate"], "引擎变化应改变 simulate 指纹")
        _, engine_statuses, _ = self.run_tree("engine-change", "r4-engine")
        self.assertEqual(engine_statuses.get("simulate"), "miss", engine_statuses)
        # validation 实现变化 → validate 失效（真实 run）
        before_validate = {sid: self.fingerprint(sid, self.tree)
                           for sid in ("simulate", "validate_result", "render_report")}
        self.append("tools/phase3/src/le_calc/workflow/expected_check.py", "r4 isolated validation change")
        after_validate = {sid: self.fingerprint(sid, self.tree)
                          for sid in ("simulate", "validate_result", "render_report")}
        self.assertNotEqual(before_validate["validate_result"], after_validate["validate_result"])
        _, validate_statuses, _ = self.run_tree("validation-change", "r4-validation")
        self.assertEqual(validate_statuses.get("simulate"), "hit", validate_statuses)
        self.assertEqual(validate_statuses.get("validate_result"), "miss", validate_statuses)
        # renderer 实现变化 → render 失效（真实 run）
        before_render = {sid: self.fingerprint(sid, self.tree)
                         for sid in ("simulate", "validate_result", "render_report")}
        self.append("tools/phase3/src/le_calc/html.py", "r4 isolated renderer change")
        after_render = {sid: self.fingerprint(sid, self.tree)
                        for sid in ("simulate", "validate_result", "render_report")}
        self.assertNotEqual(before_render["render_report"], after_render["render_report"])
        _, render_statuses, _ = self.run_tree("renderer-change", "r4-render")
        self.assertEqual(render_statuses.get("simulate"), "hit", render_statuses)
        self.assertEqual(render_statuses.get("validate_result"), "hit", render_statuses)
        self.assertEqual(render_statuses.get("render_report"), "miss", render_statuses)
        # 工作树真实 UI 文件必须未被改动
        self.assertEqual(sha256_file(ROOT / "tools/phase3/src/le_calc/report_ui/assets/app.js"),
                         original_ui_sha, "真实工作树 UI 文件被测试修改")
        self.assertEqual(base_statuses.get("simulate"), "miss")
        self.runs["fingerprints"] = {"before": before, "ui": after_ui,
                                     "engine": after_engine, "validation": after_validate,
                                     "render": after_render}
        self.runs["assertions"] = {"ui_only_render": True, "engine_simulate": True,
                                   "validation_impl": True, "renderer": True,
                                   "worktree_ui_unchanged": True}


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
        "schema": "p4-review3-acceptance-v1",
        "session": STAMP,
        "started_at": MODULE_STATE.get("started_at"),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "work_root": "reports/p4-acceptance/work/review3-" + STAMP,
        "summary": {"pass": passed, "fail": failed, "total": len(RECORDS)},
        "tests": RECORDS,
    }
    (P4_ROOT / "review3-acceptance.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
