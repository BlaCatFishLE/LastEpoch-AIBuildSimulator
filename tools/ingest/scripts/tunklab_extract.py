"""Tunklab extraction driver: discover -> fetch -> parse -> staging JSONL.

This is the reproducible entry point used for the phase-1 Tunklab snapshot.  It
never executes downloaded JavaScript: parsing goes through
``le_data.jsdata.parse_literal`` only.

Usage (PowerShell, from the project root):

  $env:PYTHONPATH = 'tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_extract.py --snapshot tunklab-20261101 --run w1-tunklab
  python tools/ingest/scripts/tunklab_extract.py --snapshot tunklab-20261101 --run w1-tunklab-offline --offline

Outputs
  data/raw/tunklab/<snapshot>/responses/        original page + chunk bytes
  data/raw/tunklab/<snapshot>/inventory.json    resource inventory + version evidence
  data/staging/<run>/source_records/tunklab.jsonl
  data/staging/<run>/tunklab_summary.json       entity counts, chunk map, issues
  data/staging/<run>/issues/tunklab_issues.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.adapters.tunklab import KIND_ORDER, TunklabAdapter  # noqa: E402
from le_data.config import load_sources  # noqa: E402
from le_data.paths import RAW_DIR, STAGING_DIR  # noqa: E402
from le_data.records import write_jsonl  # noqa: E402


def build_summary(adapter: TunklabAdapter, records: list, snapshot_id: str,
                  run_id: str, offline: bool, inventory: dict | None) -> dict:
    kind_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    per_resource: dict[str, dict[str, int]] = {}
    for record in records:
        kind_counts[record.entity_kind] = kind_counts.get(record.entity_kind, 0) + 1
        status_counts[record.parse_status] = status_counts.get(record.parse_status, 0) + 1
        bucket = per_resource.setdefault(record.resource_name, {})
        bucket[record.entity_kind] = bucket.get(record.entity_kind, 0) + 1
    ordered = {kind: kind_counts[kind] for kind in KIND_ORDER if kind in kind_counts}
    for kind in sorted(kind_counts):
        if kind not in ordered:
            ordered[kind] = kind_counts[kind]

    issue_type_counts: dict[str, int] = {}
    for issue in adapter.issues:
        issue_type_counts[issue.issue_type] = issue_type_counts.get(issue.issue_type, 0) + 1

    inventory = inventory or adapter._load_inventory(snapshot_id) or {}
    resources = inventory.get("resources", [])
    return {
        "source": adapter.name,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "offline": offline,
        "base_url": adapter.base_url,
        "claimed_version": adapter.claimed_version,
        "discovery": {
            "page_count": sum(1 for r in resources if r.get("role") == "page"),
            "chunk_count": sum(1 for r in resources if r.get("role") == "chunk"),
            "lazy_chunks": sorted(
                r["resource_name"] for r in resources
                if any(str(ref).startswith("chunk:") for ref in (r.get("referenced_by") or []))
            ),
            "page_chunks": inventory.get("page_chunks", {}),
            "version_evidence": inventory.get("version_evidence", {}),
        },
        "fetch": {
            "summary": inventory.get("fetch_summary", {}),
            "resources": [
                {
                    "resource_name": r.get("resource_name"),
                    "role": r.get("role"),
                    "status": r.get("status"),
                    "http_status": r.get("http_status"),
                    "size": r.get("size"),
                    "sha256": r.get("sha256"),
                    "error": r.get("error"),
                }
                for r in resources
            ],
        },
        "parse": {
            "record_count": len(records),
            "entity_kind_counts": ordered,
            "parse_status_counts": dict(sorted(status_counts.items())),
            "per_resource_kind_counts": dict(sorted(per_resource.items())),
            "chunk_entity_map": dict(sorted(adapter.chunk_entity_map.items())),
            "module_inventory": adapter.module_inventory,
            "stats": adapter.parse_stats,
            "relation_counts": getattr(adapter, "relation_counts", {}),
            "relation_resolved_counts": getattr(adapter, "relation_resolved_counts", {}),
        },
        "issues": {
            "count": len(adapter.issues),
            "by_type": dict(sorted(issue_type_counts.items())),
            "items": [issue.to_dict() for issue in adapter.issues],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default="tunklab-20261101",
                        help="source snapshot id (raw cache directory)")
    parser.add_argument("--run", default="w1-tunklab", help="staging run id")
    parser.add_argument("--offline", action="store_true",
                        help="never touch the network; parse/refetch from local cache only")
    parser.add_argument("--skip-discover", action="store_true")
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--config", default=None, help="explicit sources.json path")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logger = (lambda message: None) if args.quiet else (lambda message: print(message, flush=True))
    config = load_sources(Path(args.config) if args.config else None)
    source_config = config.get("tunklab")
    adapter = TunklabAdapter(source_config, RAW_DIR, logger=logger)

    inventory = None
    if not args.skip_discover:
        inventory = adapter.discover(args.snapshot, offline=args.offline)
    if not args.skip_fetch:
        results = adapter.fetch(args.snapshot, offline=args.offline, inventory=inventory)
        fetched = sum(1 for row in results if row.get("fetch_status") == "fetched")
        failed = sum(1 for row in results if row.get("fetch_status") == "failed")
        logger(f"[tunklab] fetch done: {len(results)} resources, "
               f"{fetched} fetched, {failed} failed")

    records = list(adapter.parse(args.snapshot))
    logger(f"[tunklab] parse done: {len(records)} source records, "
           f"{len(adapter.issues)} issues")

    run_dir = STAGING_DIR / args.run
    jsonl_path = run_dir / "source_records" / "tunklab.jsonl"
    count = write_jsonl(jsonl_path, (record.to_dict() for record in records))
    issues_path = run_dir / "issues" / "tunklab_issues.jsonl"
    write_jsonl(issues_path, (issue.to_dict() for issue in adapter.issues))
    summary = build_summary(adapter, records, args.snapshot, args.run,
                            args.offline, inventory)
    summary_path = run_dir / "tunklab_summary.json"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                            encoding="utf-8")

    print(f"\nrecords : {count} -> {jsonl_path}")
    print(f"issues  : {len(adapter.issues)} -> {issues_path}")
    print(f"summary : {summary_path}")
    print("entity kinds:")
    for kind, value in summary["parse"]["entity_kind_counts"].items():
        print(f"  {kind:22} {value:>6}")
    if summary["issues"]["by_type"]:
        print("issue types:")
        for kind, value in summary["issues"]["by_type"].items():
            print(f"  {kind:28} {value:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
