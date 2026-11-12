"""Query helpers for the normalized SQLite affix database.

Used by ``python -m affix_db query`` and by the test-suite.  No network access.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

DEFAULT_DB = Path("data/affix-db/v1/normalized/affix-db.sqlite")


class QueryError(RuntimeError):
    pass


@dataclass
class QueryResult:
    affix: dict[str, Any]
    base_type: dict[str, Any] | None
    rows: list[dict[str, Any]]
    eligible: bool


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(db_path)
    if not path.is_file():
        raise QueryError(f"database not found: {path}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def get_affix(connection: sqlite3.Connection, identifier: str | int) -> dict[str, Any]:
    """Look up an affix by numeric id, slug, or exact (case-insensitive) name."""
    if isinstance(identifier, int) or str(identifier).strip().isdigit():
        row = connection.execute("SELECT * FROM affixes WHERE affix_id = ?", (int(identifier),)).fetchone()
        if row:
            return _row_to_dict(row)
        raise QueryError(f"affix id not found: {identifier}")
    text = str(identifier).strip()
    row = connection.execute("SELECT * FROM affixes WHERE slug = ?", (text,)).fetchone()
    if row:
        return _row_to_dict(row)
    row = connection.execute(
        "SELECT * FROM affixes WHERE lower(name) = lower(?) OR lower(slug) = lower(?) ORDER BY affix_id LIMIT 1",
        (text, text),
    ).fetchone()
    if row:
        return _row_to_dict(row)
    raise QueryError(f"affix not found: {identifier!r} (try a numeric id or slug, e.g. fire_resistance)")


def get_base_type(connection: sqlite3.Connection, identifier: str | int) -> dict[str, Any] | None:
    """Look up an equipment type by numeric id, display name, baseTypeName or sub-base slug."""
    if isinstance(identifier, int) or str(identifier).strip().isdigit():
        row = connection.execute("SELECT * FROM equipment_types WHERE base_type_id = ?", (int(identifier),)).fetchone()
        return _row_to_dict(row) if row else None
    text = str(identifier).strip()
    row = connection.execute(
        """
        SELECT * FROM equipment_types
        WHERE lower(display_name) = lower(?) OR lower(base_type_name) = lower(?)
        ORDER BY base_type_id LIMIT 1
        """,
        (text, text),
    ).fetchone()
    if row:
        return _row_to_dict(row)
    row = connection.execute(
        """
        SELECT et.* FROM equipment_sub_bases sb
        JOIN equipment_types et ON et.base_type_id = sb.base_type_id
        WHERE lower(sb.slug) = lower(?) OR lower(sb.name) = lower(?)
        ORDER BY et.base_type_id, sb.sub_type_id LIMIT 1
        """,
        (text, text),
    ).fetchone()
    return _row_to_dict(row) if row else None


def is_eligible(connection: sqlite3.Connection, affix_id: int, base_type_id: int) -> bool:
    row = connection.execute(
        """
        SELECT 1 FROM affix_eligible_bases
        WHERE affix_id = ? AND base_type_id = ? AND resolved = 1 AND is_equippable = 1
          AND excluded_by_tunklab = 0
        """,
        (affix_id, base_type_id),
    ).fetchone()
    return row is not None


def query(
    connection: sqlite3.Connection,
    affix: str | int,
    base: str | int | None = None,
    tier: int | None = None,
    roll_slot: int | None = None,
) -> QueryResult:
    affix_row = get_affix(connection, affix)
    base_row = get_base_type(connection, base) if base is not None else None
    if base is not None and base_row is None:
        raise QueryError(f"equipment base not found: {base!r} (try an id or display name such as 'Body Armor')")
    clauses = ["affix_id = ?"]
    params: list[Any] = [affix_row["affix_id"]]
    if base_row is not None:
        clauses.append("base_type_id = ?")
        params.append(base_row["base_type_id"])
    if tier is not None:
        clauses.append("tier = ?")
        params.append(int(tier))
    if roll_slot is not None:
        clauses.append("roll_slot = ?")
        params.append(int(roll_slot))
    sql = (
        "SELECT * FROM affix_base_ranges WHERE "
        + " AND ".join(clauses)
        + " ORDER BY base_type_id, tier, roll_slot"
    )
    rows = [_row_to_dict(row) for row in connection.execute(sql, params)]
    eligible = is_eligible(connection, affix_row["affix_id"], base_row["base_type_id"]) if base_row else bool(rows)
    return QueryResult(affix=affix_row, base_type=base_row, rows=rows, eligible=eligible)


def all_bases(connection: sqlite3.Connection, affix: str | int) -> dict[int, dict[str, Any]]:
    """Return {base_type_id: {base_type, rows}} for every resolved eligible base."""
    affix_row = get_affix(connection, affix)
    rows = [
        _row_to_dict(row)
        for row in connection.execute(
            "SELECT * FROM affix_base_ranges WHERE affix_id = ? ORDER BY base_type_id, tier, roll_slot",
            (affix_row["affix_id"],),
        )
    ]
    bases = {
        _row_to_dict(row)["base_type_id"]: _row_to_dict(row)
        for row in connection.execute(
            """
            SELECT et.* FROM equipment_types et
            JOIN affix_eligible_bases eb ON eb.base_type_id = et.base_type_id
            WHERE eb.affix_id = ?
            ORDER BY et.base_type_id
            """,
            (affix_row["affix_id"],),
        )
    }
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        out.setdefault(row["base_type_id"], {"base_type": bases.get(row["base_type_id"]), "rows": []})["rows"].append(row)
    return out


def query_omen_path(
    connection: sqlite3.Connection,
    affix: str | int,
    sub_base: str | int | None = None,
) -> dict[str, Any]:
    """Return the derived Omen-idol special path rows for one affix.

    tunklab ships ``subItem.affixEffectiveness`` / ``omenIdolAffixEffectModifier``
    but does not implement the resulting range path in its page code, and its
    ``canRollOn`` for affix 111 does not include Grand Idol.  These rows are
    therefore explicitly derived (see the ``evidence`` column) and marked
    ``eligibility_status`` unresolved at the tunklab-source level.
    """
    affix_row = get_affix(connection, affix)
    clauses = ["affix_id = ?"]
    params: list[Any] = [affix_row["affix_id"]]
    if sub_base is not None:
        if isinstance(sub_base, int) or str(sub_base).strip().isdigit():
            clauses.append("sub_type_id = ?")
            params.append(int(sub_base))
        else:
            clauses.append("(lower(sub_base_slug) = lower(?) OR lower(sub_base_name) = lower(?))")
            params.extend([str(sub_base).strip(), str(sub_base).strip()])
    rows = [
        _row_to_dict(row)
        for row in connection.execute(
            "SELECT * FROM omen_special_ranges WHERE " + " AND ".join(clauses) + " ORDER BY sub_type_id, tier, roll_slot",
            params,
        )
    ]
    return {"affix": affix_row, "rows": rows}


def _format_value(value: float | None) -> str:
    if value is None:
        return "-"
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def format_range_value(row: dict[str, Any], rounding: str = "game") -> str:
    """Format one range endpoint pair for humans, e.g. ``92-112%``."""
    if "display_resolved" in row and not row["display_resolved"]:
        return f"{row['effective_min_decimal']}..{row['effective_max_decimal']}"
    if rounding == "tunklab":
        low, high = row["tunklab_display_min"], row["tunklab_display_max"]
    elif rounding == "game":
        low, high = row["display_min"], row["display_max"]
    else:
        low, high = row["display_min"], row["display_max"]
    unit = row["display_unit"] or ""
    text = f"{_format_value(low)}" if low == high else f"{_format_value(low)}-{_format_value(high)}"
    return text + unit


def format_rows(rows: Iterable[dict[str, Any]], rounding: str = "game") -> str:
    header = (
        f"{'affix':>6}  {'base':<20} {'tier':>4} {'slot':>4} {'property':<28} "
        f"{'raw':>16} {'modifier':>9} {'effective':>18} {'display':>14}"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        raw = f"{row['raw_min']:g}-{row['raw_max']:g}"
        effective = f"{row['effective_min_decimal']}..{row['effective_max_decimal']}"
        display = format_range_value(row, rounding)
        lines.append(
            f"{row['affix_id']:>6}  {row['base_type_id']:>6} {row['tier']:>4} {row['roll_slot']:>4} "
            f"{(row['property_name'] or '-'):<28} {raw:>16} {row['modifier']:>9g} {effective:>18} {display:>14}"
        )
    return "\n".join(lines)


def query_to_json(result: QueryResult) -> dict[str, Any]:
    return {
        "affix": {
            key: result.affix[key]
            for key in (
                "affix_id",
                "slug",
                "name",
                "display_name_effective",
                "title",
                "type",
                "type_name",
                "standard_affix_effect_modifier",
                "stat_property",
                "stat_property_name",
            )
        },
        "base_type": (
            {
                key: result.base_type[key]
                for key in ("base_type_id", "base_type_name", "display_name", "affix_effect_modifier", "is_equippable")
            }
            if result.base_type
            else None
        ),
        "eligible": result.eligible,
        "ranges": result.rows,
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
