# -*- coding: utf-8 -*-
"""P4 stage fingerprint/cache tests (``le-stage-cache-v1``)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
SCRIPTS = str(ROOT / "scripts")
CALC_SRC = str(ROOT / "tools" / "phase3" / "src")
for entry in (SCRIPTS, CALC_SRC):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import le_commands  # noqa: E402
from le_calc.workflow import contract, stage, stage_cache, statestore  # noqa: E402

BH_CASE = "examples/black-hole/case.legacy.json"
_DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def run_prepare(argv: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    parser = le_commands.build_parser()
    args = parser.parse_args(["prepare", *argv])
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def _request(stage_id: str, *, params: dict | None = None, deps: dict | None = None,
             inputs: list[dict] | None = None) -> contract.StageRequest:
    return contract.StageRequest(
        stage_id=stage_id,
        case_id="black-hole",
        run_id="p4-unit",
        input_refs=list(inputs or []),
        parameters=dict(params or {}),
        dependency_fingerprints={"dependencies": dict(deps or {})},
    )


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class StageCacheUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-cache-", dir=SCRATCH)
        self.tmp = Path(self._tmp.name)
        self.run_dir = self.tmp / "run"
        self.run_dir.mkdir()
        self.cache = stage_cache.StageCache(ROOT, cache_root=self.tmp / "cache")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _context(self, cache=None) -> contract.StageContext:
        return contract.StageContext(
            repo_root=ROOT,
            output_root=self.run_dir,
            adapters={"stage_cache": cache or self.cache},
        )

    def test_request_fingerprints_are_deterministic_and_relative(self) -> None:
        case_path = ROOT / BH_CASE
        ref = {
            "artifact_id": "case",
            "path": str(case_path),
            "asset_id": None,
            "schema_version": None,
            "sha256": contract.sha256_file(case_path),
        }
        req = _request("freeze_input", params={"case_ref": str(case_path)}, inputs=[ref])
        first = stage_cache.request_fingerprints(req, ROOT, handler=stage.STAGES["freeze_input"]["handler"])
        second = stage_cache.request_fingerprints(req, ROOT, handler=stage.STAGES["freeze_input"]["handler"])
        self.assertEqual(first["cache_key"], second["cache_key"])
        self.assertEqual(first["input_fingerprint"], second["input_fingerprint"])
        self.assertRegex(first["cache_key"], r"^[0-9a-f]{64}$")
        payload = json.dumps(first, ensure_ascii=False)
        self.assertIsNone(_DRIVE_RE.search(payload), payload[:200])
        self.assertIn("examples/black-hole/case.legacy.json", payload)

    def test_store_load_materialize_roundtrip(self) -> None:
        output = _write_json(self.run_dir / "sub" / "out.json", {"ok": True})
        req = _request("freeze_input", params={"case_ref": BH_CASE})
        fp = stage_cache.request_fingerprints(req, ROOT, handler=stage.STAGES["freeze_input"]["handler"])
        stored = self.cache.store(
            stage_id="freeze_input",
            cache_key=fp["cache_key"],
            fingerprints=fp,
            outputs=[output],
            output_root=self.run_dir,
            artifact_ref=lambda path, artifact_id=None: contract.make_artifact_ref(
                path, root=ROOT, artifact_id=artifact_id
            ),
            readiness="ready",
            issues=[],
        )
        self.assertTrue(stored["ok"], stored)
        entry = self.cache.load("freeze_input", fp["cache_key"])
        self.assertIsNotNone(entry)
        self.assertEqual(entry["outputs"][0]["relative_path"], "sub/out.json")
        # remove run output, materialize from cache
        output.unlink()
        refs = self.cache.materialize(
            entry, self.run_dir,
            artifact_ref=lambda path, artifact_id=None: contract.make_artifact_ref(
                path, root=ROOT, artifact_id=artifact_id
            ),
        )
        self.assertEqual(len(refs), 1)
        self.assertTrue(output.is_file())
        self.assertEqual(contract.sha256_file(output), entry["outputs"][0]["sha256"])
        # payload tamper => miss
        payload_path = self.cache.payload_dir("freeze_input", fp["cache_key"]) / entry["outputs"][0]["cached_path"]
        payload_path.write_bytes(b"tampered")
        self.assertIsNone(self.cache.load("freeze_input", fp["cache_key"]))
        # corrupt entry JSON => miss
        entry_path = self.cache.entry_path("freeze_input", fp["cache_key"])
        entry_path.write_text("{bad", encoding="utf-8")
        self.assertIsNone(self.cache.load("freeze_input", fp["cache_key"]))

    def test_execute_stage_hit_does_not_run_handler(self) -> None:
        calls = {"count": 0}
        real = stage.STAGES["freeze_input"]["handler"]

        def counting(request, context):
            calls["count"] += 1
            return real(request, context)

        settings = {
            "handler": counting,
            "description": "p4 test freeze",
        }
        with mock.patch.dict(stage.STAGES, {"freeze_input": settings}):
            req = _request("freeze_input", params={"case_ref": BH_CASE})
            first = stage.execute_stage(req, self._context())
            second = stage.execute_stage(req, self._context())
        self.assertEqual(first.cache["status"], contract.CACHE_MISS)
        self.assertEqual(second.cache["status"], contract.CACHE_HIT)
        self.assertEqual(calls["count"], 1)
        self.assertTrue(second.metrics.get("cache_hit"))
        self.assertFalse(second.metrics.get("handler_executed"))
        self.assertGreater(first.metrics["elapsed_ms"], 0)

    def test_no_cache_forces_execution_but_refreshes_cache(self) -> None:
        calls = {"count": 0}
        real = stage.STAGES["freeze_input"]["handler"]

        def counting(request, context):
            calls["count"] += 1
            return real(request, context)

        settings = {"handler": counting, "description": "p4 no-cache"}
        disabled = stage_cache.StageCache(ROOT, cache_root=self.tmp / "cache", enabled=False)
        with mock.patch.dict(stage.STAGES, {"freeze_input": settings}):
            req = _request("freeze_input", params={"case_ref": BH_CASE})
            first = stage.execute_stage(req, self._context(disabled))
            second = stage.execute_stage(req, self._context(disabled))
            warm = stage.execute_stage(req, self._context(self.cache))
        self.assertEqual(first.cache["status"], contract.CACHE_DISABLED)
        self.assertEqual(second.cache["status"], contract.CACHE_DISABLED)
        self.assertEqual(calls["count"], 2)
        self.assertTrue(first.cache.get("written"))
        # forced run refreshed the cache: a later normal run hits it
        self.assertEqual(warm.cache["status"], contract.CACHE_HIT)
        self.assertEqual(calls["count"], 2)

    def test_implementation_override_cascades_only_downstream(self) -> None:
        calls = {"alpha": 0, "beta": 0, "gamma": 0}

        def handler_alpha(request, context):
            calls["alpha"] += 1
            out = _write_json(self.run_dir / "alpha.json", {"v": 1})
            return {"execution_status": contract.EXECUTION_COMPLETED, "readiness": contract.READY,
                    "outputs": [out], "metrics": {}}

        def handler_beta(request, context):
            calls["beta"] += 1
            out = _write_json(self.run_dir / "beta.json", {"v": 1})
            return {"execution_status": contract.EXECUTION_COMPLETED, "readiness": contract.READY,
                    "outputs": [out], "metrics": {}}

        def handler_gamma(request, context):
            calls["gamma"] += 1
            out = _write_json(self.run_dir / "gamma.json", {"v": 1})
            return {"execution_status": contract.EXECUTION_COMPLETED, "readiness": contract.READY,
                    "outputs": [out], "metrics": {}}

        settings = {
            "p4_alpha": {"handler": handler_alpha, "description": "alpha"},
            "p4_beta": {"handler": handler_beta, "description": "beta"},
            "p4_gamma": {"handler": handler_gamma, "description": "gamma"},
        }
        with mock.patch.dict(stage.STAGES, settings):
            req_a = _request("p4_alpha", params={"n": 1})
            req_g = _request("p4_gamma", params={"n": 1})
            with mock.patch.dict(os.environ, {}, clear=False):
                fa = stage.execute_stage(req_a, self._context())
                fg = stage.execute_stage(req_g, self._context())
                req_b = _request("p4_beta", params={"n": 1}, deps={"p4_alpha": fa.cache["key"]})
                fb = stage.execute_stage(req_b, self._context())
                hit_g = stage.execute_stage(req_g, self._context())
                self.assertEqual(fg.cache["status"], contract.CACHE_MISS)
                self.assertEqual(hit_g.cache["status"], contract.CACHE_HIT)
                self.assertEqual(fb.cache["status"], contract.CACHE_MISS)
                # alpha implementation changes => alpha + beta miss, gamma stays hit
                with mock.patch.dict(os.environ, {"LE_PIPELINE_IMPL_OVERRIDE": '{"p4_alpha":"v2"}'}):
                    miss_a = stage.execute_stage(req_a, self._context())
                    miss_b = stage.execute_stage(req_b, self._context())
                    still_g = stage.execute_stage(req_g, self._context())
                self.assertEqual(miss_a.cache["status"], contract.CACHE_MISS)
                # orchestration chains the new upstream key into downstream requests
                req_b2 = _request(
                    "p4_beta", params={"n": 1}, deps={"p4_alpha": miss_a.cache["key"]}
                )
                miss_b2 = stage.execute_stage(req_b2, self._context())
                self.assertEqual(miss_b2.cache["status"], contract.CACHE_MISS)
                self.assertEqual(still_g.cache["status"], contract.CACHE_HIT)
                # parameters change also invalidates that stage + downstream
                changed = _request("p4_alpha", params={"n": 2})
                miss_param = stage.execute_stage(changed, self._context())
                self.assertEqual(miss_param.cache["status"], contract.CACHE_MISS)
        self.assertEqual(calls["gamma"], 1)

    def test_failed_stage_is_not_cached_and_no_half_entry(self) -> None:
        def failing(request, context):
            raise stage.StageError("boom", "injected")

        def good(request, context):
            out = _write_json(self.run_dir / "ok.json", {"ok": True})
            return {"execution_status": contract.EXECUTION_COMPLETED, "readiness": contract.READY,
                    "outputs": [out], "metrics": {}}

        with mock.patch.dict(stage.STAGES, {"p4_fail": {"handler": failing, "description": "x"}}):
            result = stage.execute_stage(_request("p4_fail"), self._context())
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertEqual(list((self.tmp / "cache").rglob("*.json")), [])
        with mock.patch.dict(stage.STAGES, {"p4_fail": {"handler": good, "description": "y"}}):
            retry = stage.execute_stage(_request("p4_fail"), self._context())
        self.assertEqual(retry.cache["status"], contract.CACHE_MISS)
        self.assertIsNotNone(self.cache.load("p4_fail", retry.cache["key"]))

    def test_stats_and_clear(self) -> None:
        real = stage.STAGES["freeze_input"]["handler"]
        with mock.patch.dict(stage.STAGES, {"freeze_input": {"handler": real, "description": "x"}}):
            stage.execute_stage(_request("freeze_input", params={"case_ref": BH_CASE}), self._context())
        stats = self.cache.stats()
        self.assertEqual(stats["entries"], 1)
        self.assertIn("freeze_input", stats["by_stage"])
        cleared = self.cache.clear()
        self.assertGreaterEqual(cleared["removed"], 1)
        self.assertEqual(self.cache.stats()["entries"], 0)


class StateStoreCacheTests(unittest.TestCase):
    def test_record_persists_cache_fields_and_fingerprints(self) -> None:
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            store = statestore.FileStateStore(Path(tmp), root=ROOT)
            state = contract.PipelineState(case_id="black-hole", run_id="r1")
            result = contract.StageResult(
                stage_id="freeze_input",
                execution_status=contract.EXECUTION_COMPLETED,
                readiness=contract.READY,
                output_refs=[],
                metrics={
                    "elapsed_ms": 1.5,
                    "input_fingerprint": "a" * 64,
                    "implementation_fingerprint": "b" * 64,
                    "parameters_fingerprint": "c" * 64,
                },
                cache={"status": contract.CACHE_HIT, "key": "d" * 64},
            )
            store.record(state, result)
            entry = state.stages[0]
            self.assertEqual(entry.cache_key, "d" * 64)
            self.assertEqual(entry.parameters_fingerprint, "c" * 64)
            self.assertEqual(entry.cache_status, "hit")
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded.stages[0].cache_key, "d" * 64)

    def test_verify_fingerprints_invalidates_stage_and_downstream(self) -> None:
        state = contract.PipelineState(case_id="black-hole", run_id="r1")
        for stage_id in ("freeze_input", "parse_input", "normalize_build"):
            state.stages.append(
                contract.StageState(
                    stage_id=stage_id,
                    execution_status=contract.EXECUTION_COMPLETED,
                    readiness=contract.READY,
                    input_fingerprint="a" * 64,
                    implementation_fingerprint="b" * 64,
                    parameters_fingerprint="c" * 64,
                    cache_key="e" * 64,
                )
            )
        store = statestore.FileStateStore(ROOT / ".scratch", root=ROOT)
        current = {
            "freeze_input": {"input_fingerprint": "a" * 64, "implementation_fingerprint": "b" * 64,
                             "parameters_fingerprint": "c" * 64, "cache_key": "e" * 64},
            "parse_input": {"input_fingerprint": "f" * 64, "implementation_fingerprint": "b" * 64,
                            "parameters_fingerprint": "c" * 64, "cache_key": "e" * 64},
            "normalize_build": {"input_fingerprint": "a" * 64, "implementation_fingerprint": "b" * 64,
                                "parameters_fingerprint": "c" * 64, "cache_key": "e" * 64},
        }
        plan = store.resume_plan(state, current_fingerprints=current)
        self.assertEqual(plan["valid"], ["freeze_input"])
        self.assertEqual(plan["restart_from"], "parse_input")
        self.assertEqual(plan["downstream_invalidated"], ["normalize_build"])


class PrepareCliCacheTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="p4-cli-", dir=SCRATCH)
        cls.out_root = Path(cls._tmp.name)
        cls.cache_root = cls.out_root / "cache"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _prepare(
        self, run_id: str, *, extra: list[str] | None = None, main_skill: str = "bh2"
    ) -> tuple[int, str, str]:
        return run_prepare(
            [
                "--case", BH_CASE,
                "--out", str(self.out_root),
                "--run-id", run_id,
                "--main-skill", main_skill,
                "--cache-root", str(self.cache_root),
                *(extra or []),
            ]
        )

    def test_cold_warm_nocache_and_output_equality(self) -> None:
        code, out, err = self._prepare("p4-cli-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("miss=8", out)
        cold_dir = self.out_root / "p4-cli-cold"

        code, out, err = self._prepare("p4-cli-warm")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("hit=8 miss=0", out)
        self.assertIn("子进程调用：0 次", out)
        warm_dir = self.out_root / "p4-cli-warm"

        code, out, err = self._prepare("p4-cli-nocache", extra=["--no-cache"])
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("disabled=8", out)
        self.assertNotIn("子进程调用：0 次", out)
        nocache_dir = self.out_root / "p4-cli-nocache"

        # force-run refreshed the cache: a later normal run is warm again
        code, out, err = self._prepare("p4-cli-warm2")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("hit=8 miss=0", out)

        # cached outputs == the bytes computed by the cold run (exact sha)
        for relative_path in _comparable_run_files(cold_dir):
            self.assertEqual(
                contract.sha256_file(cold_dir / relative_path),
                contract.sha256_file(warm_dir / relative_path),
                relative_path,
            )
            # a full (forced) recompute must be semantically identical; generated_at /
            # run-id directory names are non-semantic by contract (§4.1)
            self.assertEqual(
                _semantic_payload(cold_dir / relative_path, cold_dir.name, warm_dir.name),
                _semantic_payload(nocache_dir / relative_path, cold_dir.name, nocache_dir.name),
                relative_path,
            )

        # no machine absolute paths in run dir / cache entries
        for directory in (cold_dir, warm_dir, self.cache_root):
            for path in directory.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt"}:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
                self.assertIsNone(_DRIVE_RE.search(text), f"{path} leaks drive path")

        state = json.loads((warm_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        for entry in state["stages"]:
            self.assertRegex(entry["cache_key"], r"^[0-9a-f]{64}$")
            self.assertRegex(entry["input_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertRegex(entry["implementation_fingerprint"], r"^[0-9a-f]{64}$")
            self.assertEqual(entry["cache_status"], "hit")

    def test_implementation_override_cascade_cli(self) -> None:
        code, out, err = self._prepare("p4-cli-base")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        code, out, err = run_prepare(
            [
                "--case", BH_CASE, "--out", str(self.out_root), "--run-id", "p4-cli-cascade",
                "--main-skill", "bh2", "--cache-root", str(self.cache_root),
            ],
            env={"LE_PIPELINE_IMPL_OVERRIDE": '{"export_calc_input":"v2"}'},
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        # freeze/parse/normalize stay warm; export + all downstream are invalidated
        self.assertIn("hit=3 miss=5", out)
        state = json.loads((self.out_root / "p4-cli-cascade" / "pipeline-state.json").read_text(encoding="utf-8"))
        by_stage = {entry["stage_id"]: entry for entry in state["stages"]}
        for stage_id in ("freeze_input", "parse_input", "normalize_build"):
            self.assertEqual(by_stage[stage_id]["cache_status"], "hit", stage_id)
        for stage_id in ("export_calc_input", "apply_affix_ranges", "resolve_skills",
                         "prepare_snapshot", "min_validate"):
            self.assertEqual(by_stage[stage_id]["cache_status"], "miss", stage_id)

    def test_main_skill_change_cascades_from_resolve_skills(self) -> None:
        code, out, err = self._prepare("p4-cli-main-base")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        code, out, err = self._prepare("p4-cli-main-other", main_skill="zz99")
        self.assertEqual(code, le_commands.EXIT_PARTIAL, out + err)
        state = json.loads(
            (self.out_root / "p4-cli-main-other" / "pipeline-state.json").read_text(encoding="utf-8")
        )
        by_stage = {entry["stage_id"]: entry for entry in state["stages"]}
        for stage_id in ("freeze_input", "parse_input", "normalize_build",
                         "export_calc_input", "apply_affix_ranges"):
            self.assertEqual(by_stage[stage_id]["cache_status"], "hit", stage_id)
        self.assertEqual(by_stage["resolve_skills"]["cache_status"], "miss")

    def test_resume_invalidates_from_changed_implementation(self) -> None:
        code, out, err = self._prepare("p4-cli-resume-impl")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        code, out, err = run_prepare(
            [
                "--case", BH_CASE, "--out", str(self.out_root), "--run-id", "p4-cli-resume-impl",
                "--main-skill", "bh2", "--cache-root", str(self.cache_root), "--resume",
            ],
            env={"LE_PIPELINE_IMPL_OVERRIDE": '{"export_calc_input":"v9"}'},
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("state_cached=3", out)
        state = json.loads(
            (self.out_root / "p4-cli-resume-impl" / "pipeline-state.json").read_text(encoding="utf-8")
        )
        by_stage = {entry["stage_id"]: entry for entry in state["stages"]}
        self.assertEqual(by_stage["freeze_input"]["cache_status"], "hit")
        self.assertEqual(by_stage["export_calc_input"]["cache_status"], "miss")
        for stage_id in le_commands._PREPARE_STAGES:
            self.assertEqual(by_stage[stage_id]["execution_status"], "completed", stage_id)

    def test_deleted_cache_recovers_by_rerunning(self) -> None:
        code, out, err = self._prepare("p4-cli-cache-a")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        import shutil

        shutil.rmtree(self.cache_root)
        code, out, err = self._prepare("p4-cli-cache-b")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("miss=8", out)
        self.assertTrue((self.out_root / "p4-cli-cache-b" / "pipeline-state.json").is_file())
        self.assertTrue(any(self.cache_root.rglob("*.json")))

    def test_resume_restores_deleted_output_and_corrupt_state(self) -> None:
        code, out, err = self._prepare("p4-cli-resume")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        run_dir = self.out_root / "p4-cli-resume"
        target = run_dir / "steps" / "03-calculation-input.affix-db.json"
        self.assertTrue(target.is_file())
        target.unlink()
        code, out, err = self._prepare("p4-cli-resume", extra=["--resume"])
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertTrue(target.is_file())
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        statuses = {entry["stage_id"]: entry["execution_status"] for entry in state["stages"]}
        for stage_id in le_commands._PREPARE_STAGES:
            self.assertEqual(statuses[stage_id], "completed", stage_id)

        # corrupt state => backup + rebuild, no crash and no half state
        (run_dir / "pipeline-state.json").write_text("{broken", encoding="utf-8")
        code, out, err = self._prepare("p4-cli-resume", extra=["--resume"])
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertTrue(list(run_dir.glob("pipeline-state.corrupt-*.json")))
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["stages"]), len(le_commands._PREPARE_STAGES))


_VOLATILE_KEYS = frozenset(
    {"generated_at", "created_at", "saved_at", "timestamp", "extract_meta", "updated_at"}
)


def _strip_volatile(value):
    if isinstance(value, dict):
        return {
            key: _strip_volatile(item)
            for key, item in value.items()
            if str(key) not in _VOLATILE_KEYS
        }
    if isinstance(value, list):
        return [_strip_volatile(item) for item in value]
    return value


def _replace_run_id(value, *run_ids: str):
    if isinstance(value, str):
        for run_id in run_ids:
            if run_id:
                value = value.replace(run_id, "<run-id>")
        return value
    if isinstance(value, dict):
        return {key: _replace_run_id(item, *run_ids) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_run_id(item, *run_ids) for item in value]
    return value


def _semantic_payload(path: Path, *run_ids: str):
    if path.suffix.lower() != ".json":
        return path.read_bytes()
    payload = json.loads(path.read_text(encoding="utf-8"))
    return _replace_run_id(_strip_volatile(payload), *run_ids)


def _comparable_run_files(run_dir: Path) -> list[str]:
    files: list[str] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(run_dir).as_posix()
        if relative in {"pipeline-state.json", "prepare-context.json"}:
            continue
        files.append(relative)
    return files


FR_CASE = "examples/flame-reave/case.legacy-hit.json"
FR_SAVE = ROOT / "data/raw/saves/2026-11-07/1CHARACTERSLOT_BETA_1"
OTHER_SAVE = ROOT / "data/raw/saves/2026-11-05/1CHARACTERSLOT_BETA_2"


class PrepareOverrideInputTests(unittest.TestCase):
    """P4-F2: CLI override inputs must contribute actual content state/sha to cache."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-f2-", dir=SCRATCH)
        self.tmp = Path(self._tmp.name)
        self.runs = self.tmp / "runs"
        self.cache_root = self.tmp / "cache"
        self.override_save = self.tmp / "override-save"
        self.override_save.write_bytes(FR_SAVE.read_bytes())

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _prepare(self, run_id: str, *extra: str, env: dict | None = None):
        return run_prepare(
            [
                "--case", FR_CASE,
                "--out", str(self.runs),
                "--run-id", run_id,
                "--main-skill", "fr11mv",
                "--save", str(self.override_save),
                "--cache-root", str(self.cache_root),
                *extra,
            ],
            env=env,
        )

    def _stage_results(self, run_id: str) -> dict[str, dict]:
        context = json.loads((self.runs / run_id / "prepare-context.json").read_text(encoding="utf-8"))
        return {entry["stage_id"]: entry for entry in context["stage_results"]}

    def test_same_path_invalid_content_invalidates_and_fails(self) -> None:
        code, out, err = self._prepare("f2-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("miss=8", out)
        # replace the *same path* content with an invalid save
        self.override_save.write_text("not-a-valid-save", encoding="utf-8")
        code, out, err = self._prepare("f2-invalid")
        self.assertNotEqual(code, le_commands.EXIT_OK, "无效存档不得复用旧缓存")
        self.assertIn("DecodeError", out + err)
        stages = self._stage_results("f2-invalid")
        self.assertEqual(stages["freeze_input"]["cache_status"], "miss")
        self.assertEqual(stages["parse_input"]["cache_status"], "miss")
        self.assertNotIn("hit=8", out)
        # --no-cache control also fails, and proves this is not cache-only behavior
        code_forced, out_forced, err_forced = self._prepare("f2-invalid-forced", "--no-cache")
        self.assertNotEqual(code_forced, le_commands.EXIT_OK)
        self.assertIn("DecodeError", out_forced + err_forced)

    def test_resume_invalidates_changed_override_before_state_skip(self) -> None:
        code, out, err = self._prepare("f2-resume-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.override_save.write_text("not-a-valid-save", encoding="utf-8")
        code, out, err = self._prepare("f2-resume-cold", "--resume")
        self.assertNotEqual(code, le_commands.EXIT_OK, "--resume 不得跳过已变更的覆盖输入")
        self.assertNotIn("state_cached=8", out)
        self.assertIn("DecodeError", out + err)
        stages = self._stage_results("f2-resume-cold")
        self.assertEqual(stages["freeze_input"]["cache_status"], "miss")
        self.assertEqual(stages["parse_input"]["cache_status"], "miss")

    def test_same_path_deleted_file_invalidates_and_fails(self) -> None:
        code, out, err = self._prepare("f2-deleted-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.override_save.unlink()
        code, out, err = self._prepare("f2-deleted")
        self.assertNotEqual(code, le_commands.EXIT_OK, "删除覆盖输入不得复用旧缓存")
        self.assertNotIn("hit=8", out)
        stages = self._stage_results("f2-deleted")
        self.assertEqual(stages["freeze_input"]["cache_status"], "miss")
        self.assertEqual(stages["parse_input"]["cache_status"], "miss")

    def test_same_path_valid_content_change_invalidates(self) -> None:
        code, out, err = self._prepare("f2-valid-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        cold_stages = self._stage_results("f2-valid-cold")
        cold_fp = cold_stages["freeze_input"]["input_fingerprint"]
        # same path, different valid EPOCH save bytes
        self.override_save.write_bytes(OTHER_SAVE.read_bytes())
        code, out, err = self._prepare("f2-valid-changed")
        stages = self._stage_results("f2-valid-changed")
        self.assertEqual(stages["freeze_input"]["cache_status"], "miss")
        self.assertEqual(stages["parse_input"]["cache_status"], "miss")
        self.assertNotEqual(stages["freeze_input"]["input_fingerprint"], cold_fp)
        self.assertNotIn("hit=8", out)


class PrepareOverrideInputRefsTest(unittest.TestCase):
    """P4-F2 unit: save/planner/stash share the same content-state strategy."""

    def test_all_override_kinds_contribute_content_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="p4-f2-refs-", dir=SCRATCH) as tmp:
            tmp_path = Path(tmp)
            files = {}
            for key in ("save", "planner", "stash"):
                path = tmp_path / f"override-{key}"
                path.write_bytes(f"{key}-v1".encode("utf-8"))
                files[key] = path
            params = {key: str(path) for key, path in files.items()}
            refs = le_commands._prepare_override_input_refs(params)
            self.assertEqual(
                sorted(ref["artifact_id"] for ref in refs),
                ["override.planner", "override.save", "override.stash"],
            )
            by_role = {ref["artifact_id"]: ref for ref in refs}
            self.assertTrue(all(ref["exists"] for ref in refs))
            def freeze_key(input_refs):
                request = _request("freeze_input", params={"case_ref": BH_CASE}, inputs=input_refs)
                return stage_cache.request_fingerprints(
                    request, ROOT, handler=stage.STAGES["freeze_input"]["handler"]
                )["cache_key"]
            base_key = freeze_key(refs)
            for key in ("save", "planner", "stash"):
                path = files[key]
                original = path.read_bytes()
                path.write_bytes(b"changed-content")
                changed = le_commands._prepare_override_input_refs(params)
                self.assertNotEqual(freeze_key(changed), base_key, key)
                path.write_bytes(original)
                path.unlink()
                missing = le_commands._prepare_override_input_refs(params)
                self.assertEqual(
                    {ref["artifact_id"]: ref for ref in missing}[f"override.{key}"]["sha256"],
                    "missing",
                )
                self.assertNotEqual(freeze_key(missing), base_key, key)
                path.mkdir()
                directory = le_commands._prepare_override_input_refs(params)
                self.assertEqual(
                    {ref["artifact_id"]: ref for ref in directory}[f"override.{key}"]["sha256"],
                    "directory",
                )
                self.assertNotEqual(freeze_key(directory), base_key, key)
                path.rmdir()
                path.write_bytes(original)


class PrepareForcedRerunTests(unittest.TestCase):
    """P4-F3: --resume must not bypass --no-cache / env-disabled force semantics."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="p4-f3-", dir=SCRATCH)
        self.tmp = Path(self._tmp.name)
        self.runs = self.tmp / "runs"
        self.cache_root = self.tmp / "cache"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _prepare(self, run_id: str, *extra: str, env: dict | None = None):
        return run_prepare(
            [
                "--case", BH_CASE,
                "--out", str(self.runs),
                "--run-id", run_id,
                "--main-skill", "bh2",
                "--cache-root", str(self.cache_root),
                *extra,
            ],
            env=env,
        )

    def test_resume_no_cache_forces_execution_and_refreshes(self) -> None:
        code, out, err = self._prepare("f3-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("miss=8", out)
        code, out, err = self._prepare("f3-cold", "--resume", "--no-cache")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("强制", out)
        self.assertIn("disabled=8", out)
        self.assertIn("state_cached=0", out)
        child_calls = re.search(r"子进程调用：(\d+) 次", out)
        self.assertIsNotNone(child_calls)
        self.assertGreater(int(child_calls.group(1)), 0)
        state = json.loads((self.runs / "f3-cold" / "pipeline-state.json").read_text(encoding="utf-8"))
        for entry in state["stages"]:
            self.assertEqual(entry["execution_status"], "completed")
            self.assertEqual(entry["cache_status"], "disabled")
        # cache entries were refreshed: a fresh run is warm again
        code, out, err = self._prepare("f3-warm-after")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("hit=8 miss=0", out)

    def test_env_disabled_resume_also_forces_execution(self) -> None:
        code, out, err = self._prepare("f3-env-cold")
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        code, out, err = self._prepare(
            "f3-env-cold", "--resume", env={"LE_PIPELINE_CACHE_DISABLE": "1"}
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("disabled=8", out)
        self.assertIn("state_cached=0", out)
        child_calls = re.search(r"子进程调用：(\d+) 次", out)
        self.assertIsNotNone(child_calls)
        self.assertGreater(int(child_calls.group(1)), 0)


if __name__ == "__main__":
    unittest.main()
