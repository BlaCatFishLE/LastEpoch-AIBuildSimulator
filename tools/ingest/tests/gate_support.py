"""Synthetic fixtures shared by the review validation/publish gate tests.

The fixtures are intentionally tiny and artificial: they exercise the gate SQL
and the immutable publish contract, not real game data.  Every test that uses
them works in a temporary SQLite file; no real ``data/current.json`` or release
directory is touched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

INGEST_DIR = Path(__file__).resolve().parents[1]
SRC_DIR = INGEST_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    upsert_dataset_meta,
)

TARGET_VERSION = "1.5.2"
TARGET_SEASON = "Season 5"
TIMESTAMP = "2026-11-01T00:00:00+00:00"

LETOOLS_SNAPSHOT = "letools-v152-review"
TUNKLAB_SNAPSHOT = "tunklab-v152-review"


def _version_evidence(source: str) -> object:
    if source == "letools":
        return [
            {
                "kind": "js_global_variable",
                "name": "dataVersion",
                "raw_value": "version152",
                "locator": "window['dataVersion']",
                "url": "https://example.invalid/skills/",
            },
            {
                "kind": "js_global_variable",
                "name": "gameVersion",
                "raw_value": "Version 1.5.2",
                "locator": "window.gameVersion",
                "url": "https://example.invalid/data/version152/db/js/x.js",
            },
            {"kind": "config_claim", "value": "1.5.2", "source": "config"},
        ]
    return {
        "claimed_version": "1.5.2",
        "game_version_banners": [{"resource_name": "page_root", "page": "/", "text": "1.5.2"}],
        "claimed_version_pages": {"/": 1, "/skillsearch": 0},
        "site_counts": {"Skills": 136},
        "module_versions": {},
    }


def compatibility_conclusion(
    *,
    decision: str = "compatible",
    sources: dict[str, str] | None = None,
    target: str = TARGET_VERSION,
    evidence: str = "claimed page versions equal the target; module version evidence is "
    "stored in source_snapshots.version_evidence",
) -> dict:
    return {
        "target_game_version": target,
        "target_season": TARGET_SEASON,
        "sources": sources if sources is not None else {"letools": TARGET_VERSION, "tunklab": TARGET_VERSION},
        "decision": decision,
        "evidence": evidence,
    }


def insert_snapshots(conn) -> None:
    for source, snapshot_id in (
        ("letools", LETOOLS_SNAPSHOT),
        ("tunklab", TUNKLAB_SNAPSHOT),
    ):
        insert_row(
            conn,
            "source_snapshots",
            {
                "source_snapshot_id": snapshot_id,
                "source": source,
                "claimed_version": TARGET_VERSION,
                "version_evidence": json.dumps(_version_evidence(source), ensure_ascii=False),
                "fetch_mode": "cache",
                "status": "ok",
                "entry_url": f"https://example.invalid/{source}",
            },
        )


def _insert_entity(
    conn,
    entity_id: str,
    kind: str,
    name: str,
    *,
    source: str = "letools",
    snapshot_id: str = LETOOLS_SNAPSHOT,
    source_scope: str = "gate",
    source_id: str | None = None,
) -> None:
    insert_row(
        conn,
        "entities",
        {
            "entity_id": entity_id,
            "kind": kind,
            "canonical_name": name,
            "record_status": "active",
            "primary_source": source,
            "created_at": TIMESTAMP,
        },
    )
    insert_row(
        conn,
        "entity_names",
        {
            "entity_id": entity_id,
            "language": "en",
            "name": name,
            "is_alias": 0,
            "source": source,
        },
    )
    insert_row(
        conn,
        "source_id_map",
        {
            "mapping_id": f"map:{entity_id}",
            "source_snapshot_id": snapshot_id,
            "source": source,
            "kind": kind,
            "source_scope": source_scope,
            "source_id": source_id or entity_id,
            "entity_id": entity_id,
            "mapping_method": "source_authoritative",
            "created_at": TIMESTAMP,
        },
    )


def build_gate_db(
    path: Path,
    *,
    season_entity: bool = True,
    season_issue: bool = False,
    buff_relationship: bool = True,
    stat_linked: bool = True,
) -> Path:
    """Build a small dataset that satisfies the review gates."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        apply_migrations(conn)
        upsert_dataset_meta(
            conn,
            "gate-dataset",
            status="valid",
            target_game_version=TARGET_VERSION,
            target_season=TARGET_SEASON,
            compatibility=json.dumps(compatibility_conclusion(), ensure_ascii=False),
        )
        insert_snapshots(conn)

        _insert_entity(conn, "skill:gate:a1", "skill", "Gate Skill", source_scope="abilities", source_id="a1")
        insert_row(conn, "skills", {"entity_id": "skill:gate:a1", "skill_type": "active", "mana_cost": 5.0})

        _insert_entity(conn, "buff:gate:b1", "buff", "Gate Buff", source_scope="buffs", source_id="b1")
        insert_row(
            conn,
            "buffs",
            {"entity_id": "buff:gate:b1", "buff_category": "buff", "duration_ms": 4000, "max_stacks": 4},
        )

        _insert_entity(conn, "idol:gate:i1", "idol", "Gate Idol", source_scope="items", source_id="i1")
        insert_row(conn, "idols", {"entity_id": "idol:gate:i1", "idol_size": "small"})

        _insert_entity(conn, "blessing:gate:bl1", "blessing", "Gate Blessing", source_scope="blessings", source_id="bl1")
        insert_row(conn, "blessings", {"entity_id": "blessing:gate:bl1", "blessing_type": "monolith"})

        if season_entity:
            _insert_entity(conn, "season:gate:s1", "season_entity", "Gate Weaver Node", source_scope="weaver", source_id="wn1")
            insert_row(conn, "season_entities", {"entity_id": "season:gate:s1", "season_system": "weaver"})

        # effect -> stat link (P1-2 result)
        insert_row(conn, "stats", {"stat_id": "stat:gate:1", "name": "Gate Stat", "unit": "flat"})
        insert_row(
            conn,
            "effects",
            {
                "effect_id": "effect:gate:1",
                "owner_entity_id": "skill:gate:a1",
                "effect_category": "stat_mod",
                "parse_status": "parsed",
            },
        )
        insert_row(
            conn,
            "effect_values",
            {
                "value_id": "value:gate:1",
                "effect_id": "effect:gate:1",
                "stat_id": "stat:gate:1" if stat_linked else None,
                "value_kind": "flat",
                "value_numeric": 5.0,
                "stat_link_status": "linked" if stat_linked else None,
            },
        )

        # buff relationship (P1-1 result)
        if buff_relationship:
            insert_row(
                conn,
                "entity_relationships",
                {
                    "relationship_id": "rel:gate:buff",
                    "source_snapshot_id": LETOOLS_SNAPSHOT,
                    "source": "letools",
                    "from_entity_id": "skill:gate:a1",
                    "relationship_type": "grants_buff",
                    "to_entity_id": "buff:gate:b1",
                    "resolution_status": "resolved",
                    "evidence": json.dumps({"node": "gate"}, ensure_ascii=False),
                },
            )

        # Source records: non-empty buff config, mapped buff record, weaver records.
        insert_row(
            conn,
            "source_records",
            {
                "record_id": "rec:gate:buff-config",
                "source_snapshot_id": LETOOLS_SNAPSHOT,
                "source": "letools",
                "resource_name": "planner.js",
                "entity_kind": "buff",
                "source_scope": "buffs",
                "source_id": "b1",
                "locator": "planner.js#buff:b1",
                "raw_payload": json.dumps(
                    {"buffs": [{"id": 60, "property": 10, "value": 0.1}]}, ensure_ascii=False
                ),
                "parse_status": "parsed",
            },
        )
        for record_id, source_id in (
            ("rec:gate:weaver-node", "wn1"),
            ("rec:gate:weaver-tree", "wt1"),
        ):
            kind = "weaver_node" if "node" in record_id else "weaver_tree"
            insert_row(
                conn,
                "source_records",
                {
                    "record_id": record_id,
                    "source_snapshot_id": LETOOLS_SNAPSHOT,
                    "source": "letools",
                    "resource_name": "weaver.js",
                    "entity_kind": kind,
                    "source_scope": "weaver",
                    "source_id": source_id,
                    "locator": f"weaver.js#{source_id}",
                    "raw_payload": json.dumps({kind: source_id}, ensure_ascii=False),
                    "parse_status": "parsed",
                },
            )
            if season_entity:
                insert_row(
                    conn,
                    "source_id_map",
                    {
                        "mapping_id": f"map:weaver:{source_id}",
                        "source_snapshot_id": LETOOLS_SNAPSHOT,
                        "source": "letools",
                        "kind": kind,
                        "source_scope": "weaver",
                        "source_id": source_id,
                        "entity_id": "season:gate:s1",
                        "mapping_method": "source_authoritative",
                        "created_at": TIMESTAMP,
                    },
                )

        if season_issue:
            insert_row(
                conn,
                "data_issues",
                {
                    "issue_id": "issue:gate:season-absent",
                    "issue_type": "season_source_absent",
                    "severity": "warning",
                    "source": "letools",
                    "source_snapshot_id": LETOOLS_SNAPSHOT,
                    "field_path": "season_entity",
                    "message": "the letools source exposes no normalized season entity family",
                    "status": "open",
                    "created_at": TIMESTAMP,
                },
            )
        conn.commit()
    finally:
        conn.close()
    return path


def make_dataset_db(path: Path, dataset_id: str, status: str, entity_id: str) -> Path:
    """Create a minimal normalized dataset database for publish tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        apply_migrations(conn)
        upsert_dataset_meta(
            conn,
            dataset_id,
            status=status,
            target_game_version=TARGET_VERSION,
            target_season=TARGET_SEASON,
        )
        insert_row(
            conn,
            "entities",
            {
                "entity_id": entity_id,
                "kind": "skill",
                "canonical_name": entity_id,
                "record_status": "active",
                "primary_source": "letools",
                "created_at": TIMESTAMP,
            },
        )
        conn.commit()
    finally:
        conn.close()
    return path


def add_entity(db_path: Path, entity_id: str) -> None:
    """Mutate an existing dataset database so its logical fingerprint changes."""
    conn = connect(db_path)
    try:
        insert_row(
            conn,
            "entities",
            {
                "entity_id": entity_id,
                "kind": "skill",
                "canonical_name": entity_id,
                "record_status": "active",
                "primary_source": "letools",
                "created_at": TIMESTAMP,
            },
        )
        conn.commit()
    finally:
        conn.close()


def sqlite_state(conn, sql: str, params: tuple = ()):
    return conn.execute(sql, params).fetchall()
