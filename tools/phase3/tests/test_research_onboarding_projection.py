"""G2：research 接入状态投影、场景生成与显式 case 绑定回归。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(ROOT / "scripts"),
    str(ROOT / "tools/save_import/scripts/pipeline"),
    str(ROOT / "tools/phase3/src"),
]

import le_commands as cli  # noqa: E402
from le_pipeline import onboarding  # noqa: E402


def _bh_template() -> dict:
    return json.loads(
        (ROOT / "knowledge/definitions/black-hole-v3.json").read_text(encoding="utf-8")
    )


class OnboardingProjectionTest(unittest.TestCase):
    def test_default_case_requires_explicit_case(self):
        with self.assertRaises(cli.PipelineError):
            cli._default_case(argparse.Namespace(case=None))
        resolved = cli._default_case(argparse.Namespace(case="examples/black-hole/case.json"))
        self.assertEqual(resolved.name, "case.json")

    def test_status_projection_uses_actual_route_and_prepare(self):
        ready_route = {"engine": "unified_dps", "status": "ready", "route_id": "unified:definitions_v3"}
        ready_context = {
            "readiness": "ready",
            "stage_results": [
                {"stage_id": "resolve_skills", "execution_status": "completed"},
                {"stage_id": "compile_plan", "execution_status": "completed"},
                {"stage_id": "min_validate", "execution_status": "completed"},
            ],
        }
        prepared = onboarding._project_onboarding_status(
            ready_route, ready_context, 0, {"status": "applicable_single"}
        )
        self.assertEqual(prepared["status"], "prepared")
        self.assertEqual(prepared["blocked_stages"], [])
        self.assertIn("run", prepared["next_actions"])
        partial = onboarding._project_onboarding_status(
            ready_route, {"readiness": "partial", "stage_results": ready_context["stage_results"]}, 0, None
        )
        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["blocked_stages"], [])
        self.assertEqual(partial["next_actions"], ["run"])
        research = onboarding._project_onboarding_status(
            {"engine": "needs_research", "status": "needs_research", "diagnostics": []},
            {"readiness": "needs_research", "stage_results": []},
            3,
            {"status": "no_candidate"},
        )
        self.assertEqual(research["status"], "needs_research")
        self.assertEqual(research["prepare_exit_code"], 3)
        self.assertIn("compile_plan", research["blocked_stages"])
        needs_input = onboarding._project_onboarding_status(
            {"engine": "needs_input", "status": "needs_input"},
            {"readiness": "needs_input", "stage_results": []},
            3,
            {"status": "undetermined"},
        )
        self.assertEqual(needs_input["status"], "needs_input")
        rejected = onboarding._project_onboarding_status(
            {"engine": "unsupported_route", "status": "route_conflict"},
            {},
            3,
            None,
        )
        self.assertEqual(rejected["status"], "rejected")
        failed = onboarding._project_onboarding_status(
            {"engine": "unified_dps", "status": "ready"}, {}, 1, None
        )
        self.assertEqual(failed["status"], "failed")

    def test_scenarios_are_neutral_and_not_copied_from_old_case(self):
        template = _bh_template()
        case = {"case_id": "fixture-case", "monster_level": 100}
        with tempfile.TemporaryDirectory(dir=ROOT / ".scratch", prefix="g2-scenario-") as tmp:
            base = Path(tmp)
            documents = onboarding._generate_scenarios(
                None, ROOT, base, case, template, {"status": "applicable"}
            )
            self.assertEqual(
                [item["evaluation_id"] for item in documents], ["eager", "patient"]
            )
            for item in documents:
                path = ROOT / item["path"]
                document = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(document["schema"], "le-skill-scenario-v3")
                self.assertEqual(document["template_id"], template["template_id"])
                self.assertEqual(document["monster_level"], 100)
                self.assertEqual(document["resistance"], 0.0)
                self.assertEqual(document["monster_damage_reduction"], 0.87)
                self.assertEqual(
                    document["controls"],
                    {
                        "target_is_boss_or_rare": False,
                        "target_within_center_1m": False,
                        "mana_ge_300": False,
                    },
                )
                self.assertTrue(document["assumptions"])
                serialized = json.dumps(document, ensure_ascii=False)
                self.assertNotIn("black-hole-beta33", serialized)
                self.assertNotIn("legacy", serialized.lower())
            # 生成目录在仓库内临时目录；清理由 TemporaryDirectory 负责
            self.assertTrue(all((ROOT / item["path"]).is_file() for item in documents))

    def test_onboarding_report_status_schema_is_documented(self):
        text = (ROOT / "docs/contracts/save-onboarding.md").read_text(encoding="utf-8")
        for status in ("prepared", "partial", "needs_input", "needs_research", "rejected"):
            self.assertIn(status, text)


if __name__ == "__main__":
    unittest.main()
