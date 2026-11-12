"""第三步复验补修：P1-1/2/3/4/5/6 定向回归。"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "tools/phase3/src"
for entry in (str(ROOT), str(SRC), str(ROOT / "scripts")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import calculation_route  # noqa: E402
from le_calc import plan_compiler  # noqa: E402
from le_calc.workflow import contract as wf_contract  # noqa: E402
from le_calc.workflow import stage as wf_stage  # noqa: E402
from le_calc.workflow import stage_cache  # noqa: E402
from le_calc.workflow import unified  # noqa: E402
import test_unified_workflow as base  # noqa: E402

CLI_PATH = ROOT / "scripts/le_commands.py"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _cli_module():
    if "le_commands_regression_mod" in sys.modules:
        return sys.modules["le_commands_regression_mod"]
    spec = importlib.util.spec_from_file_location("le_commands_regression_mod", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["le_commands_regression_mod"] = module
    spec.loader.exec_module(module)
    return module


def _variant_profile(base_profile: Path, target: Path, *, note: str) -> None:
    payload = json.loads(base_profile.read_text(encoding="utf-8"))
    payload["note"] = note
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class P1ValidationGateTest(unittest.TestCase):
    def test_validate_is_read_only_over_published_run(self):
        run_dir = base.SCRATCH / f"readonly-{base.RUN_TAG}"
        shutil.rmtree(run_dir, ignore_errors=True)
        base._run_chain(run_dir, base.CASE_REL)
        before = __import__("hashlib").sha256((run_dir / "validation-result.json").read_bytes()).hexdigest()
        payload = unified.validate_stage(base._load(base.CASE), root=ROOT, run_dir=run_dir, write=False)
        self.assertTrue(payload["passed"])
        after = __import__("hashlib").sha256((run_dir / "validation-result.json").read_bytes()).hexdigest()
        self.assertEqual(before, after)
        proc = base._cli("validate", "--case", base.CASE_REL, "--run-dir", run_dir.relative_to(ROOT).as_posix(), expect=0)
        self.assertIn("PASS", proc.stdout)
        after_cli = __import__("hashlib").sha256((run_dir / "validation-result.json").read_bytes()).hexdigest()
        self.assertEqual(before, after_cli)

    def test_gate_rejects_coverage_applications_issues_wrong_revision_and_missing_expected(self):
        base_dir = base.SCRATCH / f"gate-base-{base.RUN_TAG}"
        shutil.rmtree(base_dir, ignore_errors=True)
        base._run_chain(base_dir, base.CASE_REL)
        case = base._load(base.CASE)

        def validate_variant(name, mutate):
            run = base.SCRATCH / f"gate-{name}-{base.RUN_TAG}"
            shutil.rmtree(run, ignore_errors=True)
            shutil.copytree(base_dir, run)
            result = json.loads((run / "result.json").read_text(encoding="utf-8"))
            mutate(result)
            (run / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            return unified.validate_stage(case, root=ROOT, run_dir=run, write=False)

        coverage = validate_variant(
            "coverage", lambda r: r["coverage"].update(status="complete", gap_count=0, gaps=[])
        )
        self.assertFalse(coverage["passed"])
        self.assertIn("result.coverage_projection", {c["id"] for c in coverage["payload"]["checks"] if c["status"] != "PASS"})

        def clear_ledger(r):
            r["applications"] = []
            r["contribution_application_summary"] = {}
            r["issues"] = []

        cleared = validate_variant("apps", clear_ledger)
        self.assertFalse(cleared["passed"])

        wrong_revision = dict(case)
        wrong_revision["build"] = dict(case["build"], revision_id="rev-wrong", build_id="wrong-build")
        wrong = unified.validate_stage(wrong_revision, root=ROOT, run_dir=base_dir, write=False)
        self.assertFalse(wrong["passed"])

        missing_expected = dict(case)
        missing_expected["expected"] = ".scratch/unified-dps-step3/no-such-expected.json"
        missing = unified.validate_stage(missing_expected, root=ROOT, run_dir=base_dir, write=False)
        self.assertFalse(missing["passed"])


class P1ProfileSourceTest(unittest.TestCase):
    def test_explicit_profile_source_works_without_legacy_field(self):
        case = base._load(base.CASE)
        case.pop("dps_profile", None)
        case["calculation"] = {
            "engine": "unified_dps",
            "source": {
                "kind": "profile_v2",
                "path": "examples/void-cleave-20261110/inputs/dps-profile-v2.json",
            },
        }
        route = calculation_route.resolve_calculation_route(case, root=ROOT)
        self.assertEqual(route["status"], "ready")
        resolved, compiled = calculation_route.compile_route(root=ROOT, case=case)
        self.assertEqual(compiled.execution_status, "completed")
        self.assertEqual(resolved["source"]["source_kind"], "profile_v2")
        self.assertIsNotNone(compiled.plan)
        self.assertIn("profile", {ref["role"] for ref in compiled.plan["input_refs"]})


class P1PrepareTest(unittest.TestCase):
    def test_prepare_runs_input_stages_and_hands_off_artifacts(self):
        prepare_root = base.SCRATCH / f"prepare-fix-{base.RUN_TAG}"
        shutil.rmtree(prepare_root, ignore_errors=True)
        proc = base._cli(
            "prepare",
            "--case",
            base.CASE_REL,
            "--out",
            prepare_root.relative_to(ROOT).as_posix(),
            "--run-id",
            "prepare-fix",
            expect=0,
        )
        for stage_id in ("freeze_input", "parse_input", "normalize_build", "export_calc_input", "resolve_skills", "compile_plan"):
            self.assertIn(stage_id, proc.stdout)
        context = base._load(prepare_root / "prepare-fix" / "prepare-context.json")
        self.assertIn("freeze_input", context["prepare_stages"])
        self.assertIn("compile_plan", context["prepare_stages"])
        self.assertIn("calculation_input", context["artifacts"])
        self.assertIn("steps/", context["artifacts"]["calculation_input"]["path"])
        self.assertEqual(context["input_handoff"]["mode"], "explicit_artifact_refs")
        self.assertNotIn(str(ROOT), json.dumps(context, ensure_ascii=False))

    def test_main_skill_override_and_resume_enter_prepare_pipeline(self):
        out = base.SCRATCH / f"prepare-resume-{base.RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        args = (
            "prepare", "--case", base.CASE_REL, "--out", out.relative_to(ROOT).as_posix(),
            "--run-id", "prepare-resume", "--main-skill", "v01cv",
        )
        first = base._cli(*args, expect=0)
        self.assertIn("freeze_input", first.stdout)
        context = base._load(out / "prepare-resume" / "prepare-context.json")
        self.assertEqual(context.get("main_skill_source"), "cli")
        second = base._cli(*args, "--resume", expect=0)
        self.assertIn("compile_plan", second.stdout)
        self.assertTrue((out / "prepare-resume" / "prepare-context.json").is_file())
        self.assertTrue((out / "prepare-resume" / "calculation-plan.json").is_file())

    def test_research_without_source_writes_prepare_context(self):
        case_path = base.SCRATCH / f"research-{base.RUN_TAG}.json"
        case_path.parent.mkdir(parents=True, exist_ok=True)
        case_path.write_text(
            json.dumps(
                {
                    "schema": "le-case-v1",
                    "case_id": "research-no-source",
                    "model": "research",
                    "main_skill": {"ability_id": "no-such-ability"},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        out = base.SCRATCH / f"research-prep-{base.RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        proc = base._cli(
            "prepare",
            "--case",
            case_path.relative_to(ROOT).as_posix(),
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--run-id",
            "research-prep",
            expect=3,
        )
        context = base._load(out / "research-prep" / "prepare-context.json")
        self.assertEqual(context["readiness"], "needs_research")
        self.assertTrue((out / "research-prep" / "prepare-context.json").is_file())
        self.assertIn("prepare-context", proc.stdout)


class P1FingerprintCacheTest(unittest.TestCase):
    def test_actual_inputs_and_attribute_rules_enter_fingerprint_and_impl_group(self):
        module = _cli_module()
        case = base._load(base.CASE)
        case["_path"] = base.CASE_REL
        route = module._route_for_case(case)
        refs = module._unified_input_refs(case, route)
        roles = {str(ref.get("artifact_id")) for ref in refs}
        self.assertIn("profile.attribute_conversion.rules", roles)
        first = module._unified_input_fingerprint(case, route)
        # 实际 calculation-input 内容变化但 case 声明 sha 不变时，发布指纹也必须变化
        work = base.SCRATCH / f"fp-{base.RUN_TAG}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        calc_source = ROOT / case["inputs"]["calculation_input"]
        calc_path = work / "calculation-input.json"
        payload = calc_source.read_text(encoding="utf-8")
        calc_path.write_text(payload + "\n", encoding="utf-8")
        case["inputs"] = dict(case["inputs"], calculation_input=str(calc_path.relative_to(ROOT)))
        second = module._unified_input_fingerprint(case, route)
        self.assertNotEqual(first, second)
        manifest = stage_cache.implementation_manifest("compile_plan", ROOT)
        paths = {row["path"] for row in manifest["files"]}
        self.assertIn("tools/phase3/src/le_calc/calculation_route.py", paths)
        self.assertIn("tools/phase3/src/le_calc/plan_compiler.py", paths)

    def test_independent_scenario_only_invalidates_simulate(self):
        from le_calc.workflow import contract, stage
        from le_calc.workflow import unified as unified_module

        module = _cli_module()
        case = base._load(base.CASE)
        case["_path"] = base.CASE_REL
        route = module._route_for_case(case)
        refs = module._unified_input_refs(case, route)
        cache_root = base.SCRATCH / f"scenario-cache-{base.RUN_TAG}"
        shutil.rmtree(cache_root, ignore_errors=True)
        cache = stage_cache.StageCache(ROOT, cache_root=cache_root)
        root_dir = base.SCRATCH / f"scenario-src-{base.RUN_TAG}"
        shutil.rmtree(root_dir, ignore_errors=True)

        def context_for(path):
            return contract.StageContext(repo_root=ROOT, output_root=path, adapters={"stage_cache": cache})

        def request(stage_id, params):
            return contract.StageRequest(
                stage_id=stage_id,
                case_id="void-cleave-20261110",
                run_id="scenario-cache",
                input_refs=[dict(ref) for ref in refs],
                parameters={"case_ref": base.CASE_REL, **params},
            )

        compiled = stage.execute_stage(request("compile_plan", {}), context_for(root_dir))
        self.assertEqual(compiled.execution_status, "completed")
        plan = unified_module.load_plan(root_dir)
        plan_ref = {"schema": plan["schema"], "plan_id": plan["plan_id"], "plan_sha256": plan["plan_sha256"]}
        controls = plan["controls"]
        default_scenario = {
            "states": {c["key"]: c.get("default") for c in controls if c["namespace"] == "state"},
            "target": {c["key"]: c.get("default") for c in controls if c["namespace"] == "target"},
        }
        variant_scenario = json.loads(json.dumps(default_scenario))
        variant_scenario["target"]["monster_level"] = 1
        run_default = base.SCRATCH / f"scenario-default-{base.RUN_TAG}"
        run_variant = base.SCRATCH / f"scenario-variant-{base.RUN_TAG}"
        shutil.rmtree(run_default, ignore_errors=True)
        shutil.rmtree(run_variant, ignore_errors=True)
        stage.execute_stage(request("compile_plan", {}), context_for(run_default))
        default_result = stage.execute_stage(
            request("simulate", {"render_html": False, "plan_ref": plan_ref, "scenario": default_scenario}),
            context_for(run_default),
        )
        self.assertEqual(default_result.execution_status, "completed")
        self.assertNotEqual(default_result.cache.get("status"), "hit")
        # 同依赖、同计划，仅场景变化：compile 命中缓存，simulate 必须 miss
        variant_compile = stage.execute_stage(request("compile_plan", {}), context_for(run_variant))
        self.assertEqual(variant_compile.cache.get("status"), "hit")
        variant_result = stage.execute_stage(
            request("simulate", {"render_html": False, "plan_ref": plan_ref, "scenario": variant_scenario}),
            context_for(run_variant),
        )
        self.assertEqual(variant_result.execution_status, "completed")
        self.assertEqual(variant_result.cache.get("status"), "miss")
        self.assertNotEqual(
            unified_module.load_plan(run_default)["plan_sha256"],
            None,
        )
        self.assertNotEqual(
            base._load(run_default / "result.json")["single_hit"],
            base._load(run_variant / "result.json")["single_hit"],
        )

    def test_same_dependencies_hit_but_input_change_and_corrupt_cache_miss(self):
        cache_root = base.SCRATCH / f"cache-fix-{base.RUN_TAG}"
        out1 = base.SCRATCH / f"cache-fix-out1-{base.RUN_TAG}"
        out2 = base.SCRATCH / f"cache-fix-out2-{base.RUN_TAG}"
        out3 = base.SCRATCH / f"cache-fix-out3-{base.RUN_TAG}"
        for path in (cache_root, out1, out2, out3):
            shutil.rmtree(path, ignore_errors=True)
        previous = os.environ.get("LE_PIPELINE_CACHE_ROOT")
        os.environ["LE_PIPELINE_CACHE_ROOT"] = cache_root.relative_to(ROOT).as_posix()
        try:
            run_id = f"cache-fix-{base.RUN_TAG}"
            base._cli("run", "--case", base.CASE_REL, "--out", out1.relative_to(ROOT).as_posix(), "--run-id", run_id, expect=3)
            base._cli("run", "--case", base.CASE_REL, "--out", out2.relative_to(ROOT).as_posix(), "--run-id", run_id, expect=3)
            state2 = base._load(out2 / "pipeline-state.json")
            entries = {entry["stage_id"]: entry for entry in state2.get("stages") or []}
            for stage_id in ("compile_plan", "simulate", "validate_result"):
                self.assertEqual(entries[stage_id].get("cache_status"), "hit", stage_id)
            # 端点变化只应影响 render；同依赖的 compile/simulate/validate 仍应命中
            base._cli("run", "--case", base.CASE_REL, "--out", out3.relative_to(ROOT).as_posix(), "--run-id", run_id, "--port", "8833", expect=3)
            state3 = base._load(out3 / "pipeline-state.json")
            entries3 = {entry["stage_id"]: entry for entry in state3.get("stages") or []}
            for stage_id in ("compile_plan", "simulate", "validate_result"):
                self.assertEqual(entries3[stage_id].get("cache_status"), "hit", stage_id)
            self.assertEqual(entries3["render_report"].get("cache_status"), "miss")
            # 输入变化：复制 case/profile 指向不同 profile，编译必须 miss
            variant_case = base.SCRATCH / f"cache-variant-{base.RUN_TAG}.json"
            variant_profile = base.SCRATCH / f"cache-variant-profile-{base.RUN_TAG}.json"
            _variant_profile(ROOT / "examples/void-cleave-20261110/inputs/dps-profile-v2.json", variant_profile, note="cache variant")
            source_case = base._load(base.CASE)
            source_case["dps_profile"] = variant_profile.relative_to(ROOT).as_posix()
            variant_case.write_text(json.dumps(source_case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            out4 = base.SCRATCH / f"cache-fix-out4-{base.RUN_TAG}"
            shutil.rmtree(out4, ignore_errors=True)
            base._cli("run", "--case", variant_case.relative_to(ROOT).as_posix(), "--out", out4.relative_to(ROOT).as_posix(), "--run-id", run_id + "-variant", expect=3)
            state4 = base._load(out4 / "pipeline-state.json")
            entries4 = {entry["stage_id"]: entry for entry in state4.get("stages") or []}
            self.assertEqual(entries4["compile_plan"].get("cache_status"), "miss")
            # 损坏缓存：定位第一次 simulate 实际使用的条目并破坏 payload，必须 miss 重算
            cache = stage_cache.StageCache(ROOT, cache_root=cache_root)
            state1 = base._load(out1 / "pipeline-state.json")
            entries1 = {entry["stage_id"]: entry for entry in state1.get("stages") or []}
            simulate_key = entries1["simulate"].get("cache_key")
            self.assertTrue(simulate_key)
            entry_path = cache.entry_path("simulate", simulate_key)
            self.assertTrue(entry_path.is_file())
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            cached = entry["outputs"][0]["cached_path"]
            (cache.payload_dir("simulate", simulate_key) / cached).write_bytes(b"corrupted")
            out5 = base.SCRATCH / f"cache-fix-out5-{base.RUN_TAG}"
            shutil.rmtree(out5, ignore_errors=True)
            base._cli("run", "--case", base.CASE_REL, "--out", out5.relative_to(ROOT).as_posix(), "--run-id", run_id, expect=3)
            state5 = base._load(out5 / "pipeline-state.json")
            entries5 = {entry["stage_id"]: entry for entry in state5.get("stages") or []}
            self.assertEqual(entries5["simulate"].get("cache_status"), "miss")
            self.assertEqual(
                base._load(out1 / "result.json")["sustained_dps"],
                base._load(out5 / "result.json")["sustained_dps"],
            )
        finally:
            if previous is None:
                os.environ.pop("LE_PIPELINE_CACHE_ROOT", None)
            else:
                os.environ["LE_PIPELINE_CACHE_ROOT"] = previous


class P1InputIdentityAndDatasetTest(unittest.TestCase):
    def test_handoff_rejects_cross_build_identity_and_accepts_matching(self):
        case = base._load(base.CASE)
        case["_path"] = base.CASE_REL
        work = base.SCRATCH / f"identity-{base.RUN_TAG}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        source = ROOT / case["inputs"]["calculation_input"]
        mismatched = json.loads(source.read_text(encoding="utf-8"))
        mismatched["build_id"] = "letools:another-build"
        mismatched["revision_id"] = "rev-another"
        bad_path = work / "calculation-input.json"
        bad_path.write_text(json.dumps(mismatched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _, rejected = calculation_route.compile_route(
            root=ROOT,
            case=case,
            input_overrides={
                "calculation_input": bad_path.relative_to(ROOT).as_posix(),
                "calculation_input_sha256": planmod.sha256_file(bad_path),
            },
        )
        self.assertIsNone(rejected.plan)
        self.assertTrue(
            any(item.get("code") in ("input_load_failed", "input_build_identity_mismatch") for item in rejected.diagnostics)
        )
        _, accepted = calculation_route.compile_route(
            root=ROOT,
            case=case,
            input_overrides={
                "calculation_input": source.relative_to(ROOT).as_posix(),
                "calculation_input_sha256": planmod.sha256_file(source),
            },
        )
        self.assertEqual(accepted.execution_status, "completed")
        self.assertEqual(
            accepted.plan["subject"]["revision_id"], case["build"]["revision_id"]
        )
        self.assertEqual(
            accepted.plan["subject"]["build_id"], case["build"]["build_id"]
        )
        # artifact_ref sha 与实际文件不一致必须拒绝
        _, mismatch_sha = calculation_route.compile_route(
            root=ROOT,
            case=case,
            input_overrides={
                "calculation_input": source.relative_to(ROOT).as_posix(),
                "calculation_input_sha256": "0" * 64,
            },
        )
        self.assertIsNone(mismatch_sha.plan)

    def test_handoff_override_requires_valid_sha256(self):
        """P1 补修：声明 override 时必须同时提供合法 SHA-256；缺失/空串/格式非法一律拒绝。"""
        case = base._load(base.CASE)
        case["_path"] = base.CASE_REL
        calc_source = ROOT / case["inputs"]["calculation_input"]
        calc_rel = calc_source.relative_to(ROOT).as_posix()
        good_calc_sha = planmod.sha256_file(calc_source)

        def compile_with(overrides):
            return calculation_route.compile_route(
                root=ROOT, case=case, input_overrides=overrides
            )[1]

        # 契约成功路径：正确身份 + 正确 SHA 仍然通过
        accepted = compile_with(
            {"calculation_input": calc_rel, "calculation_input_sha256": good_calc_sha}
        )
        self.assertEqual(accepted.execution_status, "completed")
        self.assertIsNotNone(accepted.plan)

        # 缺失/空串/长度非法/大写/非十六进制/错误 SHA -> 明确拒绝
        bad_calc_variants = {
            "missing": None,
            "empty": "",
            "short": good_calc_sha[:32],
            "uppercase": good_calc_sha.upper(),
            "non_hex": "z" * 64,
            "wrong": "0" * 64,
        }
        for label, sha in bad_calc_variants.items():
            overrides = {"calculation_input": calc_rel}
            if sha is not None:
                overrides["calculation_input_sha256"] = sha
            with self.subTest(target="calculation-input", variant=label):
                rejected = compile_with(overrides)
                self.assertIsNone(rejected.plan, label)
                self.assertEqual(rejected.execution_status, "rejected", label)
                self.assertEqual(rejected.readiness, "blocked", label)

        # BuildSpec override 走同一条契约
        build_rel = case["build"]["build_spec"]
        build_source = ROOT / build_rel
        good_build_sha = planmod.sha256_file(build_source)
        accepted_build = compile_with(
            {"build_spec": build_rel, "build_spec_sha256": good_build_sha}
        )
        self.assertEqual(accepted_build.execution_status, "completed")
        for label, sha in (("missing", None), ("empty", ""), ("malformed", "not-a-sha")):
            overrides = {"build_spec": build_rel}
            if sha is not None:
                overrides["build_spec_sha256"] = sha
            with self.subTest(target="build-spec", variant=label):
                rejected = compile_with(overrides)
                self.assertIsNone(rejected.plan, label)
                self.assertEqual(rejected.execution_status, "rejected", label)

    def test_stage_compile_plan_rejects_override_without_valid_sha256(self):
        """P1 补修：阶段适配层不得把“缺 SHA 的 override”静默降级为按路径覆盖。"""
        case = base._load(base.CASE)
        calc_rel = case["inputs"]["calculation_input"]
        calc_source = ROOT / calc_rel
        run_dir = base.SCRATCH / f"override-sha-{base.RUN_TAG}"
        shutil.rmtree(run_dir, ignore_errors=True)
        run_dir.mkdir(parents=True)
        ctx = wf_contract.StageContext(repo_root=ROOT, output_root=run_dir, adapters={})

        def request(params):
            return wf_contract.StageRequest(
                stage_id="compile_plan",
                case_id=str(case.get("case_id") or "case"),
                run_id="override-sha",
                parameters={"case_ref": base.CASE_REL, **params},
            )

        for label, sha in (("missing", None), ("empty", ""), ("malformed", "not-a-sha")):
            params = {"calc_input_ref": calc_rel}
            if sha is not None:
                params["calc_input_sha256"] = sha
            with self.subTest(variant=label):
                result = wf_stage.stage_compile_plan(request(params), ctx)
                self.assertEqual(result["execution_status"], wf_contract.EXECUTION_FAILED)
                self.assertEqual(result["issues"][0]["code"], "override_sha256_required")

        ok = wf_stage.stage_compile_plan(
            request(
                {
                    "calc_input_ref": calc_rel,
                    "calc_input_sha256": planmod.sha256_file(calc_source),
                }
            ),
            ctx,
        )
        self.assertEqual(ok["execution_status"], wf_contract.EXECUTION_COMPLETED)

    def test_dataset_database_content_enters_refs_and_fingerprint(self):
        module = _cli_module()
        case = base._load(base.CASE)
        case["_path"] = base.CASE_REL
        work = base.SCRATCH / f"dataset-{base.RUN_TAG}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        db = work / "small.sqlite"
        db.write_bytes(b"dataset-v1")
        pointer = work / "current.json"
        pointer.write_text(
            json.dumps(
                {
                    "dataset_id": "synthetic-dataset",
                    "database": db.relative_to(ROOT).as_posix(),
                    "file_sha256": planmod.sha256_file(db),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        case["dataset"] = pointer.relative_to(ROOT).as_posix()
        route = module._route_for_case(case)
        refs = module._unified_input_refs(case, route)
        dataset_refs = [ref for ref in refs if ref.get("artifact_id") == "dataset_content"]
        self.assertEqual(len(dataset_refs), 1)
        self.assertEqual(dataset_refs[0]["path"], db.relative_to(ROOT).as_posix())
        first = module._unified_input_fingerprint(case, route)
        db.write_bytes(b"dataset-v2")
        second = module._unified_input_fingerprint(case, route)
        self.assertNotEqual(first, second)


class P1RendererDependencyTest(unittest.TestCase):
    def test_renderer_only_files_do_not_invalidate_simulate_or_validate(self):
        simulate = {
            row["path"] for row in stage_cache.implementation_manifest("simulate", ROOT)["files"]
        }
        validate = {
            row["path"] for row in stage_cache.implementation_manifest("validate_result", ROOT)["files"]
        }
        render = {
            row["path"] for row in stage_cache.implementation_manifest("render_report", ROOT)["files"]
        }
        renderer_files = (
            "tools/phase3/src/le_calc/dps_report.py",
            "tools/phase3/src/le_calc/report.py",
            "tools/phase3/src/le_calc/hit_report.py",
            "tools/phase3/src/le_calc/report_ui",
        )
        for path in renderer_files:
            self.assertFalse(any(path in item for item in simulate), f"{path} 不应进入 simulate 依赖")
            self.assertFalse(any(path in item for item in validate), f"{path} 不应进入 validate 依赖")
        self.assertTrue(any("dps_report.py" in item for item in render), "render 必须依赖渲染模块")
        # 端点变化只 render miss 的断言在缓存测试中；此处保证 renderer 文件不在数值阶段依赖清单。


class P1StagedConflictRecoveryTest(unittest.TestCase):
    def test_staged_input_change_conflicts_and_interruption_recovers(self):
        out = base.SCRATCH / f"staged-conflict-{base.RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        first_args = (
            "run",
            "--case",
            base.CASE_REL,
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--run-id",
            f"staged-conflict-{base.RUN_TAG}",
            "--publish",
            "staged",
        )
        base._cli(*first_args, expect=3)
        first_plan = base._load(out / "calculation-plan.json")["plan_sha256"]
        variant_profile = base.SCRATCH / f"staged-variant-profile-{base.RUN_TAG}.json"
        _variant_profile(ROOT / "examples/void-cleave-20261110/inputs/dps-profile-v2.json", variant_profile, note="staged variant")
        variant_case = base.SCRATCH / f"staged-variant-case-{base.RUN_TAG}.json"
        case = base._load(base.CASE)
        case["dps_profile"] = variant_profile.relative_to(ROOT).as_posix()
        variant_case.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        conflict = base._cli(
            "run",
            "--case",
            variant_case.relative_to(ROOT).as_posix(),
            "--out",
            out.relative_to(ROOT).as_posix(),
            "--run-id",
            f"staged-conflict-{base.RUN_TAG}",
            "--publish",
            "staged",
        )
        self.assertEqual(conflict.returncode, 1)
        self.assertIn("conflict", (conflict.stdout + conflict.stderr).lower())
        self.assertEqual(base._load(out / "calculation-plan.json")["plan_sha256"], first_plan)

    def test_interrupted_staged_publish_recovers_without_duplicate(self):
        module = _cli_module()
        out = base.SCRATCH / f"staged-recover-{base.RUN_TAG}"
        shutil.rmtree(out, ignore_errors=True)
        argv = [
            "run", "--case", base.CASE_REL, "--out", out.relative_to(ROOT).as_posix(),
            "--run-id", f"staged-recover-{base.RUN_TAG}", "--publish", "staged",
        ]
        original = module._publish_staged_run
        calls = {"count": 0}

        def failing_publish(*args, **kwargs):
            calls["count"] += 1
            raise RuntimeError("injected publish interruption")

        module._publish_staged_run = failing_publish
        try:
            first_code = module.main(argv)
        finally:
            module._publish_staged_run = original
        self.assertEqual(first_code, 1)
        self.assertFalse(out.exists(), "故障注入后不得发布最终 run")
        # 模拟进程已退出：租约记录一个不存在的 PID，恢复应清理失活候选
        lease_dir = out.parent / ".staging-locks"
        leases = sorted(lease_dir.glob("*.lease"))
        self.assertTrue(leases, "staged 中断后应保留目标租约")
        for lease in leases:
            payload = json.loads(lease.read_text(encoding="utf-8"))
            payload["pid"] = 999999999
            payload["host"] = __import__("socket").gethostname()
            lease.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        second_code = module.main(argv)
        self.assertEqual(second_code, 3)
        self.assertTrue((out / "calculation-plan.json").is_file())
        self.assertTrue((out / "dps-report.html").is_file())
        self.assertEqual(calls["count"], 1)
        self.assertEqual(base._load(out / "pipeline-state.json")["last_commit"]["status"], "committed")


class P1MechanismMergeTest(unittest.TestCase):
    def test_mechanism_and_rule_multi_file_conflicts_are_explicit(self):
        from le_calc.calculation_route import (
            _merge_mechanism_documents,
            _merge_rule_documents,
        )

        entry = {"effects": [{"id": "e1", "stat": "damage", "operation": "increased", "value": 0.1}]}
        other = {"effects": [{"id": "e1", "stat": "damage", "operation": "increased", "value": 0.5}]}
        merged, diagnostics = _merge_mechanism_documents(
            [("a.json", {"mechanisms": {"m1": entry}}), ("b.json", {"mechanisms": {"m1": other}})]
        )
        self.assertEqual(merged["mechanisms"]["m1"], entry)
        self.assertTrue(any(item["code"] == "mechanism_definition_conflict" for item in diagnostics))
        merged_reverse, diagnostics_reverse = _merge_mechanism_documents(
            [("b.json", {"mechanisms": {"m1": other}}), ("a.json", {"mechanisms": {"m1": entry}})]
        )
        self.assertEqual(merged_reverse["mechanisms"]["m1"], other)
        self.assertTrue(any(item["code"] == "mechanism_definition_conflict" for item in diagnostics_reverse))
        same, same_diagnostics = _merge_mechanism_documents(
            [("a.json", {"mechanisms": {"m1": entry}}), ("b.json", {"mechanisms": {"m1": entry}})]
        )
        self.assertEqual(same_diagnostics, [])
        self.assertEqual(same["source_files"]["m1"], ["a.json", "b.json"])
        rule_conflict, rule_diagnostics = _merge_rule_documents(
            [("r1.json", {"x": 1}), ("r2.json", {"x": 2})]
        )
        self.assertEqual(rule_conflict["x"], 1)
        self.assertTrue(any(item["code"] == "rule_definition_conflict" for item in rule_diagnostics))
        rule_same, rule_same_diag = _merge_rule_documents(
            [("r1.json", {"x": 1}), ("r2.json", {"x": 1})]
        )
        self.assertEqual(rule_same_diag, [])
        self.assertEqual(rule_same["x"], 1)

    def test_route_load_conflict_and_identical_duplicate_merge(self):
        import copy as _copy

        case = base._load(base.NATIVE_CASE)
        case["_path"] = base.NATIVE_CASE_REL
        shared = ROOT / "tools/phase3/rules/shared-conditional-mechanisms-v1.json"
        shared_doc = json.loads(shared.read_text(encoding="utf-8"))
        work = base.SCRATCH / f"merge-route-{base.RUN_TAG}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        identical = work / "identical.json"
        identical.write_text(json.dumps(shared_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        conflict = _copy.deepcopy(shared_doc)
        first_key = sorted(conflict["mechanisms"])[0]
        conflict["mechanisms"][first_key] = {"effects": [{"id": "conflict", "value": 999}]}
        conflict_path = work / "conflict.json"
        conflict_path.write_text(json.dumps(conflict, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        identical_case = _copy.deepcopy(case)
        identical_case["calculation"]["source"]["mechanisms"] = [
            "tools/phase3/rules/shared-conditional-mechanisms-v1.json",
            identical.relative_to(ROOT).as_posix(),
        ]
        resolved, compiled = calculation_route.compile_route(root=ROOT, case=identical_case)
        self.assertEqual(compiled.execution_status, "completed")
        self.assertIsNotNone(compiled.plan)
        self.assertEqual(len(resolved.get("diagnostics") or []), 0)
        conflict_case = _copy.deepcopy(case)
        conflict_case["calculation"]["source"]["mechanisms"] = [
            "tools/phase3/rules/shared-conditional-mechanisms-v1.json",
            conflict_path.relative_to(ROOT).as_posix(),
        ]
        _, rejected = calculation_route.compile_route(root=ROOT, case=conflict_case)
        self.assertIsNone(rejected.plan)
        self.assertTrue(any(item.get("code") == "mechanism_definition_conflict" for item in rejected.diagnostics))


if __name__ == "__main__":
    unittest.main(verbosity=2)
