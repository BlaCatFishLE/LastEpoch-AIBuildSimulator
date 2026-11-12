# -*- coding: utf-8 -*-
"""Apply the explicit, data-driven manual corrections to one calculation input.

Promoted from ``.scratch/save-import/apply_save_corrections.py``.  The formal
version keeps the three user-confirmed corrections but takes **all of them from
an explicit JSON input** (``examples/<case>/inputs/corrections.json``,
schema ``le-case-corrections-v1``) instead of hardcoded Python constants:

* ``quest_all_attributes`` — add the +2 All Attributes quest reward mod,
* ``chest_fire_res``       — chest fire resistance override (in-game 1.5.3
  tooltip 112%, archived phase-1 dataset is 1.5.2),
* ``idol4_corrupted_all_res`` — corrupted All Resistances on Adorned Idol 5.

The refracted-idol altar amplification is **not** applied here: it belongs to
the affix-db recalculation step (``run_affix_db_pipeline.py``) so it cannot be
multiplied twice.  This file only *carries* the refraction configuration, and
``run_affix_db_pipeline.py`` reads it from the same corrections document.

The produced ``save-build-corrections.json`` keeps the provenance schema of the
prototype (``save-build-corrections-v1``): source/output calculation input,
source build revision, the confirmed correction list and per-mod change notes.

No wall-clock timestamp is written: ``generated_at`` comes from the corrections
file so repeated runs stay byte-identical.
"""

from __future__ import annotations

import argparse
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

SCHEMA_REPORT = "save-build-corrections-v1"


def _find_mod(mods: list[dict], mod_id: str) -> dict | None:
    for mod in mods:
        if mod.get("mod_id") == mod_id:
            return mod
    return None


def _apply_add_mod(correction: dict[str, Any], mods: list[dict]) -> list[dict]:
    mod = correction.get("mod")
    if not isinstance(mod, dict) or not mod.get("mod_id"):
        raise PipelineError(f"correction {correction.get('id')!r}: add_mod needs a 'mod' object")
    mod_id = str(mod["mod_id"])
    if _find_mod(mods, mod_id) is not None:
        return [{"kind": "add_mod", "id": correction.get("id"), "mod_id": mod_id, "status": "already_present"}]
    mods.append(mod)
    return [
        {
            "kind": str(correction.get("kind") or "add_mod"),
            "id": correction.get("id"),
            "mod_id": mod_id,
            "value": mod.get("value_numeric"),
            "status": "applied",
        }
    ]


def _apply_set_value(correction: dict[str, Any], mods: list[dict]) -> list[dict]:
    mod_ids = correction.get("mod_ids")
    if not mod_ids:
        single = correction.get("mod_id")
        if not single:
            raise PipelineError(f"correction {correction.get('id')!r}: set_value needs mod_id/mod_ids")
        mod_ids = [single]
    notes: list[dict] = []
    applied = 0
    for mod_id in mod_ids:
        mod = _find_mod(mods, str(mod_id))
        if mod is None:
            notes.append(
                {
                    "kind": "set_value",
                    "id": correction.get("id"),
                    "mod_id": mod_id,
                    "status": "mod_not_found",
                }
            )
            continue
        old = mod.get("value_numeric")
        if "value_numeric" in correction:
            mod["value_numeric"] = correction["value_numeric"]
        if "display_value" in correction:
            mod["display_value"] = correction["display_value"]
        if "readable_text" in correction:
            mod["readable_text"] = correction["readable_text"]
        replace = correction.get("text_replace")
        if isinstance(replace, dict) and mod.get("readable_text"):
            source_text = str(replace.get("from") or "")
            target_text = str(replace.get("to") or "")
            if source_text and source_text in str(mod["readable_text"]):
                mod["readable_text"] = str(mod["readable_text"]).replace(source_text, target_text)
        if "range" in correction:
            mod["range"] = correction["range"]
        if "value_source" in correction:
            mod["value_source"] = correction["value_source"]
        extra_provenance = correction.get("provenance")
        if isinstance(extra_provenance, dict):
            mod["provenance"] = {**(mod.get("provenance") or {}), **extra_provenance}
        notes.append(
            {
                "kind": str(correction.get("kind") or "set_value"),
                "id": correction.get("id"),
                "mod_id": mod_id,
                "old": old,
                "new": mod.get("value_numeric"),
                "status": "applied",
            }
        )
        applied += 1
    if applied == 0:
        raise PipelineError(
            f"correction {correction.get('id')!r}: none of its target mods were found in the input"
        )
    return notes


