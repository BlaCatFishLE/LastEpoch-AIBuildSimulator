"""Source ledger (属性来源账) built from a resolved input snapshot.

Owner: input-ledger (phase-3 stage-1).  The ledger turns every
:class:`le_calc.model.Contribution` into one flat, display-ready row that
answers the execution-plan §4.1 questions:

    source_id / source_path / 来源类型 / 装备槽或节点 ID / 玩家可读名称
    属性 ID 与命名空间 / 原始数值 / 规范数值 / 单位
    Added、Increased、More 等运算类型
    技能、伤害类型、hit/DoT、玩家/召唤物等作用域
    条件 / 是否纳入 / 排除原因 / 数值状态 / 证据引用

Rows are deterministic and sorted with ``model.contribution_sort_key`` so the
same snapshot always produces byte-identical JSON.

Entry points
------------
``build_source_ledger(snapshot) -> list[dict]``
    The rows, one per contribution.
``ledger_document(snapshot) -> dict``
    ``{schema, build_meta, summary, rows}`` — the artifact written to
    ``tools/phase3/work/source-ledger.json``.
``ledger_json(document) -> str``
    Sorted-key UTF-8 JSON text with a trailing newline.

Self-check::

    python -m le_calc.ledger --snapshot tools/phase3/work/snapshot.json \
        --dump tools/phase3/work/source-ledger.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from . import model

SCHEMA_VERSION = "source-ledger-v1"


# ---------------------------------------------------------------------------
# Row helpers
# ---------------------------------------------------------------------------


def _source_location(source_path: str) -> str | None:
    """Human locator for the item slot / tree node behind a source path."""
    parts = source_path.split(".")
    if source_path.startswith("data.equipment."):
        return ".".join(parts[:3])
    if source_path.startswith("data.idols.entries."):
        return f"idol[{parts[3]}]"
    if source_path.startswith("data.blessings.entries."):
        return f"blessing[{parts[3]}]"
    if source_path.startswith("data.passive_tree.nodes."):
        return f"passive node mg-1:{parts[3]}"
    if source_path.startswith("data.specializations.bh2.nodes."):
        return f"skill node bh2:{parts[4]}"
    if source_path.startswith("data.character."):
        return source_path[len("data.character."):]
    return source_path or None


def _raw_value(contribution: model.Contribution) -> Any:
    """Original displayed / declared value when the contribution carries one."""
    raw = contribution.raw or {}
    for key in ("display_value", "readable_text"):
        value = raw.get(key)
        if value is not None:
            return value
    if contribution.value_source in ("local_input", "local_value_override"):
        return raw.get("display_value", contribution.value)
    return None


def contribution_row(contribution: model.Contribution, row_number: int) -> dict[str, Any]:
    """One flat ledger row (contribution dict + ledger-only display fields)."""
    row = contribution.to_dict()
    row.update(
        {
            "row_id": f"sl-{row_number:04d}",
            "source_id": contribution.contribution_id,
            "source_location": _source_location(contribution.source_path),
            "player_label": f"{contribution.source_label} — {contribution.stat_name}",
            "raw_value": _raw_value(contribution),
            "normalized_value": contribution.value,
            "active": contribution.active,
            "evidence_refs": [
                evidence.locator or evidence.detail for evidence in contribution.evidence
            ],
        }
    )
    return row


# ---------------------------------------------------------------------------
# Ledger + summary
# ---------------------------------------------------------------------------


def build_source_ledger(snapshot: model.Snapshot) -> list[dict[str, Any]]:
    """Build the deterministic source-ledger rows for one snapshot."""
    contributions = sorted(snapshot.contributions, key=model.contribution_sort_key)
    return [
        contribution_row(contribution, number)
        for number, contribution in enumerate(contributions, start=1)
    ]


def summarize(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Counts used by the self-check / HTML overview."""
    rows = list(rows)
    active = [row for row in rows if row.get("active")]
    included = [row for row in rows if row.get("included")]
    excluded = [row for row in rows if not row.get("included")]
    by_source_kind = Counter(row.get("source_kind") for row in rows)
    by_status = Counter(row.get("status") for row in rows)
    by_condition = Counter(
        f"{row.get('condition_code')}={row.get('condition_status')}"
        for row in rows
        if row.get("condition_code")
    )
    exclusion_histogram = Counter(
        row.get("exclusion_reason") for row in excluded if row.get("exclusion_reason")
    )
    included_by_source_kind = Counter(row.get("source_kind") for row in included)
    return {
        "total_rows": len(rows),
        "included_rows": len(included),
        "active_rows": len(active),
        "excluded_rows": len(excluded),
        "by_source_kind": dict(sorted(by_source_kind.items())),
        "included_by_source_kind": dict(sorted(included_by_source_kind.items())),
        "by_status": dict(sorted(by_status.items())),
        "by_condition": dict(sorted(by_condition.items())),
        "exclusion_reason_histogram": dict(
            sorted(
                exclusion_histogram.items(),
                key=lambda item: (-item[1], item[0]),
            )
        ),
        "distinct_exclusion_reasons": len(exclusion_histogram),
        "unresolved_rows": sum(1 for row in rows if row.get("status") == "unresolved"),
    }


def ledger_document(snapshot: model.Snapshot) -> dict[str, Any]:
    """The full ledger artifact: schema, build fingerprints, summary, rows."""
    rows = build_source_ledger(snapshot)
    return {
        "schema": SCHEMA_VERSION,
        "generated_by": "le_calc.ledger",
        "build_meta": snapshot.build_meta,
        "summary": summarize(rows),
        "rows": rows,
    }


def ledger_json(document: dict[str, Any]) -> str:
    """Deterministic JSON text: sorted keys, UTF-8, trailing newline."""
    return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dump_ledger(snapshot: model.Snapshot, path: str | Path) -> Path:
    """Write the deterministic ledger JSON (UTF-8, LF, sorted keys)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ledger_json(ledger_document(snapshot)).encode("utf-8"))
    return target


# ---------------------------------------------------------------------------
# Self-check CLI (snapshot -> ledger JSON); le_calc.cli is Lead-owned
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m le_calc.ledger",
        description="Build the phase-3 source ledger from a snapshot JSON.",
    )
    parser.add_argument(
        "--snapshot",
        default="tools/phase3/work/snapshot.json",
        help="input snapshot JSON (default: tools/phase3/work/snapshot.json)",
    )
    parser.add_argument(
        "--dump",
        default="tools/phase3/work/source-ledger.json",
        help="output ledger JSON (default: tools/phase3/work/source-ledger.json)",
    )
    args = parser.parse_args(argv)

    data = json.loads(Path(args.snapshot).read_text(encoding="utf-8"))
    snapshot = model.Snapshot.from_dict(data)
    document = ledger_document(snapshot)
    text = ledger_json(document)
    target = Path(args.dump)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))
    summary = document["summary"]
    print(f"ledger written: {target} ({len(text.encode('utf-8'))} bytes)")
    print(
        f"rows: {summary['total_rows']} total, {summary['included_rows']} included, "
        f"{summary['active_rows']} active, {summary['excluded_rows']} excluded, "
        f"{summary['unresolved_rows']} unresolved"
    )
    print(f"source kinds: {summary['by_source_kind']}")
    print(f"statuses: {summary['by_status']}")
    print(f"conditions: {summary['by_condition']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())


__all__ = [
    "SCHEMA_VERSION",
    "build_source_ledger",
    "contribution_row",
    "dump_ledger",
    "ledger_document",
    "ledger_json",
    "main",
    "summarize",
]
