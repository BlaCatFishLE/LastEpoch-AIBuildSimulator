# -*- coding: utf-8 -*-
"""Recompute the offline-save calculation input using the normalized affix DB.

Promoted from ``.scratch/save-import/run_affix_db_pipeline.py``; the one-off
prototype is no longer the formal entry point.  This script owns exactly one
step of the formal chain:

    calculator export (le_build export)
        -> **affix-db per-base recalculation**            <- this script
        -> manual corrections                             (apply_corrections.py)
        -> input snapshot                                 (make_snapshot.py)
        -> le_calc run

Scope of the recalculation (general affix-db rules, not sample values):

* target resistance affixes (Fire / Elemental / All Resistances),
* fire penetration affixes (``Penetration`` property 59), incl. 2H weapon
  per-base ranges,
* Omnis amulet affix 100 (Elemental Damage Over Time), per-base Amulet range,
* the constant "All Resistances" affix 1070 (Adorned Idol),
* per-base effective ranges / game-side display values,
* refracted-idol altar prefix/suffix amplification.

Sample-specific inputs are read from data, never hardcoded here:

* ``--case``            — case configuration (``affix_db`` path, correction file),
* ``corrections.json``  — supplies the ``refraction`` block (selected idol
  indices, altar prefix/suffix multipliers, excluded channels).

Game-side rounding: display values use the 1.5.3 half-even rounding the game
applies (``round()`` on the percent value); this behaviour is preserved on
purpose and must not be replaced by a different rounding mode.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (  # noqa: E402
    PipelineError,
    case_path,
    load_case,
    load_corrections,
    load_json,
    print_step,
    relative,
    repo_root,
    resolve,
    write_json,
)

SCHEMA_REPORT = "save-build-affix-db-recompute-v1"

#: Properties recalculated from the affix DB.  These are the affix categories
#: whose per-base range differs from the global tier range published in the
#: phase-1 dataset (see docs/affix-db.zh-CN.md for the pipeline overview).
TARGET_PROPERTIES = {
    13: "Fire Resistance",
    30: "All Resistances",
    52: "Elemental Resistance",
    59: "Penetration",
}

#: Omnis amulet: affix 100 (Elemental Damage Over Time) is emitted as a property
#: 0 stat and scaled by the Amulet ``affixEffectModifier`` (1.17).
OMNIS_AFFIX_ID = 100
OMNIS_PROPERTY = 0
#: Adorned Idol: affix 1070 "All Resistances" has a constant, display-only value.
CONSTANT_ALL_RES_AFFIX_ID = 1070

BASE_RE = re.compile(r"equippable_(\d+):(\d+)")
AFFIX_RE = re.compile(r":(\d+)$")


# ---------------------------------------------------------------------------
# Small helpers (behaviour-preserving copies of the prototype)
# ---------------------------------------------------------------------------


def base_ids(item: dict | None) -> tuple[int, int] | None:
    if not item:
        return None
    text = str(item.get("base_entity_id") or item.get("entity_id") or "")
    match = BASE_RE.search(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def fetch_range(
    cur,
    affix_id: int,
    base_type: int,
    sub_type: int,
    tier: int,
    prop: int,
    slot: int,
):
    row = cur.execute(
        """
        select * from affix_base_ranges
        where affix_id=? and base_type_id=? and tier=? and property_id=? and roll_slot=?
        """,
        (affix_id, base_type, tier, prop, slot),
    ).fetchone()
    if row is not None:
        return dict(row), False
    row = cur.execute(
        """
        select * from omen_special_ranges
        where affix_id=? and base_type_id=? and sub_type_id=? and tier=? and property_id=? and roll_slot=?
        """,
        (affix_id, base_type, sub_type, tier, prop, slot),
    ).fetchone()
    if row is not None:
        return dict(row), True
    return None, False


def _display_for_percent(value: float) -> str:
    return f"{round(value * 100)}%"


def _refraction_config(corrections: dict[str, Any]) -> dict[str, Any]:
    config = corrections.get("refraction")
    if not isinstance(config, dict):
        raise PipelineError(
            "corrections file has no 'refraction' block; refracted-idol selection must be "
            "explicit data (see examples/<case>/inputs/corrections.json)"
        )
    try:
        idols = [int(value) for value in config.get("refracted_idols") or []]
        prefix = float(config["altar_prefix_effect"])
        suffix = float(config["altar_suffix_effect"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"invalid 'refraction' block in corrections file: {exc}") from exc
    excluded = [str(channel) for channel in config.get("excluded_channels") or []]
    return {
        "refracted_idols": idols,
        "altar_prefix_effect": prefix,
        "altar_suffix_effect": suffix,
        "excluded_channels": excluded,
    }


def _target_properties(case: dict[str, Any] | None) -> dict[int, str]:
    """Base target properties + case-declared extra normalization properties.

    Some affixes apply a base-type display multiplier (crit multi / ailment chance
    etc.); a case can opt into normalizing them with
    ``"affix_normalization": {"properties": [5, 1]}``.  Black Hole keeps the
    default set, so its frozen input is unchanged.
    """
    targets = dict(TARGET_PROPERTIES)
    extra = (case or {}).get("affix_normalization") or {}
    for value in extra.get("properties") or []:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        targets.setdefault(pid, f"property {pid}")
    return targets


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def recompute(
    calc: dict[str, Any],
    *,
    db_path: Path,
    root: Path,
    refraction: dict[str, Any],
    target_properties: dict[int, str] | None = None,
) -> tuple[dict[str, Any], list[dict], list[dict]]:
    """Apply per-base affix ranges + refracted-idol altar effects to ``calc``."""
    mods = calc["mods"]
    targets = dict(TARGET_PROPERTIES if target_properties is None else target_properties)
    items = calc["items"]
    item_by_id = {item["instance_id"]: item for item in items}
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    changes: list[dict] = []
    try:
        # per item/affix occurrence counter for multi-affix roll slots
        occurrences: dict[tuple[str, int, int], int] = {}
        for mod in mods:
            if mod.get("source") != "affix":
                continue
            prop = mod.get("property")
            meta = mod.get("affix") or {}
            match = AFFIX_RE.search(str(meta.get("entity_id") or ""))
            if not match:
                continue
            affix_id = int(match.group(1))
            if prop not in targets and not (
                affix_id == OMNIS_AFFIX_ID and prop == OMNIS_PROPERTY
            ):
                continue
            item = item_by_id.get(str(mod.get("item_instance_id") or ""))
            ids = base_ids(item)
            if not ids:
                continue
            base_type, sub_type = ids
            tier = int(meta.get("source_tier") or (int(meta.get("tier_index") or 0) + 1))
            key = (str(mod.get("item_instance_id")), affix_id, prop)
            slot = occurrences.get(key, 0)
            occurrences[key] = slot + 1
            row, derived = fetch_range(cur, affix_id, base_type, sub_type, tier, prop, slot)
            if row is None:
                # fall back to a row with any slot only if exactly one exists
                rows = cur.execute(
                    "select * from affix_base_ranges "
                    "where affix_id=? and base_type_id=? and tier=? and property_id=?",
                    (affix_id, base_type, tier, prop),
                ).fetchall()
                if len(rows) == 1:
                    row = dict(rows[0])
                    derived = False
                else:
                    changes.append(
                        {
                            "mod_id": mod.get("mod_id"),
                            "affix_id": affix_id,
                            "base_type": base_type,
                            "sub_type": sub_type,
                            "tier": tier,
                            "prop": prop,
                            "status": "no_db_row",
                        }
                    )
                    continue
            roll = (mod.get("roll") or {}).get("raw")
            display_scale = float(row.get("display_scale") or 100.0)
            old = mod.get("value_numeric")
            if roll is None:
                continue
            new = old
            reason = None
            if affix_id == CONSTANT_ALL_RES_AFFIX_ID:
                # constant value after the base multiplier; 4% on Adorned Idol
                new = float(row["display_min"]) / display_scale
                reason = "constant_all_res"
            elif int(roll) == 255:
                new = float(row["display_max"]) / display_scale
                reason = "max_roll"
            elif str(row.get("display_unit") or "") == "%" and display_scale == 100:
                fraction = int(roll) / 255.0
                effective = float(row["effective_min"]) + (
                    float(row["effective_max"]) - float(row["effective_min"])
                ) * fraction
                # Unity/.NET game-side display uses banker's (half-even) rounding
                new = round(effective * 100) / 100.0
                reason = "roll_interpolated_game_round"
            if new != old:
                mod["value_numeric"] = new
                mod["display_value"] = (
                    _display_for_percent(new)
                    if str(row.get("display_unit") or "") == "%"
                    else str(new)
                )
                mod["value_source"] = "affix_db"
                mod["range"] = {
                    "min": row.get("effective_min"),
                    "max": row.get("effective_max"),
                    "direction": "ascending",
                    "range_text": (
                        f'{row.get("display_min")}-{row.get("display_max")}'
                        f'{row.get("display_unit") or ""}'
                    ),
                }
                mod["provenance"] = {
                    **(mod.get("provenance") or {}),
                    "affix_db": relative(db_path, root),
                    "affix_db_row": {
                        "affix_id": affix_id,
                        "base_type_id": base_type,
                        "sub_type_id": sub_type,
                        "tier": tier,
                        "roll_slot": row.get("roll_slot"),
                        "derived": derived,
                    },
                    "note": "per-base affix range from normalized affix-db; "
                    "game-side half-even display",
                }
            changes.append(
                {
                    "mod_id": mod.get("mod_id"),
                    "affix_id": affix_id,
                    "base_type": base_type,
                    "sub_type": sub_type,
                    "tier": tier,
                    "property": prop,
                    "old": old,
                    "new": new,
                    "reason": reason,
                    "db": {
                        "raw": [row.get("raw_min"), row.get("raw_max")],
                        "effective": [row.get("effective_min"), row.get("effective_max")],
                        "display": [row.get("display_min"), row.get("display_max")],
                        "modifier": row.get("modifier"),
                        "derived": derived,
                        "eligibility_status": row.get("eligibility_status"),
                    },
                }
            )
    finally:
        con.close()

    # refracted idol prefix/suffix altar effects (explicit selection, not a default)
    refracted = set(refraction["refracted_idols"])
    excluded_channels = set(refraction["excluded_channels"])
    refraction_changes: list[dict] = []
    for mod in mods:
        instance_id = str(mod.get("item_instance_id") or "")
        if not instance_id.startswith("idols."):
            continue
        try:
            index = int(instance_id.split(".")[1])
        except (IndexError, ValueError):
            continue
        if index not in refracted:
            continue
        meta = mod.get("affix") or {}
        slot = meta.get("affix_slot")
        if mod.get("source") != "affix" or slot not in ("prefix", "suffix"):
            continue
        channel = str(mod.get("channel") or "")
        if channel in excluded_channels:
            # user-verified in game: corrupted lines are not amplified by the altar
            continue
        effect = (
            refraction["altar_prefix_effect"]
            if slot == "prefix"
            else refraction["altar_suffix_effect"]
        )
        old = float(mod.get("value_numeric") or 0.0)
        new = old * (1.0 + effect)
        mod["value_numeric"] = new
        mod["display_value"] = (
            _display_for_percent(new)
            if str((mod.get("stat") or {}).get("unit")) == "percent"
            else str(new)
        )
        mod["value_source"] = "manual_override"
        mod["provenance"] = {
            **(mod.get("provenance") or {}),
            "refracted_slot": True,
            "altar_effect": effect,
        }
        refraction_changes.append(
            {"mod_id": mod.get("mod_id"), "idol": index, "slot": slot, "old": old, "new": new}
        )

    # keep items[].mods and the top-level mods list consistent
    by_item: dict[str | None, list[dict]] = {}
    for mod in mods:
        by_item.setdefault(mod.get("item_instance_id"), []).append(mod)
    for item in items:
        item["mods"] = by_item.get(item["instance_id"], [])
    calc["mods"] = [mod for item in items for mod in item.get("mods") or []]
    return calc, changes, refraction_changes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run_affix_db_pipeline.py",
        description="Recalculate one calculation input with the normalized affix DB "
        "(formal save-import pipeline step).",
    )
    parser.add_argument("--case", default=None, help="case configuration (le-case-v1)")
    parser.add_argument("--root", default=None, help="project root override")
    parser.add_argument("--calc-input", default=None, help="calculation-input JSON to recalculate")
    parser.add_argument("--affix-db", default=None, help="normalized affix DB sqlite")
    parser.add_argument(
        "--corrections",
        default=None,
        help="corrections file supplying the refraction block (default: case inputs.corrections)",
    )
    parser.add_argument("--out", required=True, help="output calculation-input JSON")
    parser.add_argument("--report", default=None, help="output recompute report JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve(Path.cwd(), args.root) if args.root else repo_root()
        case = load_case(args.case, root=root) if args.case else None
        if case is not None:
            source = resolve(root, args.calc_input) if args.calc_input else case_path(
                case, "inputs.calculation_input"
            )
            db_path = resolve(root, args.affix_db) if args.affix_db else case_path(case, "affix_db")
        else:
            if not args.calc_input:
                raise PipelineError("pass --case or explicit --calc-input")
            source = resolve(root, args.calc_input)
            if not args.affix_db:
                raise PipelineError("pass --case or explicit --affix-db")
            db_path = resolve(root, args.affix_db)
        corrections = (
            load_corrections(resolve(root, args.corrections), root=root)
            if args.corrections
            else load_corrections(case or {}, root=root)
        )
        refraction = _refraction_config(corrections)
        calc = load_json(source, what="calculation input")
        if not db_path.is_file():
            raise PipelineError(f"affix DB not found: {db_path}")
        print_step(f"affix DB: {relative(db_path, root)}")
        print_step(f"calculation input: {relative(source, root)}")
        print_step(
            "refraction: idols={} prefix=x{:g} suffix=x{:g} excluded={}".format(
                refraction["refracted_idols"],
                1 + refraction["altar_prefix_effect"],
                1 + refraction["altar_suffix_effect"],
                refraction["excluded_channels"] or "-",
            )
        )
        calc, changes, refraction_changes = recompute(
            calc, db_path=db_path, root=root, refraction=refraction,
            target_properties=_target_properties(case),
        )
        out = resolve(root, args.out)
        write_json(out, calc)
        print_step(f"wrote {relative(out, root)}")
        if args.report:
            report = {
                "schema": SCHEMA_REPORT,
                "source_calc_input": relative(source, root),
                "output_calc_input": relative(out, root),
                "affix_db": relative(db_path, root),
                "refracted_idols": refraction["refracted_idols"],
                "altar_prefix_effect": refraction["altar_prefix_effect"],
                "altar_suffix_effect": refraction["altar_suffix_effect"],
                "excluded_channels": refraction["excluded_channels"],
                "target_properties": {str(key): value for key, value in TARGET_PROPERTIES.items()},
                "range_changes": changes,
                "refraction_changes": refraction_changes,
            }
            report_path = write_json(resolve(root, args.report), report)
            print_step(f"wrote {relative(report_path, root)}")
        changed = len([entry for entry in changes if entry.get("new") != entry.get("old")])
        print(
            f"range changes: {changed} (rows examined {len(changes)}), "
            f"refraction mods: {len(refraction_changes)}"
        )
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
