"""LETools review fixes (P1-1 / P1-6 / P2-1) on the real W1 snapshot.

The tests normalize ``data/staging/phase1-001/source_records/letools.jsonl``
into a throwaway SQLite database built from ``tools/ingest/migrations`` (which
includes the schema-0003 review columns) and then assert:

* buff UI semantics: ``maxInstances=0`` is a raw UI value, never "uncapped";
* the BuffId -> UIBuffIds -> UIBuffs.id name-based id-space resolution for
  ``planner.skillBonuses`` / ``passiveBonuses`` buff configs;
* Flurry/Adrenaline Rush base duration/stacks and node parameter evidence
  (``flur3`` nodes 7/10/11, properties 5093/54/20067 with statNameKey
  translations) even though ``partialSkillBonuses['flur3'].buffs`` is empty;
* equipment semantics from the source item-type records (Calamity, set items),
  prefix/suffix from the source binary affix type, and affixProperties rows that
  the stat-link pass can resolve (Void Penetration);
* mastery base-class vs selectable-mastery classification.

Nothing in the repository database, the staging database or the network is
touched.

Run (project root, no pytest required)::

    python -m unittest discover -s tools/ingest/tests -p "test_letools_review_fixes.py" -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize.base import Crosswalk, Normalizer  # noqa: E402
from le_data.normalize.stat_link import link_stats  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import apply_migrations, connect, insert_row  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = "letools-v152-20261101"
SOURCE = "letools"
STAGED_JSONL = (
    PROJECT_ROOT / "data" / "staging" / "phase1-001" / "source_records" / "letools.jsonl"
)

BUFF_ADRENALINE = f"buff:{SOURCE}:UIBuffs:30"
BUFF_RIME = f"buff:{SOURCE}:UIBuffs:10"
BUFF_CRATERBORN = f"buff:{SOURCE}:UIBuffs:14"
NODE = lambda tree, node_id: f"node:{SOURCE}:{tree}:{node_id}"  # noqa: E731


@unittest.skipUnless(STAGED_JSONL.exists(), f"staged source records missing: {STAGED_JSONL}")
class LetoolsReviewFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmpdir.name) / "letools.sqlite"
        cls.conn = connect(cls.db_path)
        apply_migrations(cls.conn)
        cls.conn.execute(
            "INSERT INTO source_snapshots (source_snapshot_id, source, claimed_version, status) "
            "VALUES (?, ?, '1.5.2', 'ok')",
            (SNAPSHOT, SOURCE),
        )
        cls.records = [SourceRecord.from_dict(row) for row in read_jsonl(STAGED_JSONL)]
        for record in cls.records:
            insert_row(
                cls.conn,
                "source_records",
                {
                    "record_id": record.record_id,
                    "source_snapshot_id": record.source_snapshot_id,
                    "source": record.source,
                    "resource_name": record.resource_name,
                    "entity_kind": record.entity_kind,
                    "source_scope": record.source_scope,
                    "source_id": record.source_id,
                    "locator": record.locator,
                    "raw_payload": json.dumps(record.raw, ensure_ascii=False),
                    "parse_status": record.parse_status,
                    "notes": None,
                },
                or_ignore=True,
            )
        cls.conn.commit()
        # The production pipeline normalizes letools first and then runs
        # stat_link before publishing; mirror that here so the affix-property
        # link assertions run against the same shape as the dataset.
        normalizer = Normalizer(cls.conn, SOURCE, SNAPSHOT, crosswalk=Crosswalk())
        cls.summary = normalizer.normalize_records(cls.records)
        cls.stat_link_summary = link_stats(cls.conn)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        cls._tmpdir.cleanup()

    # -- helpers -------------------------------------------------------------
    def _scalar(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def _row(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()

    def _rows(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchall()

    def _buff(self, entity_id: str):
        row = self._row(
            "SELECT b.*, e.canonical_name FROM buffs b JOIN entities e "
            "ON e.entity_id = b.entity_id WHERE b.entity_id = ?",
            entity_id,
        )
        self.assertIsNotNone(row, f"buff row missing: {entity_id}")
        return row

    def _buff_config_entries(self) -> int:
        """Count non-empty buff config entries in the raw source records."""
        total = 0
        for record in self.records:
            if record.entity_kind not in ("skill_bonus", "passive_bonus"):
                continue
            for section in ("buffs", "masteryBuffs"):
                entries = record.raw.get(section) or {}
                if isinstance(entries, dict):
                    total += sum(len(v) for v in entries.values() if isinstance(v, list))
        return total

    # -- P1-6: UI value semantics -------------------------------------------
    def test_buff_ui_max_instances_zero_is_not_uncapped(self):
        adrenaline = self._buff(BUFF_ADRENALINE)
        self.assertEqual(adrenaline["ui_max_instances"], 0)
        lifecycle = json.loads(adrenaline["lifecycle"])
        self.assertEqual(lifecycle["max_instances_interpretation"], "raw_ui")
        self.assertNotIn("uncapped", json.dumps(lifecycle).lower())
        self.assertEqual(lifecycle["raw_max_instances"], 0)
        # The UI 0 was not promoted to the mechanic stack cap: the node evidence
        # gives the base cap and it is marked as node-sourced.
        self.assertEqual(adrenaline["max_stacks"], 4)
        self.assertEqual(adrenaline["max_stacks_basis"], "node_text")
        flags = json.loads(adrenaline["ui_flags"])
        self.assertEqual(flags["raw_max_instances"], 0)

    def test_buff_without_node_evidence_keeps_null_mechanic_cap(self):
        rows = self._rows(
            "SELECT b.* FROM buffs b WHERE b.ui_max_instances = 0 "
            "AND b.max_stacks IS NULL AND b.max_stacks_basis = 'raw_ui'"
        )
        self.assertGreater(len(rows), 0,
                           "expected at least one buff whose only stack information is "
                           "the UI value")
        total = self._scalar("SELECT COUNT(*) FROM buffs")
        fixed = self._scalar(
            "SELECT COUNT(*) FROM buffs WHERE max_stacks_basis = 'raw_ui' "
            "AND max_stacks IS NULL")
        self.assertGreaterEqual(fixed, 1)
        self.assertLessEqual(fixed, total)
        # no buff lifecycle may claim an uncapped mechanic any more
        uncapped = self._scalar(
            "SELECT COUNT(*) FROM buffs WHERE lifecycle LIKE '%uncapped%'")
        self.assertEqual(uncapped, 0)

    # -- P1-1/P1-6: Flurry / Adrenaline Rush --------------------------------
    def test_adrenaline_rush_base_duration_and_stacks_trace_to_flur3_nodes(self):
        adrenaline = self._buff(BUFF_ADRENALINE)
        self.assertEqual(adrenaline["canonical_name"], "Adrenaline Rush")
        self.assertEqual(adrenaline["duration_ms"], 4000)
        self.assertEqual(adrenaline["base_duration_ms"], 4000)
        self.assertEqual(adrenaline["duration_basis"], "node_text")
        self.assertEqual(adrenaline["max_stacks"], 4)
        self.assertEqual(adrenaline["max_stacks_basis"], "node_text")
        evidence = self._rows(
            "SELECT field_path, raw_value, normalized_value, locator, notes "
            "FROM field_evidence WHERE entity_id = ? AND field_path IN "
            "('base_duration_ms->duration_ms', 'max_stacks') "
            "AND evidence_status = 'known'",
            BUFF_ADRENALINE,
        )
        self.assertTrue(evidence)
        locators = " ".join(row["locator"] or "" for row in evidence)
        self.assertIn("flur3", locators)
        self.assertIn("nodes['7']", locators)

    def test_flurry_nodes_link_to_adrenaline_rush_buff_by_name(self):
        relationships = self._rows(
            "SELECT from_entity_id, relationship_type, to_entity_id, "
            "resolution_status, raw_target_ref, qualifiers "
            "FROM entity_relationships WHERE from_entity_id IN (?, ?, ?) "
            "AND to_entity_id = ?",
            NODE("flur3", 7), NODE("flur3", 10), NODE("flur3", 11),
            BUFF_ADRENALINE,
        )
        by_node = {row["from_entity_id"]: row for row in relationships}
        self.assertIn(NODE("flur3", 7), by_node)
        self.assertEqual(by_node[NODE("flur3", 7)]["relationship_type"],
                         "grants_buff_candidate")
        for node_id in (10, 11):
            self.assertIn(NODE("flur3", node_id), by_node)
            self.assertEqual(by_node[NODE("flur3", node_id)]["relationship_type"],
                             "modifies_buff_candidate")
        for row in relationships:
            self.assertEqual(row["resolution_status"], "candidate")
            self.assertTrue(row["raw_target_ref"])
            self.assertIn("node_text.buff_mention", row["qualifiers"])

    def test_flurry_node_parameter_effects_are_queryable(self):
        effects = {}
        for row in self._rows(
            "SELECT e.effect_id, e.owner_entity_id, e.raw_text, v.value_kind, "
            "v.value_numeric, v.unit, e.extensions, v.raw "
            "FROM effects e JOIN effect_values v ON v.effect_id = e.effect_id "
            "WHERE e.effect_category = 'buff_param_candidate' AND e.owner_entity_id "
            "IN (?, ?, ?)",
            NODE("flur3", 7), NODE("flur3", 10), NODE("flur3", 11),
        ):
            extensions = json.loads(row["extensions"])
            if extensions.get("buff_entity_id") != BUFF_ADRENALINE:
                continue
            effects[(row["owner_entity_id"], extensions["parameter"],
                     json.loads(row["raw"]).get("property"))] = row
        # node 7: grant chance (property 20067), duration 4s (5093), max stacks 4 (54)
        chance = effects[(NODE("flur3", 7), "chance", 20067)]
        self.assertEqual(chance["value_kind"], "chance")
        self.assertAlmostEqual(chance["value_numeric"], 0.10)
        duration = effects[(NODE("flur3", 7), "duration", 5093)]
        self.assertEqual(duration["value_kind"], "duration")
        self.assertEqual(duration["value_numeric"], 4000)
        self.assertEqual(duration["unit"], "ms")
        stacks = effects[(NODE("flur3", 7), "max_stacks", 54)]
        self.assertEqual(stacks["value_kind"], "max_stacks")
        self.assertEqual(stacks["value_numeric"], 4)
        # node 10: +25% duration; node 11: +1 maximum stack
        duration_mod = effects[(NODE("flur3", 10), "duration", 5093)]
        self.assertEqual(duration_mod["value_kind"], "duration_increased")
        self.assertAlmostEqual(duration_mod["value_numeric"], 0.25)
        stacks_mod = effects[(NODE("flur3", 11), "max_stacks", 5093)]
        self.assertEqual(stacks_mod["value_kind"], "max_stacks")
        self.assertEqual(stacks_mod["value_numeric"], 1)
        # raw property id space is not decoded as a parameter namespace: the
        # stat translation carries the semantics and the raw id stays in the row.
        self.assertIn("property", effects[(NODE("flur3", 7), "max_stacks", 54)]["extensions"])

    # -- P1-1: bonus buff configs and the three id spaces --------------------
    def test_buff_config_ids_resolve_by_name_not_by_number(self):
        # gl14 (Glacier) config entry id=60 must resolve to UIBuffs:10 (Rime),
        # never to UIBuffs:60 (Deliverance).
        rows = self._rows(
            "SELECT from_entity_id, to_entity_id, resolution_status, qualifiers "
            "FROM entity_relationships "
            "WHERE from_entity_id = ? AND relationship_type LIKE '%buff%'",
            NODE("gl14", 13),
        )
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row["to_entity_id"], BUFF_RIME)
            self.assertEqual(row["resolution_status"], "resolved")
            qualifiers = json.loads(row["qualifiers"])
            self.assertEqual(qualifiers["buff_id"], 60)
            self.assertEqual(qualifiers["buff_id_name"], "Rime")
            self.assertEqual(qualifiers["uibuff_id"], 10)
        rime_name = self._scalar(
            "SELECT canonical_name FROM entities WHERE entity_id = ?", BUFF_RIME)
        deliverance = self._scalar(
            "SELECT canonical_name FROM entities WHERE entity_id = ?",
            f"buff:{SOURCE}:UIBuffs:60")
        self.assertEqual(rime_name, "Rime")
        self.assertEqual(deliverance, "Deliverance")
        self.assertNotEqual(rime_name, deliverance)
        self.assertEqual(
            self._scalar("SELECT canonical_name FROM entities WHERE entity_id = ?",
                         BUFF_ADRENALINE),
            "Adrenaline Rush",
        )
        # me27 buff id 59 -> BuffId 'Craterborn' -> UIBuffs:14
        me27 = self._rows(
            "SELECT to_entity_id, resolution_status FROM entity_relationships "
            "WHERE from_entity_id = ? AND relationship_type LIKE '%buff%'",
            NODE("me27", 25),
        )
        self.assertTrue(me27)
        self.assertEqual(me27[0]["to_entity_id"], BUFF_CRATERBORN)
        self.assertEqual(me27[0]["resolution_status"], "resolved")

    def test_buff_prop_id_names_are_used_for_config_parameters(self):
        rows = self._rows(
            "SELECT e.effect_id, e.raw_text, v.value_kind, v.value_numeric, v.unit, "
            "e.extensions FROM effects e JOIN effect_values v "
            "ON v.effect_id = e.effect_id "
            "WHERE e.effect_id LIKE "
            "'skill_bonus:letools:planner.skillBonuses:gl14#buff%' "
            "ORDER BY e.effect_id"
        )
        self.assertTrue(rows)
        params = {}
        for row in rows:
            extensions = json.loads(row["extensions"])
            params[extensions["prop_name"]] = row
        self.assertIn("Duration", params)
        self.assertIn("IncreasedEffect", params)
        self.assertIn("DoT", params)
        self.assertIn("FreezeRateMulti", params)
        # Rime base duration 4s from the granting node (gl14 node 13, prop 2)
        duration_rows = [row for row in rows
                         if json.loads(row["extensions"])["prop_name"] == "Duration"]
        base = next(row for row in duration_rows if row["value_numeric"] == 4000)
        self.assertEqual(base["value_kind"], "duration")
        self.assertEqual(base["unit"], "ms")
        # gl14 node 11 prop 3 = Rime_IncreasedEffect 0.25
        effect = params["IncreasedEffect"]
        self.assertAlmostEqual(effect["value_numeric"], 0.25)
        self.assertEqual(effect["value_kind"], "percent_modifier")

    def test_every_non_empty_buff_config_entry_is_accounted_for(self):
        expected = self._buff_config_entries()
        self.assertEqual(expected, 140)
        relations = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE relationship_type LIKE '%buff%' "
            "AND qualifiers LIKE '%\"mapping\": \"planner.%'"
        )
        self.assertGreaterEqual(relations, expected)
        unresolved = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE resolution_status = 'unresolved' "
            "AND relationship_type LIKE '%buff%'")
        self.assertGreater(unresolved, 0,
                           "buff ids whose BuffId name has no UIBuffs entry must stay "
                           "visible as unresolved relationships")
        missing_ref = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE resolution_status != 'resolved' AND relationship_type LIKE '%buff%' "
            "AND (raw_target_ref IS NULL OR raw_target_ref = '')")
        self.assertEqual(missing_ref, 0)
        issues = self._scalar(
            "SELECT COUNT(*) FROM data_issues WHERE issue_type = "
            "'unresolved_buff_reference'")
        self.assertGreaterEqual(issues, unresolved,
                                "every unresolved buff config id needs a per-entry issue")
        # the non-buff config sections are structured too
        self.assertGreater(
            self._scalar("SELECT COUNT(*) FROM effects WHERE effect_scope LIKE "
                         "'skill_bonus_config'"), 0)
        self.assertGreater(
            self._scalar("SELECT COUNT(*) FROM effects WHERE effect_scope LIKE "
                         "'passive_bonus_config'"), 0)

    # -- P2-1: equipment semantics ------------------------------------------
    def test_calamity_equipment_semantics(self):
        row = self._row(
            "SELECT i.*, e.canonical_name FROM items i JOIN entities e "
            "ON e.entity_id = i.entity_id "
            "WHERE i.entity_id = 'item:letools:uniqueList.uniques:0'"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["canonical_name"], "Calamity")
        self.assertEqual(row["item_category"], "unique")
        self.assertEqual(row["is_unique"], 1)
        self.assertEqual(row["is_set"], 0)  # raw unique isSetItem == 0
        self.assertEqual(row["base_type"], "0")
        self.assertEqual(row["item_type"], "Helmet")
        self.assertEqual(row["gear_category"], "armor")
        self.assertEqual(row["equip_slot"], "helm")
        evidence = self._rows(
            "SELECT field_path, normalized_value FROM field_evidence "
            "WHERE entity_id = ? AND field_path LIKE 'baseTypeId->%'",
            row["entity_id"])
        self.assertTrue(evidence)

    def test_set_items_keep_is_set_and_memberships(self):
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM items WHERE is_set = 1"), 61)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM set_memberships"), 61)
        sample = self._row(
            "SELECT i.entity_id, i.is_set, i.gear_category FROM items i "
            "WHERE i.is_set = 1 ORDER BY i.entity_id LIMIT 1")
        self.assertEqual(sample["is_set"], 1)
        memberships = self._scalar(
            "SELECT COUNT(*) FROM set_memberships WHERE item_entity_id = ?",
            sample["entity_id"])
        self.assertEqual(memberships, 1)
        # plain base items are classified too (scope colons are cleaned to '_')
        helmet = self._row(
            "SELECT * FROM items WHERE entity_id = "
            "'item:letools:itemList.equippable_0:1'")
        self.assertIsNotNone(helmet)
        self.assertEqual(helmet["gear_category"], "armor")
        self.assertEqual(helmet["equip_slot"], "helm")
        self.assertEqual(helmet["is_set"], 0)

    def test_affix_slot_uses_source_binary_type(self):
        void_pen = self._row(
            "SELECT a.*, e.canonical_name FROM affixes a JOIN entities e "
            "ON e.entity_id = a.entity_id "
            "WHERE a.source_affix_id = '0' AND a.affix_type = 'singleAffixes'")
        self.assertEqual(void_pen["canonical_name"], "Void Penetration")
        self.assertEqual(void_pen["affix_slot"], "prefix")
        suffix = self._row(
            "SELECT affix_slot FROM affixes WHERE source_affix_id = '1' "
            "AND affix_type = 'singleAffixes'")
        self.assertEqual(suffix["affix_slot"], "suffix")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM affixes WHERE affix_slot = 'prefix'"), 884)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM affixes WHERE affix_slot = 'suffix'"), 272)
        evidence = self._row(
            "SELECT * FROM field_evidence WHERE entity_id = ? AND "
            "field_path = 'type->affix_slot'", void_pen["entity_id"])
        self.assertIsNotNone(evidence)

    def test_affix_properties_are_linked_for_stat_semantics(self):
        # Void Penetration property 59 is the review's fixed equipment sample.
        rows = self._rows(
            "SELECT v.stat_id, v.stat_link_status, v.value_min, v.value_max, "
            "v.value_kind, v.unit, v.raw FROM effect_values v "
            "JOIN effects e ON e.effect_id = v.effect_id "
            "WHERE e.owner_entity_id = 'affix:letools:affixList.singleAffixes:0' "
            "AND e.effect_category = 'affix_property'"
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stat_id"], "stat:letools:coreDB.propertyList:59")
        self.assertEqual(row["stat_link_status"], "composite")  # tags=16 qualifier
        self.assertAlmostEqual(row["value_min"], 0.04)
        self.assertAlmostEqual(row["value_max"], 0.3)
        self.assertEqual(row["value_kind"], "flat")
        self.assertEqual(row["unit"], "%")
        self.assertEqual(json.loads(row["raw"])["property"], 59)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM effects WHERE effect_category = "
                         "'affix_property'"), 1696)
        affix_summary = self.stat_link_summary["by_owner_kind"].get("affix") or {}
        self.assertGreater(affix_summary.get("linked", 0), 0)
        self.assertGreater(affix_summary.get("composite", 0), 0)

    # -- P2-1: masteries -----------------------------------------------------
    def test_masteries_split_base_class_and_mastery(self):
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM masteries WHERE is_base = 1"), 5)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM masteries WHERE branch_kind = "
                         "'mastery'"), 15)
        base = self._row(
            "SELECT m.*, e.canonical_name FROM masteries m JOIN entities e "
            "ON e.entity_id = m.entity_id WHERE m.source_mastery_id = '0.0'")
        self.assertEqual(base["branch_kind"], "base_class")
        self.assertEqual(base["is_base"], 1)
        self.assertEqual(base["canonical_name"], "Primalist")
        class_name = self._scalar(
            "SELECT canonical_name FROM entities WHERE entity_id = "
            "'class:letools:classInfoForSources:0'")
        self.assertEqual(class_name, base["canonical_name"])
        mastery = self._row(
            "SELECT * FROM masteries WHERE source_mastery_id = '0.2'")
        self.assertEqual(mastery["branch_kind"], "mastery")
        self.assertEqual(mastery["is_base"], 0)

    # -- integrity -----------------------------------------------------------
    def test_foreign_keys_and_integrity(self):
        self.assertEqual(list(self.conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(self._scalar("PRAGMA integrity_check"), "ok")
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM entity_relationships "
                         "WHERE resolution_status != 'resolved' AND "
                         "(raw_target_ref IS NULL OR raw_target_ref = '')"), 0)


if __name__ == "__main__":
    unittest.main()
