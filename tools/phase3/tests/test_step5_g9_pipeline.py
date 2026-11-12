"""G9 复验后边界：plan-v4 是受控原型，生产 CLI 明确拒绝而非模板+case 直编。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc.workflow import unified  # noqa: E402

FIXTURES = ROOT / "tools/phase3/tests/fixtures/step5"


class PlanV4PrototypeBoundaryTest(unittest.TestCase):
    def test_production_compile_rejects_plan_v4_template_without_bypass(self):
        case = {
            "schema": "le-case-v1",
            "case_id": "syn-dot-poison-g9",
            "main_skill": {"ability_id": "syn-dot-poison"},
            "build": {"build_id": "syn", "revision_id": "syn-rev"},
            "calculation": {
                "engine": "unified_dps",
                "source": {
                    "kind": "definitions_v3",
                    "template": (FIXTURES / "syn-dot-poison.template.json").relative_to(ROOT).as_posix(),
                    "scenario": (FIXTURES / "syn-dot-poison.scenario.json").relative_to(ROOT).as_posix(),
                },
            },
            "output": {"root": ".scratch/unused"},
        }
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch", prefix="g9-boundary-") as tmp:
            out = unified.compile_stage(case, root=ROOT, run_dir=Path(tmp))
            self.assertEqual(out["execution_status"], "failed")
            self.assertEqual(out["readiness"], "blocked")
            codes = {item.get("code") for item in out["issues"]}
            self.assertTrue(codes & {"input_load_failed", "definitions_v3_binding_failed", "capability_gap"}, codes)
            self.assertFalse((Path(tmp) / "calculation-plan.json").exists())


if __name__ == "__main__":
    unittest.main()
