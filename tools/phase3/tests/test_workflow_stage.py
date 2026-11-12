"""Stage execution contract tests (P3)."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from le_calc.workflow import contract, stage

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
BH_CASE = "examples/black-hole/case.legacy.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def make_context(run_dir: Path, *, adapters=None) -> contract.StageContext:
    return contract.StageContext(repo_root=ROOT, output_root=run_dir, adapters=adapters or {})


def make_request(stage_id: str, params: dict, *, run_id: str = "stage-test") -> contract.StageRequest:
    return contract.StageRequest(
        stage_id=stage_id,
        case_id="black-hole",
        run_id=run_id,
        parameters=params,
    )


class RegistryTest(unittest.TestCase):
    def test_stage_ids_frozen_contract_order(self):
        self.assertEqual(
            stage.STAGE_IDS,
            (
                "freeze_input", "parse_input", "normalize_build", "export_calc_input",
                "apply_affix_ranges", "resolve_skills", "prepare_snapshot", "min_validate",
                "simulate", "validate_result", "render_report", "commit",
            ),
        )
        self.assertEqual(tuple(stage.STAGES), stage.STAGE_IDS)

    def test_stage_info_flags_all_stages_implemented(self):
        self.assertTrue(stage.stage_info("simulate")["implemented"])
        self.assertTrue(stage.stage_info("commit")["implemented"])
        self.assertTrue(stage.stage_info("resolve_skills")["implemented"])
        with self.assertRaises(KeyError):
            stage.stage_info("nope")

    def test_unknown_stage_fails_with_issue(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = stage.execute_stage(
                make_request("not_a_stage", {"case_ref": BH_CASE}), make_context(Path(tmp))
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertEqual(result.issues[0]["code"], "unknown_stage_id")
        self.assertEqual(result.cache["status"], contract.CACHE_DISABLED)
        self.assertEqual(result.to_dict()["schema_version"], contract.SCHEMA_STAGE_RESULT)

    def test_missing_case_ref_fails_structured(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = stage.execute_stage(make_request("freeze_input", {}), make_context(Path(tmp)))
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertEqual(result.issues[0]["code"], "missing_case_ref")
        self.assertEqual(result.readiness, contract.NEEDS_INPUT)

    def test_commit_requires_adapter(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = stage.execute_stage(
                make_request("commit", {"case_ref": BH_CASE}), make_context(Path(tmp))
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertEqual(result.issues[0]["code"], "commit_adapter_missing")
        self.assertIsNotNone(result.issues[0]["required_input"])


class FreezeInputTest(unittest.TestCase):
    def test_freeze_input_records_hashes_and_output_ref(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir = Path(tmp) / "run"
            result = stage.execute_stage(
                make_request("freeze_input", {"case_ref": BH_CASE}), make_context(run_dir)
            )
            payload = json.loads((run_dir / "input-freeze.json").read_text(encoding="utf-8"))
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertEqual(result.readiness, contract.READY)
        self.assertEqual(payload["schema_version"], "le-input-freeze-v1")
        self.assertTrue(payload["inputs"]["case"]["sha256"])
        self.assertTrue(payload["inputs"]["calculation_input"]["exists"])
        self.assertEqual(len(result.output_refs), 1)
        self.assertRegex(result.output_refs[0]["sha256"], r"^[0-9a-f]{64}$")
        self.assertGreater(result.metrics["elapsed_ms"], 0)

    def test_freeze_input_missing_file_becomes_needs_input(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            base = Path(tmp)
            case = json.loads((ROOT / BH_CASE).read_text(encoding="utf-8"))
            case["source"] = dict(case.get("source") or {})
            case["source"]["character_save"] = "examples/black-hole/does-not-exist.sav"
            (base / "case.json").write_text(json.dumps(case), encoding="utf-8")
            result = stage.execute_stage(
                make_request("freeze_input", {"case_ref": str(base / "case.json")}),
                make_context(base / "run"),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertEqual(result.readiness, contract.NEEDS_INPUT)
        self.assertIn("missing_input_files", [issue["code"] for issue in result.issues])


class ResolveSkillsTest(unittest.TestCase):
    def test_known_skill_writes_analysis_artifacts(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir = Path(tmp) / "run"
            result = stage.execute_stage(
                make_request("resolve_skills", {"case_ref": BH_CASE, "main_skill": "bh2"}),
                make_context(run_dir),
            )
            names = {path.name for path in run_dir.glob("*.json")}
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertIn(result.readiness, (contract.READY, contract.PARTIAL))
        self.assertTrue({"analysis-plan.json", "coverage.json", "research-queue.json"} <= names)

    def test_unknown_skill_completes_with_needs_research(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir = Path(tmp) / "run"
            result = stage.execute_stage(
                make_request("resolve_skills", {"case_ref": BH_CASE, "main_skill": "zz99"}),
                make_context(run_dir),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertEqual(result.readiness, contract.NEEDS_RESEARCH)
        self.assertIn("unknown_skill_mapping", [issue["code"] for issue in result.issues])
        self.assertTrue(any(path.endswith("analysis-plan.json") for path in
                            [ref["path"] for ref in result.output_refs]))

    def test_resolve_without_main_skill_needs_input(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = stage.execute_stage(
                make_request("resolve_skills", {"case_ref": BH_CASE}), make_context(Path(tmp))
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertEqual(result.issues[0]["code"], "missing_main_skill")


class OutputStageFailureTest(unittest.TestCase):
    def test_simulate_missing_input_fails_structured(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir = Path(tmp) / "run"
            result = stage.execute_stage(
                make_request(
                    "simulate",
                    {"case_ref": BH_CASE, "snapshot_ref": "examples/black-hole/missing.json"},
                ),
                make_context(run_dir),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertTrue(result.issues)

    def test_validate_missing_run_dir_reports_failure(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir = Path(tmp) / "run"
            result = stage.execute_stage(
                make_request("validate_result", {"case_ref": BH_CASE}),
                make_context(run_dir),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertIn("validation_failed", [issue["code"] for issue in result.issues])

    def test_render_report_waits_without_html(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            result = stage.execute_stage(
                make_request("render_report", {"case_ref": BH_CASE}),
                make_context(Path(tmp) / "run"),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_WAITING)
        self.assertEqual(result.issues[0]["code"], "report_not_rendered")


class AdapterInjectionTest(unittest.TestCase):
    def test_run_child_adapter_is_used_and_captured(self):
        calls: list[dict] = []

        def fake_runner(cmd, *, pythonpath=(), cwd=None):
            calls.append({"cmd": [str(part) for part in cmd], "pythonpath": pythonpath, "cwd": cwd})
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            base = Path(tmp)
            steps = base / "run" / "steps" / "01-save-import"
            steps.mkdir(parents=True)
            (steps / "bd.json").write_text("{}", encoding="utf-8")
            result = stage.execute_stage(
                make_request("parse_input", {"case_ref": BH_CASE}),
                make_context(base / "run", adapters={"run_child": fake_runner}),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertEqual(len(calls), 1)
        self.assertIn("le_save_import", calls[0]["cmd"])
        self.assertIn("--save", calls[0]["cmd"])



class HitSnapshotUnknownSkillTest(unittest.TestCase):
    """A *known* gap (unbound skill template) must not be attempted for hit snapshots."""

    def _run_dir_with_plan(self, tmp: Path, template) -> tuple[Path, dict]:
        run_dir = Path(tmp) / "run"
        run_dir.mkdir(parents=True)
        plan = {
            "schema_version": "le-analysis-plan-v1",
            "main_skill": {"ability_id": "zz99", "model_id": "hit"},
            "skill_template": template,
        }
        (run_dir / "analysis-plan.json").write_text(
            json.dumps(plan), encoding="utf-8"
        )
        return run_dir, plan

    def test_hit_unknown_template_returns_waiting_with_required_input(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir, _ = self._run_dir_with_plan(tmp, None)
            result = stage.execute_stage(
                make_request(
                    "prepare_snapshot",
                    {"case_ref": "examples/flame-reave/case.legacy-hit.json", "main_skill": "zz99"},
                ),
                make_context(run_dir),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_WAITING)
        self.assertEqual(result.readiness, contract.NEEDS_RESEARCH)
        issue = result.issues[0]
        self.assertEqual(issue["code"], "skill_mapping_required")
        self.assertEqual(issue["required_input"]["kind"], "skill_mapping")
        self.assertEqual(issue["required_input"]["ability_id"], "zz99")
        self.assertEqual(result.output_refs, [])

    def test_event_model_unknown_template_keeps_existing_behaviour(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir, _ = self._run_dir_with_plan(tmp, None)
            result = stage.execute_stage(
                make_request(
                    "prepare_snapshot",
                    {"case_ref": BH_CASE, "main_skill": "zz99"},
                ),
                make_context(run_dir),
            )
        # event-model snapshots do not need the specialization: no short-circuit,
        # the real (missing upstream input) failure is reported as failed.
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertIn(
            "missing_affix_output", [issue["code"] for issue in result.issues]
        )

    def test_hit_known_template_real_failure_stays_failed(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            run_dir, _ = self._run_dir_with_plan(tmp, {"skill_id": "fr-flame-reave"})
            result = stage.execute_stage(
                make_request(
                    "prepare_snapshot",
                    {"case_ref": "examples/flame-reave/case.legacy-hit.json", "main_skill": "fr11mv"},
                ),
                make_context(run_dir),
            )
        self.assertEqual(result.execution_status, contract.EXECUTION_FAILED)
        self.assertIn(
            "missing_affix_output", [issue["code"] for issue in result.issues]
        )


    def test_parse_input_normalises_step_artifacts(self):
        """D3: prepare step artefacts get the same portable normalisation as reproduce."""

        def fake_runner(cmd, *, pythonpath=(), cwd=None):
            return {"returncode": 0, "stdout": "", "stderr": ""}

        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            base = Path(tmp)
            steps = base / "run" / "steps" / "01-save-import"
            steps.mkdir(parents=True)
            (steps / "bd.json").write_text(
                json.dumps(
                    {
                        "provenance": {
                            "generator_tool": (
                                "lastepochsupertools_v1.3.1 (user-provided local save/build tool; "
                                "<game-install>\\Editor\\tool)"
                            )
                        }
                    }
                ),
                encoding="utf-8",
            )
            result = stage.execute_stage(
                make_request("parse_input", {"case_ref": BH_CASE}),
                make_context(base / "run", adapters={"run_child": fake_runner}),
            )
            cleaned = json.loads((steps / "bd.json").read_text(encoding="utf-8"))
        tool = cleaned["provenance"]["generator_tool"]
        self.assertEqual(result.execution_status, contract.EXECUTION_COMPLETED)
        self.assertIn("<external-path>", tool)
        self.assertNotRegex(tool, r"[A-Za-z]:[\\\\/]")
        self.assertGreater(result.metrics.get("portable_replacements", 0), 0)
        self.assertEqual(result.metrics.get("portable_files"), 1)


if __name__ == "__main__":
    unittest.main()
