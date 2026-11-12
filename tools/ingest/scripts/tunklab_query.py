"""Ad-hoc query helper over Tunklab data modules (development helper).

Parses a chunk's embedded `{version:"...",data:...}` modules once and caches the
result as JSON-lines/pickle under data/staging/_scratch/tunklab so repeated
exploration is cheap.  Never executes JavaScript.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_query.py MODULE_INDEX [--find TEXT] [--n 3]
  python tools/ingest/scripts/tunklab_query.py MODULE_INDEX --dump [--n 2]
  python tools/ingest/scripts/tunklab_query.py MODULE_INDEX --path a.b.0

MODULE_INDEX is a chunk file name relative to the recon responses dir, or a
`file@offset` selector, e.g. `0430-8cl4viex.js@2417433`.
"""
from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import UnsupportedJsConstruct, js_to_jsonable, parse_literal  # noqa: E402

RESPONSES = ROOT / "data" / "raw" / "_probe" / "tunklab-recon" / "tunklab-recon-001" / "responses"
CACHE = ROOT / "data" / "staging" / "_scratch" / "tunklab"
MODULE_HINT = re.compile(r'\{version:"')


def load_modules(chunk_file: Path) -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE / (chunk_file.name + ".modules.pickle")
    stat = chunk_file.stat()
    if cache_path.exists():
        try:
            cached = pickle.loads(cache_path.read_bytes())
            if cached.get("size") == stat.st_size:
                return cached["modules"]
        except Exception:  # noqa: BLE001
            pass
    text = chunk_file.read_text(encoding="utf-8", errors="replace")
    modules: list[dict] = []
    for m in MODULE_HINT.finditer(text):
        try:
            value, end = parse_literal(text, m.start())
        except UnsupportedJsConstruct:
            continue
        except Exception:  # noqa: BLE001
            continue
        if isinstance(value, dict) and "version" in value and "data" in value:
            modules.append({"offset": m.start(), "end": end,
                            "version": value["version"], "data": value["data"]})
    cache_path.write_bytes(pickle.dumps({"size": stat.st_size, "modules": modules}))
    return modules


def get_path(data, path: str):
    cur = data
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def walk(value, where=""):
    yield where, value
    if isinstance(value, dict):
        for k, v in value.items():
            yield from walk(v, f"{where}.{k}" if where else str(k))
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from walk(v, f"{where}.{i}" if where else str(i))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("chunk")
    ap.add_argument("--at", type=int, default=None)
    ap.add_argument("--find", default=None)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    name, _, at = args.chunk.partition("@")
    chunk_file = RESPONSES / name
    if not chunk_file.exists():
        chunk_file = ROOT / "data" / "raw" / "_probe" / "tunklab" / name
    at_offset = args.at if args.at is not None else (int(at) if at else None)
    modules = load_modules(chunk_file)
    if args.list:
        for mod in modules:
            data = mod["data"]
            print(f"@{mod['offset']} v={mod['version']} "
                  f"{type(data).__name__} n={len(data) if hasattr(data,'__len__') else '-'}")
        return 0
    if at_offset is None:
        print("need --at or chunk@offset")
        return 2
    mod = min(modules, key=lambda m: abs(m["offset"] - at_offset))
    data = mod["data"]
    print(f"module @{mod['offset']} v={mod['version']} "
          f"{type(data).__name__} n={len(data) if hasattr(data,'__len__') else '-'}")
    if args.find:
        needle = args.find.lower()
        hits = 0
        for where, value in walk(data):
            if isinstance(value, str) and needle in value.lower():
                # print the enclosing record for the first hit per top-level item
                print(f"  hit {where}: {value[:160]!r}")
                hits += 1
                if hits >= args.n:
                    break
        print(f"  ({hits} shown)")
        return 0
    if args.dump:
        items = data if isinstance(data, list) else list(data.values())
        for i, item in enumerate(items[: args.n]):
            print(f"[{i}] " + json.dumps(js_to_jsonable(item), ensure_ascii=False)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
