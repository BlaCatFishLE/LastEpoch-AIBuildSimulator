"""Dataset diffing (workguide section 12.3).

Diffing compares two normalized databases by logical content, not file bytes.
Deletion confirmation requires an explicit full-scan success flag from the
caller; otherwise missing entities are reported as "undetermined".
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..store import FINGERPRINT_IGNORED_COLUMNS, connect, logical_fingerprint


def _rows(conn, table: str) -> list[dict[str, Any]]:
    try:
        rows = list(conn.execute(f"SELECT * FROM {table}"))
    except Exception:
        return []
    return [
        {key: row[key] for key in row.keys() if key not in FINGERPRINT_IGNORED_COLUMNS}
        for row in rows
    ]


def _row_key(row: dict[str, Any], table: str) -> str:
    keys = {
        "entities": ["entity_id"],
        "entity_names": ["entity_id", "language", "name"],
        "source_id_map": ["source_snapshot_id", "kind", "source_scope", "source_id"],
        "skills": ["entity_id"],
        "trees": ["entity_id"],
        "nodes": ["entity_id"],
        "node_edges": ["edge_id"],
        "buffs": ["entity_id"],
        "ailments": ["entity_id"],
        "items": ["entity_id"],
        "affixes": ["entity_id"],
        "affix_tiers": ["tier_id"],
        "entity_relationships": ["relationship_id"],
        "field_evidence": ["entity_id", "field_path", "source_record_id", "raw_value"],
        "data_issues": ["issue_id"],
    }.get(table, [])
    if keys:
        return json.dumps([row.get(key) for key in keys], ensure_ascii=False)
    return json.dumps(row, ensure_ascii=False, sort_keys=True)


def diff_databases(old_path: Path, new_path: Path, *, full_enumeration: bool = False) -> dict[str, Any]:
    old = connect(Path(old_path), readonly=True)
    new = connect(Path(new_path), readonly=True)
    try:
        old_fp = logical_fingerprint(old)
        new_fp = logical_fingerprint(new)
        changed_tables: dict[str, Any] = {}
        for table in new_fp["tables"]:
            old_meta = old_fp["tables"].get(table, {})
            new_meta = new_fp["tables"].get(table, {})
            if old_meta.get("sha256") == new_meta.get("sha256"):
                continue
            old_rows = {_row_key(row, table): row for row in _rows(old, table)}
            new_rows = {_row_key(row, table): row for row in _rows(new, table)}
            added = sorted(set(new_rows) - set(old_rows))
            removed = sorted(set(old_rows) - set(new_rows))
            changed = sorted(
                key for key in set(old_rows) & set(new_rows) if old_rows[key] != new_rows[key]
            )

            def sample(keys: list[str]) -> list[dict[str, Any]]:
                return [new_rows.get(key) or old_rows.get(key) for key in keys[:10]]

            changed_tables[table] = {
                "rows_old": len(old_rows),
                "rows_new": len(new_rows),
                "added": len(added),
                "removed": len(removed),
                "modified": len(changed),
                "sample_added": sample(added),
                "sample_modified": sample(changed),
            }

        entity_level = _entity_diff(old, new)
        old_meta = old.execute("SELECT dataset_id, status FROM dataset_meta").fetchone()
        new_meta = new.execute("SELECT dataset_id, status FROM dataset_meta").fetchone()
        result = {
            "old": {
                "path": str(old_path),
                "dataset_id": old_meta["dataset_id"] if old_meta else None,
                "status": old_meta["status"] if old_meta else None,
                "logical_sha256": old_fp["logical_sha256"],
            },
            "new": {
                "path": str(new_path),
                "dataset_id": new_meta["dataset_id"] if new_meta else None,
                "status": new_meta["status"] if new_meta else None,
                "logical_sha256": new_fp["logical_sha256"],
            },
            "identical": old_fp["logical_sha256"] == new_fp["logical_sha256"],
            "full_enumeration_available": full_enumeration,
            "changed_tables": changed_tables,
            "entities": entity_level,
            "deletion_confirmation": {
                # A removal is only "confirmed" when a full successful
                # enumeration is available; otherwise it is undetermined.
                "confirmed": bool(full_enumeration),
                "deletions_confirmed": bool(full_enumeration),
                "removed_candidates": entity_level["removed_count"],
                "note": "removals are only confirmed when a full successful enumeration of the "
                "source is available; otherwise they are undetermined",
            },
        }
        return result
    finally:
        old.close()
        new.close()


def _entity_diff(old, new) -> dict[str, Any]:
    old_entities = {
        row["entity_id"]: (row["kind"], row["canonical_name"])
        for row in old.execute("SELECT entity_id, kind, canonical_name FROM entities")
    }
    new_entities = {
        row["entity_id"]: (row["kind"], row["canonical_name"])
        for row in new.execute("SELECT entity_id, kind, canonical_name FROM entities")
    }
    added = sorted(set(new_entities) - set(old_entities))
    removed = sorted(set(old_entities) - set(new_entities))
    by_kind: dict[str, int] = {}
    for entity_id in added:
        kind = new_entities[entity_id][0]
        by_kind[kind] = by_kind.get(kind, 0) + 1
    renamed = [
        {"entity_id": eid, "old": old_entities[eid][1], "new": new_entities[eid][1]}
        for eid in sorted(set(old_entities) & set(new_entities))
        if old_entities[eid][1] != new_entities[eid][1]
    ]
    return {
        "added_count": len(added),
        "removed_count": len(removed),
        "renamed_count": len(renamed),
        "added_by_kind": by_kind,
        "sample_added": added[:20],
        "sample_removed": removed[:20],
        "renamed": renamed[:20],
    }
