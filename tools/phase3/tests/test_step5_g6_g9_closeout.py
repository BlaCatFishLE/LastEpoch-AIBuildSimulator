"""G6/G7/G8 收尾的最小守卫：历史案例分层、package 显式 case、reproduce unsupported。"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "tools/phase3/src"), str(ROOT / "tools/save_import/scripts/pipeline")]

import le_commands as cli  # noqa: E402
from le_calc import calculation_route  # noqa: E402



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class HistoryLayeringTest(unittest.TestCase):
    def test_unmigrated_cases_are_read_only_layers(self):
        expected = {
            "black-hole-beta33": ("legacy", "legacy:black-hole"),
            "flame-reave-variant": ("legacy", "legacy:hit"),
            "save33-formal": ("needs_input", None),
            "save33-formal-final": ("legacy", "legacy:black-hole"),
            "save33-formal-v2": ("legacy", "legacy:black-hole"),
            "save33-knowledge-v1": ("legacy", "legacy:black-hole"),
            "save33-pipeline": ("legacy", "legacy:black-hole"),
            "save4-pipeline": ("legacy", "legacy:hit"),
        }
        for name, (engine, route_id) in expected.items():
            with self.subTest(case=name):
                case = json.loads((ROOT / "examples" / name / "case.json").read_text(encoding="utf-8"))
                case["_path"] = f"examples/{name}/case.json"
                route = calculation_route.resolve_calculation_route(case, root=ROOT)
                self.assertEqual(route.get("engine"), engine)
                if route_id:
                    self.assertEqual(route.get("route_id"), route_id)
        beta0 = ROOT / "examples/void-cleave-beta0/source/save-1CHARACTERSLOT_BETA_0"
        full = ROOT / "examples/void-cleave-20261110/source/save-1CHARACTERSLOT_BETA_0"
        if beta0.is_file() and full.is_file():
            digest = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(digest(beta0), digest(full))

    def test_package_uses_explicit_selected_case_and_no_legacy_expected(self):
        package = (ROOT / "scripts/package.py").read_text(encoding="utf-8")
        checker = (ROOT / "scripts/check_package.py").read_text(encoding="utf-8")
        self.assertIn('"--case"', package)
        self.assertIn('"--case"', checker)
        self.assertNotIn("case.legacy.json", package)
        self.assertNotIn("case.legacy.json", checker)
        self.assertIn("不读取 BH legacy expected", checker)

    def test_unified_reproduce_is_unsupported(self):
        case = json.loads((ROOT / "examples/flame-reave/case.json").read_text(encoding="utf-8"))
        case["_path"] = "examples/flame-reave/case.json"
        args = argparse.Namespace(case=str(ROOT / "examples/flame-reave/case.json"))
        captured = io.StringIO()
        with mock.patch.object(cli, "_load_case", return_value=case):
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                code = cli.cmd_reproduce(args)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("unsupported_operation", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
