"""Tests for effect value -> stat linking (review P1-2) and the Tunklab
``maxInstances=0`` UI semantics (review P1-6).

Building blocks:

* ``StatLinkTests`` constructs a small in-memory-like SQLite database with
  ``store.apply_migrations`` and the four tables the pass touches (entities,
  stats, effects, effect_values).  It covers direct ids, composite qualified
  ids, unknown namespaces, ambiguity, unparsed payloads, description-only
  effects, per-source namespace isolation, owner-kind stats, idempotency and
  foreign-key cleanliness.
* ``TunklabMaxInstancesZeroTests`` calls the Tunklab mappers directly with the
  real shapes (Tunklab buff id 30 is Adrenaline Rush; its ``maxInstances`` is 0)
  and asserts the review's required classification.

Run from the project root (no pytest)::

    python -m unittest tools.ingest.tests.test_stat_link -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize import mapping_tunklab  # noqa: E402
from le_data.normalize.stat_link import (  # noqa: E402
    STATUS_AMBIGUOUS,
    STATUS_COMPOSITE,
    STATUS_LINKED,
    STATUS_NOT_APPLICABLE,
    STATUS_UNKNOWN_NAMESPACE,
    STATUS_UNPARSED,
    link_stats,
)
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import apply_migrations, connect, insert_row  # noqa: E402

CREATED_AT = "2026-11-01T00:00:00+00:00"
LETOOLS = "letools"
TUNKLAB = "tunklab"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
STAGED_TUNKLAB = (
    PROJECT_ROOT / "data" / "staging" / "w1-tunklab" / "source_records" / "tunklab.jsonl"
)


# -- fixture helpers --------------------------------------------------------
def _entity(conn, entity_id: str, kind: str, source: str) -> None:
    insert_row(
        conn,
        "entities",
        {
            "entity_id": entity_id,
            "kind": kind,
            "canonical_name": entity_id.split(":")[-1],
            "record_status": "active",
            "primary_source": source,
            "created_at": CREATED_AT,
        },
    )


def _stat(conn, stat_id: str, name: str, raw_enum: str, source_scope: str) -> None:
    insert_row(
        conn,
        "stats",
        {
            "stat_id": stat_id,
            "name": name,
            "raw_enum": raw_enum,
            "source_scope": source_scope,
        },
    )


def _effect(conn, effect_id: str, owner_id: str, category: str, extensions=None) -> None:
    insert_row(
        conn,
        "effects",
        {
            "effect_id": effect_id,
            "owner_entity_id": owner_id,
            "effect_category": category,
            "parse_status": "parsed",
            "extensions": json.dumps(extensions) if extensions is not None else None,
        },
    )


def _value(conn, value_id: str, effect_id: str, raw) -> None:
    insert_row(
        conn,
        "effect_values",
        {
            "value_id": value_id,
            "effect_id": effect_id,
            "raw": json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else raw,
        },
    )


class StatLinkTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.conn = connect(Path(tmp.name) / "stat_link.sqlite")
        apply_migrations(self.conn)

        # -- entities -------------------------------------------------------
        _entity(self.conn, "node:letools:fl44:17", "node", LETOOLS)
        _entity(self.conn, "item:letools:uniqueList.uniques:0", "item", LETOOLS)
        _entity(self.conn, "item:tunklab:_:248", "item", TUNKLAB)

        # -- stat namespaces ------------------------------------------------
        _stat(self.conn, "stat:letools:coreDB.propertyList:0", "Damage", "0", "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:1", "Ailment Chance", "1",
              "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:3", "Cast Speed", "3",
              "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:10", "Armor", "10",
              "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:51", "letools leech", "51",
              "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:69", "Mana Efficiency", "69",
              "coreDB.propertyList")
        # player-property ids must not be used to resolve effect property ids
        _stat(self.conn, "stat:letools:coreDB.playerPropertyList:200",
              "player conditional stat", "200", "coreDB.playerPropertyList")
        _stat(self.conn, "stat:letools:coreDB.playerPropertyList:0",
              "player stat sharing id 0", "0", "coreDB.playerPropertyList")
        _stat(self.conn, "stat:letools:coreDB.abilityPropertyList:1.0",
              "Pierce Chance with Fireball", "1.0", "coreDB.abilityPropertyList")
        _stat(self.conn, "stat:tunklab:_:51", "Damage Leeched as Health", "51", "")
        _stat(self.conn, "stat:tunklab:_:0", "Damage", "0", "")
        # duplicate raw enum in one namespace -> ambiguous, stat_id stays NULL
        _stat(self.conn, "stat:letools:coreDB.propertyList:999", "dupe A", "999",
              "coreDB.propertyList")
        _stat(self.conn, "stat:letools:coreDB.propertyList:999b", "dupe B", "999",
              "coreDB.propertyList")

        # -- effects + effect values ---------------------------------------
        _effect(self.conn, "node:letools:fl44:17#stat#0", "node:letools:fl44:17", "stat_mod")
        _value(self.conn, "node:letools:fl44:17#stat#0#v0", "node:letools:fl44:17#stat#0",
               {"property": 69, "tags": 0, "value": "+15%"})  # linked

        _effect(self.conn, "item:letools:uniqueList.uniques:0#implicit#0",
                "item:letools:uniqueList.uniques:0", "implicit_stat",
                extensions={"implicit": {"property": 10, "tags": 0}})
        _value(self.conn, "item:letools:uniqueList.uniques:0#implicit#0#v0",
               "item:letools:uniqueList.uniques:0#implicit#0",
               {"property": 10, "specialTag": 0, "tags": 0, "extraTag": 0,
                "type": 0, "implicitValue": 40, "implicitMaxValue": 40})  # linked

        _effect(self.conn, "item:letools:uniqueList.uniques:0#mod#1",
                "item:letools:uniqueList.uniques:0", "unique_mod")
        _value(self.conn, "item:letools:uniqueList.uniques:0#mod#1#v0",
               "item:letools:uniqueList.uniques:0#mod#1",
               {"value": 1, "maxValue": 1.5, "property": 1, "specialTag": 1,
                "tags": 8, "extraTag": 0, "type": 0})  # composite

        # skill stat property id from the unexported skill-stat id space
        _effect(self.conn, "node:letools:fl44:17#stat#1", "node:letools:fl44:17", "stat_mod")
        _value(self.conn, "node:letools:fl44:17#stat#1#v0", "node:letools:fl44:17#stat#1",
               {"property": 5015, "tags": 0})  # unknown_namespace

        # item property 98 has no stat row in Tunklab/LE Tools property lists
        _effect(self.conn, "item:letools:uniqueList.uniques:0#implicit#9",
                "item:letools:uniqueList.uniques:0", "implicit_stat")
        _value(self.conn, "item:letools:uniqueList.uniques:0#implicit#9#v0",
               "item:letools:uniqueList.uniques:0#implicit#9",
               {"property": 98, "tags": 57})  # unknown_namespace

        # a player-property id must stay unresolved for a node stat
        _effect(self.conn, "node:letools:fl44:17#stat#2", "node:letools:fl44:17", "stat_mod")
        _value(self.conn, "node:letools:fl44:17#stat#2#v0", "node:letools:fl44:17#stat#2",
               {"property": 200, "tags": 0})  # unknown_namespace

        _effect(self.conn, "node:letools:fl44:17#stat#3", "node:letools:fl44:17", "stat_mod")
        _value(self.conn, "node:letools:fl44:17#stat#3#v0", "node:letools:fl44:17#stat#3",
               {"property": 999, "tags": 0})  # ambiguous

        _effect(self.conn, "node:letools:fl44:17#stat#4", "node:letools:fl44:17", "stat_mod")
        _value(self.conn, "node:letools:fl44:17#stat#4#v0", "node:letools:fl44:17#stat#4",
               "{not valid json")  # unparsed

        _effect(self.conn, "node:letools:fl44:17#stat#5", "node:letools:fl44:17",
                "description")
        _value(self.conn, "node:letools:fl44:17#stat#5#v0", "node:letools:fl44:17#stat#5",
               {"value": 1})  # not_applicable (no property field)

        # Tunklab effect: property 51 must resolve in the Tunklab namespace
        _effect(self.conn, "effect:tunklab-hash", "item:tunklab:_:248", "unique_mod",
                extensions={"mod": {"property": 51}})
        _value(self.conn, "effect:tunklab-hash:0", "effect:tunklab-hash",
               {"property": 51, "specialTag": 0, "tags": 0, "type": 2})
        self.conn.commit()

        self.summary = link_stats(self.conn)

    def tearDown(self) -> None:
        self.conn.close()

    # -- helpers ------------------------------------------------------------
    def row(self, value_id: str):
        return self.conn.execute(
            "SELECT stat_id, stat_link_status, stat_link_evidence "
            "FROM effect_values WHERE value_id = ?",
            (value_id,),
        ).fetchone()

    def evidence(self, value_id: str) -> dict:
        return json.loads(self.row(value_id)["stat_link_evidence"])

    # -- classification -----------------------------------------------------
    def test_total_and_status_counts(self):
        self.assertEqual(self.summary["total"], 10)
        self.assertEqual(self.summary["linked"], 3)
        self.assertEqual(self.summary["composite"], 1)
        self.assertEqual(self.summary["unknown_namespace"], 3)
        self.assertEqual(self.summary["ambiguous"], 1)
        self.assertEqual(self.summary["unparsed"], 1)
        self.assertEqual(self.summary["not_applicable"], 1)
        self.assertEqual(self.summary["composite_with_base_stat"], 1)
        self.assertEqual(sum(self.summary["by_status"].values()), 10)

    def test_direct_id_links_unique_stat_in_letools_namespace(self):
        row = self.row("node:letools:fl44:17#stat#0#v0")
        self.assertEqual(row["stat_link_status"], STATUS_LINKED)
        self.assertEqual(row["stat_id"], "stat:letools:coreDB.propertyList:69")
        evidence = self.evidence("node:letools:fl44:17#stat#0#v0")
        self.assertEqual(evidence["namespace"], "letools:coreDB.propertyList")
        self.assertEqual(evidence["match_rule"], "direct_id_unique_in_namespace")
        self.assertEqual(evidence["property"], 69)
        self.assertEqual(evidence["field"], "raw.property")
        # value semantics (type) are evidence, not a composite qualifier
        self.assertEqual(
            self.row("item:letools:uniqueList.uniques:0#implicit#0#v0")["stat_id"],
            "stat:letools:coreDB.propertyList:10",
        )

    def test_qualified_id_is_composite_but_keeps_base_stat(self):
        row = self.row("item:letools:uniqueList.uniques:0#mod#1#v0")
        self.assertEqual(row["stat_link_status"], STATUS_COMPOSITE)
        self.assertEqual(row["stat_id"], "stat:letools:coreDB.propertyList:1")
        evidence = self.evidence("item:letools:uniqueList.uniques:0#mod#1#v0")
        self.assertEqual(evidence["match_rule"], "base_property_with_qualifier")
        self.assertEqual(evidence["qualifiers"], {"specialTag": 1, "tags": 8})
        self.assertEqual(evidence["value_semantics"], {"type": 0})

    def test_unknown_property_id_is_classified_not_faked(self):
        for value_id in (
            "node:letools:fl44:17#stat#1#v0",   # skill-stat id space (5015)
            "item:letools:uniqueList.uniques:0#implicit#9#v0",  # 98 has no stat row
            "node:letools:fl44:17#stat#2#v0",   # only exists in playerPropertyList
        ):
            with self.subTest(value_id=value_id):
                row = self.row(value_id)
                self.assertEqual(row["stat_link_status"], STATUS_UNKNOWN_NAMESPACE)
                self.assertIsNone(row["stat_id"])
                self.assertEqual(
                    self.evidence(value_id)["match_rule"],
                    "no_stat_for_property_in_namespace",
                )

    def test_duplicate_stat_id_is_ambiguous_without_link(self):
        row = self.row("node:letools:fl44:17#stat#3#v0")
        self.assertEqual(row["stat_link_status"], STATUS_AMBIGUOUS)
        self.assertIsNone(row["stat_id"])
        evidence = self.evidence("node:letools:fl44:17#stat#3#v0")
        self.assertEqual(evidence["match_rule"], "multiple_stat_candidates")
        candidates = evidence["candidates"]["letools:coreDB.propertyList"]
        self.assertEqual(len(candidates), 2)

    def test_unparsed_and_not_applicable_are_distinct(self):
        unparsed = self.row("node:letools:fl44:17#stat#4#v0")
        self.assertEqual(unparsed["stat_link_status"], STATUS_UNPARSED)
        self.assertIsNone(unparsed["stat_id"])
        self.assertEqual(self.evidence("node:letools:fl44:17#stat#4#v0")["match_rule"],
                         "raw_payload_not_json")
        not_applicable = self.row("node:letools:fl44:17#stat#5#v0")
        self.assertEqual(not_applicable["stat_link_status"], STATUS_NOT_APPLICABLE)
        self.assertIsNone(not_applicable["stat_id"])
        self.assertEqual(self.evidence("node:letools:fl44:17#stat#5#v0")["match_rule"],
                         "no_property_field_in_effect")

    def test_source_namespaces_do_not_cross_link(self):
        row = self.row("effect:tunklab-hash:0")
        self.assertEqual(row["stat_link_status"], STATUS_LINKED)
        self.assertEqual(row["stat_id"], "stat:tunklab:_:51")
        evidence = self.evidence("effect:tunklab-hash:0")
        self.assertEqual(evidence["namespace"], "tunklab:property")
        self.assertEqual(evidence["source"], TUNKLAB)

    def test_owner_kind_and_source_distribution(self):
        by_kind = self.summary["by_owner_kind"]
        self.assertIn("node", by_kind)
        self.assertIn("item", by_kind)
        self.assertEqual(by_kind["node"]["linked"], 1)
        self.assertEqual(by_kind["node"]["unknown_namespace"], 2)
        self.assertEqual(by_kind["item"]["composite"], 1)
        self.assertEqual(by_kind["item"]["linked"], 2)
        self.assertEqual(by_kind["item"]["unknown_namespace"], 1)
        by_source = self.summary["by_source"]
        self.assertEqual(by_source["letools"]["composite"], 1)
        self.assertEqual(by_source["tunklab"]["linked"], 1)
        self.assertEqual(self.summary["namespaces"]["letools:coreDB.propertyList"], 8)

    # -- idempotency / integrity -------------------------------------------
    def test_link_is_idempotent(self):
        snapshot = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT value_id, stat_id, stat_link_status, stat_link_evidence "
                "FROM effect_values ORDER BY value_id"
            )
        ]
        second = link_stats(self.conn)
        self.assertEqual(second["updated"], 0)
        self.assertEqual(second["unchanged"], second["total"])
        self.assertEqual(second["linked"], self.summary["linked"])
        after = [
            tuple(row)
            for row in self.conn.execute(
                "SELECT value_id, stat_id, stat_link_status, stat_link_evidence "
                "FROM effect_values ORDER BY value_id"
            )
        ]
        self.assertEqual(snapshot, after)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM effect_values").fetchone()[0],
            self.summary["total"],
        )

    def test_linked_rows_are_not_rewritten_on_second_call(self):
        before = self.conn.total_changes
        link_stats(self.conn)
        self.assertEqual(self.conn.total_changes, before)
        linked = self.row("node:letools:fl44:17#stat#0#v0")
        self.assertEqual(linked["stat_link_status"], STATUS_LINKED)

    def test_foreign_keys_stay_clean(self):
        self.assertEqual(list(self.conn.execute("PRAGMA foreign_key_check")), [])
        self.assertEqual(self.conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        # every non-null stat_id must reference an existing stats row
        dangling = self.conn.execute(
            "SELECT COUNT(*) FROM effect_values ev LEFT JOIN stats s "
            "ON s.stat_id = ev.stat_id WHERE ev.stat_id IS NOT NULL AND s.stat_id IS NULL"
        ).fetchone()[0]
        self.assertEqual(dangling, 0)


class TunklabMaxInstancesZeroTests(unittest.TestCase):
    """Tunklab buff/ailment ``maxInstances=0`` is a UI value, not "uncapped"."""

    @staticmethod
    def _buff_record(raw: dict) -> SourceRecord:
        return SourceRecord(
            source=TUNKLAB,
            source_snapshot_id="synthetic-tunklab-snapshot",
            resource_name="tunklab_buffs.jsonl",
            entity_kind="buff",
            source_scope="",
            source_id=str(raw["id"]),
            locator=f"tunklab_buffs.jsonl#{raw['id']}",
            raw=raw,
        )

    def _mapped_buff(self, raw: dict) -> tuple[dict, list[dict]]:
        plan = mapping_tunklab.map_buff(self._buff_record(raw))
        typed = dict(plan.typed_rows)["buffs"]
        return typed, plan.evidence

    def test_adrenaline_rush_zero_max_instances_is_raw_ui(self):
        """Real Tunklab buff id 30 (Adrenaline Rush) has maxInstances=0."""
        raw = {
            "id": 30,
            "displayName": "Adrenaline Rush",
            "maxInstances": 0,
            "positive": True,
            "noFixedDuration": False,
            "description": "5% more Flurry damage and 5% increased attack speed",
            "newStacksRefreshDuration": False,
        }
        typed, evidence = self._mapped_buff(raw)
        self.assertIsNone(typed["max_stacks"])
        self.assertEqual(typed["ui_max_instances"], 0)
        self.assertEqual(typed["max_stacks_basis"], "raw_ui")
        lifecycle = json.loads(typed["lifecycle"])
        self.assertEqual(lifecycle["raw_max_instances"], 0)
        self.assertEqual(lifecycle["ui_semantics"], "unknown")
        self.assertNotIn("uncapped", json.dumps(lifecycle))
        max_stacks = next(e for e in evidence if e["field_path"] == "max_stacks")
        self.assertEqual(max_stacks["evidence_status"], "unparsed")
        self.assertIsNone(max_stacks["normalized_value"])
        self.assertEqual(max_stacks["raw_value"], 0)
        self.assertIn("not established", max_stacks["notes"])
        ui = next(e for e in evidence if e["field_path"] == "ui_max_instances")
        self.assertEqual(ui["raw_value"], 0)
        self.assertEqual(ui["normalized_value"], 0)
        self.assertEqual(ui["evidence_status"], "known")

    def test_nonzero_max_instances_still_numeric(self):
        typed, _ = self._mapped_buff(
            {"id": 80, "displayName": "Abyssal Rite", "maxInstances": 10,
             "positive": True, "noFixedDuration": True}
        )
        self.assertEqual(typed["max_stacks"], 10)
        self.assertEqual(typed["ui_max_instances"], 10)
        self.assertEqual(typed["max_stacks_basis"], "raw_ui")
        self.assertEqual(
            json.loads(typed["lifecycle"])["max_instances_interpretation"], "numeric"
        )

    def test_real_staged_adrenaline_rush_record_maps_to_raw_ui_zero(self):
        """The real staged Tunklab record (id=30) must reproduce the semantics."""
        if not STAGED_TUNKLAB.exists():  # pragma: no cover - data snapshot optional
            self.skipTest(f"staged Tunklab records missing: {STAGED_TUNKLAB}")
        records = [SourceRecord.from_dict(row) for row in read_jsonl(STAGED_TUNKLAB)]
        buff = next(
            r for r in records
            if r.entity_kind == "buff" and r.raw.get("displayName") == "Adrenaline Rush"
        )
        self.assertEqual(str(buff.source_id), "30")
        self.assertEqual(buff.raw["maxInstances"], 0)
        typed, _ = self._mapped_buff(buff.raw)
        self.assertIsNone(typed["max_stacks"])
        self.assertEqual(typed["ui_max_instances"], 0)
        self.assertEqual(typed["max_stacks_basis"], "raw_ui")
        self.assertNotIn("uncapped", json.dumps(json.loads(typed["lifecycle"])))

    def test_ailment_zero_max_instances_has_unknown_ui_semantics(self):
        record = SourceRecord(
            source=TUNKLAB,
            source_snapshot_id="synthetic-tunklab-snapshot",
            resource_name="tunklab_ailments.jsonl",
            entity_kind="ailment",
            source_scope="",
            source_id="100",
            locator="tunklab_ailments.jsonl#100",
            raw={
                "id": 100,
                "displayName": "Ignite",
                "duration": 2.5,
                "baseDamage": {"damage": [0, 40, 0, 0, 0, 0, 0]},
                "maxInstances": 0,
                "tags": [4],
            },
        )
        plan = mapping_tunklab.map_ailment(record)
        typed = dict(plan.typed_rows)["ailments"]
        stack_rule = json.loads(typed["stack_rule"])
        self.assertEqual(stack_rule["maxInstances"], 0)
        self.assertEqual(stack_rule["ui_semantics"], "unknown")
        self.assertNotIn("uncapped", json.dumps(stack_rule))
        max_stacks = next(e for e in plan.evidence if e["field_path"] == "max_stacks")
        self.assertEqual(max_stacks["evidence_status"], "unparsed")
        self.assertIsNone(max_stacks["normalized_value"])
        self.assertEqual(max_stacks["raw_value"], 0)
        self.assertIn("not established", max_stacks["notes"])


if __name__ == "__main__":
    unittest.main()
