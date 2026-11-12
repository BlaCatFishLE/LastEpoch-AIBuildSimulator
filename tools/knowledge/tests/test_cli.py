# -*- coding: utf-8 -*-
"""CLI：退出码 0/1/2、短摘要/JSON 输出、rebuild --check 与无绝对路径泄漏。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import cli


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = _support.fixture(self.root)
        self.fixture.rebuild()

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv + self._paths())
        return code, stdout.getvalue(), stderr.getvalue()

    def _paths(self) -> list[str]:
        return [
            "--root",
            str(self.root),
            "--knowledge-dir",
            str(self.fixture.knowledge_dir),
            "--index",
            str(self.fixture.index_path),
        ]

    def _write_query(self, document: dict, name: str = "query.json") -> Path:
        path = self.root / name
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        return path

    def test_search_short_summary(self) -> None:
        code, stdout, _ = self._run(["search", "--query", "fixture-ignite"])
        self.assertEqual(code, 0)
        self.assertIn("命中条目", stdout)
        self.assertIn("fixture-ignite", stdout)
        self.assertIn("match_quality=exact", stdout)
        self.assertIn("不参与自动绑定", stdout)
        self.assertNotIn(str(self.root), stdout)
        self.assertNotIn("C:", stdout)

    def test_search_json(self) -> None:
        code, stdout, _ = self._run(["search", "--query", "fixture-ignite", "--json"])
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        self.assertEqual(result["schema_version"], "le-mechanism-result-v1")
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")
        self.assertTrue(result["index_digest"].startswith("sha256:"))
        self.assertIn("matched_queries", result["entries"][0])

    def test_search_batch_query_json(self) -> None:
        query_path = self._write_query(
            {
                "schema_version": "le-mechanism-query-v1",
                "queries": [
                    {"query_id": "q1", "term": "fixture-ignite", "kind": "id"},
                    {"query_id": "q2", "term": "no-such-thing", "kind": "id"},
                ],
            }
        )
        code, stdout, _ = self._run(["search", "--query-json", str(query_path), "--json"])
        self.assertEqual(code, 0)
        result = json.loads(stdout)
        self.assertEqual(result["unmatched_queries"][0]["query_id"], "q2")
        self.assertEqual(result["unmatched_queries"][0]["reason"], "no_match")
        self.assertEqual(result["summary"]["matched_query_count"], 1)

    def test_search_filters_only(self) -> None:
        code, stdout, _ = self._run(["search", "--component", "ignite"])
        self.assertEqual(code, 0)
        self.assertIn("fixture-ignite", stdout)

    def test_search_unknown_kind_is_argument_error(self) -> None:
        code, _, stderr = self._run(["search", "--query", "x", "--kind", "nope"])
        self.assertEqual(code, 2)

    def test_search_without_query_is_argument_error(self) -> None:
        code, _, stderr = self._run(["search"])
        self.assertEqual(code, 2)
        self.assertIn("[失败]", stderr)

    def test_node_filter_requires_namespace(self) -> None:
        code, _, stderr = self._run(["search", "--node", "16"])
        self.assertEqual(code, 2)

    def test_show_human_and_json(self) -> None:
        code, stdout, _ = self._run(["show", "--id", "fixture-ignite"])
        self.assertEqual(code, 0)
        self.assertIn("fixture-ignite", stdout)
        self.assertIn("点燃测试机制", stdout)
        self.assertIn("规则引用", stdout)
        self.assertNotIn(str(self.root), stdout)

        code, stdout, _ = self._run(["show", "--id", "fixture-fr-template", "--json"])
        self.assertEqual(code, 0)
        detail = json.loads(stdout)
        self.assertEqual(detail["ability_id"], "fr11mv")
        self.assertIn("related_mechanisms", detail)

    def test_show_missing_is_failure(self) -> None:
        code, _, stderr = self._run(["show", "--id", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("[失败]", stderr)
        self.assertIn("不存在", stderr)

    def test_rebuild_and_check(self) -> None:
        code, stdout, _ = self._run(["rebuild", "--check"])
        self.assertEqual(code, 0)
        self.assertIn("状态：ok", stdout)

        path = self.fixture.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["revision"] = 4
        _support.write_json(path, document)
        code, stdout, _ = self._run(["rebuild", "--check"])
        self.assertEqual(code, 1)
        self.assertIn("需要运行 knowledge rebuild", stdout)

        code, stdout, _ = self._run(["rebuild", "--json"])
        self.assertEqual(code, 0)
        report = json.loads(stdout)
        self.assertTrue(report["ok"])
        self.assertEqual(report["index"]["path"], "data/cache/knowledge.sqlite")
        self.assertEqual(report["index_md"]["path"], "knowledge/INDEX.generated.md")

        code, stdout, _ = self._run(["rebuild", "--no-fts"])
        self.assertEqual(code, 0)
        self.assertIn("回退 LIKE", stdout)

    def test_rebuild_reports_validation_errors(self) -> None:
        path = self.fixture.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["evidence_state"] = "maybe"
        _support.write_json(path, document)
        code, _, stderr = self._run(["rebuild"])
        self.assertEqual(code, 1)
        self.assertIn("校验失败", stderr)
        self.assertIn("$.evidence_state", stderr)

    def test_no_subcommand_is_argument_error(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main([])
        self.assertEqual(code, 2)
        self.assertIn("search", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
