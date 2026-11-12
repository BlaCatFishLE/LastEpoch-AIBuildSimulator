# -*- coding: utf-8 -*-
"""loader：选择器归一化、状态字段、未解决项、内容指纹与告警。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import loader
from le_knowledge.errors import KnowledgeValidationError


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.knowledge_dir = _support.make_knowledge_dir(self.root)

    def _corpus(self) -> dict:
        return loader.load_corpus(self.knowledge_dir, root=self.root)

    def _entry(self, corpus: dict, entry_id: str) -> dict:
        return next(entry for entry in corpus["entries"] if entry["entry_id"] == entry_id)

    def test_selector_normalization_supports_both_forms(self) -> None:
        corpus = self._corpus()
        ignite = self._entry(corpus, "fixture-ignite")
        selectors = [row["selector"] for row in ignite["selectors"]]
        # ids: ["test:10"] + namespace node → node:test:10（不是 test 命名空间）
        self.assertEqual(selectors, ["node:test:10", "node:test:11", "stat:ignite_damage"])
        self.assertEqual(corpus["warnings"], [])

    def test_skill_selectors_derived_from_ability_and_mappings(self) -> None:
        corpus = self._corpus()
        skill = self._entry(corpus, "fixture-fr-template")
        selectors = [row["selector"] for row in skill["selectors"]]
        self.assertEqual(selectors, ["ability:fr11mv", "node:fr11mv:3"])
        self.assertEqual([row["namespace"] for row in skill["selectors"]], ["ability", "node"])
        self.assertEqual(skill["mappings"][0]["mechanism_id"], "fixture-rhythm")

    def test_entry_id_namespace_in_namespaces(self) -> None:
        corpus = self._corpus()
        self.assertIn("mechanism", self._entry(corpus, "fixture-ignite")["namespaces"])
        skill = self._entry(corpus, "fixture-fr-template")
        self.assertIn("skill", skill["namespaces"])
        self.assertIn("ability", skill["namespaces"])

    def test_states_and_references_extracted(self) -> None:
        corpus = self._corpus()
        ignite = self._entry(corpus, "fixture-ignite")
        self.assertEqual(ignite["evidence_state"], "assumed")
        self.assertEqual(ignite["implementation_state"], "validated")
        self.assertEqual(ignite["rule_refs"][0]["ref"], "rule-fixture-ignite")
        self.assertEqual(ignite["implementation_refs"][0]["symbol"], "ignite_scaling_pool")
        self.assertEqual(ignite["unresolved"], ["点燃玩家侧增伤池构成是否完整？"])
        self.assertEqual(ignite["aliases"], ["Ignite Scaling", "点燃增伤池"])
        self.assertEqual(ignite["components"], ["ignite", "dot"])
        self.assertEqual(ignite["conditions"], ["ignite_active"])
        self.assertEqual(ignite["required_nodes"], ["node:test:10"])

        skill = self._entry(corpus, "fixture-fr-template")
        blocks = [item["block"] for item in skill["implementation_refs"]]
        self.assertEqual(blocks, ["more_groups"])
        self.assertTrue(any("技能等级缩放" in item for item in skill["unresolved"]))
        self.assertEqual(skill["components"], ["hit"])

    def test_condition_none_is_dropped(self) -> None:
        document = json.loads(
            (self.knowledge_dir / "mechanisms" / "fixture-monster-dr.json").read_text(encoding="utf-8")
        )
        document["applicability"]["conditions"] = ["none"]
        _support.write_json(self.knowledge_dir / "mechanisms" / "fixture-monster-dr.json", document)
        corpus = self._corpus()
        self.assertEqual(self._entry(corpus, "fixture-monster-dr")["conditions"], [])

    def test_digest_stable_and_changes_with_content(self) -> None:
        first = loader.scan_source_files(self.knowledge_dir)
        second = loader.scan_source_files(self.knowledge_dir)
        self.assertEqual(first["digest"], second["digest"])
        path = self.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["revision"] = 9
        _support.write_json(path, document)
        changed = loader.scan_source_files(self.knowledge_dir)
        self.assertNotEqual(first["digest"], changed["digest"])
        # schema 文件也参与指纹（字段契约变化会让索引失效）
        schema_path = self.knowledge_dir / "schemas" / "mechanism-v1.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        schema["title"] = "changed title only"
        _support.write_json(schema_path, schema)
        self.assertNotEqual(changed["digest"], loader.scan_source_files(self.knowledge_dir)["digest"])

    def test_selector_combination_rule_and_compat_forms(self) -> None:
        document = _support.mechanism(
            "fixture-combination",
            selectors=[
                {"kind": "node", "namespace": "bh2", "ids": ["0"]},
                {"kind": "ability", "namespace": "dataset", "ids": ["bh2"]},
                {"kind": "stat", "namespace": "le_calc", "ids": ["damage"]},
                {"kind": "item", "namespace": "slot", "ids": ["weapon1"]},
                {"kind": "condition", "namespace": "scenario", "ids": ["boss_or_rare"]},
                {"kind": "unique", "namespace": "dataset", "ids": ["443"]},
                {"kind": "node", "namespace": "node", "ids": ["fr11mv:16"]},
            ],
        )
        _support.write_json(
            self.knowledge_dir / "mechanisms" / "fixture-combination.json", document
        )
        corpus = self._corpus()
        entry = self._entry(corpus, "fixture-combination")
        selectors = [row["selector"] for row in entry["selectors"]]
        self.assertIn("node:bh2:0", selectors)
        self.assertIn("ability:dataset:bh2", selectors)
        self.assertIn("ability:bh2", selectors)
        self.assertIn("stat:le_calc:damage", selectors)
        self.assertIn("stat:damage", selectors)
        self.assertIn("item:slot:weapon1", selectors)
        self.assertIn("item:weapon1", selectors)
        self.assertIn("condition:scenario:boss_or_rare", selectors)
        self.assertIn("condition:boss_or_rare", selectors)
        self.assertIn("unique:dataset:443", selectors)
        self.assertIn("unique:443", selectors)
        self.assertIn("node:fr11mv:16", selectors)
        self.assertEqual(corpus["warnings"], [])

    def test_empty_knowledge_dir_is_valid(self) -> None:
        empty_root = self.root / "empty-root"
        empty_dir = empty_root / "knowledge"
        corpus = loader.load_corpus(empty_dir, root=empty_root)
        self.assertEqual(corpus["entries"], [])
        self.assertEqual(corpus["counts"]["entries"], 0)
        self.assertTrue(corpus["digest"].startswith("sha256:"))

    def test_underscore_files_are_ignored(self) -> None:
        (self.knowledge_dir / "mechanisms" / "_notes.json").write_text("{not json", encoding="utf-8")
        corpus = self._corpus()
        self.assertEqual(len(corpus["entries"]), 4)

    def test_duplicate_entry_id_is_error(self) -> None:
        document = json.loads(
            (self.knowledge_dir / "mechanisms" / "fixture-ignite.json").read_text(encoding="utf-8")
        )
        _support.write_json(self.knowledge_dir / "mechanisms" / "copy.json", document)
        with self.assertRaises(KnowledgeValidationError) as caught:
            self._corpus()
        messages = " ".join(error["message"] for error in caught.exception.errors)
        self.assertIn("重复的条目 ID", messages)

    def test_filename_mismatch_is_warning_not_error(self) -> None:
        source = self.knowledge_dir / "mechanisms" / "fixture-ignite.json"
        document = json.loads(source.read_text(encoding="utf-8"))
        target = self.knowledge_dir / "mechanisms" / "renamed.json"
        target.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        source.unlink()
        corpus = self._corpus()
        self.assertEqual(len(corpus["entries"]), 4)
        self.assertTrue(any("文件名与条目 ID 不一致" in item["message"] for item in corpus["warnings"]))

    def test_missing_mechanism_reference_is_warning(self) -> None:
        path = self.knowledge_dir / "skills" / "fixture-fr-template.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mappings"] = [{"selector": "node:fr11mv:3", "mechanism_id": "not-registered"}]
        _support.write_json(path, document)
        corpus = self._corpus()
        self.assertTrue(
            any("不存在的 mechanism_id" in item["message"] for item in corpus["warnings"])
        )

    def test_entries_sorted_and_counts(self) -> None:
        corpus = self._corpus()
        keys = [(entry["entry_kind"], entry["entry_id"]) for entry in corpus["entries"]]
        self.assertEqual(keys, sorted(keys))
        self.assertEqual(corpus["counts"]["mechanisms"], 3)
        self.assertEqual(corpus["counts"]["skill_templates"], 1)
        self.assertEqual(corpus["counts"]["content_files"], 4)
        self.assertEqual(corpus["counts"]["schema_files"], 2)


if __name__ == "__main__":
    unittest.main()
