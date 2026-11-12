"""Monster damage reduction (enemy DR) by monster level.

Source table: https://lastepoch.tunklab.com/mobdr (see tools/phase3/data/monster_dr.json).
The game applies this as a target-side multiplicative reduction to all damage dealt
to monsters: actual_damage = model_damage * (1 - monster_dr).

Level 0 is a calculator-only convenience meaning "no monster DR" (used to compare
with pre-monster-DR runs); levels 1..100 come from the source table.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

DATA_PATH = Path(__file__).resolve().parents[2] / "data" / "monster_dr.json"


class MonsterDRError(ValueError):
    pass


@lru_cache(maxsize=1)
def load_monster_dr(path: str | Path | None = None) -> dict[str, Any]:
    p = Path(path) if path is not None else DATA_PATH
    if not p.is_file():
        raise MonsterDRError(f"monster DR data not found: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    levels = data.get("levels") or {}
    parsed = {int(k): float(v) for k, v in levels.items()}
    if sorted(parsed) != list(range(1, 101)):
        raise MonsterDRError("monster DR table must cover levels 1..100")
    for level, value in parsed.items():
        if not 0.0 <= value < 1.0:
            raise MonsterDRError(f"monster DR out of range at level {level}: {value}")
    data["levels_int"] = parsed
    return data


def dr_for_level(level: int | None) -> float:
    """Return monster damage reduction as a fraction (0.0 .. 0.87)."""
    if level is None:
        return 0.0
    level = int(level)
    if level == 0:
        return 0.0
    table = load_monster_dr()["levels_int"]
    if level not in table:
        raise MonsterDRError(
            f"monster level {level} not in source table (supported: 0 or 1..100)"
        )
    return table[level]


def factor_for_level(level: int | None) -> float:
    return 1.0 - dr_for_level(level)


def describe(level: int | None) -> dict[str, Any]:
    """Structured target-side metadata for the result/scenario."""
    dr = dr_for_level(level)
    data = load_monster_dr()
    return {
        "monster_level": 0 if level is None else int(level),
        "monster_damage_reduction": dr,
        "monster_dr_factor": 1.0 - dr,
        "source": data.get("source", {}),
        "note": (
            "monster DR is a separate multiplicative target-side factor: "
            "actual = model * (1 - effective_resistance) * (1 - monster_dr); "
            "level 0 = disabled"
        ),
    }


__all__ = [
    "DATA_PATH",
    "MonsterDRError",
    "describe",
    "dr_for_level",
    "factor_for_level",
    "load_monster_dr",
]
