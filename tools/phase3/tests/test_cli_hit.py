# -*- coding: utf-8 -*-
"""Tests for the Flame Reave ``hit`` CLI surface (``le_calc.cli`` hit subcommands).

Run from the repository root::

    $env:PYTHONPATH = 'tools\\phase3\\src'
    & .\\.venv\\Scripts\\python.exe -m unittest discover -s tools/phase3/tests -p 'test_cli_hit.py'

The fr-engine modules are mocked: these tests cover argparse wiring, dispatch,
exit codes and the boundary between ``cli`` and ``hit_report`` without needing
the real engine to be importable.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from le_calc import cli, hit_report

try:  # unittest discovery puts tools/phase3/tests on sys.path
    import test_hit_report as fixtures
except ImportError:  # pragma: no cover - direct invocation fallback
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_hit_report as fixtures


def _namespace(**values: object) -> argparse.Namespace:
    return argparse.Namespace(**values)


def _fake_engine(
    *,
    single_hit: object = None,
    simulation: object = None,
    screening: object = None,
    ruleset: object = None,
    compute_error: Exception | None = None,
) -> tuple[SimpleNamespace, dict]:
    calls: dict = {}

    def build_snapshot(case, **kwargs):
        calls["build_snapshot"] = {"case": case, **kwargs}
        return fixtures.snapshot_fixture()

    def load_ruleset(path):
        calls["load_ruleset"] = path
        return ruleset if ruleset is not None else fixtures.ruleset_fixture()

    def compute_single_hit(snapshot, loaded, **kwargs):
        calls["compute_single_hit"] = {"snapshot": snapshot, "ruleset": loaded, **kwargs}
        calls.setdefault("history", []).append(
            {"function": "compute_single_hit", "scenario": kwargs.get("scenario")}
        )
        if compute_error is not None:
            raise compute_error
        return (
            single_hit
            if single_hit is not None
            else fixtures.engine_outputs_fixture()["single_hit"]
        )

    def simulate(snapshot, loaded, **kwargs):
        calls["simulate"] = {"snapshot": snapshot, "ruleset": loaded, **kwargs}
        calls.setdefault("history", []).append(
            {"function": "simulate", "scenario": kwargs.get("scenario")}
        )
        return (
            simulation
            if simulation is not None
            else fixtures.engine_outputs_fixture()["simulate"]
        )

    def screen_buffs(snapshot, loaded, **kwargs):
        calls["screen_buffs"] = {"snapshot": snapshot, "ruleset": loaded, **kwargs}
        return (
            screening
            if screening is not None
            else fixtures.engine_outputs_fixture()["buff_screening"]
        )

    modules = SimpleNamespace(
        hit_inputs=SimpleNamespace(build_snapshot=build_snapshot),
        hit_ruleset=SimpleNamespace(load_ruleset=load_ruleset),
        hit_damage=SimpleNamespace(compute_single_hit=compute_single_hit),
        hit_simulate=SimpleNamespace(simulate=simulate),
        hit_buffs=SimpleNamespace(screen_buffs=screen_buffs),
        hit_report=hit_report,
    )
    return modules, calls


class HitArgparseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = cli.build_parser()

    def test_hit_snapshot_arguments(self) -> None:
        args = self.parser.parse_args(
            [
                "hit-snapshot",
                "--case",
                "examples/flame-reave/case.legacy-hit.json",
                "--calc-input",
                "examples/flame-reave/inputs/calculation-input.json",
                "--out",
                "examples/flame-reave/snapshot/input-snapshot.hit.json",
            ]
        )
        self.assertIs(args.func, cli.cmd_hit_snapshot)
        self.assertEqual(args.case, "examples/flame-reave/case.legacy-hit.json")
        self.assertEqual(args.calc_input, "examples/flame-reave/inputs/calculation-input.json")
        self.assertEqual(args.out, "examples/flame-reave/snapshot/input-snapshot.hit.json")
        self.assertIsNone(args.dataset)
        self.assertIsNone(args.repo_root)

    def test_hit_snapshot_calc_input_alias(self) -> None:
        args = self.parser.parse_args(
            [
                "hit-snapshot",
                "--case",
                "case.json",
                "--calculation-input",
                "calc.json",
                "--out",
                "snap.json",
            ]
        )
        self.assertEqual(args.calc_input, "calc.json")

    def test_hit_run_arguments(self) -> None:
        args = self.parser.parse_args(
            [
                "hit-run",
                "--case",
                "examples/flame-reave/case.legacy-hit.json",
                "--snapshot",
                "examples/flame-reave/snapshot/input-snapshot.hit.json",
                "--ruleset",
                "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json",
                "--out",
                "reports/flame-reave/run-1",
                "--run-id",
                "run-1",
                "--scenario",
                "examples/flame-reave/scenario/conditional-scenario.json",
                "--no-html",
                "--json",
            ]
        )
        self.assertIs(args.func, cli.cmd_hit_run)
        self.assertEqual(args.run_id, "run-1")
        self.assertTrue(args.no_html)
        self.assertTrue(args.json)
        self.assertIn("conditional-scenario", args.scenario)

    def test_hit_run_required_arguments(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["hit-run", "--case", "c.json"])

    def test_hit_snapshot_requires_case_and_out(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(["hit-snapshot", "--case", "c.json"])

    def test_existing_bh_commands_unchanged(self) -> None:
        """The hit subcommands must not change the existing BH command surface."""
        run_args = self.parser.parse_args(["run", "--out", "x"])
        self.assertIs(run_args.func, cli.cmd_run)
        self.assertEqual(run_args.out, "x")
        self.assertIs(self.parser.parse_args(["conditions", "--snapshot", "s", "--out", "o"]).func, cli.cmd_conditions)
        self.assertIs(self.parser.parse_args(["check"]).func, cli.cmd_check)
        self.assertIs(self.parser.parse_args(["serve", "--snapshot", "s"]).func, cli.cmd_serve)
        choices = self.parser._subparsers._group_actions[0].choices  # type: ignore[attr-defined]
        self.assertEqual(set(choices), {"run", "conditions", "check", "serve", "hit-snapshot", "hit-run", "hit-serve"})


class HitCommandDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hit-cli-")
        self.root = Path(self._tmp.name)
        self.case_path = self.root / "case.json"
        self.case_path.write_text(json.dumps(fixtures.case_fixture()), encoding="utf-8")
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(json.dumps(fixtures.snapshot_fixture()), encoding="utf-8")
        self.ruleset_path = self.root / "ruleset.json"
        self.ruleset_path.write_text(json.dumps(fixtures.ruleset_fixture()), encoding="utf-8")
        self.out_dir = self.root / "run"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _patch_engine(self, modules):
        patcher = unittest.mock.patch.object(cli, "_hit_modules", return_value=modules)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _snapshot_args(self, out: Path) -> argparse.Namespace:
        return _namespace(
            case=str(self.case_path),
            calc_input=str(self.root / "calc-input.json"),
            dataset=None,
            repo_root=None,
            out=str(out),
        )

    def _run_args(self, *, out: Path | None = None, scenario: str | None = None) -> argparse.Namespace:
        return _namespace(
            case=str(self.case_path),
            snapshot=str(self.snapshot_path),
            ruleset=str(self.ruleset_path),
            out=str(out or self.out_dir),
            run_id="cli-run",
            scenario=scenario,
            generated_at="2026-11-07T00:00:00+00:00",
            no_html=False,
            json=False,
        )

    # -- hit-snapshot ---------------------------------------------------

    def test_hit_snapshot_writes_snapshot(self) -> None:
        modules, calls = _fake_engine()
        self._patch_engine(modules)
        out = self.root / "snapshot-out.json"
        with redirect_stdout(io.StringIO()) as stdout:
            code = cli.cmd_hit_snapshot(self._snapshot_args(out))
        self.assertEqual(code, 0)
        document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["derived"]["model"], "hit")
        self.assertEqual(document["build_meta"]["build"]["build_id"], "letools:toolnative-fr-save")
        self.assertEqual(calls["build_snapshot"]["case"], str(self.case_path))
        self.assertIn("hit snapshot", stdout.getvalue())

    def test_hit_snapshot_engine_unavailable_returns_2(self) -> None:
        self._patch_engine(None)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.cmd_hit_snapshot(self._snapshot_args(self.root / "snap.json"))
        self.assertEqual(code, 2)
        self.assertFalse((self.root / "snap.json").exists())

    # -- hit-run --------------------------------------------------------

    def test_hit_run_writes_complete_run(self) -> None:
        modules, calls = _fake_engine()
        self._patch_engine(modules)
        with redirect_stdout(io.StringIO()) as stdout:
            code = cli.cmd_hit_run(self._run_args())
        self.assertEqual(code, 0)
        for name in (
            "result.json",
            "input-snapshot.json",
            "source-ledger.json",
            "rule-ledger.json",
            "buff-screening.json",
            "flame-reave-damage.html",
            "README.zh-CN.md",
            "run-manifest.json",
        ):
            self.assertTrue((self.out_dir / name).is_file(), name)
        result = json.loads((self.out_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["schema"], "le-hit-result-v1")
        self.assertEqual(result["run"]["run_id"], "cli-run")
        self.assertEqual(result["run"]["status"], "assumption")
        self.assertEqual(result["consistency"]["all_pass"], True)
        manifest = json.loads((self.out_dir / "run-manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["consistency"]["all_pass"])
        self.assertIn("hit run: cli-run", stdout.getvalue())
        # engine got a model.Snapshot + the loaded ruleset
        from le_calc import model

        self.assertIsInstance(calls["compute_single_hit"]["snapshot"], model.Snapshot)
        self.assertEqual(calls["compute_single_hit"]["ruleset"]["ruleset_id"], "fr-hit-stage1-v2")
        # primary call first, then the baseline / sensitivity variants computed for the report
        history = calls.get("history") or []
        self.assertGreater(len(history), 1)
        primary_calls = [item for item in history if item["function"] == "compute_single_hit"]
        self.assertIsNone(primary_calls[0]["scenario"])
        self.assertTrue(any(item["scenario"] is not None for item in primary_calls[1:]))

    def test_hit_run_passes_scenario_to_engine(self) -> None:
        modules, calls = _fake_engine()
        self._patch_engine(modules)
        scenario = self.root / "scenario.json"
        scenario.write_text(
            json.dumps(
                {
                    "scenario_id": "ignited",
                    "states": {"ignited": True},
                    "target": {"monster_level": 100},
                }
            ),
            encoding="utf-8",
        )
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_hit_run(self._run_args(scenario=str(scenario)))
        self.assertEqual(code, 0)
        self.assertEqual(calls["compute_single_hit"]["scenario"]["scenario_id"], "ignited")
        result = json.loads((self.out_dir / "result.json").read_text(encoding="utf-8"))
        states = {row["state"]: row for row in result["buffs"]["applied"]}
        self.assertTrue(states["ignited"]["active"])

    def test_hit_run_tolerates_engine_without_scenario_parameter(self) -> None:
        modules, _ = _fake_engine()

        def old_style(snapshot, ruleset):  # intentionally no scenario kwarg
            return fixtures.engine_outputs_fixture()["single_hit"]

        modules.hit_damage = SimpleNamespace(compute_single_hit=old_style)
        self._patch_engine(modules)
        scenario = self.root / "scenario.json"
        scenario.write_text(json.dumps({"states": {"ignited": True}}), encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_hit_run(self._run_args(scenario=str(scenario)))
        self.assertEqual(code, 0)

    def test_hit_run_engine_unavailable_returns_2(self) -> None:
        self._patch_engine(None)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = cli.cmd_hit_run(self._run_args())
        self.assertEqual(code, 2)
        self.assertFalse((self.out_dir / "result.json").exists())

    def test_hit_run_compute_failure_returns_1(self) -> None:
        modules, _ = _fake_engine(compute_error=RuntimeError("engine boom"))
        self._patch_engine(modules)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            code = cli.cmd_hit_run(self._run_args())
        self.assertEqual(code, 1)
        self.assertIn("engine boom", stderr.getvalue())

    def test_hit_run_consistency_failure_returns_2(self) -> None:
        outputs = fixtures.engine_outputs_fixture()
        broken_single = outputs["single_hit"]
        broken_single["totals"]["damage"] = 9999.0
        modules, _ = _fake_engine(single_hit=broken_single)
        self._patch_engine(modules)
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_hit_run(self._run_args())
        self.assertEqual(code, 2)
        manifest = json.loads((self.out_dir / "run-manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["consistency"]["all_pass"])

    def test_hit_run_no_html(self) -> None:
        modules, _ = _fake_engine()
        self._patch_engine(modules)
        args = self._run_args()
        args.no_html = True
        with redirect_stdout(io.StringIO()):
            code = cli.cmd_hit_run(args)
        self.assertEqual(code, 0)
        self.assertFalse((self.out_dir / "flame-reave-damage.html").exists())
        self.assertTrue((self.out_dir / "result.json").is_file())

    def test_hit_run_json_output(self) -> None:
        modules, _ = _fake_engine()
        self._patch_engine(modules)
        args = self._run_args()
        args.json = True
        with redirect_stdout(io.StringIO()) as stdout:
            code = cli.cmd_hit_run(args)
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["summary"]["run_id"], "cli-run")
        self.assertEqual(payload["summary"]["status"], "assumption")
        self.assertIn("result.json", payload["written"])

    def test_hit_run_invalid_snapshot_returns_1(self) -> None:
        modules, _ = _fake_engine()
        self._patch_engine(modules)
        broken = fixtures.snapshot_fixture()
        broken["contributions"][0]["operation"] = "bogus-operation"
        self.snapshot_path.write_text(json.dumps(broken), encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            code = cli.cmd_hit_run(self._run_args())
        self.assertEqual(code, 1)
        self.assertIn("hit-run 计算失败", stderr.getvalue())


class HitMainDispatchTests(unittest.TestCase):
    def test_main_dispatches_hit_snapshot(self) -> None:
        modules, _ = _fake_engine()
        with tempfile.TemporaryDirectory(prefix="hit-main-") as tmp:
            root = Path(tmp)
            case_path = root / "case.json"
            case_path.write_text(json.dumps(fixtures.case_fixture()), encoding="utf-8")
            out = root / "snapshot.json"
            with unittest.mock.patch.object(cli, "_hit_modules", return_value=modules):
                with redirect_stdout(io.StringIO()):
                    code = cli.main(
                        [
                            "hit-snapshot",
                            "--case",
                            str(case_path),
                            "--calc-input",
                            str(root / "calc.json"),
                            "--out",
                            str(out),
                        ]
                    )
            self.assertEqual(code, 0)
            self.assertTrue(out.is_file())

    def test_main_dispatches_hit_run(self) -> None:
        modules, _ = _fake_engine()
        with tempfile.TemporaryDirectory(prefix="hit-main-") as tmp:
            root = Path(tmp)
            case_path = root / "case.json"
            case_path.write_text(json.dumps(fixtures.case_fixture()), encoding="utf-8")
            snapshot_path = root / "snapshot.json"
            snapshot_path.write_text(json.dumps(fixtures.snapshot_fixture()), encoding="utf-8")
            ruleset_path = root / "ruleset.json"
            ruleset_path.write_text(json.dumps(fixtures.ruleset_fixture()), encoding="utf-8")
            out = root / "run"
            with unittest.mock.patch.object(cli, "_hit_modules", return_value=modules):
                with redirect_stdout(io.StringIO()):
                    code = cli.main(
                        [
                            "hit-run",
                            "--case",
                            str(case_path),
                            "--snapshot",
                            str(snapshot_path),
                            "--ruleset",
                            str(ruleset_path),
                            "--out",
                            str(out),
                            "--run-id",
                            "main-run",
                        ]
                    )
            self.assertEqual(code, 0)
            result = json.loads((out / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["run"]["run_id"], "main-run")


class LeCommandsHitDispatchTests(unittest.TestCase):
    """``scripts/le_commands.py`` must dispatch ``case.model == "hit"`` without
    touching the Black Hole branch."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from scripts import le_commands  # type: ignore
        except Exception as exc:  # pragma: no cover - scripts not importable
            raise unittest.SkipTest(f"scripts/le_commands.py not importable: {exc}")
        cls.le_commands = le_commands

    def _case(self, model: str = "hit") -> dict:
        from pathlib import Path

        root = Path(__file__).resolve().parents[3]
        case = fixtures.case_fixture()
        case["model"] = model
        case["inputs"] = {
            "bd": "examples/flame-reave/inputs/bd.json",
            "calculation_input": "examples/flame-reave/inputs/calculation-input.affix-db.corrected.json",
            "snapshot": "examples/flame-reave/snapshot/input-snapshot.hit.json",
            "scenario": None,
        }
        case["ruleset"] = {
            "id": "fr-hit-stage1-v2",
            "path": "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json",
            "sha256": None,
        }
        case["_root"] = root
        case["_path"] = root / "examples" / "flame-reave" / "case.json"
        case["_dir"] = case["_path"].parent
        return case

    def test_case_model_selector(self) -> None:
        self.assertEqual(self.le_commands._case_model({"model": "hit"}), "hit")
        self.assertEqual(self.le_commands._case_model({"model": "HIT"}), "hit")
        self.assertEqual(self.le_commands._case_model({}), "black-hole")
        self.assertEqual(self.le_commands._case_model({"model": None}), "black-hole")

    def test_run_dispatches_hit_case(self) -> None:
        case = self._case()
        args = argparse.Namespace(case="case.json", out=None, run_id="hit-run-1", out_root=None)
        captured: dict = {}

        def fake_run_hit_case(case_arg, **kwargs):
            captured["case"] = case_arg
            captured.update(kwargs)
            return 0

        with unittest.mock.patch.object(self.le_commands, "_load_case", return_value=case), \
                unittest.mock.patch.object(self.le_commands, "_run_hit_case", side_effect=fake_run_hit_case):
            code = self.le_commands.cmd_run(args)
        self.assertEqual(code, 0)
        self.assertIs(captured["case"], case)
        self.assertEqual(captured["run_id"], "hit-run-1")
        self.assertTrue(str(captured["out_dir"]).replace("\\", "/").endswith("reports/flame-reave/hit-run-1"))
        self.assertIn("snapshot", captured)
        self.assertIn("ruleset", captured)

    def test_run_hit_case_missing_ruleset_fails_without_output_dir(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="le-hit-") as tmp:
            root = Path(tmp)
            out_dir = root / "run"
            code = self.le_commands._run_hit_case(
                self._case(),
                run_id="hit-run-2",
                out_dir=out_dir,
                snapshot=root / "snapshot.json",
                calc_input=root / "calc.json",
                ruleset=root / "ruleset.json",
            )
        self.assertEqual(code, self.le_commands.EXIT_FAIL)
        self.assertFalse(out_dir.exists())

    def test_validate_dispatches_hit_checks(self) -> None:
        case = self._case()
        args = argparse.Namespace(case="case.json")
        checks = [
            {"name": "案例 model", "status": "PASS", "detail": "hit"},
            {"name": "hit 快照", "status": "SKIP", "detail": "尚未生成"},
        ]
        with unittest.mock.patch.object(self.le_commands, "_load_case", return_value=case), \
                unittest.mock.patch.object(self.le_commands, "_hit_case_checks", return_value=checks) as hit_checks, \
                unittest.mock.patch.object(self.le_commands, "_case_checks") as bh_checks:
            with redirect_stdout(io.StringIO()):
                code = self.le_commands.cmd_validate(args)
        self.assertEqual(code, self.le_commands.EXIT_OK)
        hit_checks.assert_called_once_with(case)
        bh_checks.assert_not_called()

    def test_reproduce_hit_dispatches_to_hit_pipeline(self) -> None:
        """``le.ps1 reproduce`` now reuses the BH-style chain for hit cases."""
        case = self._case()
        args = argparse.Namespace(case="case.json", out=None, run_id="hit-repro-1")
        with unittest.mock.patch.object(self.le_commands, "_load_case", return_value=case), \
                unittest.mock.patch.object(
                    self.le_commands, "_reproduce_hit_case", return_value=self.le_commands.EXIT_OK
                ) as hit_reproduce, redirect_stdout(io.StringIO()):
            code = self.le_commands.cmd_reproduce(args)
        self.assertEqual(code, self.le_commands.EXIT_OK)
        hit_reproduce.assert_called_once_with(case, args)


if __name__ == "__main__":
    unittest.main()
