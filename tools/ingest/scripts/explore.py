"""Ad-hoc data-shape exploration helper (development only, not part of the pipeline).

Usage:
  python tools/ingest/scripts/explore.py shapes
  python tools/ingest/scripts/explore.py letools <file> [--depth 2]
  python tools/ingest/scripts/explore.py tunklab <file>
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import (  # noqa: E402
    JsParseError,
    extract_balanced_object,
    find_js_assignments,
    parse_literal,
    UNDEFINED,
)

RAW = ROOT / "data" / "raw" / "_probe"


def brief(value, depth=0, max_items=3, max_depth=2):
    if isinstance(value, dict):
        if depth >= max_depth:
            return f"<dict[{len(value)}]>"
        items = list(value.items())[:max_items]
        body = ", ".join(f"{k!r}: {brief(v, depth+1, max_items, max_depth)}" for k, v in items)
        return "{" + body + ("..." if len(value) > max_items else "") + "}"
    if isinstance(value, list):
        if depth >= max_depth:
            return f"<list[{len(value)}]>"
        items = value[:max_items]
        body = ", ".join(brief(v, depth + 1, max_items, max_depth) for v in items)
        return "[" + body + ("..." if len(value) > max_items else "") + "]"
    if value is UNDEFINED:
        return "undefined"
    text = repr(value)
    return text[:120]


def cmd_shapes():
    for path in sorted(RAW.glob("*.js")):
        size = path.stat().st_size
        print(f"== {path.name} {size} bytes")
        text = path.read_text(encoding="utf-8", errors="replace")
        n = 0
        try:
            for name, raw, start, end in find_js_assignments(text):
                n += 1
                try:
                    value, _ = parse_literal(text, start)
                    if isinstance(value, dict):
                        print(f"   window.{name}: dict keys={list(value)[:15]}")
                    elif isinstance(value, list):
                        print(f"   window.{name}: list len={len(value)} sample={brief(value[0] if value else None)}")
                    else:
                        print(f"   window.{name}: {brief(value)}")
                except JsParseError as exc:
                    print(f"   window.{name}: PARSE FAIL {exc}")
                if n > 40:
                    print("   ... more assignments omitted")
                    break
        except JsParseError as exc:
            print(f"   assignment scan failed: {exc}")


def cmd_letools(filename, max_depth=2):
    text = (RAW / filename).read_text(encoding="utf-8", errors="replace")
    for name, raw, start, end in find_js_assignments(text):
        value, _ = parse_literal(text, start)
        print(f"window.{name}: ")
        if isinstance(value, dict):
            for k, v in value.items():
                print(f"   {k}: {brief(v, max_depth=max_depth)}")
        else:
            print("  ", brief(value, max_depth=max_depth))


def cmd_tunklab(filename):
    text = (RAW / filename).read_text(encoding="utf-8", errors="replace")
    idx = text.find("{version:")
    if idx < 0:
        print("no {version: literal found")
        return
    value, end = extract_balanced_object(text, idx)
    print("module data keys:", list(value))
    data = value.get("data", {})
    print("data trees:", len(data), "sample ids:", list(data)[:10])
    first_key = next(iter(data))
    print("first tree", first_key, "len", len(data[first_key]))
    print(json.dumps(data[first_key][0], indent=2)[:2000])


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "shapes":
        cmd_shapes()
    elif cmd == "letools":
        cmd_letools(sys.argv[2], int(sys.argv[3]) if len(sys.argv) > 3 else 2)
    elif cmd == "tunklab":
        cmd_tunklab(sys.argv[2])
