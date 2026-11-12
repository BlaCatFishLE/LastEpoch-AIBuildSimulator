# -*- coding: utf-8 -*-
"""Read-only bridge from save-import item affixes to the normalized affix DB.

The phase-1 dataset used by ``save_import`` stores one global tier range per affix;
the game scales that range by the item base type (``affixEffectModifier``).  This
module resolves the per-base effective range from ``data/affix-db/v1`` so the local
BD document (``bd.json`` / tables) does not silently show the global range for items
such as a two-handed weapon or body armour.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DB = REPO_ROOT / "data" / "affix-db" / "v1" / "normalized" / "affix-db.sqlite"


class AffixRangeResolver:
    """Read-only per-base affix range resolver; disabled when the DB is absent."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else DEFAULT_DB
        self._conn: sqlite3.Connection | None = None
        self.status = "unavailable"
        if self.db_path.exists():
            try:
                self._conn = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
                self._conn.row_factory = sqlite3.Row
                self.status = "available"
            except sqlite3.Error:
                self._conn = None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def rows(self, affix_id: Any, base_type_id: Any, sub_type_id: Any, tier: Any) -> list[dict]:
        if self._conn is None or affix_id is None or base_type_id is None or tier is None:
            return []
        rows = [
            dict(r)
            for r in self._conn.execute(
                """
                select * from affix_base_ranges
                where affix_id=? and base_type_id=? and tier=?
                order by roll_slot, property_id
                """,
                (int(affix_id), int(base_type_id), int(tier)),
            ).fetchall()
        ]
        if rows:
            return rows
        if sub_type_id is not None:
            rows = [
                dict(r)
                for r in self._conn.execute(
                    """
                    select * from omen_special_ranges
                    where affix_id=? and base_type_id=? and sub_type_id=? and tier=?
                    order by roll_slot, property_id
                    """,
                    (int(affix_id), int(base_type_id), int(sub_type_id), int(tier)),
                ).fetchall()
            ]
        return rows

    @staticmethod
    def _display_range(row: dict) -> tuple[float, float] | None:
        lo, hi = row.get("display_min"), row.get("display_max")
        if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
            return float(lo), float(hi)
        return None

    @staticmethod
    def _value_for_roll(row: dict, roll: int) -> float | None:
        lo, hi = row.get("effective_min"), row.get("effective_max")
        if not isinstance(lo, (int, float)) or not isinstance(hi, (int, float)):
            return None
        value = float(hi) if roll == 255 else float(lo) + (float(hi) - float(lo)) * (roll / 255.0)
        scale = float(row.get("display_scale") or 100.0)
        if str(row.get("display_unit") or "") == "%" and scale == 100:
            # Game-side display uses banker's rounding; 0.5 endpoints round to even.
            value = round(value * 100) / 100.0
            return value
        return value / (scale / 100.0) if scale else value

    @staticmethod
    def _replace_ranges(text: str | None, ranges: list[tuple[float, float]]) -> str | None:
        """Replace old numeric range endpoints in display text with per-base ranges."""
        if not text or not ranges:
            return text
        import re

        # Matches "19-22", "19–22", "1" (single-valued ranges); percent/unit suffix is kept.
        pattern = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)(?!\d)")
        matches = list(pattern.finditer(text))
        if not matches:
            # Constant scalar affixes are rendered as "+1% to All Resistances" with no
            # range.  Replace that single value when the DB has a constant display range.
            if ranges and ranges[0][0] == ranges[0][1]:
                value = f"{ranges[0][0]:g}"
                return re.sub(r"(?<!\d)\d+(?:\.\d+)?(\s*%)", lambda m: value + m.group(1), text)
            return text
        out = []
        cursor = 0
        for index, match in enumerate(matches):
            lo, hi = ranges[min(index, len(ranges) - 1)]
            lo_s = f"{lo:g}"
            hi_s = f"{hi:g}"
            out.append(text[cursor : match.start()])
            out.append(f"{lo_s}-{hi_s}")
            cursor = match.end()
        out.append(text[cursor:])
        return "".join(out)

    def annotate(self, affix_id: Any, base_type_id: Any, sub_type_id: Any,
                 tier: Any, roll: Any, text: str | None) -> dict:
        rows = self.rows(affix_id, base_type_id, sub_type_id, tier)
        if not rows:
            return {}
        ranges = [r for r in (self._display_range(row) for row in rows) if r is not None]
        first = ranges[0] if ranges else None
        roll_int = int(roll) if isinstance(roll, int) or (isinstance(roll, str) and roll.isdigit()) else None
        value = self._value_for_roll(rows[0], roll_int) if roll_int is not None else None
        return {
            "affix_db_status": self.status,
            "affix_db_path": str(self.db_path),
            "base_type_id": int(base_type_id),
            "sub_type_id": int(sub_type_id) if sub_type_id is not None else None,
            "effective_ranges": [
                {
                    "roll_slot": row.get("roll_slot"),
                    "property_id": row.get("property_id"),
                    "property_name": row.get("property_name"),
                    "raw_min": row.get("raw_min"),
                    "raw_max": row.get("raw_max"),
                    "modifier": row.get("modifier"),
                    "effective_min": row.get("effective_min"),
                    "effective_max": row.get("effective_max"),
                    "display_min": row.get("display_min"),
                    "display_max": row.get("display_max"),
                    "display_unit": row.get("display_unit"),
                    "derived": row.get("derived"),
                    "eligibility_status": row.get("eligibility_status"),
                }
                for row in rows
            ],
            "effective_range_text": (
                f"{first[0]:g}-{first[1]:g}" if first else None
            ),
            "effective_value": value,
            "text": self._replace_ranges(text, ranges),
        }
