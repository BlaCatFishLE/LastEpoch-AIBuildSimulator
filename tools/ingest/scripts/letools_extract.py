"""Development driver for the Last Epoch Tools adapter (phase-1 deliverable).

Pipeline: discover -> fetch/cache -> parse, then persist the source records as
JSONL plus a machine-readable summary.

Usage (project root):

  <python> tools/ingest/scripts/letools_extract.py \
      --snapshot letools-20261101 --run w1-letools

  # offline: no network at all, only cached responses are used
  <python> tools/ingest/scripts/letools_extract.py \
      --snapshot letools-20261101 --run w1-letools --offline

  # re-parse only (no discover/fetch), for reproducibility checks
  <python> tools/ingest/scripts/letools_extract.py \
      --snapshot letools-20261101 --run w1-letools-reparse --offline --parse-only

Outputs:
  data/raw/letools/<snapshot>/responses/           exact response bytes + meta
  data/raw/letools/<snapshot>/inventory.json       discovery/inventory evidence
  data/staging/<run>/source_records/letools.jsonl  SourceRecords (one per entity)
  data/staging/<run>/letools_summary.json          counts, issues, hashes
  data/staging/<run>/letools_inventory.json        copy of the inventory
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import traceback
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.adapters.letools import LeToolsAdapter  # noqa: E402
from le_data.config import load_sources  # noqa: E402
from le_data.paths import RAW_DIR, staging_dir  # noqa: E402
from le_data.records import write_jsonl  # noqa: E402
from le_data.runs import RunStore  # noqa: E402


def translation_stats(records: list[dict]) -> dict:
    """Count translation records and check that referenced *Key fields resolve."""
    by_namespace: dict[str, int] = {}
    translations: dict[str, str] = {}
    for row in records:
        if row["entity_kind"] != "translation":
            continue
        raw = row.get("raw") or {}
        key = raw.get("key", row.get("source_id"))
        value = raw.get("value")
        namespace = raw.get("namespace") or row.get("source_scope") or "default"
        by_namespace[namespace] = by_namespace.get(namespace, 0) + 1
        if isinstance(value, str) and key:
            translations[str(key)] = value

    referenced: set[str] = set()

    def walk(value) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).endswith("Key"):
                    if isinstance(item, str):
                        referenced.add(item)
                    elif isinstance(item, list):
                        referenced.update(entry for entry in item if isinstance(entry, str))
                if isinstance(item, (dict, list)):
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for row in records:
        if row["entity_kind"] == "translation":
            continue
        walk(row.get("raw"))

    unresolved = sorted(key for key in referenced if key not in translations)
    return {
        "translation_records": sum(by_namespace.values()),
        "translation_records_by_namespace": dict(sorted(by_namespace.items())),
        "translation_lookup_keys": len(translations),
        "referenced_translation_keys": len(referenced),
        "resolved_translation_keys": len(referenced) - len(unresolved),
        "unresolved_translation_keys": len(unresolved),
        "unresolved_translation_sample": unresolved[:40],
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def log(message: str) -> None:
    print(message, flush=True)


def build_adapter() -> LeToolsAdapter:
    config = load_sources()
    source_config = config.get("letools")
    return LeToolsAdapter(source_config, RAW_DIR, logger=log)


def collect_records(adapter: LeToolsAdapter, snapshot: str) -> list[dict]:
    records: list[dict] = []
    for record in adapter.parse(snapshot):
        records.append(record.to_dict())
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshot", required=True, help="source snapshot id, e.g. letools-20261101")
    parser.add_argument("--run", required=True, help="run id, e.g. w1-letools")
    parser.add_argument("--offline", action="store_true", help="never touch the network (cache only)")
    parser.add_argument("--parse-only", action="store_true", help="skip discover/fetch; parse cached responses")
    parser.add_argument("--limit-issues", type=int, default=0, help="cap issues embedded in the summary (0 = all)")
    args = parser.parse_args(argv)

    staging = staging_dir(args.run)
    records_path = staging / "source_records" / "letools.jsonl"
    summary_path = staging / "letools_summary.json"
    inventory_copy = staging / "letools_inventory.json"
    staging.mkdir(parents=True, exist_ok=True)

    adapter = build_adapter()
    run_store = RunStore(args.run)
    inventory: dict | None = None
    fetch_results: list[dict] = []
    try:
        if not args.parse_only:
            log(f"[discover] snapshot={args.snapshot} offline={args.offline}")
            inventory = adapter.discover(args.snapshot, offline=args.offline)
            log(f"[fetch] {len(inventory.get('resources') or [])} bundles")
            fetch_results = adapter.fetch(args.snapshot, offline=args.offline, inventory=inventory)
        else:
            inventory = adapter._load_inventory(args.snapshot)
            log("[parse-only] skipping discover/fetch; cached responses only")
        inventory_status = (inventory or {}).get("status", "ok")
        run_store.set_snapshot("letools", args.snapshot, status=inventory_status)
        if not args.parse_only:
            run_store.mark_stage("discover", "letools", inventory_status)
            fetch_status = "ok" if all(
                row.get("fetch_status") != "failed" for row in fetch_results
            ) else "partial"
            run_store.mark_stage("fetch", "letools", fetch_status)
        log(f"[parse] snapshot={args.snapshot} offline=True (cache only)")
        records = collect_records(adapter, args.snapshot)
        run_store.mark_stage("parse", "letools", "ok" if records else "failed")
    except Exception:  # keep the traceback: no silent failure
        traceback.print_exc()
        return 1

    if inventory is None:
        inventory = adapter._load_inventory(args.snapshot) or {}

    records.sort(key=lambda row: row["record_id"])
    write_jsonl(records_path, records)
    digest = sha256_file(records_path)

    by_kind = Counter(row["entity_kind"] for row in records)
    by_status = Counter(row["parse_status"] for row in records)
    by_kind_status = Counter(f"{row['entity_kind']}:{row['parse_status']}" for row in records)
    record_ids = [row["record_id"] for row in records]
    duplicates = [rid for rid, count in Counter(record_ids).items() if count > 1]
    issues = [issue.to_dict() for issue in adapter.issues]
    issues_by_type = Counter(issue["issue_type"] for issue in issues)
    severity_counts = Counter(issue["severity"] for issue in issues)

    if inventory:
        inventory_copy.write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")

    presented_issues = issues if args.limit_issues <= 0 else issues[: args.limit_issues]
    summary = {
        "source": "letools",
        "snapshot_id": args.snapshot,
        "run_id": args.run,
        "generated_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(timespec="seconds"),
        "offline": args.offline,
        "parse_only": args.parse_only,
        "inventory_status": inventory.get("status") if inventory else None,
        "claimed_version": inventory.get("claimed_version") if inventory else None,
        "data_version": inventory.get("data_version") if inventory else None,
        "version_evidence": inventory.get("version_evidence") if inventory else [],
        "entry_url": inventory.get("entry_url") if inventory else None,
        "resources": [
            {
                "resource_name": res.get("resource_name"),
                "role": res.get("role"),
                "url": res.get("url"),
                "sha256": res.get("sha256"),
                "size": res.get("size"),
                "fetch_status": res.get("fetch_status"),
                "windows": res.get("windows"),
            }
            for res in (inventory.get("resources") or [])
        ],
        "fetch_results": fetch_results,
        "records_path": str(records_path.relative_to(ROOT)),
        "records_count": len(records),
        "records_sha256": digest,
        "records_by_entity_kind": dict(sorted(by_kind.items())),
        "records_by_parse_status": dict(sorted(by_status.items())),
        "records_by_entity_kind_status": dict(sorted(by_kind_status.items())),
        "translation_coverage": translation_stats(records),
        "duplicate_record_ids": duplicates,
        "issues_count": len(issues),
        "issues_by_type": dict(sorted(issues_by_type.items())),
        "issues_by_severity": dict(sorted(severity_counts.items())),
        "issues": presented_issues,
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    log("")
    log(f"records: {len(records)}  sha256: {digest}")
    log(f"by entity_kind: {json.dumps(dict(sorted(by_kind.items())), ensure_ascii=False)}")
    log(f"by parse_status: {json.dumps(dict(sorted(by_status.items())), ensure_ascii=False)}")
    log(f"issues: {len(issues)} {json.dumps(dict(sorted(issues_by_type.items())), ensure_ascii=False)}")
    log(f"translations: {json.dumps(summary['translation_coverage'], ensure_ascii=False)[:400]}")
    if duplicates:
        log(f"WARNING duplicate record ids: {duplicates[:10]}")
    log(f"wrote {records_path}")
    log(f"wrote {summary_path}")

    if not records:
        return 1
    error_issues = [issue for issue in issues if issue["severity"] == "error"]
    return 1 if error_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
