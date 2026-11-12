"""Domain reshaping: derive idols / blessings from item bases.

Both sites describe idols and blessings as item bases (Last Epoch Tools
``item_type`` 25..33 are idol sizes, 34 is "Blessing"; Tunklab carries the same
ids).  A plain item row would hide that domain, so this module reclassifies
those base items into the ``idol`` / ``blessing`` entity kinds and writes the
dedicated tables.  The reclassification is evidence-backed: it is derived from
the source's own item-type grouping, never from a name guess.

Runs once per dataset, after all sources are normalized.
"""
from __future__ import annotations

import json
from typing import Any

from ..store import insert_row, now_utc

# Last Epoch Tools / game item base-type ids for idol sizes.
IDOL_BASE_TYPES = set(range(25, 34))
BLESSING_BASE_TYPE = "34"


def _base_type_name(conn, base_type: str) -> str | None:
    row = conn.execute(
        "SELECT canonical_name FROM entities WHERE entity_id = ?",
        (f"item_type:letools:itemList.equippable:{base_type}",),
    ).fetchone()
    if row is not None and row["canonical_name"]:
        return row["canonical_name"]
    row = conn.execute(
        "SELECT canonical_name FROM entities WHERE entity_id = ?",
        (f"item_type:tunklab:equippableItems:{base_type}",),
    ).fetchone()
    return row["canonical_name"] if row is not None else None


def reshape_domains(conn) -> dict[str, int]:
    counts = {"idol": 0, "blessing": 0}
    rows = list(
        conn.execute(
            "SELECT entity_id, base_type, item_category FROM items "
            "WHERE item_category = 'base' AND base_type IS NOT NULL"
        )
    )
    for row in rows:
        entity_id = row["entity_id"]
        base_type = str(row["base_type"])
        is_idol = base_type.isdigit() and int(base_type) in IDOL_BASE_TYPES
        is_blessing = base_type == BLESSING_BASE_TYPE
        if not (is_idol or is_blessing):
            continue
        type_name = _base_type_name(conn, base_type)
        extension = {
            "derived_from": "item_base",
            "base_type_id": base_type,
            "item_type_name": type_name,
            "derivation": "source groups these base items under this item type",
        }
        if is_idol:
            size = None
            if type_name:
                size = type_name.replace(" Idol", "").strip() or type_name
            conn.execute(
                "UPDATE entities SET kind = 'idol', record_status = record_status, "
                "extensions = ? WHERE entity_id = ?",
                (json.dumps(extension, ensure_ascii=False), entity_id),
            )
            insert_row(
                conn,
                "idols",
                {
                    "entity_id": entity_id,
                    "idol_size": size,
                    "restrictions": json.dumps(
                        {"base_type_id": base_type, "item_type_name": type_name},
                        ensure_ascii=False,
                    ),
                    "extensions": json.dumps(extension, ensure_ascii=False),
                },
                or_replace=True,
            )
            conn.execute("DELETE FROM items WHERE entity_id = ?", (entity_id,))
            counts["idol"] += 1
        else:
            conn.execute(
                "UPDATE entities SET kind = 'blessing', extensions = ? WHERE entity_id = ?",
                (json.dumps(extension, ensure_ascii=False), entity_id),
            )
            insert_row(
                conn,
                "blessings",
                {
                    "entity_id": entity_id,
                    "blessing_type": "monolith_blessing",
                    "restrictions": json.dumps(
                        {"base_type_id": base_type, "item_type_name": type_name},
                        ensure_ascii=False,
                    ),
                    "extensions": json.dumps(extension, ensure_ascii=False),
                },
                or_replace=True,
            )
            conn.execute("DELETE FROM items WHERE entity_id = ?", (entity_id,))
            counts["blessing"] += 1
    # Evidence rows for the reclassification (owner entity, inferred status).
    for kind, count in counts.items():
        if not count:
            continue
        conn.execute(
            "INSERT INTO field_evidence (entity_id, owner_type, owner_id, field_path, "
            "source, extraction_method, evidence_status, notes) "
            "SELECT entity_id, 'entity', entity_id, 'entity_kind', "
            "COALESCE(primary_source, 'letools'), 'derived', 'inferred', "
            "'derived from the source item-type grouping (item base -> domain entity)' "
            "FROM entities WHERE kind = ?",
            (kind,),
        )
    return counts
