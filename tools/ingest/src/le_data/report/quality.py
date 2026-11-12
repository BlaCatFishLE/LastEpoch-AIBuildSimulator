"""Quality report: issues, evidence status and parse failures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def build_quality(conn) -> dict[str, Any]:
    def rows(sql: str) -> list[dict[str, Any]]:
        return [dict(row) for row in conn.execute(sql)]

    return {
        "issues_by_type": rows(
            "SELECT issue_type, severity, status, COUNT(*) AS n FROM data_issues "
            "GROUP BY issue_type, severity, status ORDER BY n DESC"
        ),
        "issue_samples": rows(
            "SELECT issue_id, issue_type, severity, source, entity_id, field_path, message, locator "
            "FROM data_issues WHERE status = 'open' ORDER BY severity DESC LIMIT 50"
        ),
        "evidence_by_status": rows(
            "SELECT evidence_status, COUNT(*) AS n FROM field_evidence "
            "GROUP BY evidence_status ORDER BY n DESC"
        ),
        "parse_status": rows(
            "SELECT source, parse_status, COUNT(*) AS n FROM source_records "
            "GROUP BY source, parse_status ORDER BY source, n DESC"
        ),
        "unresolved_relationships": rows(
            "SELECT relationship_type, COUNT(*) AS n FROM entity_relationships "
            "WHERE resolution_status = 'unresolved' GROUP BY relationship_type ORDER BY n DESC"
        ),
        "source_entity_coverage": rows(
            "SELECT e.kind, COUNT(*) AS entities, "
            "SUM(CASE WHEN e.record_status = 'provisional' THEN 1 ELSE 0 END) AS provisional "
            "FROM entities e GROUP BY e.kind ORDER BY entities DESC"
        ),
    }


def write_quality_reports(quality: dict[str, Any], report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "quality.json"
    json_path.write_text(json.dumps(quality, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Quality report", "", "## Issues by type", "", "| type | severity | status | count |", "|---|---|---|---|"]
    for row in quality["issues_by_type"]:
        lines.append(
            f"| {row['issue_type']} | {row['severity']} | {row['status']} | {row['n']} |"
        )
    lines += ["", "## Evidence status", "", "| status | count |", "|---|---|"]
    for row in quality["evidence_by_status"]:
        lines.append(f"| {row['evidence_status']} | {row['n']} |")
    lines += ["", "## Parse status", "", "| source | status | count |", "|---|---|---|"]
    for row in quality["parse_status"]:
        lines.append(f"| {row['source']} | {row['parse_status']} | {row['n']} |")
    lines += ["", "## Unresolved relationships", ""]
    if not quality["unresolved_relationships"]:
        lines.append("- none")
    for row in quality["unresolved_relationships"]:
        lines.append(f"- {row['relationship_type']}: {row['n']}")
    lines += ["", "## Open issue samples", ""]
    if not quality["issue_samples"]:
        lines.append("- none")
    for row in quality["issue_samples"]:
        lines.append(
            f"- `{row['issue_id']}` [{row['severity']}] {row['issue_type']}: "
            f"{row['message']} ({row['locator'] or 'no locator'})"
        )
    md_path = report_dir / "quality.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "md": md_path}
