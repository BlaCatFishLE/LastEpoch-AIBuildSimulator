# -*- coding: utf-8 -*-
"""P5 drill: derived variant case reuses the FR engine/validate/report pipeline."""

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
VARIANT = "examples/flame-reave-variant/case.json"
FR_CASE = "examples/flame-reave/case.legacy-hit.json"
FR_EXPECTED = "examples/flame-reave/expected/expected-summary.json"
VARIANT_EXPECTED = "examples/flame-reave-variant/expected/expected-summary.json"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def run_cli(args: list[str], *, cache_root: Path, timeout: float = 300) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "tools" / "phase3" / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["LE_PIPELINE_CACHE_ROOT"] = str(cache_root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
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


def stage_statuses(output: str) -> dict[str, str]:
    rows = {}
    for line in output.splitlines():
        match = re.match(r"\s+(simulate|validate_result|render_report|commit)\s+\w+\s+cache=(\w+)", line)
        if match:
            rows[match.group(1)] = match.group(2)
    return rows


class P5VariantReuseTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory(prefix="p5-variant-", dir=SCRATCH)
        cls.root = Path(cls._tmp.name)
        cls.cache = cls.root / "cache"

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def test_variant_expected_is_calibrated_from_variant_run(self) -> None:
        variant = read_json(ROOT / VARIANT)
        self.assertEqual(variant["case_id"], "flame-reave-variant")
        self.assertEqual(variant["inputs"]["scenario"],
                         "examples/flame-reave-variant/scenario/conditional-scenario.json")
        variant_expected = read_json(ROOT / VARIANT_EXPECTED)
        fr_expected = read_json(ROOT / FR_EXPECTED)
        self.assertEqual(variant_expected["case_id"], "flame-reave-variant")
        self.assertEqual(variant_expected["source_run"]["run_id"], "p5-variant-calibration")
        self.assertIn("reports/p5-drill/work/calibration", variant_expected["source_run"]["path"])
        fr_hit = next(c["expected"] for c in fr_expected["checks"] if c["pointer"] == "/single_hit/totals/damage")
        var_hit = next(c["expected"] for c in variant_expected["checks"] if c["pointer"] == "/single_hit/totals/damage")
        self.assertNotEqual(fr_hit, var_hit, "变体 expected 不得照抄 FR 数值")
        self.assertAlmostEqual(var_hit / fr_hit, 0.75, places=12)
        scenario = read_json(ROOT / variant["inputs"]["scenario"])
        self.assertEqual(scenario["case_id"], "flame-reave-variant")
        self.assertEqual(scenario["target"]["fire_resistance"], 0.25)

    def test_variant_validate_run_and_reproduce(self) -> None:
        validate = run_cli(["validate", "--case", VARIANT], cache_root=self.cache)
        self.assertEqual(validate.returncode, 0, validate.stdout[-3000:] + validate.stderr[-1000:])
        cold_dir = self.root / "runs" / "cold"
        warm_dir = self.root / "runs" / "warm"
        cold = run_cli(
            ["run", "--case", VARIANT, "--out", str(cold_dir), "--run-id", "p5-test-cold"],
            cache_root=self.cache,
        )
        self.assertEqual(cold.returncode, 0, cold.stdout[-4000:] + cold.stderr[-2000:])
        warm = run_cli(
            ["run", "--case", VARIANT, "--out", str(warm_dir), "--run-id", "p5-test-warm"],
            cache_root=self.cache,
        )
        self.assertEqual(warm.returncode, 0, warm.stdout[-4000:] + warm.stderr[-2000:])
        statuses = stage_statuses(warm.stdout)
        self.assertEqual(statuses.get("simulate"), "hit", statuses)
        self.assertEqual(statuses.get("validate_result"), "hit", statuses)
        for run_dir in (cold_dir, warm_dir):
            self.assertTrue((run_dir / "result.json").is_file())
            self.assertTrue((run_dir / "run-manifest.json").is_file())
            self.assertTrue((run_dir / "flame-reave-damage.html").is_file())
            manifest = read_json(run_dir / "run-manifest.json")
            self.assertTrue(manifest["files"])
        cold_result = read_json(cold_dir / "result.json")
        warm_result = read_json(warm_dir / "result.json")
        cold_result.pop("run", None)
        warm_result.pop("run", None)
        self.assertEqual(cold_result, warm_result, "冷/热变体 result（去 run 块）应一致")
        reproduce = run_cli(
            ["reproduce", "--case", VARIANT, "--out", str(self.root / "repro"), "--run-id", "p5-test-repro"],
            cache_root=self.cache,
        )
        self.assertEqual(reproduce.returncode, 0, reproduce.stdout[-3000:] + reproduce.stderr[-1000:])
        report = read_json(self.root / "repro" / "p5-test-repro" / "reproduce-report.json")
        self.assertTrue(report.get("ok"))
        self.assertEqual(len(report.get("checks") or []), 41)


if __name__ == "__main__":
    unittest.main()
