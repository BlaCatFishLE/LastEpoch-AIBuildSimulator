"""Ad-hoc inventory of every parseable literal in the recon chunks.

Development helper used to design the structural classifier of the Tunklab
adapter: it lists candidate literals (versioned modules and assigned literals)
with size, container type, count and first-record keys.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_scan_all.py [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import js_to_jsonable, parse_literal  # noqa: E402

RESPONSES = ROOT / "data" / "raw" / "_probe" / "tunklab-recon" / "tunklab-recon-001" / "responses"
VERSION_RE = re.compile(r'\{version:"')
ASSIGN_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)=([\[{])")


def candidate_starts(text: str) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for m in VERSION_RE.finditer(text):
        out.append(("version", m.start()))
    for m in ASSIGN_RE.finditer(text):
        out.append((m.group(1), m.start(2)))
    out.sort(key=lambda x: x[1])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", default=None)
    args = ap.parse_args()
    rows = []
    for body in sorted(RESPONSES.glob("chunk_*.body")):
        text = body.read_text(encoding="utf-8", errors="replace")
        covered: list[tuple[int, int]] = []
        parsed = []
        for label, pos in candidate_starts(text):
            if any(lo <= pos < hi for lo, hi in covered):
                continue
            try:
                value, end = parse_literal(text, pos)
            except Exception:  # noqa: BLE001
                continue
            covered.append((pos, end))
            if isinstance(value, dict) and "version" in value and "data" in value:
                payload = value["data"]
                kind = "versioned"
                version = value["version"]
            else:
                payload = value
                kind = "literal"
                version = None
            if not hasattr(payload, "__len__") or len(payload) < 2:
                continue
            first = None
            if isinstance(payload, list):
                dicts = [x for x in payload if isinstance(x, dict)]
                if not dicts:
                    continue
                first = dicts[0]
                n = len(payload)
            elif isinstance(payload, dict):
                if not payload:
                    continue
                first = next(iter(payload.values()))
                if isinstance(first, dict) and isinstance(next(iter(payload.values())), list):
                    pass
                n = len(payload)
            if isinstance(first, list):
                inner = [x for x in first if isinstance(x, dict)]
                keys = sorted(inner[0])[:24] if inner else None
                extra = f"dict-of-lists inner_n={len(first)}"
            elif isinstance(first, dict):
                keys = sorted(first)[:24]
                extra = ""
            else:
                keys = None
                extra = f"first type {type(first).__name__}"
            rows.append({
                "chunk": body.name,
                "label": label,
                "offset": pos,
                "end": end,
                "kind": kind,
                "version": version,
                "container": type(payload).__name__,
                "count": n,
                "keys": keys,
                "extra": extra,
                "first": json.dumps(js_to_jsonable(first), ensure_ascii=False)[:600],
            })
    for row in rows:
        print(f"\n{row['chunk']} @{row['offset']} [{row['kind']}] v={row['version']} "
              f"{row['container']} n={row['count']} {row['extra']}")
        print(f"   keys={row['keys']}")
        print(f"   first={row['first']}")
    if args.json:
        Path(args.json).write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json} ({len(rows)} literals)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
