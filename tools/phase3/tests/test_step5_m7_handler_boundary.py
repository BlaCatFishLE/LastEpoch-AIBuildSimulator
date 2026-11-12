"""G4/M7：handler registry 不再反向依赖 plan_executor_v3 私有函数。"""
from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools/phase3/src"))


class HandlerBoundaryTest(unittest.TestCase):
    def test_handler_source_has_no_executor_private_calls(self):
        source = (ROOT / "tools/phase3/src/le_calc/plan_handlers.py").read_text(encoding="utf-8")
        self.assertNotIn("plan_executor_v3._", source)
        self.assertNotIn("from . import plan_executor_v3", source)
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "plan_executor_v3":
                imported.add(node.module)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "plan_executor_v3":
                    imported.add(node.attr)
        self.assertEqual(imported, set())

    def test_handler_import_does_not_load_executor_or_cycle(self):
        script = ROOT / ".scratch" / "m7_handler_guard.py"
        script.write_text(
            "\n".join(
                [
                    "import sys",
                    f"sys.path.insert(0, {str(ROOT / 'tools/phase3/src')!r})",
                    "import le_calc.plan_handlers as handlers",
                    "assert 'le_calc.plan_executor_v3' not in sys.modules, 'handler import loaded executor'",
                    "from le_calc.mechanics.dot import dot_component_result, cohort_between, max_concurrent, window_metrics",
                    "from le_calc.mechanics.applications import application_core, application_cohort, single_cast",
                    "from le_calc.mechanics.charge import charge_loop, charge_parameters, blocked_attempts",
                    "public = [dot_component_result, cohort_between, max_concurrent, window_metrics,",
                    "          application_core, application_cohort, single_cast, charge_loop,",
                    "          charge_parameters, blocked_attempts]",
                    "assert all(callable(item) for item in public)",
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
            timeout=120,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        self.assertIn("OK", proc.stdout)

    def test_executor_imports_public_mechanics(self):
        source = (ROOT / "tools/phase3/src/le_calc/plan_executor_v3.py").read_text(encoding="utf-8")
        self.assertIn("from .mechanics.dot import", source)
        self.assertIn("from .mechanics.applications import", source)
        self.assertIn("from .mechanics.charge import", source)
        for name in (
            "_dot_component_result",
            "_ignite_core",
            "_ignite_application",
            "_charge_loop",
            "_charge_parameters",
        ):
            self.assertNotIn(f"def {name}(", source)
        plan_executor_v3 = importlib.import_module("le_calc.plan_executor_v3")
        for name in (
            "_dot_component_result",
            "_ignite_core",
            "_ignite_application",
            "_charge_loop",
            "_charge_parameters",
            "_single_cast",
        ):
            self.assertTrue(callable(getattr(plan_executor_v3, name)), name)


if __name__ == "__main__":
    unittest.main()
