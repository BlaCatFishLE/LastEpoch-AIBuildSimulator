"""Tests for normalization identity, crosswalk merging and evidence writes."""
from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize.base import (  # noqa: E402
    Crosswalk,
    EntityPlan,
    Normalizer,
    make_entity_id,
)
from le_data.records import SourceRecord  # noqa: E402
from le_data.store import apply_migrations, connect  # noqa: E402


def _fake_mapping_module():
    module = types.ModuleType("le_data.normalize.mapping_fixture")

    def map_skill(record):
        raw = record.raw
        return EntityPlan(
            source_record=record,
            kind="skill",
            entity_id=make_entity_id("skill", record.source, record.source_scope, record.source_id),
            canonical_name=raw.get("name"),
            names=[{"name": raw.get("name"), "language": "en"}],
            typed_rows=[
                (
                    "skills",
                    {
                        "entity_id": make_entity_id("skill", record.source, record.source_scope, record.source_id),
                        "skill_type": raw.get("type"),
                        "mana_cost": raw.get("mana"),
                    },
                )
            ],
            evidence=[
                {"field_path": "mana_cost", "raw_value": raw.get("mana"), "normalized_value": raw.get("mana")},
            ],
        )

    module.map_skill = map_skill
    return module


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "n.sqlite"
        self.conn = connect(self.path)
        apply_migrations(self.conn)
        self.conn.execute(
            "INSERT INTO source_snapshots (source_snapshot_id, source, claimed_version, status) "
            "VALUES ('snap-1', 'fixture', '1.5.2', 'ok')"
        )
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def record(self, source_id="aa1", name="Fixture Skill", scope="ability"):
        return SourceRecord(
            source="fixture",
            source_snapshot_id="snap-1",
            resource_name="fixture.js",
            entity_kind="skill",
            source_scope=scope,
            source_id=source_id,
            locator="fixture.js#0",
            raw={"name": name, "type": "active", "mana": 5},
        )

    def test_entity_id_is_scoped(self):
        first = make_entity_id("skill", "fixture", "ability", "aa1")
        second = make_entity_id("skill", "fixture", "ability", "aa1")
        other = make_entity_id("skill", "fixture", "tree", "aa1")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)

    def test_normalize_creates_entity_mapping_and_evidence(self):
        module = _fake_mapping_module()
        with mock.patch("le_data.normalize.base.load_mapping_module", return_value=module):
            normalizer = Normalizer(self.conn, "fixture", "snap-1")
            summary = normalizer.normalize_records([self.record()])
        self.conn.commit()
        self.assertEqual(summary["entity_counts"], {"skill": 1})
        entity = self.conn.execute("SELECT * FROM entities").fetchone()
        self.assertEqual(entity["entity_id"], "skill:fixture:ability:aa1")
        self.assertEqual(entity["canonical_name"], "Fixture Skill")
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM entity_names WHERE entity_id = ?",
                              (entity["entity_id"],)).fetchone()[0],
            1,
        )
        self.assertEqual(self.conn.execute("SELECT mana_cost FROM skills").fetchone()[0], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT mapping_method FROM source_id_map").fetchone()[0],
            "source_authoritative",
        )

    def test_crosswalk_merges_secondary_source(self):
        module = _fake_mapping_module()
        crosswalk = Crosswalk(
            {
                "revision": 2,
                "merges": [
                    {
                        "canonical_entity_id": "skill:other:ability:zz",
                        "members": [
                            {"source": "fixture", "kind": "skill", "scope": "ability",
                             "source_id": "aa1"}
                        ],
                        "method": "test",
                        "evidence": "fixture",
                    }
                ],
            }
        )
        with mock.patch("le_data.normalize.base.load_mapping_module", return_value=module):
            Normalizer(self.conn, "fixture", "snap-1", crosswalk=crosswalk).normalize_records(
                [self.record()]
            )
        self.conn.commit()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 1)
        self.assertEqual(
            self.conn.execute("SELECT entity_id FROM entities").fetchone()[0],
            "skill:other:ability:zz",
        )

    def test_generic_fallback_preserves_unknown_kind(self):
        module = types.ModuleType("le_data.normalize.mapping_fixture")
        with mock.patch("le_data.normalize.base.load_mapping_module", return_value=module):
            Normalizer(self.conn, "fixture", "snap-1").normalize_records(
                [
                    SourceRecord(
                        source="fixture",
                        source_snapshot_id="snap-1",
                        resource_name="fixture.js",
                        entity_kind="mystery",
                        source_scope="scope",
                        source_id="x1",
                        locator="fixture.js#x",
                        raw={"whatever": 1},
                    )
                ]
            )
        self.conn.commit()
        entity = self.conn.execute("SELECT * FROM entities WHERE kind = 'mystery'").fetchone()
        self.assertIsNotNone(entity)
        self.assertIn('"whatever"', entity["extensions"])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM data_issues WHERE issue_type = 'unmapped_entity_kind'")
            .fetchone()[0],
            1,
        )


if __name__ == "__main__":
    unittest.main()
