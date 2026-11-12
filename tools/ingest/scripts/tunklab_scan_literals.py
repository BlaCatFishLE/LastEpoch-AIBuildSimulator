"""Ad-hoc literal-structure scanner for Tunklab chunks (development helper).

Finds `NAME=<literal>` assignment sites whose right-hand side parses as a
literal (using le_data.jsdata, i.e. without executing JS) and is "entity-like":
a list of dicts, or a dict of lists. Prints a compact summary so the data
module of each page-specific chunk can be located.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_scan_literals.py CHUNK1.js [CHUNK2.js ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import parse_literal  # noqa: E402

PROBE = ROOT / "data" / "raw" / "_probe"
ASSIGN = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)=([\[{])")


def looks_entity_like(value) -> tuple[bool, str]:
    if isinstance(value, list):
        dicts = [v for v in value if isinstance(v, dict)]
        if len(value) >= 2 and len(dicts) >= len(value) * 0.8:
            keys = list(dicts[0])[:14]
            return True, f"list({len(value)}) keys={keys}"
        return False, ""
    if isinstance(value, dict):
        if len(value) >= 3:
            return True, f"dict({len(value)}) keys={list(value)[:14]}"
    return False, ""


def main() -> int:
    for name in sys.argv[1:]:
        path = PROBE / name
        text = path.read_text(encoding="utf-8", errors="replace")
        print(f"\n===== {name} ({len(text)} chars) =====")
        seen: set[int] = set()
        for m in ASSIGN.finditer(text):
            var, brace = m.group(1), m.start(2)
            if brace in seen:
                continue
            seen.add(brace)
            try:
                value, end = parse_literal(text, brace)
            except Exception:  # noqa: BLE001
                continue
            ok, desc = looks_entity_like(value)
            if not ok:
                continue
            print(f"  {var}= @{brace}..{end} len={end-brace}  {desc}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
