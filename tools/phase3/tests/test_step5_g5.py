"""G5：生产 import 隔离、v2 死分支、data-only fixture 与数据化报告。"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))

from le_calc import calculation_plan as planmod  # noqa: E402
from le_calc import plan_v4  # noqa: E402

FIXTURES = ROOT / "tools/phase3/tests/fixtures/step5"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

def _load_fixture(name: str):
    return (
        json.loads((FIXTURES / f"{name}.template.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.case.json").read_text(encoding="utf-8")),
        json.loads((FIXTURES / f"{name}.scenario.json").read_text(encoding="utf-8")),
    )


class ProductionImportIsolationTest(unittest.TestCase):
    def test_production_import_chain_does_not_load_legacy_adapters(self):
        script = ROOT / ".scratch" / "g5_import_guard.py"
        script.write_text(
            "\n".join(
                [
                    "import importlib.abc, json, sys",
                    "BLOCKED = {'le_calc.damage','le_calc.ignite','le_calc.cooldown','le_calc.simulate',",
                    "           'le_calc.hit_inputs','le_calc.hit_damage','le_calc.hit_simulate','le_calc.dps'}",
                    "class Block(importlib.abc.MetaPathFinder):",
                    "    def find_spec(self, fullname, path=None, target=None):",
                    "        if fullname in BLOCKED:",
                    "            raise RuntimeError('legacy adapter imported: ' + fullname)",
                    "        return None",
                    "sys.meta_path.insert(0, Block())",
                    f"sys.path.insert(0, {str(ROOT / 'tools/phase3/src')!r})",
                    "from le_calc.workflow import unified  # production render/serve/run import surface",
                    "from le_calc import plan_v4, dps_report",
                    f"base = {str(FIXTURES)!r}",
                    "from pathlib import Path",
                    "def load(name):",
                    "    return (json.loads(Path(f'{base}/{name}.template.json').read_text(encoding='utf-8')),",
                    "            json.loads(Path(f'{base}/{name}.case.json').read_text(encoding='utf-8')),",
                    "            json.loads(Path(f'{base}/{name}.scenario.json').read_text(encoding='utf-8')))",
                    "for name in ('syn-hit-cold', 'syn-dot-poison', 'syn-charge'):",
                    "    t, c, s = load(name)",
                    "    p = plan_v4.compile_plan_v4(t, c, s)",
                    "    r = plan_v4.execute_plan_v4(p)",
                    "    plan_v4.validate_result_v6(p, r)",
                    "    html = plan_v4.render_report_v6(r)",
                    "    assert '<html' in html",
                    "assert not (set(sys.modules) & BLOCKED), set(sys.modules) & BLOCKED",
                    "print('OK')",
                ]
            ),
            encoding="utf-8",
        )
        env = dict(os.environ, PYTHONPATH=str(ROOT / "tools/phase3/src"))
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
        self.assertIn("OK", proc.stdout)

    def test_v2_dot_branch_is_rejected_by_schema(self):
        fr_plan = json.loads(
            (
                ROOT
                / "reports/flame-reave/dps-step4-fr-4/calculation-plan.json"
            ).read_text(encoding="utf-8")
        )
        mutated = copy.deepcopy(fr_plan)
        mutated["components"][0] = {**mutated["components"][0], "kind": "dot"}
        mutated["plan_sha256"] = "0" * 64
        problems = planmod.validate_plan_structure(mutated)
        codes = {str(item.get("code")) for item in problems}
        self.assertIn("unsupported_plan_v2_feature", codes)

    def test_data_only_fixtures_are_declared_without_code_branches(self):
        for name in ("syn-hit-cold", "syn-dot-poison", "syn-charge"):
            for suffix in (".template.json", ".case.json", ".scenario.json"):
                self.assertTrue((FIXTURES / f"{name}{suffix}").is_file())
            template, case, scenario = _load_fixture(name)
            plan = plan_v4.compile_plan_v4(template, case, scenario)
            result = plan_v4.execute_plan_v4(plan)
            self.assertEqual(result["schema"], plan_v4.SCHEMA_RESULT)
            html = plan_v4.render_report_v6(result)
            self.assertIn(result["presentation"]["title"], html)
            self.assertNotIn("bh-ignite", html)

    def test_presentation_labels_are_not_bh_defaults(self):
        template, case, scenario = _load_fixture("syn-hit-cold")
        result = plan_v4.execute_plan_v4(plan_v4.compile_plan_v4(template, case, scenario))
        html = plan_v4.render_report_v6(result)
        self.assertIn("Synthetic Cold Hit", html)
        for forbidden in ("Black Hole", "Ignite", "Heat Wave", "bh-binary"):
            self.assertNotIn(forbidden, html)


if __name__ == "__main__":
    unittest.main()
