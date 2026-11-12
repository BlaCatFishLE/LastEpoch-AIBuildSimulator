"""Validate the letools -> normalized schema mapping without the full CLI run.

Loads ``data/staging/<run>/source_records/letools.jsonl``, recreates the
provenance + source_records rows the way ``le-data normalize`` does, runs the
Normalizer with ``le_data.normalize.mapping_letools``, then runs the phase-1
acceptance queries (entity counts, field evidence, FK/integrity checks,
Fireball / Avalanche lookups, unresolved relationship count).

Usage (project root):

  python tools/ingest/scripts/letools_normalize_check.py --run w1-letools
  python tools/ingest/scripts/letools_normalize_check.py --run w1-letools \
      --db data/staging/w1-letools/letools_check.sqlite --keep

Exit code 0 when the database is structurally clean and the Fixture lookups
succeed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.normalize.base import Crosswalk, Normalizer  # noqa: E402
from le_data.paths import staging_dir  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    checkpoint,
    connect,
    insert_row,
    integrity_report,
    logical_fingerprint,
    table_counts,
)

FINGERPRINT_TABLES = [
    "entities", "entity_names", "source_id_map", "classes", "masteries", "skills",
    "skill_components", "trees", "nodes", "node_edges", "tags", "entity_tags",
    "stats", "effects", "effect_values", "buffs", "ailments", "items", "affixes",
    "affix_tiers", "set_memberships", "minions", "entity_relationships",
    "field_evidence", "data_issues", "translations",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", required=True)
    parser.add_argument("--snapshot", default="letools-20261101")
    parser.add_argument("--db", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    run_dir = staging_dir(args.run)
    records_path = run_dir / "source_records" / "letools.jsonl"
    if not records_path.exists():
        print(f"missing source records: {records_path}")
        return 1
    db_path = Path(args.db) if args.db else run_dir / "letools_check.sqlite"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()

    records: list[SourceRecord] = []
    for row in read_jsonl(records_path):
        records.append(SourceRecord.from_dict(row))
    print(f"[check] {len(records)} source records from {records_path}")

    conn = connect(db_path)
    result: dict = {"run": args.run, "snapshot": args.snapshot, "db": str(db_path)}
    try:
        apply_migrations(conn)
        # provenance + source_records (same content le-data normalize writes)
        inventory_path = ROOT / "data" / "raw" / "letools" / args.snapshot / "inventory.json"
        fetch_manifest_path = ROOT / "data" / "raw" / "letools" / args.snapshot / "fetch_manifest.json"
        inventory = json.loads(inventory_path.read_text(encoding="utf-8")) if inventory_path.exists() else {}
        manifest = json.loads(fetch_manifest_path.read_text(encoding="utf-8")) if fetch_manifest_path.exists() else {}
        insert_row(conn, "source_snapshots", {
            "source_snapshot_id": args.snapshot,
            "source": "letools",
            "claimed_version": inventory.get("claimed_version"),
            "version_evidence": json.dumps(inventory.get("version_evidence"), ensure_ascii=False)
            if inventory.get("version_evidence") else None,
            "fetch_mode": "cache" if manifest.get("offline") else "auto",
            "fetch_started_at": manifest.get("fetched_at"),
            "fetch_finished_at": manifest.get("fetched_at"),
            "status": inventory.get("status", "ok"),
            "entry_url": inventory.get("entry_url"),
            "notes": None,
        }, or_replace=True)
        for res in manifest.get("results", []):
            insert_row(conn, "raw_resources", {
                "resource_id": f"{args.snapshot}:{res.get('resource_name')}",
                "source_snapshot_id": args.snapshot,
                "source": "letools",
                "resource_name": res.get("resource_name"),
                "url": res.get("url"),
                "local_path": res.get("path"),
                "sha256": res.get("sha256"),
                "size_bytes": res.get("size"),
                "content_type": res.get("content_type"),
                "http_status": res.get("http_status"),
                "etag": res.get("etag"),
                "last_modified": res.get("last_modified"),
                "fetched_at": res.get("fetched_at"),
                "fetch_status": res.get("fetch_status"),
                "role": res.get("role"),
                "notes": res.get("error"),
            }, or_ignore=True)
        for record in records:
            insert_row(conn, "source_records", {
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
                "notes": "; ".join(record.notes) if record.notes else None,
            }, or_ignore=True)
        conn.commit()

        normalizer = Normalizer(conn, "letools", args.snapshot,
                                crosswalk=Crosswalk.load(), logger=print)
        summary = normalizer.normalize_records(records)
        conn.commit()
        result["normalizer_summary"] = summary

        # ---------------- acceptance queries ----------------
        by_kind = dict(conn.execute(
            "SELECT kind, COUNT(*) FROM entities GROUP BY kind ORDER BY COUNT(*) DESC"
        ).fetchall())
        result["entities_by_kind"] = by_kind
        result["table_counts"] = table_counts(conn, FINGERPRINT_TABLES)
        result["integrity"] = integrity_report(conn)

        fireball = conn.execute(
            "SELECT e.entity_id, e.kind, e.canonical_name, s.skill_type, s.mana_cost, "
            "       s.cast_time_ms, s.cooldown_ms, s.class_entity_id, s.mastery_entity_id "
            "FROM entity_names n JOIN entities e ON e.entity_id = n.entity_id "
            "LEFT JOIN skills s ON s.entity_id = e.entity_id "
            "WHERE n.name = 'Fireball'"
        ).fetchall()
        result["fireball"] = [dict(row) for row in fireball]

        avalanche = conn.execute(
            "SELECT e.entity_id, e.kind, e.canonical_name, nd.node_type, nd.max_points, "
            "       nd.position_x, nd.position_y, nd.position_w, nd.position_h, "
            "       t.source_tree_id, t.tree_kind "
            "FROM entity_names n JOIN entities e ON e.entity_id = n.entity_id "
            "LEFT JOIN nodes nd ON nd.entity_id = e.entity_id "
            "LEFT JOIN trees t ON t.entity_id = nd.tree_entity_id "
            "WHERE n.name = 'Avalanche'"
        ).fetchall()
        result["avalanche"] = [dict(row) for row in avalanche]

        result["relationships_by_status"] = dict(conn.execute(
            "SELECT resolution_status, COUNT(*) FROM entity_relationships "
            "GROUP BY resolution_status"
        ).fetchall())
        result["relationships_by_type"] = dict(conn.execute(
            "SELECT relationship_type, COUNT(*) FROM entity_relationships "
            "GROUP BY relationship_type ORDER BY COUNT(*) DESC"
        ).fetchall())
        result["issues_by_type"] = dict(conn.execute(
            "SELECT issue_type, COUNT(*) FROM data_issues GROUP BY issue_type "
            "ORDER BY COUNT(*) DESC"
        ).fetchall())
        result["issues_by_severity"] = dict(conn.execute(
            "SELECT severity, COUNT(*) FROM data_issues GROUP BY severity"
        ).fetchall())
        result["evidence_count"] = conn.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0]
        result["evidence_by_status"] = dict(conn.execute(
            "SELECT evidence_status, COUNT(*) FROM field_evidence GROUP BY evidence_status"
        ).fetchall())
        result["evidence_sample_fireball"] = [
            dict(row) for row in conn.execute(
                "SELECT field_path, raw_value, normalized_value, unit, evidence_status "
                "FROM field_evidence WHERE entity_id = ("
                "  SELECT entity_id FROM entities WHERE kind='skill' AND canonical_name='Fireball'"
                ") LIMIT 10"
            )
        ]
        result["node_edges"] = conn.execute("SELECT COUNT(*) FROM node_edges").fetchone()[0]
        result["node_edges_unresolved_source"] = conn.execute(
            "SELECT COUNT(*) FROM node_edges WHERE from_node_entity_id IS NULL"
        ).fetchone()[0]
        result["nodes_with_layout"] = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL"
        ).fetchone()[0]
        coverage = {}
        for kind in ("skill", "tree", "node", "buff", "ailment", "item", "affix",
                     "set", "class", "mastery", "stat", "minion", "item_type",
                     "skill_bonus", "passive_bonus", "quest", "faction", "invocation"):
            coverage[kind] = {
                "entities": conn.execute(
                    "SELECT COUNT(*) FROM entities WHERE kind=?", (kind,)
                ).fetchone()[0],
                "named": conn.execute(
                    "SELECT COUNT(*) FROM entities e WHERE kind=? AND canonical_name IS NOT NULL",
                    (kind,),
                ).fetchone()[0],
            }
        result["kind_coverage"] = coverage
        # example relationship chain: Fireball applied ailments + tree ownership
        result["fireball_relationships"] = [
            dict(row) for row in conn.execute(
                "SELECT r.relationship_type, r.resolution_status, r.from_entity_id, "
                "       r.to_entity_id, r.qualifiers "
                "FROM entity_relationships r "
                "WHERE r.from_entity_id LIKE 'skill:letools:%fi9' OR r.to_entity_id LIKE 'skill:letools:%fi9' "
                "LIMIT 12"
            )
        ]
        result["fingerprint"] = logical_fingerprint(conn)["logical_sha256"]
        checkpoint(conn)
    finally:
        conn.close()

    report_path = run_dir / "letools_normalize_report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print("")
    print(f"entities by kind: {json.dumps(result['entities_by_kind'], ensure_ascii=False)}")
    print(f"tables: {json.dumps(result['table_counts'], ensure_ascii=False)}")
    print(f"integrity: {json.dumps(result['integrity'], ensure_ascii=False)}")
    print(f"fireball: {json.dumps(result['fireball'], ensure_ascii=False)}")
    print(f"avalanche: {json.dumps(result['avalanche'], ensure_ascii=False)}")
    print(f"relationships: {json.dumps(result['relationships_by_status'], ensure_ascii=False)}")
    print(f"issues: {json.dumps(result['issues_by_type'], ensure_ascii=False)}")
    print(f"evidence: {result['evidence_count']} {json.dumps(result['evidence_by_status'])}")
    print(f"node_edges: {result['node_edges']} (unresolved source: {result['node_edges_unresolved_source']})")
    print(f"nodes_with_layout: {result['nodes_with_layout']}")
    print(f"fingerprint: {result['fingerprint']}")
    print(f"wrote {report_path}")

    ok = result["integrity"]["integrity_ok"] and result["integrity"]["foreign_keys_ok"]
    if not result["fireball"] or not result["avalanche"]:
        ok = False
        print("FAIL: Fireball / Avalanche lookup missing")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
