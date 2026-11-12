"""Ad-hoc probe for Tunklab JS chunks (development helper, not part of the pipeline).

Reads local probe chunks under data/raw/_probe/tunklab/ and reports where the
embedded data modules live, without executing any JavaScript.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_probe.py
"""
from __future__ import annotations

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
PATTERN = re.compile(r"\{version:\"([0-9][^\"]*)\",data:")
MODULE_HINT = re.compile(r"\{version:\"")


def scan(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    print(f"\n===== {path.name} ({len(text)} chars) =====")
    for m in MODULE_HINT.finditer(text):
        start = m.start()
        # backtrack to the opening brace of the literal
        brace = text.rfind("{", 0, m.end())
        print(f"  module candidate at {start}: {text[start:start+60]!r}")
        try:
            value, end = parse_literal(text, brace)
        except UnsupportedJsConstruct as exc:
            print(f"    UNSUPPORTED: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            print(f"    ERROR: {exc}")
            continue
        print(f"    parsed ok, end={end}, keys={list(value)[:12]}")
        if isinstance(value, dict) and isinstance(value.get("data"), dict):
            data = value["data"]
            print(f"    data keys={len(data)} sample={list(data)[:8]}")
            first = next(iter(data.items()))
            key, val = first
            print(f"    sample key={key!r} type={type(val).__name__} "
                  f"len={len(val) if hasattr(val,'__len__') else '-'}")
            sample = js_to_jsonable(val)
            if isinstance(val, list) and val:
                print("    first element:", json.dumps(sample[0], ensure_ascii=False)[:900])
            else:
                print("    value:", json.dumps(sample, ensure_ascii=False)[:900])


def main() -> int:
    for path in sorted(PROBE.glob("*.js")):
        scan(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
