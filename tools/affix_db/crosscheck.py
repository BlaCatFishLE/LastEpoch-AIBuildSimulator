"""Cross-check tunklab (1.5.2) raw tier rolls against locally extracted game 1.5.3 data.

This is *evidence only*: the affix DB is explicitly tunklab 1.5.2 data and is not
relabelled as game 1.5.3 data.  The cross-check answers a narrower question --
are the raw tier rolls stable between the two versions for the affixes we can
inspect locally?

Input: the game-data JSON extracted earlier by the project's reverse-engineering
pipeline (must be passed explicitly via ``--game-evidence``; the reference data is
author-local and is not shipped with the public release), or any file with the
same shape ``{"<affixId>": {"affixName": ..., "tiers": [[min,max],...]}}``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .query import connect

GAME_EVIDENCE_SHAPE = "{affixId: {affixName, affixTitle, tiers: [[minRoll, maxRoll], ...]}}"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _tier_pair(tier: Any) -> tuple[float, float]:
    """Accept both ``[min, max]`` pairs and ``{"minRoll", "maxRoll"}`` dicts."""
    if isinstance(tier, dict):
        return float(tier["minRoll"]), float(tier["maxRoll"])
    return float(tier[0]), float(tier[1])


def crosscheck_against_game(
    db_path: Path | str,
    game_evidence_path: Path | str,
    out_path: Path | str,
) -> dict[str, Any]:
    game = _load_json(Path(game_evidence_path))
    if not isinstance(game, dict):
        raise ValueError(f"unexpected game evidence shape (expected {GAME_EVIDENCE_SHAPE})")
    connection = connect(db_path)
    results: list[dict[str, Any]] = []
    try:
        for key, game_affix in sorted(game.items(), key=lambda item: int(item[0])):
            affix_id = int(key)
            row = connection.execute(
                "SELECT affix_id, slug, name, tier_count, tiers_json FROM affixes WHERE affix_id = ?",
                (affix_id,),
            ).fetchone()
            if row is None:
                results.append(
                    {
                        "affix_id": affix_id,
                        "slug": None,
                        "status": "missing_in_affix_db",
                        "game_name": game_affix.get("affixName"),
                    }
                )
                continue
            tiers = json.loads(row["tiers_json"])
            db_tiers = [(tier["minRoll"], tier["maxRoll"]) for tier in tiers]
            game_tiers = [_tier_pair(pair) for pair in (game_affix.get("tiers") or [])]
            tier_matches: list[bool] = []
            diffs: list[dict[str, Any]] = []
            for index in range(max(len(db_tiers), len(game_tiers))):
                db_pair = db_tiers[index] if index < len(db_tiers) else None
                game_pair = game_tiers[index] if index < len(game_tiers) else None
                if db_pair is None or game_pair is None:
                    tier_matches.append(False)
                    diffs.append({"tier": index + 1, "db": db_pair, "game": game_pair})
                    continue
                match = abs(db_pair[0] - game_pair[0]) < 1e-6 and abs(db_pair[1] - game_pair[1]) < 1e-6
                tier_matches.append(match)
                if not match:
                    diffs.append({"tier": index + 1, "db": db_pair, "game": game_pair})
            results.append(
                {
                    "affix_id": affix_id,
                    "slug": row["slug"],
                    "name": row["name"],
                    "game_name": game_affix.get("affixName"),
                    "db_tier_count": len(db_tiers),
                    "game_tier_count": len(game_tiers),
                    "tiers_match": all(tier_matches),
                    "mismatched_tiers": diffs,
                }
            )
    finally:
        connection.close()

    payload = {
        "generator": "tools/affix_db/crosscheck.py",
        "note": (
            "tunklab data is version 1.5.2; local game evidence is 1.5.3. "
            "This check only compares raw tier rolls (minRoll/maxRoll) for the affixes "
            "that were locally extracted; it does not relabel tunklab data as game data."
        ),
        "game_evidence": str(game_evidence_path),
        "summary": {
            "affixes_compared": len(results),
            "affixes_with_matching_tiers": sum(
                1 for item in results if item.get("tiers_match") is True
            ),
            "affixes_with_mismatched_tiers": sum(
                1 for item in results if item.get("tiers_match") is False
            ),
            "affixes_missing_in_db": sum(1 for item in results if item.get("status") == "missing_in_affix_db"),
        },
        "results": results,
    }
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload
