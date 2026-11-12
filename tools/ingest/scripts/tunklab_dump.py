"""Ad-hoc detailed dump of Tunklab data modules (development helper).

Prints, for every `{version:"...",data:...}` literal in a chunk:
  offset, version, data type, count, top-level keys, and the first element.
Optionally dumps a slice of elements as JSON to inspect semantics.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_dump.py 0430-8cl4viex.js [--at OFFSET] [--n 2]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import (  # noqa: E402
    UnsupportedJsConstruct,
    js_to_jsonable,
    parse_literal,
)

PROBE = ROOT / "data" / "raw" / "_probe" / "tunklab"
MODULE_HINT = re.compile(r"\{version:\"")


def iter_modules(text: str):
    for m in MODULE_HINT.finditer(text):
        brace = text.rfind("{", 0, m.end())
        try:
            value, end = parse_literal(text, brace)
        except (UnsupportedJsConstruct, Exception):  # noqa: BLE001
            continue
        if isinstance(value, dict) and "version" in value and "data" in value:
            yield brace, end, value


def describe(value) -> str:
    data = value.get("data")
    if isinstance(data, list):
        head = js_to_jsonable(data[0]) if data else None
        keys = list(head)[:14] if isinstance(head, dict) else None
        return (f"list len={len(data)} first_keys={keys}")
    if isinstance(data, dict):
        return f"dict keys={len(data)} names={list(data)[:16]}"
    return f"{type(data).__name__}: {json.dumps(js_to_jsonable(data), ensure_ascii=False)[:200]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chunk")
    ap.add_argument("--at", type=int, default=None)
    ap.add_argument("--n", type=int, default=1)
    ap.add_argument("--fields", default=None)
    args = ap.parse_args()
    path = PROBE / args.chunk
    text = path.read_text(encoding="utf-8", errors="replace")
    print(f"{path.name} {len(text)} chars")
    for start, end, value in iter_modules(text):
        data = value.get("data")
        n = len(data) if hasattr(data, "__len__") else 0
        print(f"\n@ {start:>9}..{end:<9} version={value.get('version')!r} {describe(value)}")
        if args.at is None or abs(start - args.at) > 4:
            continue
        if not isinstance(data, list):
            print(json.dumps(js_to_jsonable(data), ensure_ascii=False)[:4000])
            continue
        for i, item in enumerate(data[: args.n]):
            out = js_to_jsonable(item)
            if args.fields:
                out = {k: out[k] for k in args.fields.split(",") if k in out}
            print(f"  [{i}] " + json.dumps(out, ensure_ascii=False)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
