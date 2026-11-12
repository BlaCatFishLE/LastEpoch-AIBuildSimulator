"""``le.ps1 prepare`` pipeline tests (P3)."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
SCRIPTS = str(ROOT / "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)
CALC_SRC = str(ROOT / "tools" / "phase3" / "src")
if CALC_SRC not in sys.path:
    sys.path.insert(0, CALC_SRC)

import le_commands  # noqa: E402

BH_CASE = "examples/black-hole/case.legacy.json"
FR_CASE = "examples/flame-reave/case.legacy-hit.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def run_prepare(argv: list[str]) -> tuple[int, str, str]:
    parser = le_commands.build_parser()
    args = parser.parse_args(["prepare", *argv])
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = args.func(args)
    return code, out.getvalue(), err.getvalue()


def run_dir_for(tmp: Path, run_id: str) -> Path:
    return tmp / run_id


_DRIVE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\\\/]")
_UNC_RE = re.compile(r"\\\\[A-Za-z0-9._-]+\\")


def _machine_paths_in(value, prefix="") -> list[str]:
    """Return JSON paths of string values that look like machine absolute paths."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_machine_paths_in(item, f"{prefix}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_machine_paths_in(item, f"{prefix}/{index}"))
    elif isinstance(value, str):
        if _DRIVE_RE.search(value) or _UNC_RE.search(value):
            found.append(f"{prefix} = {value[:120]}")
    return found


def assert_no_machine_paths(testcase: unittest.TestCase, run_dir: Path) -> None:
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".txt", ".html"}:
            continue
        if path.suffix.lower() == ".json":
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            hits = _machine_paths_in(document)
            testcase.assertEqual(hits, [], f"{path}: {hits[:3]}")
        else:
            text = path.read_text(encoding="utf-8")
            testcase.assertIsNone(_DRIVE_RE.search(text), f"{path} contains a drive path")


class PrepareTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix="p3-prepare-test-", dir=SCRATCH)
        cls.out_root = Path(cls.tmp.name)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_prepare_black_hole(self):
        run_id = "test-bh"
        code, out, err = run_prepare(
            ["--case", BH_CASE, "--out", str(self.out_root), "--run-id", run_id,
             "--main-skill", "bh2"]
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        run_dir = run_dir_for(self.out_root, run_id)
        for name in ("analysis-plan.json", "coverage.json", "research-queue.json",
                     "pipeline-state.json", "prepare-context.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        plan = json.loads((run_dir / "analysis-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(len(plan["checklist"]), 9)
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["schema_version"], "le-pipeline-state-v1")
        self.assertEqual(
            [entry["stage_id"] for entry in state["stages"]],
            list(le_commands._PREPARE_STAGES),
        )
        context = json.loads((run_dir / "prepare-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["schema_version"], "le-prepare-context-v1")
        # kh-fr's D2 route model adds the combined ``save+planner`` route for BH
        self.assertIn(
            context["route"], ("save-tool-native-wire", "planner-payload", "save+planner")
        )
        # D3: no machine absolute paths in prepare step artefacts / run artefacts
        assert_no_machine_paths(self, run_dir)

    def test_prepare_flame_reave(self):
        run_id = "test-fr"
        code, out, err = run_prepare(
            ["--case", FR_CASE, "--out", str(self.out_root), "--run-id", run_id,
             "--main-skill", "fr11mv"]
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        run_dir = run_dir_for(self.out_root, run_id)
        self.assertTrue((run_dir / "analysis-plan.json").is_file())
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["stages"]), len(le_commands._PREPARE_STAGES))
        # D3: bd.json (and sibling step artefacts) share reproduce's normalisation
        assert_no_machine_paths(self, run_dir)
        bd = json.loads(
            (run_dir / "steps" / "01-save-import" / "bd.json").read_text(encoding="utf-8")
        )
        generator = (bd.get("provenance") or {}).get("generator_tool") or ""
        self.assertIn("<external-path>", generator)
        self.assertNotRegex(generator, r"[A-Za-z]:[\\\\/]")
        reproduce_bds = sorted(
            (ROOT / "reports" / "flame-reave").glob("*/steps/01-save-import/bd.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        reproduce_generators = [
            ((json.loads(path.read_text(encoding="utf-8")).get("provenance") or {}).get(
                "generator_tool"
            ))
            for path in reproduce_bds
        ]
        if reproduce_generators:
            self.assertIn(generator, reproduce_generators)

    def test_prepare_unknown_skill_completes_data_preparation(self):
        case = json.loads((ROOT / BH_CASE).read_text(encoding="utf-8"))
        case["case_id"] = "unknown-skill-case"
        case["_path"] = None
        case_path = self.out_root / "unknown-case.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        run_id = "test-unknown"
        code, out, err = run_prepare(
            ["--case", str(case_path), "--out", str(self.out_root), "--run-id", run_id,
             "--main-skill", "zz99"]
        )
        self.assertEqual(code, le_commands.EXIT_PARTIAL, out + err)
        run_dir = run_dir_for(self.out_root, run_id)
        self.assertTrue((run_dir / "analysis-plan.json").is_file())
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        by_stage = {entry["stage_id"]: entry for entry in state["stages"]}
        self.assertEqual(by_stage["resolve_skills"]["readiness"], "needs_research")
        # event model (BH) keeps its existing behaviour: snapshot + min_validate run
        self.assertEqual(by_stage["prepare_snapshot"]["execution_status"], "completed")
        self.assertEqual(by_stage["min_validate"]["execution_status"], "completed")

    def test_resume_rebuilds_invalidated_stage_and_downstream(self):
        run_id = "test-bh"
        run_dir = run_dir_for(self.out_root, run_id)
        if not run_dir.is_dir():
            self.skipTest("BH prepare run not created")
        (run_dir / "steps" / "03-calculation-input.affix-db.json").unlink()
        code, out, err = run_prepare(
            ["--case", BH_CASE, "--out", str(self.out_root), "--run-id", run_id,
             "--main-skill", "bh2", "--resume"]
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        self.assertIn("cached", out)
        self.assertIn("apply_affix_ranges", out)
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        statuses = {entry["stage_id"]: entry["execution_status"] for entry in state["stages"]}
        for stage_id in le_commands._PREPARE_STAGES:
            self.assertEqual(statuses[stage_id], "completed", stage_id)
        # all recorded outputs must still exist
        for entry in state["stages"]:
            for ref in entry["output_refs"]:
                self.assertTrue((ROOT / ref["path"]).is_file(), ref["path"])


    def test_prepare_hit_unknown_skill_waits_and_keeps_data_preparation(self):
        case = json.loads((ROOT / FR_CASE).read_text(encoding="utf-8"))
        case["case_id"] = "p3-test-unknown-hit"
        case.pop("_path", None)
        case_path = self.out_root / "unknown-hit-case.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
        run_id = "test-unknown-hit"
        code, out, err = run_prepare(
            ["--case", str(case_path), "--out", str(self.out_root), "--run-id", run_id,
             "--main-skill", "zz99"]
        )
        self.assertEqual(code, le_commands.EXIT_PARTIAL, out + err)
        self.assertIn("PARTIAL", out)
        run_dir = run_dir_for(self.out_root, run_id)
        # earlier data preparation + analysis artifacts are complete
        for name in ("analysis-plan.json", "coverage.json", "research-queue.json",
                     "pipeline-state.json"):
            self.assertTrue((run_dir / name).is_file(), name)
        self.assertTrue((run_dir / "steps" / "01-save-import" / "bd.json").is_file())
        self.assertTrue(
            (run_dir / "steps" / "03-calculation-input.affix-db.json").is_file()
        )
        # snapshot is a known mapping gap: not executed, not reported as failure
        self.assertFalse((run_dir / "steps" / "05-input-snapshot.hit.json").exists())
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        by_stage = {entry["stage_id"]: entry for entry in state["stages"]}
        self.assertEqual(by_stage["prepare_snapshot"]["execution_status"], "waiting")
        self.assertEqual(by_stage["prepare_snapshot"]["readiness"], "needs_research")
        self.assertNotIn("min_validate", by_stage)
        self.assertEqual(by_stage["resolve_skills"]["readiness"], "needs_research")
        self.assertIn(
            {"kind": "skill_mapping", "ability_id": "zz99", "artifact_hint": "knowledge/skills/"},
            state["pending_input"],
        )
        plan = json.loads((run_dir / "analysis-plan.json").read_text(encoding="utf-8"))
        self.assertIsNone(plan["skill_template"])
        self.assertEqual(plan["main_skill"]["ability_id"], "zz99")
        # never fake support: no result/snapshot claim for the unknown skill
        self.assertFalse((run_dir / "result.json").exists())

    def test_prepare_without_main_skill_returns_needs_input(self):
        run_id = "test-no-skill"
        code, out, err = run_prepare(
            ["--case", BH_CASE, "--out", str(self.out_root), "--run-id", run_id]
        )
        self.assertEqual(code, le_commands.EXIT_PARTIAL, out + err)
        self.assertIn("needs_input", out)
        self.assertIn("--main-skill bh2", out)
        # gate runs before any stage/output directory creation: no snapshot, no fake plan
        self.assertFalse(run_dir_for(self.out_root, run_id).exists())

    def test_prepare_flame_reave_uses_case_declared_main_skill(self):
        run_id = "test-fr-declared"
        code, out, err = run_prepare(
            ["--case", FR_CASE, "--out", str(self.out_root), "--run-id", run_id]
        )
        self.assertEqual(code, le_commands.EXIT_OK, out + err)
        run_dir = run_dir_for(self.out_root, run_id)
        context = json.loads((run_dir / "prepare-context.json").read_text(encoding="utf-8"))
        self.assertEqual(context["main_skill"], "fr11mv")
        self.assertEqual(context["main_skill_source"], "case")
        state = json.loads((run_dir / "pipeline-state.json").read_text(encoding="utf-8"))
        self.assertEqual(len(state["stages"]), len(le_commands._PREPARE_STAGES))

    def test_resume_requires_run_id(self):
        code, out, err = run_prepare(["--case", BH_CASE, "--out", str(self.out_root), "--resume"])
        self.assertEqual(code, le_commands.EXIT_ARGS)
        self.assertIn("--resume", err + out)

    def test_doctor_dependency_check_reports_needs_input_without_inference(self):
        """D1: doctor/check_routes keep needs_input; the CLI must not infer a value."""
        if not (ROOT / "scripts" / "le_pipeline").is_dir():
            self.skipTest("le_pipeline not delivered")
        routes = mock.Mock()
        routes.list_routes.return_value = [{"route_id": "fake-route"}]
        routes.resolve_route_detail.return_value = {
            "route": {"route_id": "fake-route", "label": "fake", "stage_sequence": []}
        }
        routes.route_to_dict.side_effect = lambda route: route
        dependencies = mock.Mock()
        dependencies.check_routes.return_value = {
            "route_id": "fake-route",
            "ready": False,
            "needs_input": [
                {
                    "code": "main_skill_required",
                    "kind": "local_config",
                    "dependency_ids": ["input.main_skill"],
                    "required": True,
                }
            ],
            "dependencies": [],
            "notes": "main skill required",
        }
        with mock.patch.object(
            le_commands, "_pipeline_modules", return_value=(routes, dependencies)
        ):
            checks = le_commands._pipeline_dependency_checks(
                {"case_id": "black-hole", "model": "black-hole"}
            )
        dep_checks = [check for check in checks if check["name"] == "解析依赖检查"]
        self.assertTrue(dep_checks)
        check = dep_checks[0]
        self.assertIn(check["status"], ("INFO", "FAIL"))
        self.assertIn("needs_input", check["detail"])
        self.assertIn("main_skill_required", check["detail"])


if __name__ == "__main__":
    unittest.main()
