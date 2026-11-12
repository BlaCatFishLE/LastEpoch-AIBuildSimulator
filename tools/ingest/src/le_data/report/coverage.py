"""Coverage reporting (workguide section 12.2).

Coverage describes data, not calculation correctness.  Counts always come
from the dataset under inspection, never from hand-written estimates.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DOMAIN_KINDS = {
    "classes_masteries": ["class", "mastery"],
    "skills": ["skill", "skill_component"],
    "trees_nodes": ["tree", "node"],
    "buffs": ["buff"],
    "ailments": ["ailment"],
    "items": ["item", "item_base", "unique", "set"],
    "affixes": ["affix"],
    "idols_blessings_season": ["idol", "blessing", "season_entity"],
    "minions": ["minion"],
}


def _scalar(conn, sql: str, params: tuple = ()) -> Any:
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def _counts_by(conn, table: str, column: str) -> dict[str, int]:
    try:
        rows = conn.execute(f"SELECT {column}, COUNT(*) AS n FROM {table} GROUP BY {column}")
    except Exception:
        return {}
    return {str(row[0]): row[1] for row in rows}


def build_coverage(conn, scope: dict[str, Any]) -> dict[str, Any]:
    dataset = conn.execute("SELECT * FROM dataset_meta").fetchone()
    sources = list(conn.execute("SELECT * FROM source_snapshots ORDER BY source"))
    source_reports: dict[str, Any] = {}
    for snapshot in sources:
        source = snapshot["source"]
        sid = snapshot["source_snapshot_id"]
        fetched = _scalar(
            conn,
            "SELECT COUNT(*) FROM raw_resources WHERE source_snapshot_id = ? AND fetch_status IN ('fetched','not_modified','cache_hit')",
            (sid,),
        )
        failed = _scalar(
            conn,
            "SELECT COUNT(*) FROM raw_resources WHERE source_snapshot_id = ? AND fetch_status = 'failed'",
            (sid,),
        )
        records = _scalar(
            conn, "SELECT COUNT(*) FROM source_records WHERE source_snapshot_id = ?", (sid,)
        )
        by_kind = _counts_by(conn, "source_records", "entity_kind")
        kind_rows = conn.execute(
            "SELECT entity_kind, parse_status, COUNT(*) AS n FROM source_records "
            "WHERE source_snapshot_id = ? GROUP BY entity_kind, parse_status",
            (sid,),
        )
        status_by_kind: dict[str, dict[str, int]] = {}
        for row in kind_rows:
            status_by_kind.setdefault(row["entity_kind"], {})[row["parse_status"]] = row["n"]
        mapped = conn.execute(
            "SELECT COUNT(*) FROM source_id_map WHERE source_snapshot_id = ?", (sid,)
        ).fetchone()[0]
        source_reports[source] = {
            "source_snapshot_id": sid,
            "claimed_version": snapshot["claimed_version"],
            "fetch_mode": snapshot["fetch_mode"],
            "status": snapshot["status"],
            "raw_resources_fetched": fetched,
            "raw_resources_failed": failed,
            "source_records": records,
            "records_by_kind": by_kind,
            "parse_status_by_kind": status_by_kind,
            "source_ids_mapped": mapped,
        }

    domain_reports: dict[str, Any] = {}
    for domain in scope.get("required_domains", []):
        domain_id = domain["id"]
        kinds = domain.get("entity_kinds") or DOMAIN_KINDS.get(domain_id, [])
        entity_count = 0
        kind_breakdown: dict[str, int] = {}
        for kind in kinds:
            n = _scalar(conn, "SELECT COUNT(*) FROM entities WHERE kind = ?", (kind,))
            kind_breakdown[kind] = n
            entity_count += n
        if domain_id == "relationships":
            entity_count = _scalar(conn, "SELECT COUNT(*) FROM entity_relationships")
        minimum = int(domain.get("minimum_entities", 0))
        denominator_known = minimum > 0 and domain_id != "relationships"
        count_sql = domain.get("count_sql")
        if count_sql:
            # Functional-scope domains (e.g. season entities represented as the
            # weaver tree/nodes) must be counted with their own SQL, not just by
            # entity kind, so the report and Validator share one definition.
            try:
                entity_count = _scalar(conn, count_sql) or 0
                kind_breakdown = {"functional_count": entity_count}
            except Exception:
                pass
        domain_reports[domain_id] = {
            "label": domain.get("label"),
            "priority": domain.get("priority"),
            "entity_kinds": kinds,
            "normalized_entities": entity_count,
            "kind_breakdown": kind_breakdown,
            "denominator": minimum if denominator_known else None,
            "denominator_known": denominator_known,
            "coverage_ratio": round(entity_count / minimum, 4)
            if denominator_known and minimum
            else None,
            "count_basis": "functional_sql" if count_sql else "entity_kind",
        }

    field_coverage = _field_coverage(conn)
    tier_coverage = _tier_coverage(conn)

    return {
        "dataset_id": dataset["dataset_id"] if dataset else None,
        "target_game_version": dataset["target_game_version"] if dataset else None,
        "status": dataset["status"] if dataset else None,
        "compatibility_conclusion": dataset["compatibility_conclusion"] if dataset else None,
        "sources": source_reports,
        "domains": domain_reports,
        "field_coverage": field_coverage,
        "tier_coverage": tier_coverage,
        "semantic_normalization": _semantic_normalization(conn),
        "issue_summary": _issue_summary(conn),
        "entity_status": _counts_by(conn, "entities", "record_status"),
        "parse_status_totals": _counts_by(conn, "source_records", "parse_status"),
    }


def _semantic_normalization(conn) -> dict[str, Any]:
    """Review P1-1/P1-2 metrics: effect->stat linking and buff source configs."""
    result: dict[str, Any] = {}
    try:
        result["stat_links"] = {
            str(row[0]): row[1]
            for row in conn.execute(
                "SELECT stat_link_status, COUNT(*) FROM effect_values GROUP BY 1"
            )
        }
        result["stat_links_linked_or_composite"] = sum(
            result["stat_links"].get(key, 0) for key in ("linked", "composite")
        )
        result["effect_values_total"] = sum(result["stat_links"].values())
    except Exception:
        result["stat_links"] = {}
    try:
        result["buff_related_relationships"] = conn.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE relationship_type LIKE '%buff%'"
        ).fetchone()[0]
    except Exception:
        result["buff_related_relationships"] = 0
    try:
        result["buff_source_configs_raw"] = conn.execute(
            "SELECT COUNT(*) FROM source_records WHERE source = 'letools' "
            "AND entity_kind IN ('skill_bonus','passive_bonus') AND json_valid(raw_payload) "
            "AND json_type(json_extract(raw_payload, '$.buffs')) = 'object' "
            "AND (SELECT COUNT(*) FROM json_each(json_extract(raw_payload, '$.buffs'))) > 0"
        ).fetchone()[0]
    except Exception:
        result["buff_source_configs_raw"] = None
    try:
        result["buff_source_configs_with_relationship_or_issue"] = conn.execute(
            "SELECT COUNT(*) FROM source_records sr WHERE sr.source = 'letools' "
            "AND sr.entity_kind IN ('skill_bonus','passive_bonus') AND json_valid(sr.raw_payload) "
            "AND json_type(json_extract(sr.raw_payload, '$.buffs')) = 'object' "
            "AND (SELECT COUNT(*) FROM json_each(json_extract(sr.raw_payload, '$.buffs'))) > 0 "
            "AND (EXISTS (SELECT 1 FROM entity_relationships r WHERE r.source = 'letools' "
            "AND r.evidence LIKE '%skill_bonus%') OR EXISTS (SELECT 1 FROM data_issues i "
            "WHERE i.source = 'letools' AND i.issue_type LIKE '%buff%'))"
        ).fetchone()[0]
    except Exception:
        result["buff_source_configs_with_relationship_or_issue"] = None
    return result


def _field_coverage(conn) -> dict[str, Any]:
    def ratio(total_sql: str, present_sql: str, note: str | None = None) -> dict[str, Any]:
        total = _scalar(conn, total_sql) or 0
        present = _scalar(conn, present_sql) or 0
        result = {
            "applicable": total,
            "known": present,
            "ratio": round(present / total, 4) if total else None,
        }
        if note:
            result["note"] = note
        return result

    return {
        "skills_with_class": ratio(
            "SELECT COUNT(*) FROM skills WHERE skill_type IS NULL OR skill_type != 'internal'",
            "SELECT COUNT(*) FROM skills WHERE skill_type IS NULL OR skill_type != 'internal' "
            "AND (class_entity_id IS NOT NULL OR mastery_entity_id IS NOT NULL)",
            "denominator is non-internal skills; internal sub-abilities are not owned by a class "
            "in the source data",
        ),
        "skills_with_mana_cost": ratio(
            "SELECT COUNT(*) FROM skills", "SELECT COUNT(*) FROM skills WHERE mana_cost IS NOT NULL"
        ),
        "skills_with_cooldown": ratio(
            "SELECT COUNT(*) FROM skills",
            "SELECT COUNT(*) FROM skills WHERE cooldown_ms IS NOT NULL",
            "neither source publishes a cooldown field; absence is recorded as absent evidence, "
            "not as zero",
        ),
        "skills_with_components": ratio(
            "SELECT COUNT(*) FROM skills",
            "SELECT COUNT(DISTINCT skill_entity_id) FROM skill_components",
        ),
        "nodes_with_max_points": ratio(
            "SELECT COUNT(*) FROM nodes", "SELECT COUNT(*) FROM nodes WHERE max_points IS NOT NULL"
        ),
        "nodes_with_description": ratio(
            "SELECT COUNT(*) FROM nodes", "SELECT COUNT(*) FROM nodes WHERE description IS NOT NULL"
        ),
        "nodes_with_position": ratio(
            "SELECT COUNT(*) FROM nodes",
            "SELECT COUNT(*) FROM nodes WHERE position_x IS NOT NULL AND position_y IS NOT NULL",
        ),
        "nodes_with_edges": ratio(
            "SELECT COUNT(*) FROM nodes",
            "SELECT COUNT(DISTINCT to_node_entity_id) FROM node_edges WHERE to_node_entity_id IS NOT NULL",
        ),
        "buffs_with_duration": ratio(
            "SELECT COUNT(*) FROM buffs", "SELECT COUNT(*) FROM buffs WHERE duration_ms IS NOT NULL",
            "Tunklab buff data has no duration field (only noFixedDuration); LE Tools UI buff data "
            "has none either, so this is a source limitation recorded as absent evidence",
        ),
        "buffs_with_max_stacks": ratio(
            "SELECT COUNT(*) FROM buffs", "SELECT COUNT(*) FROM buffs WHERE max_stacks IS NOT NULL"
        ),
        "buffs_with_source_relation": ratio(
            "SELECT COUNT(*) FROM buffs",
            "SELECT COUNT(DISTINCT from_entity_id) FROM entity_relationships "
            "WHERE relationship_type IN ('grants_buff','applies_buff')",
            "no source lists which skill/node grants each UI buff; this stays a research gap",
        ),
        "affixes_with_tiers": ratio(
            "SELECT COUNT(*) FROM affixes",
            "SELECT COUNT(DISTINCT affix_entity_id) FROM affix_tiers",
        ),
        "affixes_with_roll_range": ratio(
            "SELECT COUNT(*) FROM affix_tiers",
            "SELECT COUNT(*) FROM affix_tiers WHERE roll_min IS NOT NULL AND roll_max IS NOT NULL",
        ),
    }


def _tier_coverage(conn) -> dict[str, Any]:
    """Distinguish raw-only, structurally typed and relationship-resolved data."""
    entities = _scalar(conn, "SELECT COUNT(*) FROM entities") or 0
    typed_tables = {
        "skill": "skills",
        "tree": "trees",
        "node": "nodes",
        "buff": "buffs",
        "ailment": "ailments",
        "item": "items",
        "affix": "affixes",
        "class": "classes",
        "mastery": "masteries",
        "idol": "idols",
        "blessing": "blessings",
        "minion": "minions",
    }
    typed = 0
    for kind, table in typed_tables.items():
        typed += _scalar(conn, f"SELECT COUNT(DISTINCT entity_id) FROM {table}") or 0
    with_relations = _scalar(
        conn,
        "SELECT COUNT(DISTINCT from_entity_id) FROM entity_relationships "
        "WHERE from_entity_id IS NOT NULL",
    ) or 0
    with_evidence = _scalar(
        conn, "SELECT COUNT(DISTINCT entity_id) FROM field_evidence WHERE entity_id IS NOT NULL"
    ) or 0
    return {
        "entities_total": entities,
        "structured_typed": typed,
        "raw_only": max(entities - typed, 0),
        "with_relationships": with_relations,
        "with_field_evidence": with_evidence,
        "note": "raw_only entities are preserved with extensions but have no typed columns yet",
    }


def _issue_summary(conn) -> dict[str, Any]:
    rows = list(
        conn.execute(
            "SELECT severity, status, COUNT(*) AS n FROM data_issues GROUP BY severity, status"
        )
    )
    summary: dict[str, Any] = {}
    for row in rows:
        summary.setdefault(row["severity"], {})[row["status"]] = row["n"]
    return summary


def write_coverage_reports(coverage: dict[str, Any], report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "coverage.json"
    json_path.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "# Coverage report",
        "",
        f"- dataset: `{coverage['dataset_id']}`",
        f"- target game version: {coverage['target_game_version']}",
        f"- dataset status: {coverage['status']}",
        "",
        "## Sources",
        "",
        "| source | snapshot | version | resources fetched | failed | source records | mapped ids |",
        "|---|---|---|---|---|---|---|",
    ]
    for source, info in coverage["sources"].items():
        lines.append(
            f"| {source} | {info['source_snapshot_id']} | {info['claimed_version']} | "
            f"{info['raw_resources_fetched']} | {info['raw_resources_failed']} | "
            f"{info['source_records']} | {info['source_ids_mapped']} |"
        )
    lines += ["", "## Domains", "", "| domain | priority | normalized | denominator | ratio | kinds |", "|---|---|---|---|---|---|"]
    for domain_id, info in coverage["domains"].items():
        kinds = ", ".join(f"{k}={v}" for k, v in info["kind_breakdown"].items())
        lines.append(
            f"| {info['label'] or domain_id} | {info['priority']} | {info['normalized_entities']} | "
            f"{info['denominator'] if info['denominator_known'] else 'unknown'} | "
            f"{info['coverage_ratio'] if info['coverage_ratio'] is not None else '-'} | {kinds} |"
        )
    lines += ["", "## Field coverage", "", "| field | known / applicable | ratio |", "|---|---|---|"]
    for field, info in coverage["field_coverage"].items():
        ratio = info["ratio"] if info["ratio"] is not None else "-"
        lines.append(f"| {field} | {info['known']} / {info['applicable']} | {ratio} |")
    notes = [
        f"- {field}: {info['note']}"
        for field, info in coverage["field_coverage"].items()
        if info.get("note")
    ]
    if notes:
        lines += ["", "Field notes:", ""] + notes
    semantic = coverage.get("semantic_normalization") or {}
    if semantic:
        lines += ["", "## Semantic normalization (review P1-1/P1-2)", ""]
        links = semantic.get("stat_links") or {}
        lines.append(
            f"- effect_values linked/composite: {semantic.get('stat_links_linked_or_composite')} / "
            f"{semantic.get('effect_values_total')} ({links})"
        )
        lines.append(
            f"- buff-related relationships: {semantic.get('buff_related_relationships')}"
        )
        lines.append(
            "- non-empty source buff configs: "
            f"{semantic.get('buff_source_configs_raw')} raw / "
            f"{semantic.get('buff_source_configs_with_relationship_or_issue')} with relation-or-issue"
        )
    lines += ["", "## Data tiers", ""]
    for key, value in coverage["tier_coverage"].items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Parse status totals", ""]
    for key, value in coverage["parse_status_totals"].items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## Issues", "", "| severity | status | count |", "|---|---|---|"]
    for severity, statuses in coverage["issue_summary"].items():
        for status, count in statuses.items():
            lines.append(f"| {severity} | {status} | {count} |")
    md_path = report_dir / "coverage.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "md": md_path}
