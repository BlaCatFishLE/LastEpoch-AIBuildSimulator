# -*- coding: utf-8 -*-
"""索引构建：表结构、指纹、幂等、损坏恢复、Markdown 生成与手工改动告警。"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import api, loader, store
from le_knowledge.paths import INDEX_FORMAT


class BuildIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = _support.fixture(self.root)

    def _tables(self) -> set[str]:
        connection = sqlite3.connect(self.fixture.index_path)
        try:
            rows = connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table')")
            return {row[0] for row in rows}
        finally:
            connection.close()

    def test_rebuild_creates_index_and_markdown(self) -> None:
        report = self.fixture.rebuild()
        self.assertTrue(report["ok"])
        self.assertTrue(self.fixture.index_path.is_file())
        self.assertTrue(self.fixture.index_md_path.is_file())
        tables = self._tables()
        for expected in ("meta", "files", "entries", "selectors", "aliases", "search_terms", "mappings"):
            self.assertIn(expected, tables)
        meta = store.read_meta(self.fixture.index_path)
        self.assertEqual(meta["index_format"], INDEX_FORMAT)
        self.assertEqual(meta["source_digest"], report["source_digest"])
        self.assertEqual(meta["fts"], "1")
        self.assertEqual(report["counts"]["mechanisms"], 3)
        self.assertEqual(report["counts"]["skill_templates"], 1)
        self.assertEqual(report["tables"]["entries"], 4)

    def test_report_paths_are_portable(self) -> None:
        report = self.fixture.rebuild()
        text = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("C:", text)
        self.assertEqual(report["index"]["path"], "data/cache/knowledge.sqlite")
        self.assertEqual(report["index_md"]["path"], "knowledge/INDEX.generated.md")

    def test_markdown_content(self) -> None:
        report = self.fixture.rebuild()
        text = self.fixture.index_md_path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("<!-- GENERATED"))
        self.assertIn("GENERATED", text.splitlines()[0])
        self.assertIn(report["source_digest"], text)
        self.assertIn("le.ps1 knowledge rebuild", text)
        self.assertIn("fixture-ignite", text)
        self.assertIn("fixture-fr-template", text)
        self.assertIn("assumed", text)
        self.assertIn("validated", text)
        self.assertIn("node:test:10", text)
        self.assertIn("rule-fixture-ignite", text)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn("C:", text)

    def test_rebuild_is_idempotent(self) -> None:
        first = self.fixture.rebuild()
        text_first = self.fixture.index_md_path.read_text(encoding="utf-8")
        second = self.fixture.rebuild()
        text_second = self.fixture.index_md_path.read_text(encoding="utf-8")
        self.assertEqual(first["source_digest"], second["source_digest"])
        self.assertFalse(second["index_md"]["changed"])
        self.assertEqual(text_first, text_second)

    def test_digest_changes_when_content_changes(self) -> None:
        first = self.fixture.rebuild()
        path = self.fixture.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["revision"] = 5
        _support.write_json(path, document)
        second = self.fixture.rebuild()
        self.assertNotEqual(first["source_digest"], second["source_digest"])
        self.assertTrue(second["index_md"]["changed"])

    def test_search_auto_rebuilds_on_content_change(self) -> None:
        first = self.fixture.rebuild()
        path = self.fixture.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["revision"] = 6
        _support.write_json(path, document)
        state = api.index_state(
            root=self.root,
            knowledge_dir=self.fixture.knowledge_dir,
            index_path=self.fixture.index_path,
        )
        self.assertEqual(state["status"], "stale")
        self.assertTrue(state["stale"])
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "fixture-ignite", "kind": "id"}]}
        )
        self.assertTrue(result["index_state"]["rebuilt"])
        self.assertEqual(result["entries"][0]["revision"], 6)
        self.assertNotEqual(first["source_digest"], result["index_digest"])
        self.assertTrue(
            any("自动重建" in warning["message"] for warning in result["warnings"])
        )

    def test_missing_index_is_rebuilt(self) -> None:
        self.fixture.rebuild()
        self.fixture.index_path.unlink()
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "fixture-ignite", "kind": "id"}]}
        )
        self.assertTrue(result["index_state"]["rebuilt"])
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")
        self.assertTrue(self.fixture.index_path.is_file())

    def test_corrupt_index_is_rebuilt(self) -> None:
        self.fixture.rebuild()
        self.fixture.index_path.write_bytes(b"this is not a sqlite database")
        state = api.index_state(
            root=self.root,
            knowledge_dir=self.fixture.knowledge_dir,
            index_path=self.fixture.index_path,
        )
        self.assertEqual(state["status"], "corrupt")
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "fixture-ignite", "kind": "id"}]}
        )
        self.assertTrue(result["index_state"]["rebuilt"])
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_incompatible_index_format_is_rebuilt(self) -> None:
        self.fixture.rebuild()
        connection = sqlite3.connect(self.fixture.index_path)
        try:
            connection.execute(
                "UPDATE meta SET value = 'le-knowledge-index-v0' WHERE key = 'index_format'"
            )
            connection.commit()
        finally:
            connection.close()
        state = api.index_state(
            root=self.root,
            knowledge_dir=self.fixture.knowledge_dir,
            index_path=self.fixture.index_path,
        )
        self.assertEqual(state["status"], "incompatible")
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "fixture-ignite", "kind": "id"}]}
        )
        self.assertTrue(result["index_state"]["rebuilt"])

    def test_empty_knowledge_rebuild(self) -> None:
        empty_root = Path(self._temporary.name) / "empty"
        fixture = _support.KnowledgeFixture(empty_root, empty_root / "knowledge")
        report = fixture.rebuild()
        self.assertTrue(report["ok"])
        self.assertEqual(report["counts"]["entries"], 0)
        self.assertTrue(fixture.index_path.is_file())
        text = fixture.index_md_path.read_text(encoding="utf-8")
        self.assertIn("GENERATED", text)
        self.assertIn("尚无机制条目", text)
        result = fixture.search(
            {"queries": [{"query_id": "q1", "term": "anything", "kind": "id"}]}
        )
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["unmatched_queries"][0]["reason"], "no_match")

    def test_hand_edited_markdown_is_overwritten_with_warning(self) -> None:
        self.fixture.rebuild()
        generated = self.fixture.index_md_path.read_text(encoding="utf-8")
        self.fixture.index_md_path.write_text(
            generated + "\n手工追加的一行\n", encoding="utf-8"
        )
        report = self.fixture.rebuild()
        self.assertTrue(report["index_md"]["overwritten"])
        self.assertTrue(report["index_md"]["changed"])
        messages = " ".join(warning["message"] for warning in report["warnings"])
        self.assertIn("禁止手改", messages)
        self.assertEqual(self.fixture.index_md_path.read_text(encoding="utf-8"), generated)

    def test_markdown_without_marker_gets_stronger_warning(self) -> None:
        self.fixture.rebuild()
        self.fixture.index_md_path.write_text("no marker here\n", encoding="utf-8")
        report = self.fixture.rebuild()
        self.assertTrue(report["index_md"]["overwritten"])
        messages = " ".join(warning["message"] for warning in report["warnings"])
        self.assertIn("缺少 GENERATED 头", messages)

    def test_force_like_fallback_index(self) -> None:
        report = self.fixture.rebuild(use_fts=False)
        self.assertFalse(report["index"]["fts"])
        self.assertEqual(store.read_meta(self.fixture.index_path)["fts"], "0")
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "ignite", "kind": "keyword"}]}
        )
        self.assertEqual(result["summary"]["keyword_backend"], "like")
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_missing_fts_table_falls_back_to_like(self) -> None:
        self.fixture.rebuild()
        connection = sqlite3.connect(self.fixture.index_path)
        try:
            connection.execute("DROP TABLE terms_fts")
            connection.commit()
        finally:
            connection.close()
        result = self.fixture.search(
            {"queries": [{"query_id": "q1", "term": "ignite", "kind": "keyword"}]}
        )
        self.assertEqual(result["summary"]["keyword_backend"], "like")
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_corpus_load_does_not_write_index(self) -> None:
        corpus = loader.load_corpus(self.fixture.knowledge_dir, root=self.root)
        self.assertEqual(corpus["counts"]["entries"], 4)
        self.assertFalse(self.fixture.index_path.exists())


if __name__ == "__main__":
    unittest.main()
