# -*- coding: utf-8 -*-
"""与统一入口 scripts/le_commands.py 的集成：knowledge 子命令与测试套件登记。"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

import _support

REPO_ROOT = _support.REPO_ROOT
LE_COMMANDS_PATH = REPO_ROOT / "scripts" / "le_commands.py"


def load_le_commands():
    """按文件路径加载统一入口（scripts/ 不是包）。"""
    spec = importlib.util.spec_from_file_location("le_commands_under_test", LE_COMMANDS_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LeCommandsIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.le = load_le_commands()

    def test_knowledge_suite_registered(self) -> None:
        self.assertIn("knowledge", self.le.SUITES)
        spec = self.le.SUITES["knowledge"]
        self.assertIn("tools/knowledge/tests", spec["args"])
        self.assertIn(REPO_ROOT / "tools" / "knowledge" / "src", [Path(item) for item in spec["pythonpath"]])
        self.assertEqual(Path(spec["cwd"]), REPO_ROOT)

    def test_parser_forwards_knowledge_arguments(self) -> None:
        parser = self.le.build_parser()
        args = parser.parse_args(["knowledge", "search", "--query", "Flame Reave", "--json"])
        self.assertEqual(args.command, "knowledge")
        self.assertEqual(args.func, self.le.cmd_knowledge)
        self.assertEqual(args.knowledge_args, ["search", "--query", "Flame Reave", "--json"])

    def test_parser_forwards_rebuild_and_show(self) -> None:
        parser = self.le.build_parser()
        self.assertEqual(
            parser.parse_args(["knowledge", "rebuild"]).knowledge_args, ["rebuild"]
        )
        self.assertEqual(
            parser.parse_args(["knowledge", "show", "--id", "fr-rhythm-of-fire"]).knowledge_args,
            ["show", "--id", "fr-rhythm-of-fire"],
        )
        self.assertEqual(parser.parse_args(["knowledge"]).knowledge_args, [])

    def test_cmd_knowledge_delegates_to_standalone_cli(self) -> None:
        with mock.patch("le_knowledge.cli.main", return_value=0) as mocked:
            code = self.le.cmd_knowledge(
                mock.Mock(knowledge_args=["search", "--query", "x"])
            )
        self.assertEqual(code, 0)
        mocked.assert_called_once_with(["search", "--query", "x"])

    def test_existing_commands_still_registered(self) -> None:
        parser = self.le.build_parser()
        case_commands = ("doctor", "reproduce", "run", "serve", "validate", "import")
        for command in ("doctor", "reproduce", "run", "serve", "import", "validate", "test", "package"):
            extra = ["--case", "x"] if command in case_commands else []
            args = parser.parse_args([command] + extra)
            self.assertEqual(args.command, command)
        self.assertIn("knowledge", self.le.COMMANDS)

    def test_standalone_cli_importable_without_le_commands(self) -> None:
        from le_knowledge import cli as standalone

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = standalone.main([])
        self.assertEqual(code, 2)
        self.assertIn("search", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
