"""Static extraction of the tunklab data literals from frozen client chunks.

The downloaded JavaScript is parsed as text.  This module locates the versioned
data object literals (``{version:"1.5.2",data:...}``), classifies them by shape
and records an exact source locator (chunk sha256 + character/byte offsets +
snippet hash) for every dataset.  No JavaScript is executed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import SCHEMA_VERSION
from .enums import ENUM_TABLES, verify_anchors
from .js_literal import JSLiteralError, Node, parse_literal_at, snippet_hash


class ExtractionError(RuntimeError):
    """Raised when the frozen snapshot does not contain the expected datasets."""


@dataclass
class SourceFile:
    file_name: str  # path relative to the snapshot directory
    sha256: str
    size_bytes: int
    role: str
    char_count: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_name": self.file_name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "role": self.role,
        }


@dataclass
class Dataset:
    kind: str
    version: str
    chunk_sha256: str
    chunk_file: str
    char_start: int
    char_end: int
    byte_start: int
    byte_end: int
    snippet_sha256: str
    value: Any

    def locator(self) -> str:
        return (
            f"chunk_sha256={self.chunk_sha256};file={self.chunk_file};"
            f"chars={self.char_start}-{self.char_end};bytes={self.byte_start}-{self.byte_end};"
            f"snippet_sha256={self.snippet_sha256}"
        )

    def as_dict(self, include_value: bool = False) -> dict[str, Any]:
        payload = {
            "kind": self.kind,
            "version": self.version,
            "chunk_file": self.chunk_file,
            "chunk_sha256": self.chunk_sha256,
            "locator": self.locator(),
            "char_start": self.char_start,
            "char_end": self.char_end,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
            "snippet_sha256": self.snippet_sha256,
        }
        if include_value:
            payload["value"] = self.value
        return payload


@dataclass
class Extraction:
    source_dir: str
    files: list[SourceFile] = field(default_factory=list)
    datasets: dict[str, Dataset] = field(default_factory=dict)
    enum_anchors: dict[str, bool] = field(default_factory=dict)
    stat_property_enum: dict[int, str] = field(default_factory=dict)
    stat_property_enum_locator: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def data_chunk(self) -> SourceFile:
        return next(f for f in self.files if f.role == "data-chunk")

    @property
    def page_code_chunk(self) -> SourceFile | None:
        for f in self.files:
            if f.role == "page-code-chunk":
                return f
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "source_dir": self.source_dir,
            "files": [f.as_dict() for f in self.files],
            "datasets": {k: d.as_dict() for k, d in sorted(self.datasets.items())},
            "enum_anchors": dict(sorted(self.enum_anchors.items())),
            "stat_property_enum": {str(k): v for k, v in sorted(self.stat_property_enum.items())},
            "stat_property_enum_locator": self.stat_property_enum_locator,
            "warnings": list(self.warnings),
        }


# --------------------------------------------------------------------------
# Literal discovery
# --------------------------------------------------------------------------

_VERSION_PREFIXES = ('{version:"', "{version:'")


def iter_versioned_literals(text: str) -> Iterator[tuple[int, Node]]:
    """Yield every top-level object literal that starts with ``{version:"...``."""
    starts: list[int] = []
    for prefix in _VERSION_PREFIXES:
        index = text.find(prefix)
        while index >= 0:
            starts.append(index)
            index = text.find(prefix, index + len(prefix))
    for start in sorted(set(starts)):
        try:
            node = parse_literal_at(text, start)
        except JSLiteralError as exc:
            yield start, _FailedLiteral(exc)  # type: ignore[arg-type]
            continue
        yield start, node


class _FailedLiteral:
    def __init__(self, error: JSLiteralError):
        self.error = error
        self.value = None


def _looks_like(sample: Any, required: tuple[str, ...]) -> bool:
    return isinstance(sample, dict) and all(key in sample for key in required)


def classify_dataset(node: Node) -> str | None:
    """Return the dataset kind for one parsed versioned literal, or ``None``."""
    if not isinstance(node.value, dict):
        return None
    data = node.value.get("data")
    if isinstance(data, dict):
        if "equippableItems" in data:
            return "equipment"
        return None
    if not isinstance(data, list) or len(data) < 4:
        return None
    sample = data[:4]
    if all(_looks_like(item, ("affixId", "slug", "tiers", "canRollOn")) for item in sample):
        return "affixes"
    if all(_looks_like(item, ("property", "propertyName", "roundingForAdded")) for item in sample):
        return "player_properties"
    if all(_looks_like(item, ("abilityID", "properties")) for item in sample):
        return "ability_properties"
    if all(_looks_like(item, ("roundingForAdded", "propertyName")) and "property" not in item for item in sample):
        return "player_stat_properties"
    return None


def find_stat_property_enum(text: str) -> tuple[dict[int, str], str]:
    """Recover the stat-property enum (id -> PascalCase name) from the chunk.

    The enum is written as compact assignment expressions such as
    ``l[l.FireResistance=13]="FireResistance"``.  We locate the known anchor and
    scan to that enum's module export marker, then parse the assignments with a
    regular expression (static text processing only).
    """
    import re

    anchor = '[l.Damage=0]="Damage"'
    start = text.find(anchor)
    if start < 0:
        return {}, ""
    end_marker = "],730209)"
    end = text.find(end_marker, start)
    if end < 0:
        return {}, ""
    end += len(end_marker)
    pattern = re.compile(r'\[\w\.([A-Za-z0-9_]+)=(\d+)\]="([^"]*)"')
    mapping: dict[int, str] = {}
    for _enum_name, numeric, display in pattern.findall(text[start:end]):
        mapping[int(numeric)] = display
    locator = (
        f"chars={start}-{end};snippet_sha256="
        + hashlib.sha256(text[start:end].encode("utf-8")).hexdigest()
    )
    return mapping, locator


def _byte_offset(text: str, char_index: int) -> int:
    return len(text[:char_index].encode("utf-8"))


def _sha256_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
            size += len(block)
    return hasher.hexdigest(), size


# --------------------------------------------------------------------------
# Snapshot extraction
# --------------------------------------------------------------------------

REQUIRED_DATASETS = (
    "affixes",
    "equipment",
    "player_properties",
    "player_stat_properties",
    "ability_properties",
)


def extract_snapshot(chunks_dir: Path | str, page_code_markers: tuple[str, ...] = ("Scaled Values", "standardAffixEffectModifier")) -> Extraction:
    """Extract every dataset from the chunk directory of a frozen snapshot."""
    chunks_path = Path(chunks_dir)
    if not chunks_path.is_dir():
        raise ExtractionError(f"chunk directory not found: {chunks_path}")
    js_files = sorted(chunks_path.glob("*.js"))
    if not js_files:
        raise ExtractionError(f"no .js files in {chunks_path}")

    extraction = Extraction(source_dir=str(chunks_path.parent))
    texts: dict[str, str] = {}
    hashes: dict[str, tuple[str, int]] = {}
    for path in js_files:
        digest, size = _sha256_file(path)
        hashes[path.name] = (digest, size)
        texts[path.name] = path.read_text(encoding="utf-8-sig", errors="replace")

    # Locate the data chunk and the page-rendering chunk (formula evidence).
    data_chunk_name: str | None = None
    page_code_name: str | None = None
    found: dict[str, Dataset] = {}
    failures: list[str] = []
    for name, text in sorted(texts.items()):
        is_data = False
        for start, node in iter_versioned_literals(text):
            if isinstance(node, _FailedLiteral):
                failures.append(f"{name}@{start}: {node.error}")
                continue
            kind = classify_dataset(node)
            if kind is None:
                continue
            if kind in found:
                continue
            digest, _size = hashes[name]
            found[kind] = Dataset(
                kind=kind,
                version=str(node.value.get("version")),
                chunk_sha256=digest,
                chunk_file=f"chunks/{name}",
                char_start=start,
                char_end=node.end,
                byte_start=_byte_offset(text, start),
                byte_end=_byte_offset(text, node.end),
                snippet_sha256=snippet_hash(text, start, node.end),
                value=node.value["data"],
            )
            if kind in ("affixes", "equipment"):
                is_data = True
        if is_data and data_chunk_name is None:
            data_chunk_name = name
        if page_code_name is None and all(marker in text for marker in page_code_markers):
            page_code_name = name

    if data_chunk_name is None:
        raise ExtractionError("could not locate the tunklab data chunk (no chunk contained affix/equipment data)")
    if failures:
        extraction.warnings.extend(failures[:20])

    missing = [kind for kind in REQUIRED_DATASETS if kind not in found]
    if missing:
        raise ExtractionError(
            "frozen snapshot is missing required datasets: "
            + ", ".join(missing)
            + "; found: "
            + ", ".join(sorted(found))
        )

    for name, (digest, size) in sorted(hashes.items()):
        if name == data_chunk_name:
            role = "data-chunk"
        elif name == page_code_name:
            role = "page-code-chunk"
        else:
            role = "page-chunk"
        extraction.files.append(
            SourceFile(
                file_name=f"chunks/{name}",
                sha256=digest,
                size_bytes=size,
                role=role,
                char_count=len(texts[name]),
            )
        )
    extraction.datasets = dict(sorted(found.items()))

    data_text = texts[data_chunk_name]
    extraction.enum_anchors = verify_anchors(data_text)
    missing_anchors = [name for name, present in extraction.enum_anchors.items() if not present]
    if missing_anchors:
        raise ExtractionError(
            "enum anchors were not found in the data chunk (the tunklab build changed): "
            + ", ".join(missing_anchors)
        )
    enum_mapping, enum_locator = find_stat_property_enum(data_text)
    if not enum_mapping:
        raise ExtractionError("stat-property enum could not be located in the data chunk")
    extraction.stat_property_enum = enum_mapping
    extraction.stat_property_enum_locator = enum_locator
    extraction.warnings.extend(
        [
            f"enum {table.name}: anchor verified ({table.anchor!r})"
            for table in ENUM_TABLES
        ]
    )
    return extraction


def dump_extraction(extraction: Extraction, path: Path | str) -> None:
    Path(path).write_text(
        json.dumps(extraction.as_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
