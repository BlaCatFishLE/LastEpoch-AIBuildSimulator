# -*- coding: utf-8 -*-
"""逐条匹配语义（workflow-interface §5.1）：matched_queries / applicability / unmatched / summary。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import _support
from le_knowledge import api
from le_knowledge.errors import KnowledgeQueryError


def q(query_id: str, term: str, kind: str, **extra) -> dict:
    item = {"query_id": query_id, "term": term, "kind": kind}
    item.update(extra)
    return item


class QuerySemanticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.fixture = _support.fixture(self.root)
        self.report = self.fixture.rebuild()

    def _search(self, query: dict) -> dict:
        return self.fixture.search(query)

    def _entry(self, result: dict, entry_id: str) -> dict:
        for entry in result["entries"]:
            if entry["entry_id"] == entry_id:
                return entry
        self.fail(f"结果中没有条目 {entry_id}：{[e['entry_id'] for e in result['entries']]}")

    def test_batch_known_and_unknown_ids_do_not_degrade(self) -> None:
        result = self._search(
            {
                "queries": [
                    q("q1", "fixture-ignite", "id"),
                    q("q2", "no-such-mechanism", "id"),
                    q("q3", "node:test:10", "node_id"),
                ]
            }
        )
        ignite = self._entry(result, "fixture-ignite")
        records = {record["query_id"]: record for record in ignite["matched_queries"]}
        self.assertEqual(records["q1"]["match_quality"], "exact")
        self.assertEqual(records["q1"]["match_reason"], "entry_id_exact")
        self.assertEqual(records["q1"]["selector"], "mechanism:fixture-ignite")
        self.assertEqual(records["q3"]["match_quality"], "exact")
        self.assertEqual(records["q3"]["selector"], "node:test:10")
        self.assertEqual(len(result["unmatched_queries"]), 1)
        unmatched = result["unmatched_queries"][0]
        self.assertEqual(unmatched["query_id"], "q2")
        self.assertEqual(unmatched["reason"], "no_match")
        self.assertEqual(unmatched["term"], "no-such-mechanism")
        self.assertEqual(result["summary"]["matched_query_count"], 2)
        self.assertEqual(result["summary"]["unmatched_query_count"], 1)

    def test_matched_query_contract_fields(self) -> None:
        result = self._search({"queries": [q("q1", "fixture-ignite", "id")]})
        entry = self._entry(result, "fixture-ignite")
        record = entry["matched_queries"][0]
        self.assertTrue({"query_id", "selector", "match_quality", "match_reason"}.issubset(record))
        self.assertIn(record["match_quality"], {"exact", "candidate"})
        self.assertEqual(entry["mechanism_id"], "fixture-ignite")
        self.assertEqual(entry["revision"], 1)
        for key in (
            "applicability",
            "rule_refs",
            "evidence_refs",
            "implementation_refs",
            "evidence_state",
            "implementation_state",
            "unresolved",
        ):
            self.assertIn(key, entry)

    def test_index_digest_matches_rebuild(self) -> None:
        result = self._search({"queries": [q("q1", "fixture-ignite", "id")]})
        self.assertEqual(result["index_digest"], self.report["source_digest"])
        self.assertTrue(result["index_digest"].startswith("sha256:"))

    def test_summary_match_quality_is_summary_only(self) -> None:
        result = self._search({"queries": [q("q1", "ignite", "keyword")]})
        summary = result["summary"]
        self.assertEqual(summary["match_quality"], "candidate")
        self.assertIn("不参与自动绑定", summary["note"])
        # 摘要字段不参与绑定：candidate 条目没有 binding_eligible
        self.assertEqual(summary["binding_eligible_count"], 0)

    def test_alias_exact_then_keyword_candidate(self) -> None:
        alias_result = self._search({"queries": [q("q1", "Ignite Scaling", "alias")]})
        ignite = self._entry(alias_result, "fixture-ignite")
        self.assertEqual(ignite["matched_queries"][0]["match_quality"], "exact")
        self.assertEqual(ignite["matched_queries"][0]["match_reason"], "alias_exact")

        keyword_result = self._search({"queries": [q("q1", "ignite", "keyword")]})
        ignite = self._entry(keyword_result, "fixture-ignite")
        self.assertEqual(ignite["matched_queries"][0]["match_quality"], "candidate")
        self.assertIn(
            ignite["matched_queries"][0]["match_reason"],
            {"name_substring", "keyword_fts", "keyword_like", "selector_substring"},
        )

    def test_chinese_alias_keyword(self) -> None:
        result = self._search({"queries": [q("q1", "点燃", "keyword")]})
        entry_ids = [entry["entry_id"] for entry in result["entries"]]
        self.assertIn("fixture-ignite", entry_ids)

    def test_node_and_ability_selectors_reach_skill_template(self) -> None:
        node_result = self._search({"queries": [q("q1", "node:fr11mv:3", "node_id")]})
        self.assertEqual(
            [entry["entry_id"] for entry in node_result["entries"]],
            ["fixture-rhythm", "fixture-fr-template"],
        )
        ability_result = self._search({"queries": [q("q1", "ability:fr11mv", "ability_id")]})
        self.assertIn("fixture-fr-template", [entry["entry_id"] for entry in ability_result["entries"]])
        skill = self._entry(ability_result, "fixture-fr-template")
        self.assertEqual(skill["entry_kind"], "skill_template")
        self.assertEqual(skill["mechanism_id"], "fixture-fr-template")
        self.assertEqual(skill["ability_id"], "fr11mv")
        self.assertEqual(skill["selectors"], ["ability:fr11mv", "node:fr11mv:3"])

    def test_stat_key_requires_namespace(self) -> None:
        result = self._search({"queries": [q("q1", "fire_damage", "stat_key")]})
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["unmatched_queries"][0]["reason"], "missing_namespace")

        namespaced = self._search({"queries": [q("q1", "stat:ignite_damage", "stat_key")]})
        self.assertEqual(namespaced["entries"][0]["entry_id"], "fixture-ignite")

    def test_namespace_mismatch_is_reported(self) -> None:
        result = self._search({"queries": [q("q1", "ability:fr11mv", "node_id")]})
        self.assertEqual(result["unmatched_queries"][0]["reason"], "namespace_mismatch")

    def test_node_prefix_candidate(self) -> None:
        result = self._search({"queries": [q("q1", "node:fr11mv", "node_id")]})
        qualities = {
            entry["entry_id"]: entry["matched_queries"][0]["match_quality"]
            for entry in result["entries"]
        }
        self.assertEqual(qualities.get("fixture-fr-template"), "candidate")
        self.assertEqual(result["summary"]["match_quality"], "candidate")

    def test_namespaced_id_term(self) -> None:
        result = self._search({"queries": [q("q1", "mechanism:fixture-ignite", "id")]})
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_filter_component_and_tags(self) -> None:
        component = self._search({"filters": {"component": ["ignite"]}, "queries": []})
        self.assertEqual([entry["entry_id"] for entry in component["entries"]], ["fixture-ignite"])
        self.assertEqual(component["summary"]["match_quality"], "candidate")

        tags = self._search({"filters": {"tags": ["melee"]}, "queries": []})
        self.assertEqual(
            {entry["entry_id"] for entry in tags["entries"]},
            {"fixture-rhythm", "fixture-fr-template"},
        )

    def test_filter_node_and_condition(self) -> None:
        node = self._search(
            {"queries": [q("q1", "fixture-ignite", "id")], "filters": {"node_ids": ["node:test:11"]}}
        )
        self.assertEqual(node["entries"][0]["entry_id"], "fixture-ignite")
        condition = self._search(
            {
                "queries": [q("q1", "fixture-rhythm", "id")],
                "filters": {"conditions": ["rhythm_stacks"]},
            }
        )
        self.assertEqual(condition["entries"][0]["entry_id"], "fixture-rhythm")

    def test_filter_excluded_reports_filtered_out(self) -> None:
        result = self._search(
            {"queries": [q("q1", "fixture-ignite", "id")], "filters": {"tags": ["defense"]}}
        )
        self.assertEqual(result["entries"], [])
        self.assertEqual(result["unmatched_queries"][0]["reason"], "filtered_out")

    def test_filter_node_requires_namespace(self) -> None:
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": [q("q1", "fixture-ignite", "id")], "filters": {"node_ids": ["16"]}})

    def test_namespaces_filter(self) -> None:
        result = self._search(
            {
                "queries": [q("q1", "fixture-rhythm", "id")],
                "namespaces": ["node"],
            }
        )
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-rhythm")
        excluded = self._search(
            {
                "queries": [q("q1", "fixture-rhythm", "id")],
                "namespaces": ["idol"],
            }
        )
        self.assertEqual(excluded["entries"], [])
        self.assertEqual(excluded["unmatched_queries"][0]["reason"], "filtered_out")

    def test_applicability_unknown_without_context(self) -> None:
        result = self._search({"queries": [q("q1", "fixture-ignite", "id")]})
        applicability = self._entry(result, "fixture-ignite")["applicability"]
        self.assertEqual(applicability["status"], "unknown")
        self.assertEqual(
            set(applicability["missing_conditions"]), {"ignite_active", "node:test:10"}
        )
        self.assertEqual(applicability["evaluated_on"], [])
        self.assertEqual(result["summary"]["binding_eligible_count"], 0)

    def test_applicability_satisfied_with_context(self) -> None:
        result = self._search(
            {
                "queries": [q("q1", "fixture-ignite", "id")],
                "context": {
                    "conditions": ["ignite_active"],
                    "nodes": ["node:test:10"],
                },
            }
        )
        applicability = self._entry(result, "fixture-ignite")["applicability"]
        self.assertEqual(applicability["status"], "satisfied")
        self.assertEqual(applicability["missing_conditions"], [])
        self.assertEqual(
            set(applicability["evaluated_on"]), {"context.conditions", "context.nodes"}
        )
        self.assertEqual(result["summary"]["binding_eligible_count"], 1)

    def test_applicability_not_applicable_when_exhaustive(self) -> None:
        result = self._search(
            {
                "queries": [q("q1", "fixture-ignite", "id")],
                "context": {
                    "conditions": ["other_condition"],
                    "nodes": ["node:other:1"],
                    "exhaustive": True,
                },
            }
        )
        applicability = self._entry(result, "fixture-ignite")["applicability"]
        self.assertEqual(applicability["status"], "not_applicable")
        self.assertTrue(applicability["missing_conditions"])
        self.assertEqual(result["summary"]["binding_eligible_count"], 0)

    def test_entry_without_requirements_is_satisfied(self) -> None:
        result = self._search({"queries": [q("q1", "fixture-monster-dr", "id")]})
        applicability = self._entry(result, "fixture-monster-dr")["applicability"]
        self.assertEqual(applicability["status"], "satisfied")
        self.assertEqual(applicability["evaluated_on"], ["always"])

    def test_limit_and_ordering(self) -> None:
        result = self._search(
            {"queries": [q("q1", "fixture", "keyword")], "limit": 2}
        )
        self.assertEqual(len(result["entries"]), 2)
        self.assertGreater(result["summary"]["entry_count_total"], 2)
        self.assertEqual(result["summary"]["limit"], 2)
        full = self._search({"queries": [q("q1", "fixture", "keyword")]})
        exact_ids = [entry["entry_id"] for entry in full["entries"]]
        self.assertEqual(exact_ids, [entry["entry_id"] for entry in full["entries"]])

    def test_deterministic_order(self) -> None:
        query = {"queries": [q("q1", "fixture", "keyword")]}
        first = [entry["entry_id"] for entry in self._search(query)["entries"]]
        second = [entry["entry_id"] for entry in self._search(query)["entries"]]
        self.assertEqual(first, second)

    def test_query_validation_errors(self) -> None:
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": []})
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": [q("q1", "x", "unknown-kind")]})
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": [q("q1", "x", "id")], "limit": 0})
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": [q("q1", "x", "id")], "filters": {"unknown": ["x"]}})
        with self.assertRaises(KnowledgeQueryError):
            self._search({"queries": [q("q1", "x", "id", namespace="unknown")]})
        with self.assertRaises(KnowledgeQueryError):
            self._search({"schema_version": "le-mechanism-query-v2", "queries": []})

    def test_batch_query_keeps_other_query_hits(self) -> None:
        result = self._search(
            {
                "queries": [
                    q("q1", "fixture-ignite", "id"),
                    q("q2", "Monster DR", "alias"),
                    q("q3", "Rhythm of Fire", "alias"),
                ]
            }
        )
        entry_ids = {entry["entry_id"] for entry in result["entries"]}
        self.assertEqual(entry_ids, {"fixture-ignite", "fixture-monster-dr", "fixture-rhythm"})
        for entry in result["entries"]:
            self.assertTrue(entry["matched_queries"], entry["entry_id"])

    def test_applicable_status_only_after_exact_match(self) -> None:
        result = self._search({"queries": [q("q1", "ignite", "keyword")]})
        for entry in result["entries"]:
            exact = any(record["match_quality"] == "exact" for record in entry["matched_queries"])
            if not exact:
                self.assertNotEqual(entry["applicability"]["status"], "satisfied")

    def test_index_ref_in_query(self) -> None:
        result = api.search(
            {
                "queries": [q("q1", "fixture-ignite", "id")],
                "index_ref": "data/cache/knowledge.sqlite",
            },
            root=self.root,
        )
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_index_ref_can_name_asset_root(self) -> None:
        result = api.search(
            {
                "queries": [q("q1", "fixture-ignite", "id")],
                "index_ref": self.fixture.knowledge_dir.name,
            },
            root=self.root,
        )
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")

    def test_string_shorthand_query(self) -> None:
        result = self._search("fixture-ignite")
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")
        self.assertEqual(
            result["entries"][0]["matched_queries"][0]["match_quality"], "exact"
        )

    def test_ensure_fresh_false_uses_existing_index(self) -> None:
        result = self.fixture.search(
            {"queries": [q("q1", "fixture-ignite", "id")]}, ensure_fresh=False
        )
        self.assertFalse(result["index_state"]["rebuilt"])
        self.assertEqual(result["entries"][0]["entry_id"], "fixture-ignite")


if __name__ == "__main__":
    unittest.main()
