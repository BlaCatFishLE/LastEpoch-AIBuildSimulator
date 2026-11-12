"""Source-record model shared by every adapter.

A source record is one entity exactly as the source expresses it, before any
cross-source merge (phase-1 workguide section 6.3).  Records are persisted as
JSONL in ``data/staging/<run-id>/source_records/<source>.jsonl`` so that parse
can run offline and normalization never needs the original JS/HTML again.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

PARSE_OK = "parsed"
PARSE_UNPARSED = "unparsed"      # raw content exists, parser does not support it yet
PARSE_FAILED = "failed"          # parser attempted and failed
PARSE_SKIPPED = "skipped"        # explicitly out of scope / not applicable


@dataclass
class SourceIssue:
    issue_type: str
    severity: str
    source: str
    source_snapshot_id: str
    resource_name: str = ""
    entity_kind: str = ""
    source_id: str = ""
    field_path: str = ""
    message: str = ""
    expected: str = ""
    actual: str = ""
    evidence: str = ""
    status: str = "open"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SourceRecord:
    source: str
    source_snapshot_id: str
    resource_name: str
    entity_kind: str
    source_scope: str
    source_id: str
    locator: str
    raw: dict[str, Any]
    parse_status: str = PARSE_OK
    notes: list[str] = field(default_factory=list)

    @property
    def record_id(self) -> str:
        key = "\x1f".join(
            [self.source, self.source_snapshot_id, self.resource_name,
             self.entity_kind, self.source_scope, self.source_id]
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["record_id"] = self.record_id
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SourceRecord":
        payload = {k: v for k, v in data.items() if k != "record_id"}
        return cls(**payload)


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
            count += 1
    return count


def read_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield json.loads(line)
