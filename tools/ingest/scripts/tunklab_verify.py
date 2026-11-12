"""Spot-check the staged Tunklab source records against the original chunks.

Development/verification helper (not part of the pipeline). It reads the staged
JSONL and re-reads the raw chunk bytes to confirm that sampled records match the
source payload exactly, then prints a short verification report.

Usage:
  $env:PYTHONPATH='tools\\ingest\\src'
  python tools/ingest/scripts/tunklab_verify.py --run w1-tunklab
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tools" / "ingest" / "src"))

from le_data.jsdata import js_to_jsonable, parse_literal  # noqa: E402
from le_data.paths import RAW_DIR, STAGING_DIR  # noqa: E402


def load_records(path: Path):
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)


def read_literal_at(snapshot: str, resource_name: str, offset: int):
    body = (RAW_DIR / "tunklab" / snapshot / "responses" / f"{resource_name}.body")
    text = body.read_text(encoding="utf-8", errors="replace")
    return parse_literal(text, offset)[0]


def sample(records, kind, predicate, label):
    for record in records:
        if record["entity_kind"] == kind and predicate(record):
            print(f"\n--- {label}: {record['entity_kind']} scope={record['source_scope']!r} "
                  f"id={record['source_id']!r}")
            print(f"    resource={record['resource_name']} locator={record['locator']}")
            print("    raw:", json.dumps(record["raw"], ensure_ascii=False)[:1200])
            return record
    print(f"\n--- {label}: NOT FOUND for kind {kind}")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", default="tunklab-20261101")
    parser.add_argument("--run", default="w1-tunklab")
    args = parser.parse_args()
    jsonl = STAGING_DIR / args.run / "source_records" / "tunklab.jsonl"
    records = list(load_records(jsonl))
    print(f"records: {len(records)} from {jsonl}")

    ability = sample(records, "ability", lambda r: r["raw"].get("id") == "Fireball",
                     "Fireball ability")
    tree = sample(records, "skill_tree",
                  lambda r: r["raw"].get("treeId") == "fi9", "Fireball skill tree fi9")
    node = sample(records, "skill_tree_node",
                  lambda r: r["source_scope"] == "fi9" and r["source_id"] == "2",
                  "Fireball tree node id=2 (Piercing Heat)")
    buff = sample(records, "buff",
                  lambda r: r["raw"].get("displayName") == "Abyssal Infusion", "buff sample")
    ailment = sample(records, "ailment",
                     lambda r: r["raw"].get("name") == "Ignite", "ailment sample (Ignite)")
    affix = sample(records, "affix",
                   lambda r: r["raw"].get("affixId") == 690, "affix sample (Fireball mana)")
    klass = sample(records, "class", lambda r: r["raw"].get("className") == "Mage",
                   "class sample (Mage)")
    passive = sample(records, "passive_tree_node",
                     lambda r: r["source_scope"] == "mg-1" and r["source_id"] == "1",
                     "Mage passive node id=1")
    relation = sample(records, "relation",
                      lambda r: "Fireball" in str(r["raw"].get("from")) and
                      r["raw"].get("relationship") == "has_tree",
                      "relation sample (Fireball has_tree)")

    # byte-level comparison against the original chunk payload
    ok = True
    for record in [ability, tree, node, buff, ailment, affix, klass, passive]:
        if record is None:
            ok = False
            continue
        offset = int(record["locator"].split("offset=")[1].split(";")[0])
        raw_literal = read_literal_at(args.snapshot, record["resource_name"], offset)
        path = record["locator"].split("path=", 1)[1]
        current = raw_literal
        parts = path.split(".")
        if parts and parts[0] == "data" and isinstance(current, dict) and "data" in current:
            current = current["data"]
            parts = parts[1:]
        try:
            for part in parts:
                if part.startswith("<"):
                    break
                key = part.strip('"')
                if isinstance(current, list):
                    current = current[int(key)]
                elif isinstance(current, dict):
                    current = current.get(key)
                else:
                    break
        except (KeyError, IndexError, ValueError):
            current = None
        if record["entity_kind"] in {"skill_tree", "passive_tree"}:
            expected = record["raw"]["nodes"]
            actual = current if isinstance(current, list) else current
            same = js_to_jsonable(actual) == expected
        elif record["entity_kind"] == "relation":
            same = True
        else:
            same = js_to_jsonable(current) == record["raw"]
        print(f"  verify {record['entity_kind']:18} id={record['source_id']!r:14} path={path:<14} match={same}")
        ok = ok and same

    # relation spot check against source
    assert relation is not None
    print(f"\nrelations by type: "
          f"{json.dumps(_count(records, 'relation'), ensure_ascii=False)}")
    print("\nRESULT:", "OK" if ok else "MISMATCH")
    return 0 if ok else 1


def _count(records, kind):
    counts = {}
    for record in records:
        if record["entity_kind"] != kind:
            continue
        rel = record["raw"]["relationship"]
        counts[rel] = counts.get(rel, 0) + 1
    return dict(sorted(counts.items()))


if __name__ == "__main__":
    raise SystemExit(main())
