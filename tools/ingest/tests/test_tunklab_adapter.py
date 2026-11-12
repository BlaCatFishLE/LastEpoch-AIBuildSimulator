"""Offline regression tests for the Tunklab adapter.

No test performs a network request: parse() only reads the raw snapshot that
was fetched during W1 (``data/raw/tunklab/<snapshot>/responses``).  The adapter
is additionally constructed with an unreachable ``base_url`` to prove that the
parse path never consults the network.

Run (project root, no pytest required)::

    python -m unittest discover -s tools/ingest/tests -p "test_tunklab_*.py" -v
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from le_data.adapters.tunklab import TunklabAdapter  # noqa: E402
from le_data.config import load_sources  # noqa: E402
from le_data.jsdata import js_to_jsonable, parse_literal  # noqa: E402
from le_data.paths import RAW_DIR  # noqa: E402
from le_data.records import PARSE_UNPARSED, SourceRecord, read_jsonl, write_jsonl  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SNAPSHOT = "tunklab-20261101"
RAW_SNAPSHOT = RAW_DIR / "tunklab" / SNAPSHOT
STAGED_JSONL = (
    PROJECT_ROOT / "data" / "staging" / "w1-tunklab" / "source_records" / "tunklab.jsonl"
)

#: exact counts from the W1 snapshot; update together with the adapter contract
EXPECTED_KIND_COUNTS = {
    "class": 5,
    "mastery": 20,
    "ability": 4350,
    "ability_properties": 177,
    "property": 113,
    "property_text": 713,
    "skill_tree": 137,          # 136 populated trees + one empty "" key, kept verbatim
    "skill_tree_node": 3956,
    "passive_tree": 5,
    "passive_tree_node": 541,
    "buff": 264,
    "ailment": 147,
    "affix": 1156,
    "unique_item": 487,
    "item_set": 25,
    "item_type": 47,
}
EXPECTED_RECORD_COUNT = 28478


def _source_config() -> dict:
    config = dict(load_sources().get("tunklab"))
    # parse() must be fully offline: an unreachable base_url makes any accidental
    # network call fail loudly instead of silently succeeding.
    config["base_url"] = "http://127.0.0.1:9"
    return config


def _parse(snapshot: str = SNAPSHOT, raw_root: Path = RAW_DIR) -> tuple[list[SourceRecord], TunklabAdapter]:
    adapter = TunklabAdapter(_source_config(), raw_root)
    records = list(adapter.parse(snapshot))
    return records, adapter


def _sha256_of_records(records: list[SourceRecord], directory: Path, name: str) -> str:
    path = directory / name
    write_jsonl(path, (record.to_dict() for record in records))
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _find(records: list[SourceRecord], kind: str, predicate) -> SourceRecord:
    for record in records:
        if record.entity_kind == kind and predicate(record):
            return record
    raise AssertionError(f"no {kind} record matched the sample predicate")


def _raw_at(record: SourceRecord, raw_root: Path = RAW_DIR) -> object:
    """Re-parse the raw chunk and return the value located by record.locator."""
    resource, _, rest = record.locator.partition("#offset=")
    offset = int(rest.split(";")[0])
    path = record.locator.split("path=", 1)[1]
    body = (raw_root / "tunklab" / record.source_snapshot_id / "responses" / f"{resource}.body")
    literal = parse_literal(body.read_text(encoding="utf-8", errors="replace"), offset)[0]
    parts = path.split(".")
    cursor = literal
    if parts and parts[0] == "data" and isinstance(cursor, dict) and "data" in cursor:
        cursor = cursor["data"]
        parts = parts[1:]
    for part in parts:
        if part.startswith("<"):
            break
        key = part.strip('"')
        cursor = cursor[int(key)] if isinstance(cursor, list) else cursor.get(key)
    return cursor


@unittest.skipUnless(RAW_SNAPSHOT.is_dir(), f"raw snapshot missing: {RAW_SNAPSHOT}")
class TunklabAdapterOfflineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records, cls.adapter = _parse()

    # -- idempotency / counts -------------------------------------------------
    def test_parse_is_idempotent_offline(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = _sha256_of_records(self.records, directory, "run1.jsonl")
            second_records, _ = _parse()
            second = _sha256_of_records(second_records, directory, "run2.jsonl")
        self.assertEqual(first, second, "two offline parses must produce identical JSONL bytes")
        if STAGED_JSONL.exists():
            staged = hashlib.sha256()
            with open(STAGED_JSONL, "rb") as handle:
                for chunk in iter(lambda: handle.read(1 << 20), b""):
                    staged.update(chunk)
            self.assertEqual(
                first,
                staged.hexdigest(),
                "offline parse must reproduce the staged w1-tunklab JSONL exactly",
            )

    def test_expected_record_count(self):
        self.assertEqual(len(self.records), EXPECTED_RECORD_COUNT)

    def test_expected_entity_kind_counts(self):
        counts: dict[str, int] = {}
        for record in self.records:
            counts[record.entity_kind] = counts.get(record.entity_kind, 0) + 1
        for kind, expected in EXPECTED_KIND_COUNTS.items():
            self.assertEqual(counts.get(kind), expected, f"kind {kind!r} count changed")
        self.assertGreater(counts.get("relation", 0), 14000)

    def test_no_parse_failures(self):
        self.assertEqual(self.adapter.parse_stats["parse_failures"], 0)
        failure_types = {"parse_failed", "unparsed_module", "unparsed_literal"}
        found = [issue.issue_type for issue in self.adapter.issues if issue.issue_type in failure_types]
        self.assertEqual(found, [], "the W1 snapshot must parse without unsupported constructs")
        self.assertIn("chunk_0430-8cl4viex.js", self.adapter.chunk_entity_map)
        self.assertIn("chunk_1xbwm4iifo2k2.js", self.adapter.chunk_entity_map)

    # -- spot checks against the raw chunk payloads ---------------------------
    def test_sample_records_match_raw_payload(self):
        samples = [
            _find(self.records, "ability", lambda r: r.raw.get("id") == "Fireball"),
            _find(self.records, "skill_tree", lambda r: r.raw.get("treeId") == "fi9"),
            _find(self.records, "skill_tree_node",
                  lambda r: r.source_scope == "fi9" and r.source_id == "2"),
            _find(self.records, "passive_tree_node",
                  lambda r: r.source_scope == "mg-1" and r.source_id == "1"),
            _find(self.records, "buff", lambda r: r.raw.get("displayName") == "Abyssal Infusion"),
            _find(self.records, "ailment", lambda r: r.raw.get("name") == "Ignite"),
            _find(self.records, "affix", lambda r: r.raw.get("affixId") == 690),
            _find(self.records, "class", lambda r: r.raw.get("className") == "Mage"),
            _find(self.records, "unique_item", lambda r: r.raw.get("uniqueId") == 0),
            _find(self.records, "item_set", lambda r: r.raw.get("setID") == 1),
            _find(self.records, "property", lambda r: r.raw.get("property") == 0),
        ]
        for record in samples:
            with self.subTest(kind=record.entity_kind, source_id=record.source_id):
                current = _raw_at(record)
                if record.entity_kind in {"skill_tree", "passive_tree"}:
                    self.assertEqual(js_to_jsonable(current), record.raw["nodes"])
                else:
                    self.assertEqual(js_to_jsonable(current), record.raw)

    def test_relation_record_shape(self):
        relation = _find(
            self.records,
            "relation",
            lambda r: r.raw.get("relationship") == "has_tree"
            and r.raw.get("from", {}).get("id") == "Fireball",
        )
        raw = relation.raw
        self.assertEqual(raw["to"]["kind"], "skill_tree")
        self.assertEqual(raw["to"]["id"], "fi9")
        self.assertIn(raw["resolution_status"], {"resolved", "unresolved"})
        self.assertTrue(relation.locator.endswith("playerAbilityID"))

    # -- failure modes with a synthetic snapshot ------------------------------
    def test_synthetic_snapshot_reports_unsupported_and_unparsed_payloads(self):
        synthetic_items = ",\n".join(
            "{alpha:%d,beta:%d,gamma:%d,delta:%d}" % (i, i + 1, i + 2, i + 3) for i in range(25)
        )
        chunk = "\n".join(
            [
                # 1) valid payload that must still be parsed
                'let ok={"t1":[{id:0,maxPoints:0,nodeName:"Root",description:"",'
                "stats:[],requirements:[]}]};",
                # 2) version-wrapped payload whose value is a dynamic identifier
                'let bad={version:"1.5.2",data:someIdentifier};',
                # 3) version-wrapped payload with an unrecognised shape
                'let odd={version:"1.5.2",data:{foo:1,bar:2,baz:3}};',
                # 4) entity-like assignment literal that matches no known shape
                f"let zz=[{synthetic_items}];",
                'e.s(["f",0,{version:"1.5.2",data:{"t1":'
                '[{id:0,maxPoints:0,nodeName:"Root",description:"",stats:[],requirements:[]}]}}]);',
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            raw_root = Path(tmp)
            responses = raw_root / "tunklab" / "synthetic-snapshot" / "responses"
            responses.mkdir(parents=True)
            (responses / "chunk_synthetic.js.body").write_text(chunk, encoding="utf-8")
            (responses / "chunk_synthetic.js.meta.json").write_text(
                json.dumps({"url": "https://example.invalid/chunk.js",
                            "resource_name": "chunk_synthetic.js"}),
                encoding="utf-8",
            )
            adapter = TunklabAdapter(_source_config(), raw_root)
            records = list(adapter.parse("synthetic-snapshot"))

        issue_types = {issue.issue_type for issue in adapter.issues}
        self.assertIn("parse_failed", issue_types)
        self.assertIn("unparsed_module", issue_types)
        self.assertIn("unparsed_literal", issue_types)
        self.assertIn("missing_entity_family", issue_types)
        self.assertGreaterEqual(adapter.parse_stats["parse_failures"], 1)
        modules = [r for r in records if r.entity_kind == "module"]
        self.assertTrue(modules, "unclassified version-wrapped payloads must be preserved as records")
        self.assertEqual(modules[0].parse_status, PARSE_UNPARSED)
        self.assertEqual(modules[0].raw.get("container"), "dict")
        self.assertEqual(modules[0].raw.get("version"), "1.5.2")
        # the recognised skill tree payload in the same chunk is still parsed
        trees = [r for r in records if r.entity_kind == "skill_tree"]
        self.assertTrue(any(r.raw.get("treeId") == "t1" for r in trees))


if __name__ == "__main__":
    unittest.main()
