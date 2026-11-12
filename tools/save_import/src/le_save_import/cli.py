# -*- coding: utf-8 -*-
"""CLI for :mod:`le_save_import`.

Commands
--------
inspect    print a short summary of a character save
build      extract the full build and write artifacts (bd.json, tables, evidence)
decode     decode only the savedItems byte arrays to JSON
stash      decode the stackable item lists of a STASH save
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import items as _items
from . import pipeline as _pipeline
from . import stash as _stash
from .names import Resolver

#: The public release ships no save path.  Every command that reads a save
#: requires an explicit ``--save`` so the tool never silently opens an
#: author-local file or an unrelated character.


def _cmd_inspect(args: argparse.Namespace) -> int:
    doc = _items.load_save(args.save)
    decoded = _items.decode_save_items(doc)
    resolver = Resolver(mapping_dir=args.mappings)
    by_container: dict[int, int] = {}
    for item in decoded:
        cid = item.get("container_id")
        by_container[cid] = by_container.get(cid, 0) + 1
    print(f"save: {args.save}")
    print(f"character: {doc.get('characterName')} level={doc.get('level')} "
          f"class={doc.get('characterClass')} mastery={doc.get('chosenMastery')} cycle={doc.get('cycle')}")
    class_name = resolver.class_name(doc.get("characterClass")).get("name")
    mastery_name = resolver.mastery(doc.get("characterClass"), doc.get("chosenMastery")).get("name")
    print(f"resolved: class={class_name} mastery={mastery_name}")
    print(f"passive tree: {len((doc.get('savedCharacterTree') or {}).get('nodeIDs') or [])} nodes, "
          f"{sum((doc.get('savedCharacterTree') or {}).get('nodePoints') or [])} points")
    for tree in doc.get("savedSkillTrees") or []:
        print(f"  skill tree {tree.get('treeID')}: {len(tree.get('nodeIDs') or [])} nodes, "
              f"{sum(tree.get('nodePoints') or [])} points, xp={tree.get('xp')}, slot={tree.get('slotNumber')}")
    print(f"weaver tree: {len((doc.get('savedWeaverTree') or {}).get('nodeIDs') or [])} nodes, "
          f"{sum((doc.get('savedWeaverTree') or {}).get('nodePoints') or [])} points")
    print(f"ability bar: {doc.get('abilityBar')}")
    print(f"savedItems: {len(decoded)}")
    for cid in sorted(by_container, key=lambda c: -1 if c is None else c):
        container = resolver.container_name(cid)
        print(f"  containerID {cid} ({container}): {by_container[cid]}")
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    result = _pipeline.run_pipeline(
        args.save,
        args.out,
        mapping_dir=args.mappings,
        planner_path=args.planner if args.planner else None,
        stash_path=args.stash if args.stash else None,
        affix_db_path=args.affix_db,
    )
    build = result["build"]
    counts = build["counts"]
    print(f"wrote artifacts to {args.out}")
    print(f"  equipment={counts['equipment']} idols={counts['idols']} blessings={counts['blessings']} "
          f"weaver_items={counts['weaver_tree_items']} other={counts['other']} total={counts['total']}")
    for key, path in result["paths"].items():
        print(f"  {key}: {path}")
    if "comparison" in result["paths"]:
        comparison = json.loads(Path(result["paths"]["comparison"]).read_text(encoding="utf-8"))
        print("  comparison:", comparison["summary"])
    return 0


def _cmd_decode(args: argparse.Namespace) -> int:
    doc = _items.load_save(args.save)
    payload = {"source": str(args.save), "items": _items.decode_save_items(doc)}
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


def _cmd_stash(args: argparse.Namespace) -> int:
    doc = _items.load_save(args.stash)
    payload = _stash.decrypt_stash_lists(doc)
    print(json.dumps({k: len(v) for k, v in payload.items()}, ensure_ascii=False))
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="le_save_import",
                                 description="Last Epoch offline save -> build (BD) extraction")
    sub = ap.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("inspect", help="summarise a character save")
    sp.add_argument("--save", type=Path, required=True,
                    help="character save file to read (e.g. <save-dir>/1CHARACTERSLOT_BETA_2)")
    sp.add_argument("--mappings", type=Path, default=None)
    sp.set_defaults(func=_cmd_inspect)

    sp = sub.add_parser("build", help="extract the full build and write artifacts")
    sp.add_argument("--save", type=Path, required=True,
                    help="character save file to read (e.g. <save-dir>/1CHARACTERSLOT_BETA_2)")
    sp.add_argument("--out", type=Path, required=True,
                    help="output directory for the extracted artifacts (e.g. reports/<case>/save-import)")
    sp.add_argument("--planner", type=Path, default=None,
                    help="optional LE Tools planner payload JSON to compare against (no default)")
    sp.add_argument("--stash", type=Path, default=None, help="optional STASH save to decode")
    sp.add_argument("--mappings", type=Path, default=None)
    sp.add_argument("--affix-db", type=Path, default=None,
                    help="normalized affix DB sqlite (default: data/affix-db/v1/normalized/affix-db.sqlite)")
    sp.set_defaults(func=_cmd_build)

    sp = sub.add_parser("decode", help="decode savedItems byte arrays only")
    sp.add_argument("--save", type=Path, required=True,
                    help="character save file to read (e.g. <save-dir>/1CHARACTERSLOT_BETA_2)")
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=_cmd_decode)

    sp = sub.add_parser("stash", help="decode stackable item lists of a STASH save")
    sp.add_argument("--stash", type=Path, required=True)
    sp.add_argument("--out", type=Path, default=None)
    sp.set_defaults(func=_cmd_stash)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"file not found: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
