"""Normalization tests for the Tunklab source (real W1 snapshot, temp SQLite).

The tests load ``data/staging/w1-tunklab/source_records/tunklab.jsonl`` into a
throwaway SQLite database built from ``tools/ingest/migrations`` with
``data/mappings/crosswalk.json`` applied.  Nothing in the repository database,
the staging database or the network is touched.

Run (project root, no pytest required)::

    python -m unittest discover -s tools/ingest/tests -p "test_tunklab_*.py" -v
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.config import load_sources  # noqa: E402
from le_data.normalize.base import (  # noqa: E402
    Crosswalk,
    Normalizer,
    load_mapping_module,
    make_entity_id,
)
from le_data.paths import MAPPINGS_DIR  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import apply_migrations, connect  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = "tunklab-20261101"
SOURCE = "tunklab"
STAGED_JSONL = (
    PROJECT_ROOT / "data" / "staging" / "w1-tunklab" / "source_records" / "tunklab.jsonl"
)
CROSSWALK = MAPPINGS_DIR / "crosswalk.json"

#: crosswalk members the acceptance criteria names explicitly
CROSSWALK_CANONICAL = {
    ("skill_tree", "", "aa710"): "tree:letools:LESkillTrees:aa710",
    ("buff", "", "25"): "buff:letools:UIBuffs:25",
    ("ability", "", "Fireball"): "skill:letools:LEAbilities.abilityList:fi9",
}
#: further members checked against the crosswalk file itself
CROSSWALK_MEMBERS = [
    ("skill_tree", "", "aa710"),
    ("skill_tree_node", "aa710", "0"),
    ("passive_tree", "", "mg-1"),
    ("passive_tree_node", "mg-1", "0"),
    ("buff", "", "25"),
    ("ailment", "", "1"),
    ("affix", "", "0"),
    ("unique_item", "", "0"),
    ("ability", "", "Fireball"),
    ("class", "", "Mage"),
    ("mastery", "Mage", "Mage"),
]


@unittest.skipUnless(STAGED_JSONL.exists(), f"staged source records missing: {STAGED_JSONL}")
class TunklabNormalizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls._tmpdir.name) / "tunklab.sqlite"
        cls.conn = connect(cls.db_path)
        apply_migrations(cls.conn)
        cls.conn.execute(
            "INSERT INTO source_snapshots (source_snapshot_id, source, claimed_version, status) "
            "VALUES (?, ?, '1.5.2', 'ok')",
            (SNAPSHOT, SOURCE),
        )
        cls.conn.commit()
        cls.records = [SourceRecord.from_dict(row) for row in read_jsonl(STAGED_JSONL)]
        cls.crosswalk = Crosswalk.load() if CROSSWALK.exists() else Crosswalk()
        normalizer = Normalizer(cls.conn, SOURCE, SNAPSHOT, crosswalk=cls.crosswalk)
        cls.summary = normalizer.normalize_records(cls.records)
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.conn.close()
        cls._tmpdir.cleanup()

    # -- helpers --------------------------------------------------------------
    def _scalar(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchone()[0]

    def _rows(self, sql: str, *params):
        return self.conn.execute(sql, params).fetchall()

    def _kind_count(self, kind: str) -> int:
        return self._scalar("SELECT COUNT(*) FROM entities WHERE kind = ?", kind)

    def _mapped(self, kind: str, scope: str, source_id: str):
        rows = self._rows(
            "SELECT entity_id, mapping_method FROM source_id_map "
            "WHERE source = ? AND kind = ? AND source_scope = ? AND source_id = ?",
            SOURCE, kind, scope, source_id,
        )
        self.assertEqual(len(rows), 1, f"expected exactly one id mapping for {kind}/{scope}/{source_id}")
        return rows[0]["entity_id"], rows[0]["mapping_method"]

    # -- entity counts --------------------------------------------------------
    def test_entity_kind_counts(self):
        self.assertEqual(self._kind_count("tree"), 141)          # 136 skill + 5 passive
        self.assertEqual(self._kind_count("node"), 4497)         # 3956 + 541
        self.assertEqual(self._kind_count("buff"), 264)
        self.assertEqual(self._kind_count("ailment"), 147)
        self.assertEqual(self._kind_count("affix"), 1156)
        self.assertEqual(self._kind_count("item"), 487)
        self.assertEqual(self._kind_count("class"), 5)
        self.assertEqual(self._kind_count("mastery"), 20)
        self.assertEqual(self._kind_count("monster"), 884)
        self.assertEqual(self._kind_count("quest"), 139)

    def test_ability_entity_count_matches_crosswalk_identity(self):
        expected = set()
        for record in self.records:
            if record.entity_kind != "ability":
                continue
            canonical = self.crosswalk.canonical_for(SOURCE, "ability", "", record.source_id)
            expected.add(canonical or make_entity_id("ability", SOURCE, "", record.source_id))
        self.assertEqual(self._kind_count("ability"), len(expected))
        self.assertGreaterEqual(self._kind_count("ability"), 4000)
        self.assertLessEqual(self._kind_count("ability"), 4350)

    def test_every_source_record_has_a_mapping(self):
        mapped = self._scalar(
            "SELECT COUNT(*) FROM source_id_map WHERE source = ? AND source_snapshot_id = ?",
            SOURCE, SNAPSHOT,
        )
        skipped = sum(self.summary["skipped_kinds"].values())
        unmapped = sum(self.summary["unmapped_kinds"].values())
        self.assertEqual(mapped, len(self.records) - skipped - unmapped)
        self.assertEqual(self._scalar(
            "SELECT COUNT(*) FROM source_id_map WHERE entity_id IS NULL OR entity_id = ''"), 0)

    def test_summary_records_skips_and_unmapped(self):
        skipped_relations = sum(1 for r in self.records if r.entity_kind == "relation")
        self.assertEqual(self.summary["skipped_kinds"].get("relation"), skipped_relations)
        # the single empty skill-tree key is a source artifact, not an entity
        self.assertEqual(self.summary["unmapped_kinds"], {"skill_tree": 1})

    # -- database integrity ---------------------------------------------------
    def test_foreign_keys_and_integrity(self):
        self.assertEqual(list(self.conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(self._scalar("PRAGMA integrity_check"), "ok")

    # -- crosswalk ------------------------------------------------------------
    def test_crosswalk_members_map_to_letools_canonical_ids(self):
        for key, entity_id in CROSSWALK_CANONICAL.items():
            with self.subTest(member=key):
                mapped, method = self._mapped(*key)
                self.assertEqual(mapped, entity_id)
                self.assertEqual(method, "crosswalk")
        for key in CROSSWALK_MEMBERS:
            with self.subTest(member=key):
                mapped, method = self._mapped(*key)
                expected = self.crosswalk.canonical_for(SOURCE, *key)
                self.assertIsNotNone(expected, f"{key} is not a crosswalk member")
                self.assertEqual(mapped, expected)
                self.assertIn(":letools:", mapped)
                self.assertEqual(method, "crosswalk")

    # -- node edges -----------------------------------------------------------
    def test_node_edges_written(self):
        total = self._scalar("SELECT COUNT(*) FROM node_edges")
        self.assertGreater(total, 0)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM node_edges WHERE to_node_entity_id IS NULL"), 0)
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM node_edges WHERE tree_entity_id IS NULL"), 0)
        self.assertGreater(
            self._scalar("SELECT COUNT(*) FROM node_edges WHERE points_required > 0"), 0)

    def test_node_edges_resolve_prerequisite_node(self):
        """BUG (reported): mapping reads requirement['nodeId'], Tunklab sends 'nodeID'."""
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM node_edges WHERE from_node_entity_id IS NULL"),
            0,
            "prerequisite (from) node is never resolved; expected 4777 edges, "
            "currently fewer with from_node_entity_id=NULL",
        )

    # -- relationships --------------------------------------------------------
    def test_relationships_written(self):
        total = self._scalar("SELECT COUNT(*) FROM entity_relationships")
        self.assertGreater(total, 0)
        types = {row[0] for row in self._rows(
            "SELECT DISTINCT relationship_type FROM entity_relationships")}
        for expected in ("has_tree", "triggers_skill", "applies_ailment", "belongs_to_class",
                         "belongs_to_mastery", "modifies_stat"):
            self.assertIn(expected, types)
        # Node prerequisites are stored once, in node_edges; the relationship
        # table must not duplicate them (final design decision).
        self.assertNotIn("requires_node", types)
        self.assertGreater(self._scalar("SELECT COUNT(*) FROM node_edges"), 0)
        # Mastery -> class ownership must exist even though mastery records are
        # crosswalk secondary in the combined dataset.
        self.assertGreater(
            self._scalar(
                "SELECT COUNT(*) FROM entity_relationships "
                "WHERE from_entity_id LIKE 'mastery:%' AND to_entity_id LIKE 'class:%'"
            ),
            0,
        )

    def test_unresolved_relationships_keep_raw_target_reference(self):
        unresolved = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships WHERE resolution_status != 'resolved'")
        self.assertGreater(unresolved, 0)
        missing_ref = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE resolution_status != 'resolved' AND (raw_target_ref IS NULL OR raw_target_ref = '')")
        self.assertEqual(missing_ref, 0)
        both_ends = self._scalar(
            "SELECT COUNT(*) FROM entity_relationships "
            "WHERE resolution_status = 'resolved' "
            "AND (from_entity_id IS NULL OR to_entity_id IS NULL)")
        self.assertEqual(both_ends, 0)

    def test_class_passive_tree_relationship_keeps_reference(self):
        rows = self._rows(
            "SELECT from_entity_id, to_entity_id, raw_target_ref, resolution_status "
            "FROM entity_relationships WHERE relationship_type = 'has_tree' "
            "AND from_entity_id LIKE 'class:%'")
        self.assertEqual(len(rows), 5)
        for row in rows:
            if row["resolution_status"] == "resolved":
                self.assertTrue(row["to_entity_id"])
                self.assertTrue(row["to_entity_id"].startswith("tree:"))
            else:
                self.assertIn("treeId", row["raw_target_ref"] or "")
                self.assertIsNone(row["to_entity_id"])

    # -- mapping semantics documented as known gaps ---------------------------
    def test_ailment_damage_type_uses_damage_vector_order(self):
        """BUG (reported): AT enum order applied to the [phys,fire,cold,light,necro,void,poison] vector.

        Expected Fire for Ignite (module: damage=[0,40,0,0,0,0,0], /ailments page
        shows "40 Fire"); the current mapping yields "Physical".
        """
        mapping = load_mapping_module(SOURCE)
        self.assertIsNotNone(mapping)
        mapping.configure({"crosswalk": self.crosswalk, "translations": {},
                           "source_snapshot_id": SNAPSHOT})
        ignite = next(r for r in self.records
                      if r.entity_kind == "ailment" and r.raw.get("name") == "Ignite")
        plan = mapping.map_ailment(ignite)
        typed = dict(plan.typed_rows)["ailments"]
        self.assertEqual(typed["damage_type"], "Fire")
        self.assertEqual(typed["base_damage"], 40)
        self.assertEqual(typed["duration_ms"], 2500)

    def test_quest_canonical_name_uses_display_name(self):
        """BUG (reported): Tunklab quest payloads carry displayName, not name."""
        mapping = load_mapping_module(SOURCE)
        self.assertIsNotNone(mapping)
        mapping.configure({"crosswalk": self.crosswalk, "translations": {},
                           "source_snapshot_id": SNAPSHOT})
        quest = next(r for r in self.records
                     if r.entity_kind == "quest" and r.raw.get("displayName") == "To Keeper's Camp")
        plan = mapping.map_quest(quest)
        self.assertEqual(plan.canonical_name, "To Keeper's Camp")

    # -- issues ---------------------------------------------------------------
    def test_issues_cover_skipped_and_unmapped_kinds(self):
        """Records that no mapper handles must be skipped explicitly, not dropped."""
        expected_skipped = {
            "relation": sum(1 for r in self.records if r.entity_kind == "relation"),
            "cache": sum(1 for r in self.records if r.entity_kind == "cache"),
            "loot_group": sum(1 for r in self.records if r.entity_kind == "loot_group"),
            "world_config": sum(1 for r in self.records if r.entity_kind == "world_config"),
            "monolith_config": sum(1 for r in self.records if r.entity_kind == "monolith_config"),
        }
        for kind, expected in expected_skipped.items():
            if expected:
                self.assertEqual(self.summary["skipped_kinds"].get(kind), expected, kind)
        # every non-skipped record kind has a dedicated mapper in the current mapping
        self.assertEqual(
            self._scalar("SELECT COUNT(*) FROM data_issues WHERE issue_type = 'unmapped_entity_kind'"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
