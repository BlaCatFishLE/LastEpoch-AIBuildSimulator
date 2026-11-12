# -*- coding: utf-8 -*-
"""P4-G1 run stage-graph tests (simulate -> validate -> render -> commit)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRATCH = ROOT / ".scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)
CALC_SRC = str(ROOT / "tools" / "phase3" / "src")
BH_CASE = "examples/black-hole/case.legacy.json"
FR_CASE = "examples/flame-reave/case.legacy-hit.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def run_cli(args: list[str], *, cache_root: Path, timeout: float = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = CALC_SRC + os.pathsep + env.get("PYTHONPATH", "")
    env["LE_PIPELINE_CACHE_ROOT"] = str(cache_root)
    return subprocess.run(
        [sys.executable, "scripts/le_commands.py", *args],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class RunStageGraphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="p4-g1-", dir=SCRATCH)
        cls.root = Path(cls._tmp.name)
        cls.cache = cls.root / "cache"
        cls.ops = cls.root / "ops"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _args(self, case: str, run_id: str, final_dir: Path, *extra: str) -> list[str]:
        return [
            "run",
            "--case", case,
            "--out", str(final_dir),
            "--run-id", run_id,
            "--publish", "staged",
            "--operations-root", str(self.ops),
            *extra,
        ]

    def _final_dir(self, name: str) -> Path:
        return self.root / name

    def _stage_statuses(self, output: str) -> dict[str, str]:
        rows = {}
        for line in output.splitlines():
            match = re.match(r"\s+(simulate|validate_result|render_report|commit)\s+(\w+)\s+cache=(\w+)", line)
            if match:
                rows[match.group(1)] = match.group(3)
        return rows

    def test_stage_graph_validation_and_replay(self) -> None:
        final_dir = self._final_dir("bh-graph")
        first = run_cli(self._args(BH_CASE, "g1-bh-graph", final_dir), cache_root=self.cache)
        self.assertEqual(first.returncode, 0, first.stdout[-4000:] + first.stderr[-2000:])
        self.assertIn("staged 发布：committed", first.stdout)
        state = read_json(final_dir / "pipeline-state.json")
        stage_ids = [entry["stage_id"] for entry in state["stages"]]
        self.assertEqual(stage_ids, ["simulate", "validate_result", "render_report", "commit"])
        self.assertEqual(state["last_commit"]["status"], "committed")
        validation = read_json(final_dir / "validation-result.json")
        self.assertTrue(validation["passed"])
        self.assertTrue(validation["accepted"])
        self.assertRegex(str(validation.get("result_sha256")), r"^[0-9a-f]{64}$")
        # same command: simulate/validate/render hit, commit replays, final unchanged
        state_bytes = (final_dir / "pipeline-state.json").read_bytes()
        second = run_cli(self._args(BH_CASE, "g1-bh-graph", final_dir), cache_root=self.cache)
        self.assertEqual(second.returncode, 0, second.stdout[-4000:] + second.stderr[-2000:])
        self.assertIn("staged 发布：replayed", second.stdout)
        statuses = self._stage_statuses(second.stdout)
        self.assertEqual(statuses.get("simulate"), "hit")
        self.assertEqual(statuses.get("validate_result"), "hit")
        self.assertEqual(statuses.get("render_report"), "hit")
        self.assertEqual(statuses.get("commit"), "disabled")
        self.assertEqual((final_dir / "pipeline-state.json").read_bytes(), state_bytes)
        self.assertEqual(read_json(final_dir / "result.json")["run"]["out_dir"], str(final_dir.relative_to(ROOT).as_posix()))

    def test_endpoint_only_rerenders_and_does_not_recalculate(self) -> None:
        base_dir = self._final_dir("fr-endpoint-base")
        first = run_cli(self._args(FR_CASE, "g1-fr-base", base_dir, "--no-report-cache"), cache_root=self.cache)
        self.assertEqual(first.returncode, 0, first.stdout[-4000:] + first.stderr[-2000:])
        changed_dir = self._final_dir("fr-endpoint-8899")
        second = run_cli(
            self._args(FR_CASE, "g1-fr-8899", changed_dir, "--no-report-cache", "--port", "8899"),
            cache_root=self.cache,
        )
        self.assertEqual(second.returncode, 0, second.stdout[-4000:] + second.stderr[-2000:])
        statuses = self._stage_statuses(second.stdout)
        self.assertEqual(statuses.get("simulate"), "hit")
        self.assertEqual(statuses.get("validate_result"), "hit")
        self.assertEqual(statuses.get("render_report"), "miss")
        left = read_json(base_dir / "result.json")
        right = read_json(changed_dir / "result.json")
        left.pop("run", None)
        right.pop("run", None)
        self.assertEqual(left, right, "端点变化不应改变数值/结果内容")
        html_left = (base_dir / "flame-reave-damage.html").read_text(encoding="utf-8")
        html_right = (changed_dir / "flame-reave-damage.html").read_text(encoding="utf-8")
        self.assertNotEqual(html_left, html_right)
        self.assertIn("8899", html_right)

    def test_no_cache_executes_all_stages(self) -> None:
        final_dir = self._final_dir("bh-nocache")
        completed = run_cli(
            self._args(BH_CASE, "g1-bh-nocache", final_dir, "--no-cache"),
            cache_root=self.cache,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-4000:] + completed.stderr[-2000:])
        statuses = self._stage_statuses(completed.stdout)
        for stage_id in ("simulate", "validate_result", "render_report", "commit"):
            self.assertEqual(statuses.get(stage_id), "disabled", stage_id)
        self.assertEqual(read_json(final_dir / "pipeline-state.json")["last_commit"]["status"], "committed")

    def test_scenario_change_invalidates_simulate_and_downstream(self) -> None:
        case = read_json(ROOT / BH_CASE)
        scenario = read_json(ROOT / case["inputs"]["scenario"])
        scenario["g1_test_note"] = "scenario change must invalidate simulate"
        scenario_path = self.root / "scenario-changed.json"
        scenario_path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["inputs"]["scenario"] = str(scenario_path.relative_to(ROOT).as_posix())
        case_path = self.root / "case-scenario-changed.json"
        case_path.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        final_dir = self._final_dir("bh-scenario-change")
        completed = run_cli(
            self._args(str(case_path.relative_to(ROOT).as_posix()), "g1-bh-scenario", final_dir),
            cache_root=self.cache,
        )
        statuses = self._stage_statuses(completed.stdout)
        self.assertEqual(statuses.get("simulate"), "miss")
        if "validate_result" in statuses:
            self.assertIn(statuses["validate_result"], ("miss", "disabled"))

    def _verify_state(self, run_dir: Path) -> dict:
        from le_calc.workflow import statestore

        store = statestore.FileStateStore(run_dir, root=ROOT)
        state = store.load()
        report = store.verify(state)
        self.assertEqual(report["invalidated"], [], report)
        self.assertEqual(report["downstream_invalidated"], [], report)
        self.assertEqual(report["valid"], ["simulate", "validate_result", "render_report", "commit"])
        for entry in state.stages:
            for ref in entry.output_refs:
                path = ROOT / ref["path"]
                self.assertTrue(path.is_file(), ref["path"])
                self.assertEqual(__import__("hashlib").sha256(path.read_bytes()).hexdigest(),
                                 ref["sha256"], f"{entry.stage_id}:{ref['path']}")
        return report

    def test_run_id_collisions_are_structured_rebinds(self) -> None:
        """R1/R3: base/numeric/business/long run-ids must not corrupt business text,
        and every successful state must pass FileStateStore.verify()."""
        import hashlib

        base_dir = self._final_dir("fr-collision-base")
        cold = run_cli(
            self._args(FR_CASE, "base", base_dir, "--no-report-cache"),
            cache_root=self.cache,
        )
        self.assertEqual(cold.returncode, 0, cold.stdout[-4000:] + cold.stderr[-2000:])
        self._verify_state(base_dir)
        base_result = read_json(base_dir / "result.json")
        base_cmp = dict(base_result)
        base_cmp.pop("run", None)
        base_text = json.dumps(base_result, ensure_ascii=False)
        self.assertIn("baselines", base_text)
        self.assertNotIn("copylines", base_text)
        for index, run_id in enumerate(("copy", "12345", "baseline", "normal-long-run-id-001")):
            final_dir = self._final_dir(f"fr-collision-{index}")
            completed = run_cli(
                self._args(FR_CASE, run_id, final_dir, "--no-report-cache"),
                cache_root=self.cache,
            )
            self.assertEqual(completed.returncode, 0,
                             f"run_id={run_id}: {completed.stdout[-3000:]}{completed.stderr[-1000:]}")
            self.assertIn("staged 发布：committed", completed.stdout)
            statuses = self._stage_statuses(completed.stdout)
            self.assertEqual(statuses.get("simulate"), "hit", f"run_id={run_id}: {statuses}")
            self.assertEqual(statuses.get("validate_result"), "hit", f"run_id={run_id}: {statuses}")
            result = read_json(final_dir / "result.json")
            self.assertEqual(result["run"]["run_id"], run_id)
            text = json.dumps(result, ensure_ascii=False)
            self.assertIn("baselines", text, f"run_id={run_id} 业务字段被破坏")
            self.assertNotIn("copylines", text, f"run_id={run_id} 业务字段被破坏")
            compare = dict(result)
            compare.pop("run", None)
            self.assertEqual(compare, base_cmp, f"run_id={run_id} 冷/热语义不一致")
            self._verify_state(final_dir)

    def test_default_none_state_verify_passes(self) -> None:
        run_dir = self._final_dir("fr-default-verify")
        completed = run_cli(
            ["run", "--case", FR_CASE, "--out", str(run_dir), "--run-id", "default-verify"],
            cache_root=self.cache,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout[-4000:] + completed.stderr[-2000:])
        self._verify_state(run_dir)

    def test_validation_gate_blocks_publish(self) -> None:
        case = read_json(ROOT / BH_CASE)
        expected = read_json(ROOT / case["expected"])
        expected["checks"][0]["expected"] = float(expected["checks"][0]["expected"]) + 1000.0
        bad_expected = self.root / "expected-bad.json"
        bad_expected.write_text(json.dumps(expected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        case["expected"] = str(bad_expected.relative_to(ROOT).as_posix())
        bad_case = self.root / "case-bad.json"
        bad_case.write_text(json.dumps(case, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        final_dir = self._final_dir("bh-gate")
        records_before = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        completed = run_cli(
            self._args(str(bad_case.relative_to(ROOT).as_posix()), "g1-bh-gate", final_dir),
            cache_root=self.cache,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stdout[-3000:])
        self.assertFalse(final_dir.exists(), "validation 未通过时不得发布正式目录")
        records_after = sorted(self.ops.glob("*.json")) if self.ops.is_dir() else []
        self.assertEqual(records_after, records_before, "validation 未通过时不得产生提交记录")
        self.assertTrue((final_dir.parent / ".staging").is_dir())


if __name__ == "__main__":
    unittest.main()
