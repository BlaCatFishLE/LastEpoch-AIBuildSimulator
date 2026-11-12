"""第三步定向验证 C1–C16（只覆盖正式流水线与入口统一接入）。

运行（只本文件）：

    cd tools/phase3/tests
    python -m unittest test_unified_workflow -v

C10 故障注入恢复、C11 阶段缓存：本文件未覆盖，实施记录中明确列为未执行/未实现边界。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools/phase3/src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route  # noqa: E402
from le_calc import plan_compiler  # noqa: E402
from le_calc import plan_executor  # noqa: E402
from le_calc.workflow import contract, stage  # noqa: E402
from le_calc.workflow import unified  # noqa: E402

CASE = ROOT / "examples/void-cleave-20261110/case.profile-v2.json"
CASE_REL = CASE.relative_to(ROOT).as_posix()
NATIVE_CASE = ROOT / "tools/phase3/tests/fixtures/unified-workflow/native-case.json"
NATIVE_CASE_REL = NATIVE_CASE.relative_to(ROOT).as_posix()
STEP2_RUN = ROOT / "reports/void-cleave-20261110/dps-step2-plan-3"
STEP3_RUN = ROOT / "reports/void-cleave-20261110/dps-step3-workflow-6"
SCRATCH = ROOT / ".scratch/unified-dps-step3/tests"
RUN_TAG = str(int(time.time()))



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _request(stage_id: str, case_rel: str, run_id: str, **params) -> contract.StageRequest:
    return contract.StageRequest(
        stage_id=stage_id,
        case_id=run_id,
        run_id=run_id,
        input_refs=[],
        parameters={"case_ref": case_rel, **params},
    )


def _context(run_dir: Path) -> contract.StageContext:
    return contract.StageContext(repo_root=ROOT, output_root=run_dir, adapters={})


def _run_chain(
    run_dir: Path,
    case_rel: str,
    *,
    scenario: dict | None = None,
    endpoint: str = "http://127.0.0.1:8765/recompute",
) -> dict:
    context = _context(run_dir)
    ctx = {}
    for stage_id in ("compile_plan", "min_validate"):
        result = stage.execute_stage(_request(stage_id, case_rel, run_dir.name), context)
        ctx[stage_id] = result
        if result.execution_status in ("failed", "waiting"):
            raise AssertionError(f"{stage_id} failed: {result.issues}")
    plan_ref = unified.load_plan(run_dir)
    simulate_params = {"render_html": False, "plan_ref": planmod.plan_ref(plan_ref)}
    if scenario is not None:
        simulate_params["scenario"] = scenario
    for stage_id, params in (
        ("simulate", simulate_params),
        ("validate_result", {}),
        (
            "render_report",
            {
                "recalc_endpoint": endpoint,
                "target_dir": run_dir.relative_to(ROOT).as_posix(),
            },
        ),
    ):
        result = stage.execute_stage(_request(stage_id, case_rel, run_dir.name, **params), context)
        ctx[stage_id] = result
        if result.execution_status != "completed":
            raise AssertionError(f"{stage_id} failed: {result.issues}")
    ctx["plan"] = plan_ref
    ctx["result"] = _load(run_dir / "result.json")
    ctx["validation"] = _load(run_dir / "validation-result.json")
    return ctx


def _cli(*args: str, expect: int | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts/le_commands.py"), *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if expect is not None and proc.returncode != expect:
        raise AssertionError(
            f"CLI {args} exit={proc.returncode} expected {expect}\n"
            f"stdout={proc.stdout[-2000:]}\nstderr={proc.stderr[-2000:]}"
        )
    return proc


class C1RouteTest(unittest.TestCase):
    def test_profile_compat_native_research_conflict_and_unknown(self):
        case = _load(CASE)
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        self.assertEqual(route["engine"], "unified_dps")
        self.assertEqual(route["source"]["source_kind"], "profile_v2")
        self.assertEqual(route["origin"], "legacy_dps_profile_compat")
        native = _load(NATIVE_CASE)
        native_route = calculation_route.resolve_calculation_route(native, root=ROOT)
        self.assertEqual(native_route["source"]["source_kind"], "definitions_v2")
        self.assertEqual(native_route["status"], "ready")
        # 双来源不一致
        conflict = copy.deepcopy(case)
        conflict["calculation"] = {
            "engine": "unified_dps",
            "source": {"kind": "profile_v2", "path": "examples/another/profile.json"},
        }
        resolved = calculation_route.resolve_calculation_route(conflict, root=ROOT)
        self.assertIn(resolved["status"], ("route_conflict", "unsupported_route"))
        self.assertEqual(resolved["engine"], "unsupported")
        # 未知 engine
        unknown = copy.deepcopy(case)
        unknown["calculation"] = {"engine": "magic_engine"}
        resolved = calculation_route.resolve_calculation_route(unknown, root=ROOT)
        self.assertEqual(resolved["engine"], "unsupported")
        # research 无来源且无 v2 定义
        research = {
            "schema": "le-case-v1",
            "case_id": "research-no-source",
            "model": "research",
            "main_skill": {"ability_id": "not-a-template"},
        }
        resolved = calculation_route.resolve_calculation_route(research, root=ROOT)
        self.assertEqual(resolved["engine"], "needs_research")
        # 显式 legacy 保留，不转统一
        legacy = {"schema": "le-case-v1", "case_id": "legacy", "model": "black-hole"}
        resolved = calculation_route.resolve_calculation_route(legacy, root=ROOT)
        self.assertEqual(resolved["engine"], "legacy")


class C2PrepareTest(unittest.TestCase):
    def test_compile_plan_and_min_validate_do_not_require_old_gate_files(self):
        run_dir = SCRATCH / "prepare-stage"
        shutil.rmtree(run_dir, ignore_errors=True)
        context = _context(run_dir)
        result = stage.execute_stage(_request("compile_plan", CASE_REL, "prepare-stage"), context)
        self.assertEqual(result.execution_status, "completed")
        self.assertEqual(result.readiness, "partial")
        validate = stage.execute_stage(_request("min_validate", CASE_REL, "prepare-stage"), context)
        self.assertEqual(validate.execution_status, "completed")
        self.assertTrue((run_dir / "calculation-plan.json").is_file())
        self.assertTrue((run_dir / "compile-result.json").is_file())
        min_validate = _load(run_dir / "min-validate.json")
        self.assertEqual(min_validate["mode"], "unified")
        self.assertFalse(any(check["id"] == "case.expected" for check in min_validate["checks"]))
        self.assertFalse(any(check["id"] == "case.snapshot" for check in min_validate["checks"]))


class C3InputHandoffTest(unittest.TestCase):
    def test_plan_references_current_calculation_input(self):
        run_dir = SCRATCH / "input-handoff"
        shutil.rmtree(run_dir, ignore_errors=True)
        ctx = _run_chain(run_dir, CASE_REL)
        case = _load(CASE)
        refs = {ref["role"]: ref for ref in ctx["plan"]["input_refs"]}
        self.assertEqual(refs["calculation_input"]["sha256"], case["input_sha256"]["calculation_input"])
        self.assertEqual(refs["build_spec"]["sha256"], case["build"]["build_spec_sha256"])
        self.assertIn("dataset_content", refs)


class C4ChainTest(unittest.TestCase):
    def test_chain_runs_without_legacy_numeric_entry(self):
        run_dir = SCRATCH / "chain"
        shutil.rmtree(run_dir, ignore_errors=True)
        from le_calc import dps

        original_calculate, original_run = dps.calculate, dps.run
        dps.calculate = lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy calculate called"))
        dps.run = lambda *a, **k: (_ for _ in ()).throw(AssertionError("legacy run called"))
        try:
            ctx = _run_chain(run_dir, CASE_REL)
        finally:
            dps.calculate, dps.run = original_calculate, original_run
        self.assertEqual(ctx["result"]["sustained_dps"], 21491.54090674077)
        self.assertEqual(ctx["validation"]["passed"], True)
        self.assertTrue((run_dir / "dps-report.html").is_file())
        state = _load(run_dir / "pipeline-state.json") if (run_dir / "pipeline-state.json").is_file() else {}
        # 直接调用 stage 时没有 CLI 编排的 state 文件；阶段顺序由返回值/产物观测
        self.assertEqual(ctx["plan"]["schema"], "le-calculation-plan-v1")

    def test_direct_domain_simulate_requires_plan_ref(self):
        from le_calc.workflow import contract as workflow_contract
        from le_calc.workflow import domain

        out = SCRATCH / "domain-simulate"
        shutil.rmtree(out, ignore_errors=True)
        request = {
            "schema_version": "le-simulate-request-v1",
            "case_id": "void-cleave-20261110",
            "case_ref": CASE_REL,
            "run_id": "domain-simulate",
            "output_dir": out.relative_to(ROOT).as_posix(),
        }
        rejected = domain.simulate(request, root=ROOT)
        self.assertEqual(rejected["readiness"], contract.NEEDS_INPUT)
        self.assertTrue(any(issue["code"] == "plan_ref_required" for issue in rejected["issues"]))
        # 编译一份计划后，带 plan_ref 的直接调用可执行
        compiled = _run_chain(SCRATCH / "domain-plan", CASE_REL)["plan"]
        plan_path = SCRATCH / "domain-plan" / "calculation-plan.json"
        request["inputs"] = {
            "plan_ref": {
                "path": plan_path.relative_to(ROOT).as_posix(),
                "plan_sha256": planmod.sha256_file(plan_path),
            }
        }
        accepted = domain.simulate(request, root=ROOT)
        self.assertEqual(accepted["readiness"], contract.PARTIAL)
        self.assertTrue((out / "result.json").is_file())


class C5NativeOracleTest(unittest.TestCase):
    def test_native_definition_through_formal_stages(self):
        run_dir = SCRATCH / "native-oracle"
        shutil.rmtree(run_dir, ignore_errors=True)
        case = _load(NATIVE_CASE)
        controls = None
        ctx = _run_chain(run_dir, NATIVE_CASE_REL)
        plan = ctx["plan"]
        controls = plan["controls"]
        scenario = {
            "states": {c["key"]: c.get("default") for c in controls if c["namespace"] == "state"},
            "target": {c["key"]: c.get("default") for c in controls if c["namespace"] == "target"},
        }
        scenario["target"]["monster_level"] = 1
        # 重新执行带 1 级目标场景（模拟默认场景先被持久化，再以独立场景核对 oracle）
        run_dir2 = SCRATCH / "native-oracle-level1"
        shutil.rmtree(run_dir2, ignore_errors=True)
        ctx2 = _run_chain(run_dir2, NATIVE_CASE_REL, scenario=scenario)
        result = ctx2["result"]
        self.assertAlmostEqual(result["single_hit"], 65.0)
        bleed = next(item for item in result["ailments"] if item["name"] == "Bleed")
        self.assertAlmostEqual(bleed["stack_damage"], 120.0)
        self.assertEqual(ctx2["validation"]["passed"], True)
        refs = {ref["role"] for ref in ctx2["plan"]["input_refs"]}
        self.assertIn("template", refs)
        self.assertIn("mechanism_library", refs)
        self.assertEqual(
            ctx2["plan"]["executor_requirements"],
            calculation_route.compile_route(root=ROOT, case=_load(CASE))[1].plan[
                "executor_requirements"
            ],
        )


class C6StatusTest(unittest.TestCase):
    def test_complete_partial_and_missing_definition_exit_codes(self):
        partial_dir = SCRATCH / "cli-partial"
        shutil.rmtree(partial_dir, ignore_errors=True)
        proc = _cli("run", "--case", CASE_REL, "--out", partial_dir.relative_to(ROOT).as_posix(), expect=3)
        self.assertIn("compile_plan", proc.stdout)
        self.assertIn("覆盖=partial", proc.stdout)
        native_dir = SCRATCH / "cli-native"
        shutil.rmtree(native_dir, ignore_errors=True)
        proc = _cli("run", "--case", NATIVE_CASE_REL, "--out", native_dir.relative_to(ROOT).as_posix(), expect=0)
        self.assertIn("覆盖=complete", proc.stdout)
        missing = SCRATCH / "missing-case.json"
        _write(
            missing,
            {
                "schema": "le-case-v1",
                "case_id": "missing-source",
                "model": "research",
                "main_skill": {"ability_id": "no-such-ability"},
            },
        )
        proc = _cli("run", "--case", missing.relative_to(ROOT).as_posix(), "--out", (SCRATCH / "missing-run").relative_to(ROOT).as_posix(), expect=3)
        self.assertIn("needs_research", proc.stdout + proc.stderr)


class C7TamperTest(unittest.TestCase):
    def test_plan_scenario_result_and_ledger_tamper_rejected(self):
        base_dir = SCRATCH / "tamper-base"
        shutil.rmtree(base_dir, ignore_errors=True)
        _run_chain(base_dir, CASE_REL)
        plan = _load(base_dir / "calculation-plan.json")
        scenarios = {}
        # 计划篡改
        tampered = copy.deepcopy(plan)
        tampered["effects"][0]["value"] = 1.0
        tampered = planmod.finalize_plan(tampered)
        run = SCRATCH / "tamper-plan"
        shutil.rmtree(run, ignore_errors=True)
        shutil.copytree(base_dir, run)
        _write(run / "calculation-plan.json", tampered)
        ctx = _context(run)
        result = stage.execute_stage(_request("validate_result", CASE_REL, run.name), ctx)
        self.assertEqual(result.execution_status, "failed")
        # 场景篡改
        run = SCRATCH / "tamper-scenario"
        shutil.rmtree(run, ignore_errors=True)
        shutil.copytree(base_dir, run)
        scenario = _load(run / "scenario.json")
        scenario["target"]["monster_level"] = 99
        _write(run / "scenario.json", scenario)
        result = stage.execute_stage(_request("validate_result", CASE_REL, run.name), _context(run))
        self.assertEqual(result.execution_status, "failed")
        # 结果篡改
        run = SCRATCH / "tamper-result"
        shutil.rmtree(run, ignore_errors=True)
        shutil.copytree(base_dir, run)
        result_doc = _load(run / "result.json")
        result_doc["sustained_dps"] = 1.0
        _write(run / "result.json", result_doc)
        result = stage.execute_stage(_request("validate_result", CASE_REL, run.name), _context(run))
        self.assertEqual(result.execution_status, "failed")
        # 账本篡改
        run = SCRATCH / "tamper-ledger"
        shutil.rmtree(run, ignore_errors=True)
        shutil.copytree(base_dir, run)
        ledger = _load(run / "source-ledger.json")
        ledger[0]["value"] = 999.0
        _write(run / "source-ledger.json", ledger)
        result = stage.execute_stage(_request("validate_result", CASE_REL, run.name), _context(run))
        self.assertEqual(result.execution_status, "failed")


class C8StagedTest(unittest.TestCase):
    def test_staged_publish_keeps_plan_semantics_and_final_binding(self):
        out = SCRATCH / f"staged-final-{RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        proc = _cli(
            "run",
            "--case",
            CASE_REL,
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--publish",
            "staged",
            "--port",
            "8811",
            expect=3,
        )
        plan = _load(out / "calculation-plan.json")
        result = _load(out / "result.json")
        self.assertEqual(plan["plan_sha256"], result["plan_sha256"])
        compile_result = _load(out / "compile-result.json")
        self.assertEqual(compile_result["plan_sha256"], plan["plan_sha256"])
        binding = _load(out / "report-binding.json")
        self.assertEqual(binding["report_ref"]["run_dir"], out.relative_to(ROOT).as_posix())
        self.assertIn(out.name, binding["report_ref"]["report_path"])
        manifest = _load(out / "run-manifest.json")
        for name, digest in manifest["files"].items():
            self.assertEqual(planmod.sha256_file(out / name), digest)
        for path in out.rglob("*"):
            if path.is_file() and path.suffix in (".json", ".html", ".js", ".css"):
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertNotIn(".staging", text, f"staging 路径泄漏：{path}")
        self.assertIn("committed", proc.stdout)
        self.assertTrue((out / "pipeline-state.json").is_file())


class C9ReplayTest(unittest.TestCase):
    def test_same_staged_operation_replays_without_duplicate_publish(self):
        out = SCRATCH / f"staged-replay-{RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        args = (
            "run",
            "--case",
            CASE_REL,
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--run-id",
            f"replay-run-{RUN_TAG}",
            "--publish",
            "staged",
        )
        first = _cli(*args, expect=3)
        first_plan = _load(out / "calculation-plan.json")["plan_sha256"]
        second = _cli(*args, expect=3)
        self.assertEqual(_load(out / "calculation-plan.json")["plan_sha256"], first_plan)
        state = _load(out / "pipeline-state.json")
        self.assertEqual(state.get("last_commit", {}).get("status"), "committed")
        self.assertIn("replayed", second.stdout + second.stderr)


class C11CacheTest(unittest.TestCase):
    def test_same_dependencies_hit_compile_simulate_validate_cache(self):
        cache_root = SCRATCH / f"cache-{RUN_TAG}"
        out1 = SCRATCH / f"cache-out1-{RUN_TAG}"
        out2 = SCRATCH / f"cache-out2-{RUN_TAG}"
        shutil.rmtree(cache_root, ignore_errors=True)
        shutil.rmtree(out1, ignore_errors=True)
        shutil.rmtree(out2, ignore_errors=True)
        previous = os.environ.get("LE_PIPELINE_CACHE_ROOT")
        os.environ["LE_PIPELINE_CACHE_ROOT"] = cache_root.relative_to(ROOT).as_posix()
        try:
            run_id = f"cache-run-{RUN_TAG}"
            _cli("run", "--case", CASE_REL, "--out", out1.relative_to(ROOT).as_posix(), "--run-id", run_id, expect=3)
            _cli("run", "--case", CASE_REL, "--out", out2.relative_to(ROOT).as_posix(), "--run-id", run_id, expect=3)
        finally:
            if previous is None:
                os.environ.pop("LE_PIPELINE_CACHE_ROOT", None)
            else:
                os.environ["LE_PIPELINE_CACHE_ROOT"] = previous
        state = _load(out2 / "pipeline-state.json")
        entries = {entry["stage_id"]: entry for entry in state.get("stages") or []}
        for stage_id in ("compile_plan", "simulate", "validate_result"):
            self.assertEqual(entries[stage_id].get("cache_status"), "hit", stage_id)
        self.assertEqual(
            _load(out1 / "calculation-plan.json")["plan_sha256"],
            _load(out2 / "calculation-plan.json")["plan_sha256"],
        )
        self.assertEqual(
            _load(out1 / "result.json")["sustained_dps"],
            _load(out2 / "result.json")["sustained_dps"],
        )


class C12DualEntryTest(unittest.TestCase):
    def test_profile_and_native_share_executor_and_unsupported_is_rejected(self):
        profile_plan = _run_chain(SCRATCH / "dual-profile", CASE_REL)["plan"]
        native_plan = _run_chain(SCRATCH / "dual-native", NATIVE_CASE_REL)["plan"]
        self.assertEqual(
            profile_plan["executor_requirements"], native_plan["executor_requirements"]
        )
        template = _load(ROOT / "tools/phase3/tests/fixtures/calculation-plan/synthetic-melee-hit-with-bleed.json")
        template["mechanism_refs"] = ["item-sword-catcher-frenzy-added-melee"]
        context = plan_compiler.FrozenContext(
            root=ROOT,
            case={"case_id": "synthetic"},
            calc={},
            dataset={},
            input_refs=[],
            synthetic=True,
        )
        mechanisms = _load(ROOT / "tools/phase3/rules/shared-conditional-mechanisms-v1.json")
        rejected = plan_compiler.compile_from_definitions(context, template, mechanisms=mechanisms)
        self.assertFalse(rejected.ok)
        self.assertTrue(
            any(item["code"] == "mechanism_requires_instance_binding" for item in rejected.diagnostics)
        )


class C13DeliveryCompareTest(unittest.TestCase):
    def test_delivery_matches_step2_semantics(self):
        if not (STEP3_RUN / "result.json").is_file() or not (STEP2_RUN / "result.json").is_file():
            self.skipTest("缺少交付 run")
        new = _load(STEP3_RUN / "result.json")
        old = _load(STEP2_RUN / "result.json")
        fields = [k for k in old if k not in ("plan_ref", "plan_sha256", "scenario_sha256", "execution_fingerprint")]
        for key in fields:
            self.assertEqual(new.get(key), old.get(key), key)
        self.assertEqual(new["coverage"]["gap_count"], 7)


class C14HttpRecomputeTest(unittest.TestCase):
    def test_one_real_http_recompute(self):
        out = SCRATCH / "http-report"
        shutil.rmtree(out, ignore_errors=True)
        _cli(
            "run",
            "--case",
            CASE_REL,
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--port",
            "8823",
            expect=3,
        )
        binding = _load(out / "report-binding.json")
        plan = _load(out / "calculation-plan.json")
        controls = plan["controls"]
        scenario = {
            "states": {c["key"]: c.get("default") for c in controls if c["namespace"] == "state"},
            "target": {c["key"]: c.get("default") for c in controls if c["namespace"] == "target"},
        }
        code = (
            "import json\nfrom pathlib import Path\n"
            "import sys\n"
            f"sys.path.insert(0, {str(SRC)!r})\n"
            "from le_calc import dps_report\n"
            "dps_report.serve("
            f"{str(ROOT)!r}, json.load(open({str(CASE)!r}, encoding='utf-8')),"
            f"Path({str(out)!r}), 8823)"
        )
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.Popen(
            [sys.executable, "-c", code],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        try:
            for _ in range(120):
                try:
                    with socket.create_connection(("127.0.0.1", 8823), timeout=0.2):
                        break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail(f"serve 未启动：{proc.communicate(timeout=5)}")
            request = {
                "binding": binding,
                "scenario": scenario,
                "request_id": "c14-test",
                "client_session_id": "c14-session",
                "client_edit_revision": 1,
            }
            req = urllib.request.Request(
                "http://127.0.0.1:8823/recompute",
                data=json.dumps(request, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            proc.terminate()
            proc.communicate(timeout=10)
        result = payload["result"]
        self.assertEqual(result["plan_sha256"], plan["plan_sha256"])
        self.assertEqual(result["sustained_dps"], 21491.54090674077)
        self.assertEqual(payload["binding"], binding)
        self.assertGreater(len(payload["html"]), 10000)


class C15CliBoundaryTest(unittest.TestCase):
    def test_prepare_validate_and_reproduce_route(self):
        prepare_root = SCRATCH / "cli-prepare"
        shutil.rmtree(prepare_root, ignore_errors=True)
        proc = _cli(
            "prepare",
            "--case",
            CASE_REL,
            "--out",
            prepare_root.relative_to(ROOT).as_posix(),
            "--run-id",
            "cli-prep",
            expect=0,
        )
        self.assertIn("compile_plan", proc.stdout)
        run_dir = prepare_root / "cli-prep"
        self.assertTrue((run_dir / "calculation-plan.json").is_file())
        proc = _cli(
            "validate",
            "--case",
            CASE_REL,
            "--run-dir",
            run_dir.relative_to(ROOT).as_posix(),
            expect=1,
        )  # 无 simulate 产物，validate 明确失败而非假 PASS
        self.assertIn("FAIL", proc.stdout + proc.stderr)
        proc = _cli("reproduce", "--case", CASE_REL, expect=1)
        self.assertIn("unsupported_operation", proc.stdout + proc.stderr)
        self.assertNotIn("black-hole", proc.stdout.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
