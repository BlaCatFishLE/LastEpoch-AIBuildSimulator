"""Development helper: build a combined dataset from source-record JSONL.

Mirrors ``le-data normalize`` without requiring a recorded run state; used for
integration checks while both adapters are still being iterated on.

Usage:
  python scripts/dev_build_dataset.py \
      --letools-jsonl data/staging/w1-letools/source_records/letools.jsonl \
      --tunklab-jsonl data/staging/w1-tunklab/source_records/tunklab.jsonl \
      --dataset dev-dataset --db data/staging/dev-combined/data.sqlite
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.normalize.base import Crosswalk, Normalizer  # noqa: E402
from le_data.records import SourceRecord, read_jsonl  # noqa: E402
from le_data.store import (  # noqa: E402
    apply_migrations,
    connect,
    insert_row,
    integrity_report,
    table_counts,
    upsert_dataset_meta,
)


def insert_source_records(conn, snapshot_id: str, records: list[SourceRecord]) -> None:
    for record in records:
        insert_row(
            conn,
            "source_records",
            {
                "record_id": record.record_id,
                "source_snapshot_id": record.source_snapshot_id,
                "source": record.source,
                "resource_name": record.resource_name,
                "entity_kind": record.entity_kind,
                "source_scope": record.source_scope,
                "source_id": record.source_id,
                "locator": record.locator,
                "raw_payload": json.dumps(record.raw, ensure_ascii=False),
                "parse_status": record.parse_status,
            },
            or_ignore=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--letools-jsonl", required=True)
    parser.add_argument("--tunklab-jsonl", required=True)
    parser.add_argument("--dataset", default="dev-dataset")
    parser.add_argument("--db", required=True)
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    apply_migrations(conn)
    crosswalk = Crosswalk.load()
    inputs = [
        ("letools", "letools-20261101", args.letools_jsonl),
        ("tunklab", "tunklab-20261101", args.tunklab_jsonl),
    ]
    started = time.time()
    summaries = {}
    for source, snapshot_id, path in inputs:
        conn.execute(
            "INSERT OR REPLACE INTO source_snapshots "
            "(source_snapshot_id, source, claimed_version, status) VALUES (?,?,?,?)",
            (snapshot_id, source, "1.5.2", "ok"),
        )
        records = [SourceRecord.from_dict(row) for row in read_jsonl(Path(path))]
        insert_source_records(conn, snapshot_id, records)
        conn.commit()
        normalizer = Normalizer(conn, source, snapshot_id, crosswalk=crosswalk)
        summaries[source] = normalizer.normalize_records(records)
        conn.commit()
        print(f"{source}: {len(records)} records -> {json.dumps(summaries[source]['entity_counts'])}")
    upsert_dataset_meta(conn, args.dataset, status="staging",
                        target_game_version="1.5.2", target_season="Season 5",
                        mapping_revision=crosswalk.revision)
    from le_data.normalize.domain_reshape import reshape_domains

    print("domain reshape: " + json.dumps(reshape_domains(conn), ensure_ascii=False))
    conn.commit()
    for source, snapshot_id, _path in inputs:
        conn.execute(
            "INSERT OR IGNORE INTO dataset_source_snapshots VALUES (?,?,?)",
            (args.dataset, snapshot_id, source),
        )
    conn.commit()
    counts = {k: v for k, v in table_counts(conn).items() if v}
    print("elapsed: %.1fs" % (time.time() - started))
    print("tables: " + json.dumps(counts, ensure_ascii=False))
    print("integrity: " + json.dumps(integrity_report(conn), ensure_ascii=False))
    conn.close()
    print(f"db: {db_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
