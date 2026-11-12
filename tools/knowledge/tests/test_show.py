# -*- coding: utf-8 -*-
"""show：条目详情、交叉引用与未找到错误。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import api
from le_knowledge.errors import KnowledgeNotFoundError


class ShowTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = _support.fixture(self.root)
        self.fixture.rebuild()

    def test_show_mechanism_detail(self) -> None:
        detail = self.fixture.show("fixture-ignite")
        self.assertEqual(detail["schema_version"], "le-knowledge-entry-v1")
        self.assertEqual(detail["entry_id"], "fixture-ignite")
        self.assertEqual(detail["entry_kind"], "mechanism")
        self.assertEqual(detail["revision"], 1)
        self.assertEqual(detail["title"]["zh"], "点燃测试机制")
        self.assertEqual(detail["states"]["evidence_state"], "assumed")
        self.assertEqual(detail["states"]["implementation_state"], "validated")
        self.assertEqual(
            [item["selector"] for item in detail["selectors"]],
            ["node:test:10", "node:test:11", "stat:ignite_damage"],
        )
        self.assertEqual(detail["aliases"], ["Ignite Scaling", "点燃增伤池"])
        self.assertEqual(detail["rule_refs"][0]["ref"], "rule-fixture-ignite")
        self.assertEqual(detail["implementation_refs"][0]["module"], "le_calc.ignite")
        self.assertEqual(detail["unresolved"], ["点燃玩家侧增伤池构成是否完整？"])
        self.assertEqual(detail["case_scope"], ["fixture-case"])
        self.assertEqual(detail["source"]["path"], "knowledge/mechanisms/fixture-ignite.json")
        self.assertTrue(detail["index_digest"].startswith("sha256:"))
        self.assertEqual(detail["document"]["mechanism_id"], "fixture-ignite")
        self.assertIn("applicability", detail)
        self.assertEqual(detail["applicability"]["status"], "unknown")

    def test_show_skill_template_detail_and_related(self) -> None:
        detail = self.fixture.show("fixture-fr-template")
        self.assertEqual(detail["entry_kind"], "skill_template")
        self.assertEqual(detail["skill_id"], "fixture-fr-template")
        self.assertEqual(detail["ability_id"], "fr11mv")
        related = {item["mechanism_id"]: item for item in detail["related_mechanisms"]}
        self.assertIn("fixture-rhythm", related)
        self.assertTrue(related["fixture-rhythm"]["exists"])
        self.assertEqual(related["fixture-rhythm"]["selector"], "node:fr11mv:3")
        blocks = [item["block"] for item in detail["implementation_refs"]]
        self.assertIn("more_groups", blocks)

    def test_show_resolves_prefixed_and_case_insensitive_id(self) -> None:
        by_prefix = self.fixture.show("mechanism:FIXTURE-IGNITE")
        self.assertEqual(by_prefix["entry_id"], "fixture-ignite")

    def test_show_missing_raises(self) -> None:
        with self.assertRaises(KnowledgeNotFoundError) as caught:
            self.fixture.show("does-not-exist")
        self.assertEqual(caught.exception.entry_id, "does-not-exist")

    def test_show_builds_index_when_missing(self) -> None:
        self.fixture.index_path.unlink()
        detail = self.fixture.show("fixture-monster-dr")
        self.assertEqual(detail["entry_id"], "fixture-monster-dr")
        self.assertTrue(self.fixture.index_path.is_file())

    def test_show_auto_refreshes_stale_index(self) -> None:
        path = self.fixture.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = _support.mechanism("fixture-ignite", revision=3)
        _support.write_json(path, document)
        detail = self.fixture.show("fixture-ignite")
        self.assertEqual(detail["revision"], 3)
        self.assertTrue(detail["index_state"]["rebuilt"])


if __name__ == "__main__":
    unittest.main()
