"""Compare rendered tunklab "Scaled Values" tables against the normalized DB.

The comparison is the source-of-truth check for:

* affix -> eligible base grouping and grouping order (modifier descending),
* per-tier/per-roll-slot tunklab display values (JS round-half-up),
* the count of endpoints where the game-style half-even rounding differs.

Outputs ``verification/display-comparison.json`` and
``verification/display-comparison.md`` plus ``verification/manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .enums import TUNKLAB_EXCLUDED_BASE_TYPES
from .formula import compute_modifier
from .page_parse import (
    PageRow,
    ScaledValuesTable,
    page_display_name,
    parse_applies_to,
    parse_cell_ranges,
    parse_scaled_values_table,
)
from .query import connect, get_affix

_ROW_KEY_RE = re.compile(r"mod-(?P<modifier>-?\d+(?:\.\d+)?)")


@dataclass
class CellComparison:
    tier: int
    slot: int
    page: list[dict[str, Any]]
    expected: list[dict[str, Any]]
    match: bool


@dataclass
class RowComparison:
    modifier: float
    page_items: list[str]
    expected_items: list[str]
    names_match: bool
    cells: list[CellComparison] = field(default_factory=list)
    page_visits: int = 0
    page_visits_match: int = 0

    @property
    def match(self) -> bool:
        return self.names_match and all(cell.match for cell in self.cells)


@dataclass
class PageComparison:
    slug: str
    dom_file: str
    affix_id: int | None
    tier_headers: int
    expected_tiers: int | None
    rows: list[RowComparison] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    page_applies_to: list[str] = field(default_factory=list)
    expected_applies_to: list[str] = field(default_factory=list)
    applies_to_match: bool = False

    @property
    def match(self) -> bool:
        return (
            not self.errors
            and self.tier_headers == self.expected_tiers
            and bool(self.rows)
            and all(row.match for row in self.rows)
            and self.applies_to_match
        )


def _ordered_eligible_bases(connection, affix_row: dict[str, Any]) -> list[tuple[int, str]]:
    can_roll_on: list[int] = json.loads(affix_row["can_roll_on_json"])
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for base_id in can_roll_on:
        base_id = int(base_id)
        if base_id in seen or base_id in TUNKLAB_EXCLUDED_BASE_TYPES:
            continue
        seen.add(base_id)
        row = connection.execute(
            "SELECT * FROM equipment_types WHERE base_type_id = ? AND is_equippable = 1", (base_id,)
        ).fetchone()
        if row is None:
            continue
        out.append((base_id, page_display_name(base_id, row["display_name"])))
    return out


def _expected_groups(
    connection,
    affix_row: dict[str, Any],
) -> list[dict[str, Any]]:
    groups: dict[float, dict[str, Any]] = {}
    for base_id, name in _ordered_eligible_bases(connection, affix_row):
        row = connection.execute(
            "SELECT * FROM equipment_types WHERE base_type_id = ?", (base_id,)
        ).fetchone()
        modifier = compute_modifier(row["affix_effect_modifier"], affix_row["standard_affix_effect_modifier"])
        group = groups.get(modifier)
        if group is None:
            group = {"modifier": modifier, "base_ids": [], "names": []}
            groups[modifier] = group
        group["base_ids"].append(base_id)
        group["names"].append(name)
    return [groups[key] for key in sorted(groups, reverse=True)]


def _db_cell(
    connection,
    affix_id: int,
    base_id: int,
    tier: int,
    tier_count: int,
) -> list[dict[str, Any]] | None:
    if tier > tier_count:
        return None
    rows = list(
        connection.execute(
            """
            SELECT * FROM affix_base_ranges
            WHERE affix_id=? AND base_type_id=? AND tier=?
            ORDER BY roll_slot
            """,
            (affix_id, base_id, tier),
        )
    )
    cells: list[dict[str, Any]] = []
    for row in rows:
        if not row["display_resolved"]:
            cells.append({"unresolved": True, "low": None, "high": None, "unit": row["display_unit"]})
            continue
        cells.append(
            {
                "low": row["tunklab_display_min"],
                "high": row["tunklab_display_max"],
                "unit": row["display_unit"],
                "game_low": row["display_min"],
                "game_high": row["display_max"],
            }
        )
    return cells


def _normalize_expected(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"low": cell.get("low"), "high": cell.get("high"), "unit": cell.get("unit", "")} for cell in cells]


def _split_page_cell(cell_text: str, div_texts: list[str]) -> list[dict[str, Any]]:
    """Return one parsed range per roll-slot line in a rendered cell."""
    lines = div_texts or ([cell_text] if cell_text else [])
    out: list[dict[str, Any]] = []
    for line in lines:
        parsed = parse_cell_ranges(line)
        if parsed:
            out.append(parsed[0])
        else:
            out.append({"low": None, "high": None, "unit": "", "raw": line})
    return out


def compare_page(connection, slug: str, dom_file: Path, dom_html: str) -> PageComparison:
    comparison = PageComparison(
        slug=slug,
        dom_file=dom_file.name,
        affix_id=None,
        tier_headers=0,
        expected_tiers=None,
    )
    try:
        affix_row = get_affix(connection, slug)
    except Exception as exc:  # pragma: no cover - defensive
        comparison.errors.append(f"affix lookup failed: {exc}")
        return comparison
    comparison.affix_id = affix_row["affix_id"]
    comparison.expected_tiers = affix_row["tier_count"]
    table: ScaledValuesTable | None = parse_scaled_values_table(dom_html)
    if table is None:
        comparison.errors.append("no Scaled Values table found in rendered DOM")
        return comparison
    tier_headers = table.tier_headers
    comparison.tier_headers = len(tier_headers)
    if comparison.tier_headers != comparison.expected_tiers:
        comparison.errors.append(
            f"tier column count {comparison.tier_headers} != affix tier count {comparison.expected_tiers}"
        )
    groups = _expected_groups(connection, affix_row)
    expected_by_modifier = {round(group["modifier"], 9): group for group in groups}

    # "Applies To" badges must equal the eligible (resolved, non-tunklab-excluded)
    # base-type list in canRollOn order.  Note the page uses the plain
    # equipment displayName here (e.g. "One-Handed Axe", "Off-Hand Catalyst"),
    # not the shortened Scaled-Values row label.
    ordered_bases = _ordered_eligible_bases(connection, affix_row)
    expected_applies: list[str] = []
    known_names: set[str] = set()
    for row in connection.execute(
        "SELECT base_type_id, display_name FROM equipment_types WHERE is_equippable = 1"
    ):
        known_names.add(row["display_name"])
    for base_id, _short_name in ordered_bases:
        row = connection.execute(
            "SELECT display_name FROM equipment_types WHERE base_type_id = ?", (base_id,)
        ).fetchone()
        if row is not None:
            expected_applies.append(row["display_name"])
    comparison.expected_applies_to = expected_applies
    comparison.page_applies_to = [chunk for chunk in parse_applies_to(dom_html) if chunk in known_names]
    comparison.applies_to_match = comparison.page_applies_to == comparison.expected_applies_to
    if not comparison.applies_to_match:
        comparison.errors.append(
            f"Applies To mismatch: page={comparison.page_applies_to} expected={comparison.expected_applies_to}"
        )
    page_rows: list[PageRow] = table.rows
    if len(page_rows) != len(groups):
        comparison.errors.append(f"row count {len(page_rows)} != expected group count {len(groups)}")
    used_modifiers: set[float] = set()
    for page_row in page_rows:
        row_key = page_row.row_key or ""
        match = _ROW_KEY_RE.search(row_key)
        modifier = float(match.group("modifier")) if match else float("nan")
        group = expected_by_modifier.get(round(modifier, 9)) if match else None
        row_comparison = RowComparison(
            modifier=modifier,
            page_items=list(page_row.item_names),
            expected_items=list(group["names"]) if group else [],
            names_match=bool(group) and page_row.item_names == group["names"],
        )
        if group is None:
            comparison.errors.append(f"rendered row {row_key!r} has no matching modifier group")
            comparison.rows.append(row_comparison)
            continue
        used_modifiers.add(round(modifier, 9))
        base_id = group["base_ids"][0]
        for tier in range(1, comparison.tier_headers + 1):
            cell = page_row.cells[tier] if tier < len(page_row.cells) else None
            if cell is None:
                continue
            page_values = _split_page_cell(cell.text, cell.div_texts)
            db_cell = _db_cell(connection, affix_row["affix_id"], base_id, tier, comparison.expected_tiers)
            if db_cell is None:
                expected = []
            else:
                expected = _normalize_expected(db_cell)
            # Zero-filled extra lines etc.: compare slot by slot.
            matches = len(page_values) == len(expected) and all(
                _values_match(page_values[index].get("low"), expected[index]["low"])
                and _values_match(page_values[index].get("high"), expected[index]["high"])
                and (page_values[index].get("unit") or "") == (expected[index]["unit"] or "")
                for index in range(len(expected))
            )
            row_comparison.cells.append(
                CellComparison(tier=tier, slot=0, page=page_values, expected=expected, match=matches)
            )
            row_comparison.page_visits += 1
            if matches:
                row_comparison.page_visits_match += 1
        comparison.rows.append(row_comparison)
    missing = set(expected_by_modifier) - used_modifiers
    if missing:
        comparison.errors.append(f"rendered page is missing modifier groups: {sorted(missing)}")
    return comparison


def _values_match(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    try:
        return abs(float(left) - float(right)) < 1e-9
    except (TypeError, ValueError):
        return False


def _comparison_to_dict(page: PageComparison) -> dict[str, Any]:
    return {
        "slug": page.slug,
        "dom_file": page.dom_file,
        "affix_id": page.affix_id,
        "tier_headers": page.tier_headers,
        "expected_tiers": page.expected_tiers,
        "applies_to_match": page.applies_to_match,
        "page_applies_to": page.page_applies_to,
        "expected_applies_to": page.expected_applies_to,
        "match": page.match,
        "errors": page.errors,
        "rows": [
            {
                "modifier": row.modifier,
                "page_items": row.page_items,
                "expected_items": row.expected_items,
                "names_match": row.names_match,
                "cells_matched": row.page_visits_match,
                "cells_total": row.page_visits,
                "match": row.match,
                "mismatches": [
                    {
                        "tier": cell.tier,
                        "page": cell.page,
                        "expected": cell.expected,
                    }
                    for cell in row.cells
                    if not cell.match
                ],
            }
            for row in page.rows
        ],
    }


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def verify_rendered_pages(
    db_path: Path | str,
    rendered_dir: Path | str,
    out_dir: Path | str | None = None,
    slugs: tuple[str, ...] = (),
    progress=print,
) -> dict[str, Any]:
    rendered = Path(rendered_dir)
    output = Path(out_dir) if out_dir is not None else rendered.parent
    output.mkdir(parents=True, exist_ok=True)
    dom_files = sorted(rendered.glob("*.dom.html"))
    if slugs:
        dom_files = [rendered / f"{slug}.dom.html" for slug in slugs]
    if not dom_files:
        raise FileNotFoundError(f"no rendered DOM files in {rendered}")
    connection = connect(db_path)
    try:
        pages: list[PageComparison] = []
        for dom_file in dom_files:
            slug = dom_file.name[: -len(".dom.html")]
            progress(f"[verify-display] {slug}")
            html = dom_file.read_text(encoding="utf-8", errors="replace")
            pages.append(compare_page(connection, slug, dom_file, html))
    finally:
        connection.close()

    # Also report where the in-game (half-even) rounding differs from the page.
    game_vs_page: list[dict[str, Any]] = []
    for page in pages:
        if page.affix_id is None:
            continue
        connection = connect(db_path)
        try:
            for row in connection.execute(
                """
                SELECT * FROM affix_base_ranges
                WHERE affix_id = ? AND roll_slot = 0
                  AND (display_min != tunklab_display_min OR display_max != tunklab_display_max)
                ORDER BY base_type_id, tier
                """,
                (page.affix_id,),
            ):
                game_vs_page.append(
                    {
                        "slug": page.slug,
                        "affix_id": row["affix_id"],
                        "base_type_id": row["base_type_id"],
                        "tier": row["tier"],
                        "modifier": row["modifier"],
                        "effective_min_decimal": row["effective_min_decimal"],
                        "effective_max_decimal": row["effective_max_decimal"],
                        "tunklab_display_min": row["tunklab_display_min"],
                        "tunklab_display_max": row["tunklab_display_max"],
                        "game_display_min": row["display_min"],
                        "game_display_max": row["display_max"],
                    }
                )
        finally:
            connection.close()

    result = {
        "generator": "tools/affix_db/verify_display.py",
        "db": str(db_path),
        "rendered_dir": str(rendered),
        "summary": {
            "pages": len(pages),
            "pages_match": sum(1 for page in pages if page.match),
            "cells_compared": sum(row.page_visits for page in pages for row in page.rows),
            "cells_matched": sum(row.page_visits_match for page in pages for row in page.rows),
            "endpoint_differences_game_vs_page": len(game_vs_page),
        },
        "pages": [_comparison_to_dict(page) for page in pages],
        "game_vs_tunklab_endpoint_differences": game_vs_page,
    }
    comparison_json = output / "display-comparison.json"
    comparison_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # Markdown summary (used verbatim by the build report).
    lines = [
        "# tunklab 页面 vs affix-db 显示对照",
        "",
        "| affix slug | affixId | 行(组) | tier 列 | Applies To | 单元格匹配 | 结果 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for page in pages:
        cells_total = sum(row.page_visits for row in page.rows)
        cells_matched = sum(row.page_visits_match for row in page.rows)
        lines.append(
            f"| {page.slug} | {page.affix_id} | {len(page.rows)} | {page.tier_headers} | "
            f"{'MATCH' if page.applies_to_match else 'MISMATCH'} | "
            f"{cells_matched}/{cells_total} | {'MATCH' if page.match else 'MISMATCH'} |"
        )
    lines.append("")
    lines.append("## 游戏取整（half-even）与 tunklab 页面取整（half-up）端点差异")
    lines.append("")
    lines.append(f"共 {len(game_vs_page)} 个 roll-slot-0 端点对在两种取整下不同。示例：")
    lines.append("")
    lines.append("| slug | affixId | baseTypeID | tier | 精确有效值 | tunklab | game |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in game_vs_page[:40]:
        lines.append(
            f"| {item['slug']} | {item['affix_id']} | {item['base_type_id']} | {item['tier']} | "
            f"{item['effective_min_decimal']}..{item['effective_max_decimal']} | "
            f"{item['tunklab_display_min']}..{item['tunklab_display_max']} | "
            f"{item['game_display_min']}..{item['game_display_max']} |"
        )
    (output / "display-comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    manifest_files = [
        {"path": comparison_json.name, "sha256": _sha256_file(comparison_json)},
        {"path": "display-comparison.md", "sha256": _sha256_file(output / "display-comparison.md")},
    ]
    crosscheck_path = output / "game-crosscheck.json"
    if crosscheck_path.is_file():
        manifest_files.append({"path": crosscheck_path.name, "sha256": _sha256_file(crosscheck_path)})
    validation_path = output / "validation.json"
    if validation_path.is_file():
        manifest_files.append({"path": validation_path.name, "sha256": _sha256_file(validation_path)})
    manifest = {
        "generator": "tools/affix_db/verify_display.py",
        "files": manifest_files,
        "rendered": [
            {"path": f"rendered/{path.name}", "sha256": _sha256_file(path)}
            for path in sorted(list(dom_files) + list(rendered.glob("*.png")))
        ],
        "summary": result["summary"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result
