"""Development helper: normalize one source JSONL into a throwaway database.

Usage:
  python scripts/dev_normalize_source.py --source tunklab --snapshot tunklab-20261101 \
      --jsonl data/staging/w1-tunklab/source_records/tunklab.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize.base import Crosswalk, Normalizer  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    integrity_report,
    table_counts,
    upsert_dataset_meta,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--jsonl", required=True)
    parser.add_argument("--db", default=None)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    db_path = Path(args.db) if args.db else Path(tempfile.mkdtemp()) / "norm.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    apply_migrations(conn)
    conn.execute(
        "INSERT INTO source_snapshots (source_snapshot_id, source, claimed_version, status) "
        "VALUES (?,?,?,?)",
        (args.snapshot, args.source, "1.5.2", "ok"),
    )
    conn.commit()
    records = [SourceRecord.from_dict(row) for row in read_jsonl(Path(args.jsonl))]
    started = time.time()
    normalizer = Normalizer(conn, args.source, args.snapshot,
                            crosswalk=Crosswalk.load(), logger=lambda m: None)
    summary = normalizer.normalize_records(records)
    conn.commit()
    upsert_dataset_meta(conn, "dev-dataset", status="staging")
    conn.commit()
    elapsed = time.time() - started
    print(f"records: {len(records)}  elapsed: {elapsed:.1f}s")
    print("entity counts: " + json.dumps(summary["entity_counts"], ensure_ascii=False))
    print("skipped: " + json.dumps(summary["skipped_kinds"], ensure_ascii=False))
    print("unmapped: " + json.dumps(summary["unmapped_kinds"], ensure_ascii=False))
    print("integrity: " + json.dumps(integrity_report(conn), ensure_ascii=False))
    print("tables: " + json.dumps(
        {k: v for k, v in table_counts(conn).items() if v}, ensure_ascii=False
    ))
    conn.close()
    print(f"db: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
