"""Independent resolver tests over the real read-only phase-1 catalog (P2-5).

The expectations come from two discovery/verification artifacts that were produced
without the importer:

* ``reports/discovery/phase-2-o3ZRm9my/identity-probe.json`` — the historical probe
  with 34 item instances, 47 affix occurrences and 102 node entries resolved once by
  sqlite queries.
* ``reports/phase-2/p2-0-precheck-dataset-002.json`` — the P2-0 precheck over
  dataset-002, including every node entry with its expected entity id and tree.

Per the 2026-11-03 user scope change the importer no longer includes the weaver tree
(``data.weaverTree`` / ``weaverTreeContainers`` / ``weaverTreeProgression``), so the
acceptance counts exercised here are the in-scope subset: 29 item instances
(11 equipment + 8 idols + 10 blessings) and 80 node entries (19 passive + 61 skill).
The weaver resolver API may be retained for traceability; if so it is still tested, but
it is never part of the import acceptance.

Nothing here re-derives those numbers from ``le_build`` itself.  The catalog is opened
exactly the way the CLI opens it (``data/current.json`` -> dataset release) and the
tests prove the connection is read-only.

Run from ``tools/build_import``::

    $env:PYTHONPATH='src'; & $py -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from le_build.catalog import Catalog
from le_build.resolve import AMBIGUOUS, RESOLVED, UNRESOLVED, Resolver

from _dataset_asset import require_dataset_db

REPO = Path(__file__).resolve().parents[3]
DISCOVERY = REPO / "reports" / "discovery" / "phase-2-o3ZRm9my"
IDENTITY_PROBE = DISCOVERY / "identity-probe.json"
PRECHECK = REPO / "reports" / "phase-2" / "p2-0-precheck-dataset-002.json"

WEAVER = "tree:letools:LEWeaverTree:weaver"



def setUpModule() -> None:  # noqa: N802 - unittest module hook
    from _author_assets import require_author_assets

    require_author_assets()

class ResolverCatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        require_dataset_db()
        cls.identity = json.loads(IDENTITY_PROBE.read_text(encoding="utf-8"))
        cls.precheck = json.loads(PRECHECK.read_text(encoding="utf-8"))
        cls.pointer = json.loads((REPO / "data" / "current.json").read_text(encoding="utf-8"))
        # Open exactly like `le_build ... --dataset <pointer.dataset_id>`.
        cls.catalog = Catalog.open(root=REPO, dataset=cls.pointer["dataset_id"], verify=True)
        cls.resolver = Resolver(cls.catalog)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.catalog.close()

    # -------------------------------------------------------- catalog binding
    def test_catalog_is_the_dataset_selected_by_current_pointer(self) -> None:
        self.assertEqual(self.catalog.dataset_id, self.pointer["dataset_id"])
        self.assertEqual(self.catalog.dataset_id, "phase1-dataset-003")
        self.assertTrue(self.catalog.verification["verified"])
        self.assertEqual(
            self.catalog.verification["file_sha256"], self.pointer["file_sha256"]
        )
        checks = {entry["check"] for entry in self.catalog.verification["checks"]}
        self.assertIn("file_sha256", checks)
        self.assertIn("logical_sha256", checks)
        self.assertEqual(
            self.catalog.fingerprint()["logical_sha256"],
            self.pointer["logical_content_sha256"],
        )
        meta = self.catalog.meta()
        self.assertEqual(meta["target_game_version"], "1.5.2")
        self.assertEqual(meta["target_season"], "Season 5")
        self.assertEqual(meta["schema_version"], "1")

    def test_catalog_matches_p2_0_precheck_table_counts(self) -> None:
        expected = self.precheck["dataset"]["dataset_fingerprints_row"]["table_counts"]
        for table in ("source_snapshots", "source_id_map", "entities", "items", "affixes",
                      "affix_tiers", "nodes", "trees", "stats", "translations"):
            self.assertEqual(
                self.catalog.table_count(table), expected[table], f"table {table} drifted"
            )

    def test_catalog_connection_is_read_only(self) -> None:
        with self.assertRaises(sqlite3.OperationalError):
            self.catalog._conn.execute("CREATE TABLE resolver_test_forbidden (x INTEGER)")
        # The failed write must not have created anything.
        names = {
            row["name"]
            for row in self.catalog._rows("SELECT name FROM sqlite_master WHERE type='table'")
        }
        self.assertNotIn("resolver_test_forbidden", names)

    # ----------------------------------------------------- character and trees
    def test_class_resolution_for_all_catalog_classes(self) -> None:
        expected = {1: "Mage", 2: "Sentinel", 3: "Acolyte", 4: "Rogue"}
        for class_id, name in expected.items():
            result = self.resolver.resolve_class(class_id)
            self.assertEqual(result["status"], RESOLVED, result)
            self.assertEqual(result["entity"]["name"], name)
            self.assertEqual(result["entity"]["kind"], "class")
            self.assertEqual(result["character_class"]["classID"], class_id)

    def test_mastery_resolution_is_scoped_by_class_namespace(self) -> None:
        mage = self.resolver.resolve_class(1)
        result = self.resolver.resolve_mastery(1, 1, mage)
        self.assertEqual(result["status"], RESOLVED, result)
        self.assertEqual(result["entity"]["name"], "Sorcerer")
        self.assertEqual(result["entity"]["kind"], "mastery")
        entity = self.catalog.entity(result["entity"]["entity_id"])
        self.assertEqual(entity["kind"], "mastery")
        self.assertTrue(result["query"]["source_scope"].startswith("class:"))
        # The bare mastery index alone is not a namespace: 2.1 is a different mastery.
        sentinel = self.resolver.resolve_class(2)
        other = self.resolver.resolve_mastery(2, 1, sentinel)
        self.assertEqual(other["status"], RESOLVED, other)
        self.assertEqual(other["entity"]["name"], "Void Knight")
        self.assertNotEqual(other["entity"]["entity_id"], result["entity"]["entity_id"])

    def test_passive_tree_is_derived_from_the_class_entity(self) -> None:
        mage = self.resolver.resolve_class(1)
        result = self.resolver.resolve_passive_tree(
            mage["entity"]["entity_id"], mage["entity"]["name"]
        )
        self.assertEqual(result["status"], RESOLVED, result)
        self.assertEqual(result["entity"]["entity_id"], "tree:letools:LECharTrees.trees:mg-1")
        self.assertEqual(result["source_tree_id"], "mg-1")
        self.assertEqual(len(self.catalog.passive_tree_for_class(mage["entity"]["entity_id"])), 1)

    def test_single_weaver_tree_resolves_if_the_api_is_retained(self) -> None:
        # The weaver tree is excluded from the import scope (2026-11-03); the resolver
        # may keep the historical API for traceability, but importing must not require it.
        if not hasattr(self.resolver, "resolve_weaver_tree"):
            self.skipTest("weaver tree resolution API removed with the scope change")
        result = self.resolver.resolve_weaver_tree()
        self.assertEqual(result["status"], RESOLVED, result)
        self.assertEqual(result["entity"]["entity_id"], WEAVER)
        self.assertEqual(result["source_tree_id"], "weaver")

    def test_skill_tree_resolution_including_owner_skill(self) -> None:
        expected = {
            "sb44eQ": "Enchant Weapon",
            "me27": "Meteor",
            "fw3d": "Flame Ward",
            "bh2": "Black Hole",
            "fr4wl": "Frost Wall",
        }
        for tree_id, owner in expected.items():
            result = self.resolver.resolve_skill_tree(tree_id)
            self.assertEqual(result["status"], RESOLVED, result)
            self.assertEqual(result["tree"]["entity_id"], f"tree:letools:LESkillTrees:{tree_id}")
            self.assertEqual(result["source_tree_id"], tree_id)
            self.assertEqual(result["owner_skill"]["name"], owner)

    def test_action_bar_abilities_resolve(self) -> None:
        expected = {
            "me27": "Meteor",
            "sb44eQ": "Enchant Weapon",
            "te44": "Teleport",
            "fr4wl": "Frost Wall",
            "bh2": "Black Hole",
        }
        for ability_id, name in expected.items():
            result = self.resolver.resolve_ability(ability_id)
            self.assertEqual(result["status"], RESOLVED, result)
            self.assertEqual(result["entity"]["name"], name)
            self.assertEqual(result["entity"]["kind"], "skill")

    # ---------------------------------------------------------------- items
    def test_all_29_in_scope_item_instances_resolve_to_probe_entities(self) -> None:
        items = self.identity["items"]
        # The probe is historical evidence and includes the 5 excluded weaver
        # container contents; the accepted in-scope set is the other 29.
        self.assertEqual(len(items), 34)
        in_scope = [entry for entry in items if not entry["path"].startswith("weaverTreeContainers")]
        self.assertEqual(len(in_scope), 29)
        roles: dict[str, int] = {}
        for entry in in_scope:
            path = entry["path"]
            role = path.split("/", 1)[0]
            roles[role] = roles.get(role, 0) + 1
            resolution = self.resolver.decode_item(
                entry["encoded_id"], role=role, source_path=path
            )
            self.assertEqual(resolution["status"], RESOLVED, f"{path}: {resolution}")
            self.assertEqual(resolution["codec"]["status"], "decoded", path)
            expected_match = entry["matches"][0]
            expected_base = entry["base_matches"][0]
            # Every one of the 29 carries a resolved base item.
            self.assertIsNotNone(resolution["base"], path)
            self.assertEqual(resolution["base"]["entity_id"], expected_base["entity_id"], path)
            if expected_match["kind"] == "unique":
                self.assertEqual(
                    resolution["definition"]["entity_id"], expected_match["entity_id"], path
                )
                self.assertEqual(resolution["definition"]["name"], expected_match["canonical_name"])
            else:
                self.assertEqual(expected_match["kind"], "item_base", path)
                self.assertIsNone(resolution["definition"], path)
                self.assertEqual(
                    resolution["base"]["entity_id"], expected_match["entity_id"], path
                )
        self.assertEqual(
            roles,
            {"equipment": 11, "idols": 8, "blessings": 10},
        )

    def test_unique_items_keep_their_definition_and_base(self) -> None:
        probe = {entry["path"]: entry for entry in self.identity["items"]}
        hands = probe["equipment/hands"]
        resolution = self.resolver.decode_item(
            hands["encoded_id"], role="equipment", source_path="equipment/hands"
        )
        self.assertEqual(resolution["definition"]["name"], "Withstand the Elements")
        self.assertEqual(resolution["base"]["name"], "Outcast Gloves")
        self.assertEqual(resolution["base_status"], RESOLVED)
        self.assertEqual(resolution["definition_status"], RESOLVED)
        ring = probe["equipment/ring2"]
        self.assertEqual(
            self.resolver.decode_item(
                ring["encoded_id"], role="equipment", source_path="equipment/ring2"
            )["definition"]["name"],
            "Unsated Rage",
        )

    # --------------------------------------------------------------- affixes
    def test_all_47_affix_occurrences_and_tiers_resolve(self) -> None:
        affixes = self.identity["affixes"]
        self.assertEqual(len(affixes), 47)
        distinct_ids = set()
        channels: dict[str, int] = {}
        for entry in affixes:
            path = entry["path"]
            channel = path.split("/")[-2] if path.split("/")[-1].isdigit() else path.split("/")[-1]
            channels[channel] = channels.get(channel, 0) + 1
            resolution = self.resolver.decode_item(
                entry["encoded_id"], role="affix_instance", source_path=path
            )
            self.assertEqual(resolution["affix_status"], RESOLVED, f"{path}: {resolution}")
            self.assertEqual(
                resolution["affix"]["entity_id"], entry["matches"][0]["entity_id"], path
            )
            self.assertEqual(
                resolution["affix"]["source_scope"],
                entry["matches"][0]["source_scope"],
                path,
            )
            self.assertEqual(
                resolution["affix"]["source_id"], str(entry["source_affix_id"]), path
            )
            distinct_ids.add(entry["source_affix_id"])
            tier = self.resolver.resolve_tier(
                resolution["affix"]["entity_id"], entry["tier"]
            )
            self.assertEqual(tier["status"], RESOLVED, f"{path}: {tier}")
            expected_tier = entry["tier_matches"][0]
            # Source tier is 1-based; the catalog tier_index is 0-based.
            self.assertEqual(expected_tier["tier_index"], entry["tier"] - 1, path)
            self.assertEqual(tier["tier_index"], expected_tier["tier_index"], path)
            self.assertEqual(tier["tier"]["tier_id"], expected_tier["tier_id"], path)
            self.assertAlmostEqual(tier["tier"]["roll_min"], expected_tier["roll_min"])
            self.assertAlmostEqual(tier["tier"]["roll_max"], expected_tier["roll_max"])
        self.assertEqual(len(distinct_ids), 27)
        self.assertEqual(
            channels,
            {"affixes": 37, "sealedAffix": 2, "primordialAffix": 2, "corruptedAffix": 6},
        )

    def test_affix_scope_is_not_guessed(self) -> None:
        single = self.resolver.decode_item(
            "AAwRgzEA", role="fixture", source_path="equipment/head/affixes/0"
        )
        multi = self.resolver.decode_item(
            "AKzAsQ", role="fixture", source_path="equipment/relic/primordialAffix"
        )
        self.assertEqual(single["affix"]["source_scope"], "affixList.singleAffixes")
        self.assertEqual(multi["affix"]["source_scope"], "affixList.multiAffixes")
        self.assertEqual(single["affix"]["source_id"], "13")
        self.assertEqual(multi["affix"]["source_id"], "554")

    # ----------------------------------------------------------------- nodes
    def test_all_80_in_scope_node_entries_resolve_to_precheck_entities(self) -> None:
        entries = self.precheck["nodes"]["entries"]
        # Historical precheck: 102 entries including the 22 excluded weaver nodes.
        self.assertEqual(len(entries), 102)
        in_scope = [entry for entry in entries if entry["kind"] != "weaver_node"]
        self.assertEqual(len(in_scope), 80)
        counts: dict[str, int] = {}
        for entry in in_scope:
            kind = entry["kind"]
            counts[kind] = counts.get(kind, 0) + 1
            node_row = entry["node_row"]
            result = self.resolver.resolve_node(
                kind=kind,
                scope=entry["source_scope"],
                source_id=entry["source_id"],
                tree_entity_id=node_row["tree_entity_id"],
                points=entry["points"],
                source_path=entry["source_path"],
            )
            self.assertEqual(result["status"], RESOLVED, f"{entry['source_path']}: {result}")
            self.assertEqual(result["entity"]["entity_id"], entry["entity_id"], entry["source_path"])
            self.assertEqual(result["entity"]["name"], entry["entity"]["canonical_name"])
            node = result["node"]
            self.assertEqual(node["tree_entity_id"], node_row["tree_entity_id"])
            self.assertEqual(node["node_index"], int(entry["source_id"]))
            self.assertEqual(node["node_type"], node_row["node_type"])
            self.assertEqual(node["max_points"], node_row["max_points"])
            self.assertEqual(result["points"], entry["points"])
        self.assertEqual(counts, {"passive_node": 19, "skill_tree_node": 61})
        self.assertEqual(self.precheck["summary"]["nodes"]["explicit_zero_points"], 3)

    # ------------------------------------------ wrong scope / ambiguity handling
    def test_wrong_scope_returns_unresolved_instead_of_a_guess(self) -> None:
        cases = [
            dict(kind="passive_node", scope="mg-1", source_id="999999", tree_entity_id=None),
            dict(kind="passive_node", scope="not-a-real-tree", source_id="0", tree_entity_id=None),
            dict(kind="skill_tree_node", scope="mg-1", source_id="0", tree_entity_id=None),
        ]
        for case in cases:
            result = self.resolver.resolve_node(**case)
            self.assertEqual(result["status"], UNRESOLVED, f"{case}: {result}")
            self.assertIsNone(result["entity"])
            self.assertEqual(result["candidates"], [])

    def test_unknown_class_and_mastery_are_unresolved(self) -> None:
        unknown_class = self.resolver.resolve_class(999)
        self.assertEqual(unknown_class["status"], UNRESOLVED)
        self.assertIsNone(unknown_class["entity"])
        # The class namespace is authoritative: an id absent from it is not guessed.
        self.assertEqual(self.resolver.resolve_class(5)["status"], UNRESOLVED)
        result = self.resolver.resolve_mastery(999, 1, unknown_class)
        self.assertEqual(result["status"], UNRESOLVED)

    def test_unknown_affix_id_is_unresolved_without_candidates(self) -> None:
        result = self.resolver._resolve_affix({"affix_id": 99999, "payload": "99999"})
        self.assertEqual(result["affix_status"], UNRESOLVED, result)
        self.assertIsNone(result.get("affix"))
        self.assertEqual(result["affix_candidates"], [])

    def test_cross_scope_duplicate_item_base_ids_are_ambiguous_without_scope(self) -> None:
        rows = self.resolver._lookup("item_base", None, "0")
        self.assertEqual(len(rows), 45)
        self.assertGreater(len({row["source_scope"] for row in rows}), 1)
        ambiguous = self.resolver._resolve_rows(
            what="item_base",
            query={"kind": "item_base", "source_scope": None, "source_id": "0"},
            rows=rows,
        )
        self.assertEqual(ambiguous["status"], AMBIGUOUS)
        self.assertIsNone(ambiguous["entity"])
        self.assertEqual(len(ambiguous["candidates"]), 45)
        # The full namespace disambiguates: id 0 exists in equippable:1 only,
        # and is absent from equippable:0.
        self.assertEqual(len(self.resolver._lookup("item_base", "itemList.equippable:0", "0")), 0)
        scoped = self.resolver._lookup("item_base", "itemList.equippable:1", "0")
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["entity_id"], "item:letools:itemList.equippable_1:0")

    def test_node_from_another_tree_is_ambiguous_not_rebound(self) -> None:
        result = self.resolver.resolve_node(
            kind="passive_node",
            scope="mg-1",
            source_id="0",
            tree_entity_id="tree:letools:LESkillTrees:sb44eQ",
        )
        self.assertEqual(result["status"], AMBIGUOUS, result)
        self.assertIn("belongs to tree", result["reason"])
        self.assertIn("tree:letools:LECharTrees.trees:mg-1", result["reason"])
        # The out-of-tree node must never be exposed as the resolution result...
        self.assertIsNone(result.get("entity"))
        self.assertIsNone(result.get("node"))
        # ...only as an explicitly named diagnostic candidate.
        candidate = result.get("candidate_node") or {}
        self.assertEqual(candidate.get("entity_id"), "node:letools:mg-1:0")

    def test_tier_out_of_range_is_unresolved(self) -> None:
        affix_entity = "affix:letools:affixList.singleAffixes:13"
        self.assertEqual(
            self.resolver.resolve_tier(affix_entity, 99)["status"], UNRESOLVED
        )
        self.assertEqual(
            self.resolver.resolve_tier(affix_entity, "not-an-int")["status"], UNRESOLVED
        )
        first = self.resolver.resolve_tier(affix_entity, 1)
        self.assertEqual(first["status"], RESOLVED)
        self.assertEqual(first["tier_index"], 0)
        self.assertTrue(any("1-based" in text for text in first["conversions"]))


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
