"""Recon helper: fetch Tunklab pages + referenced chunks and summarise data modules.

Development helper that uses the shared Fetcher (so the 1s policy / cache rules
apply) into a scratch snapshot. It is used to discover which page references
which chunk and which chunk carries which entity kind.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_recon.py --snapshot tunklab-recon-001 [--pages ...]
  python tools/ingest/scripts/tunklab_recon.py --offline --snapshot tunklab-recon-001
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.fetch import Fetcher, SourcePolicy, now_utc  # noqa: E402
from le_data.jsdata import UnsupportedJsConstruct, parse_literal  # noqa: E402

BASE = "https://lastepoch.tunklab.com"
DEFAULT_PAGES = [
    "/",
    "/skillsearch",
    "/buffs",
    "/ailments",
    "/class/mage",
    "/class/acolyte",
    "/class/primalist",
    "/class/sentinel",
    "/class/rogue",
    "/ability?a=Fireball",
    "/ability?a=FlameWard",
]

CHUNK_RE = re.compile(r"/_next/static/chunks/[A-Za-z0-9_.,~\-]+\.js")
MODULE_HINT = re.compile(r"\{version:\"")


def resource_name_for(url: str) -> str:
    if url.startswith(BASE):
        rest = url[len(BASE):]
    else:
        rest = url
    rest = rest.split("?")[0]
    name = "page" + rest.replace("/", "_")
    if "?" in url:
        name += "_" + re.sub(r"[^A-Za-z0-9]+", "-", url.split("?", 1)[1])
    return name or "page_root"


def scan_chunk(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    found = []
    for m in MODULE_HINT.finditer(text):
        brace = text.rfind("{", 0, m.end())
        try:
            value, end = parse_literal(text, brace)
        except UnsupportedJsConstruct as exc:
            found.append({"offset": brace, "error": f"unsupported: {exc}"})
            continue
        except Exception as exc:  # noqa: BLE001
            found.append({"offset": brace, "error": str(exc)})
            continue
        if not (isinstance(value, dict) and "version" in value and "data" in value):
            continue
        data = value["data"]
        item = {"offset": brace, "end": end, "version": value["version"],
                "data_type": type(data).__name__, "count": len(data) if hasattr(data, "__len__") else 0}
        if isinstance(data, list) and data and isinstance(data[0], dict):
            item["fields"] = list(data[0])[:20]
            item["first"] = json.dumps(data[0], ensure_ascii=False)[:300]
            item["_parsed"] = data
        elif isinstance(data, dict):
            item["fields"] = list(data)[:20]
            sample = next(iter(data.values()), None)
            item["first"] = json.dumps(sample, ensure_ascii=False, default=str)[:300]
        found.append(item)
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshot", default="tunklab-recon-001")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--pages", nargs="*", default=None)
    ap.add_argument("--chunk-at", default=None, help="FILE:OFFSET to dump a parsed list")
    ap.add_argument("--n", type=int, default=2)
    args = ap.parse_args()

    pages = [BASE + p for p in (args.pages or DEFAULT_PAGES)]
    fetcher = Fetcher(
        source="tunklab-recon",
        raw_root=ROOT / "data" / "raw" / "_probe",
        snapshot_id=args.snapshot,
        policy=SourcePolicy(min_interval=1.0, timeout=60.0),
        offline=args.offline,
    )
    manifest = {"snapshot": args.snapshot, "fetched_at": now_utc(), "pages": {}}
    chunk_owners: dict[str, list[str]] = {}
    for url in pages:
        name = resource_name_for(url)
        result = fetcher.fetch(url, name)
        entry = {"url": url, "status": result.fetch_status, "size": result.size,
                 "http_status": result.http_status, "error": result.error}
        chunks: list[str] = []
        if result.path and Path(result.path).exists() and result.size:
            body = Path(result.path).read_text(encoding="utf-8", errors="replace")
            chunks = sorted(set(CHUNK_RE.findall(body)))
            entry["chunks"] = chunks
            for c in chunks:
                chunk_owners.setdefault(c, []).append(url)
        manifest["pages"][url] = entry
        print(f"{result.fetch_status:12} {result.size:>9} {url} chunks={len(chunks)}")

    # fetch every referenced chunk once
    for chunk in sorted(chunk_owners):
        name = "chunk_" + Path(chunk).name
        result = fetcher.fetch(BASE + chunk, name)
        manifest["pages"].setdefault(chunk, {})["chunk_fetch"] = {
            "status": result.fetch_status, "size": result.size, "path": result.path,
            "error": result.error,
        }
        print(f"  chunk {result.fetch_status:12} {result.size:>9} {chunk}")

    out = ROOT / "data" / "raw" / "_probe" / "tunklab-recon" / args.snapshot / "recon.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out}")

    # scan chunks for data modules
    print("\n===== data modules per chunk =====")
    responses = ROOT / "data" / "raw" / "_probe" / "tunklab-recon" / args.snapshot / "responses"
    for body in sorted(responses.glob("chunk_*.body")):
        mods = scan_chunk(body)
        if not mods:
            continue
        print(f"\n-- {body.name[:-5]} ({body.stat().st_size} bytes)")
        for mod in mods:
            print(f"   @{mod.get('offset')} v={mod.get('version')} {mod.get('data_type')} "
                  f"count={mod.get('count')} fields={mod.get('fields')}")
            if mod.get("first"):
                print(f"      first: {mod['first']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
