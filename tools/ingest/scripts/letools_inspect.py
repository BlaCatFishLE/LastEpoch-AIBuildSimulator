"""Development-only shape inspector for Last Epoch Tools JS data bundles.

Reads a local bundle (probe file or cached snapshot response) and prints the
window.* assignment inventory plus a JSON sample of any sub-path.  This is the
repeatable command behind the author-local discovery notes (archived).

Usage examples (run from the project root):

  python tools/ingest/scripts/letools_inspect.py windows --file data/raw/_probe/letools_ff633614744f57f936f737f438363135.js
  python tools/ingest/scripts/letools_inspect.py path --file data/raw/_probe/letools_ff633614744f57f936f737f438363135.js \
      --window LEAbilities --path abilityList --sample 1 --depth 2
  python tools/ingest/scripts/letools_inspect.py counts --file data/raw/_probe/letools_db.js

It never executes downloaded JavaScript: everything goes through
``le_data.jsdata`` (literal subset only).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import (  # noqa: E402
    UNDEFINED,
    JsParseError,
    UnsupportedJsConstruct,
    find_assignment_sites,
    find_js_assignments,
    parse_literal,
    js_to_jsonable,
)
from le_data.paths import RAW_DIR  # noqa: E402


def resolve_file(args) -> Path:
    if args.file:
        path = Path(args.file)
        return path if path.is_absolute() else ROOT / path
    if args.snapshot and args.resource:
        direct = RAW_DIR / "letools" / args.snapshot / "responses" / f"{args.resource}.body"
        if direct.exists():
            return direct
        hits = sorted((RAW_DIR / "letools" / args.snapshot / "responses").glob(f"*{args.resource}*.body"))
        if len(hits) == 1:
            return hits[0]
        raise SystemExit(f"cannot resolve resource {args.resource!r}: {[str(h) for h in hits]}")
    raise SystemExit("provide --file, or --snapshot plus --resource")


def load_windows(path: Path, strict: bool = False) -> dict[str, object]:
    text = path.read_text(encoding="utf-8", errors="replace")
    values: dict[str, object] = {}
    sites = list(find_assignment_sites(text))
    for name, value_start in sites:
        try:
            value, _ = parse_literal(text, value_start)
        except UnsupportedJsConstruct as exc:
            print(f"[unsupported] window.{name}: {exc}")
            if strict:
                raise
            continue
        except JsParseError as exc:
            print(f"[error] window.{name}: {exc}")
            if strict:
                raise
            continue
        if name in values:
            print(f"[dup] window.{name} assigned more than once; keeping last parse")
        values[name] = value
    return values


def kind_of(value) -> str:
    if isinstance(value, dict):
        return f"dict[{len(value)}]"
    if isinstance(value, list):
        return f"list[{len(value)}]"
    if value is UNDEFINED:
        return "undefined"
    return type(value).__name__


def shape(value, depth: int = 0, max_depth: int = 2, max_items: int = 4) -> str:
    if isinstance(value, dict):
        if depth >= max_depth:
            return f"<dict[{len(value)}]:{list(value)[:max_items]}>"
        parts = []
        for key, item in list(value.items())[:max_items]:
            parts.append(f"{key!r}: {shape(item, depth + 1, max_depth, max_items)}")
        more = ", ..." if len(value) > max_items else ""
        return "{" + ", ".join(parts) + more + "}"
    if isinstance(value, list):
        if depth >= max_depth:
            return f"<list[{len(value)}]>"
        parts = [shape(item, depth + 1, max_depth, max_items) for item in value[:max_items]]
        more = ", ..." if len(value) > max_items else ""
        return "[" + ", ".join(parts) + more + "]"
    return json.dumps(js_to_jsonable(value))[:100]


def cmd_windows(args) -> None:
    path = resolve_file(args)
    print(f"# file: {path}")
    print(f"# size: {path.stat().st_size} bytes")
    values = load_windows(path, args.strict)
    for name, value in values.items():
        print(f"window.{name}: {kind_of(value)}")
        if args.depth:
            print(f"    {shape(value, max_depth=args.depth, max_items=args.items)}")


def cmd_path(args) -> None:
    path = resolve_file(args)
    values = load_windows(path, args.strict)
    if args.window not in values:
        raise SystemExit(f"window.{args.window} not found; have {sorted(values)}")
    value = values[args.window]
    for step in (args.path or "").split("."):
        if not step:
            continue
        if isinstance(value, list):
            value = value[int(step)]
        elif isinstance(value, dict):
            if step not in value:
                raise SystemExit(f"key {step!r} missing; keys: {list(value)[:50]}")
            value = value[step]
        else:
            raise SystemExit(f"cannot descend into {type(value).__name__} at {step!r}")
    if args.sample is not None:
        if isinstance(value, list):
            value = value[: args.sample]
        elif isinstance(value, dict):
            value = {k: value[k] for k in list(value)[: args.sample]}
    print(json.dumps(js_to_jsonable(value), indent=2, ensure_ascii=False)[: args.limit])


def cmd_counts(args) -> None:
    path = resolve_file(args)
    values = load_windows(path, args.strict)
    for name, value in values.items():
        print(f"window.{name}: {kind_of(value)}")
        if isinstance(value, dict):
            for key, item in value.items():
                if isinstance(item, (list, dict)):
                    print(f"    {key}: {kind_of(item)}")
        elif isinstance(value, list):
            print(f"    first item shape: {shape(value[0] if value else None, max_depth=2)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["windows", "path", "counts"])
    parser.add_argument("--file", default="")
    parser.add_argument("--snapshot", default="")
    parser.add_argument("--resource", default="")
    parser.add_argument("--window", default="")
    parser.add_argument("--path", default="")
    parser.add_argument("--sample", type=int, default=None)
    parser.add_argument("--depth", type=int, default=0)
    parser.add_argument("--items", type=int, default=4)
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    if args.command == "windows":
        cmd_windows(args)
    elif args.command == "path":
        cmd_path(args)
    else:
        cmd_counts(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