def apply_corrections(calc: dict[str, Any], corrections: dict[str, Any]) -> list[dict]:
    mods = calc.get("mods") or []
    if not isinstance(mods, list):
        raise PipelineError("calculation input has no 'mods' list")
    notes: list[dict] = []
    for correction in corrections.get("corrections") or []:
        if not isinstance(correction, dict):
            raise PipelineError("corrections entries must be JSON objects")
        kind = str(correction.get("kind") or "")
        if kind == "add_mod":
            notes.extend(_apply_add_mod(correction, mods))
        elif kind == "set_value":
            notes.extend(_apply_set_value(correction, mods))
        else:
            raise PipelineError(
                f"correction {correction.get('id')!r} has unsupported kind {kind!r} "
                "(supported: add_mod, set_value)"
            )

    # keep items[].mods and the top-level mods list consistent
    by_item: dict[str | None, list[dict]] = {}
    for mod in mods:
        by_item.setdefault(mod.get("item_instance_id"), []).append(mod)
    for item in calc.get("items") or []:
        item["mods"] = by_item.get(item.get("instance_id"), [])
    calc["mods"] = [mod for item in calc["items"] for mod in item.get("mods") or []]
    return notes


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apply_corrections.py",
        description="Apply the explicit manual corrections (le-case-corrections-v1) "
        "to one calculation input.",
    )
    parser.add_argument("--case", default=None, help="case configuration (le-case-v1)")
    parser.add_argument("--root", default=None, help="project root override")
    parser.add_argument("--input", default=None, help="calculation-input JSON to correct")
    parser.add_argument("--corrections", default=None, help="corrections JSON (default: case inputs.corrections)")
    parser.add_argument("--out", required=True, help="output calculation-input JSON")
    parser.add_argument("--report", default=None, help="output save-build-corrections.json provenance")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = resolve(Path.cwd(), args.root) if args.root else repo_root()
        case = load_case(args.case, root=root) if args.case else None
        if case is not None:
            source = resolve(root, args.input) if args.input else case_path(
                case, "inputs.calculation_input"
            )
        else:
            if not args.input:
                raise PipelineError("pass --case or explicit --input")
            source = resolve(root, args.input)
        corrections = (
            load_corrections(resolve(root, args.corrections), root=root)
            if args.corrections
            else load_corrections(case or {}, root=root)
        )
        if not case and not corrections.get("_path"):
            raise PipelineError("pass --case or --corrections")
        calc = load_json(source, what="calculation input")
        print_step(f"calculation input: {relative(source, root)}")
        print_step(f"corrections: {relative(corrections['_path'], root)}")
        notes = apply_corrections(calc, corrections)
        out = resolve(root, args.out)
        write_json(out, calc)
        print_step(f"wrote {relative(out, root)}")
        applied = len([note for note in notes if note.get("status") == "applied"])
        print(f"applied corrections: {applied} change(s); rows={len(calc.get('mods') or [])}")
        if args.report:
            source_build = None
            if case is not None:
                build_dir = case_path(case, "build.build_dir")
                revision = str(case["build"]["revision_id"])
                source_build = relative(build_dir / "revisions" / revision / "build.json", root)
            report = {
                "schema": SCHEMA_REPORT,
                "generated_at": corrections.get("generated_at") or corrections.get("generated"),
                "source_calc_input": relative(source, root),
                "output_calc_input": relative(out, root),
                "source_build": source_build,
                "corrections": [
                    {
                        "kind": entry.get("kind"),
                        "id": entry.get("id"),
                        "status": entry.get("status"),
                        "evidence": entry.get("evidence"),
                    }
                    for entry in corrections.get("corrections") or []
                ],
                "refraction": corrections.get("refraction"),
                "notes": notes,
            }
            report_path = write_json(resolve(root, args.report), report)
            print_step(f"wrote {relative(report_path, root)}")
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
