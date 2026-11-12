"""Normalization framework: source records -> canonical entities.

Layer contract (workguide 6.4):
  * adapters only describe one source; this package performs identity,
    cross-source mapping, unit conversion and typed column selection;
  * every field derived from a source record gets a ``field_evidence`` row;
  * unknown values stay NULL and get an ``absent``/``unparsed``/``conflict``
    evidence row instead of a fabricated 0;
  * source-specific mapping lives in ``le_data/normalize/mapping_<source>.py``
    so the framework stays source-agnostic.

Canonical id scheme: ``<kind>:<source>:<scope>:<source_id>``, e.g.
``skill:letools:ability:wo42``.  The scheme is stable across runs because it
only uses source-provided identifiers and scopes, never names or row order.
Cross-source merges are explicit in ``data/mappings/crosswalk.json``; without
an entry there, two source records stay two distinct entities.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..paths import MAPPINGS_DIR
from ..records import SourceRecord
from ..store import insert_row, now_utc

CROSSWALK_FILE = "crosswalk.json"


@dataclass
class EntityPlan:
    source_record: SourceRecord
    kind: str
    entity_id: str
    canonical_name: str | None = None
    record_status: str = "active"
    extensions: dict[str, Any] = field(default_factory=dict)
    names: list[dict[str, Any]] = field(default_factory=list)
    typed_rows: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    relationships: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)


MappingFn = Callable[[SourceRecord], EntityPlan]


def _clean_part(value: str) -> str:
    text = str(value).strip()
    return text.replace(":", "_").replace("|", "_") or "_"


def make_entity_id(kind: str, source: str, scope: str, source_id: str) -> str:
    return ":".join(
        [_clean_part(kind), _clean_part(source), _clean_part(scope), _clean_part(source_id)]
    )


def issue_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"issue-{digest}"


def relation_id(source: str, source_id: str, rel_type: str, target: str) -> str:
    digest = hashlib.sha256(
        "\x1f".join([source, source_id, rel_type, target]).encode("utf-8")
    ).hexdigest()[:24]
    return f"rel-{digest}"


class Crosswalk:
    """Explicit entity merge registry.

    File format::

        {"revision": 1,
         "merges": [
           {"canonical_entity_id": "skill:letools:ability:wo42",
            "members": [{"source": "tunklab", "kind": "ability",
                         "scope": "...", "source_id": "..."}],
            "method": "confirmed_shared_game_id", "evidence": "..."}]}
    """

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = data or {"revision": 1, "merges": []}
        self._index: dict[tuple[str, str, str, str], str] = {}
        for merge in self.data.get("merges", []):
            canonical = merge["canonical_entity_id"]
            for member in merge.get("members", []):
                key = (
                    member["source"],
                    member["kind"],
                    member["scope"],
                    member["source_id"],
                )
                self._index[key] = canonical

    @classmethod
    def load(cls, path: Path | None = None) -> "Crosswalk":
        target = Path(path) if path else MAPPINGS_DIR / CROSSWALK_FILE
        if not target.exists():
            return cls()
        return cls(json.loads(target.read_text(encoding="utf-8")))

    @property
    def revision(self) -> str:
        return str(self.data.get("revision", 1))

    def canonical_for(
        self, source: str, kind: str, scope: str, source_id: str
    ) -> str | None:
        return self._index.get((source, kind, scope, source_id))


def load_mapping_module(source: str):
    """Import ``le_data.normalize.mapping_<source>`` lazily."""
    import importlib

    try:
        return importlib.import_module(f"le_data.normalize.mapping_{source}")
    except ModuleNotFoundError:
        return None


class Normalizer:
    def __init__(
        self,
        conn,
        source: str,
        snapshot_id: str,
        *,
        crosswalk: Crosswalk | None = None,
        logger=None,
    ) -> None:
        self.conn = conn
        self.source = source
        self.snapshot_id = snapshot_id
        self.crosswalk = crosswalk or Crosswalk()
        self.logger = logger or (lambda msg: None)
        module = load_mapping_module(source)
        if module is None:
            raise RuntimeError(
                f"no mapping module for source {source!r} "
                f"(expected le_data/normalize/mapping_{source}.py)"
            )
        self.mapping_module = module
        self.counts: dict[str, int] = {}
        self.unmapped_kinds: dict[str, int] = {}
        self.skipped_kinds: dict[str, int] = {}

    # -- main entry ---------------------------------------------------------
    def normalize_records(self, records: Iterable[SourceRecord]) -> dict[str, Any]:
        materialized = list(records)
        translations = self._load_translations(materialized)
        context = {
            "conn": self.conn,
            "source": self.source,
            "source_snapshot_id": self.snapshot_id,
            "translations": translations,
            "records": materialized,
            "crosswalk": self.crosswalk,
        }
        configure = getattr(self.mapping_module, "configure", None)
        if configure is not None:
            configure(context)
        for record in materialized:
            if record.entity_kind == "translation":
                self.counts["translation"] = self.counts.get("translation", 0) + 1
                continue
            if record.entity_kind in getattr(self.mapping_module, "SKIP_KINDS", set()):
                self.skipped_kinds[record.entity_kind] = (
                    self.skipped_kinds.get(record.entity_kind, 0) + 1
                )
                continue
            plan = self.map_record(record)
            if plan is None:
                self.unmapped_kinds[record.entity_kind] = (
                    self.unmapped_kinds.get(record.entity_kind, 0) + 1
                )
                continue
            self.persist(plan)
        post_process = getattr(self.mapping_module, "post_process", None)
        if post_process is not None:
            post_process(self.conn, context)
        return {
            "source": self.source,
            "source_snapshot_id": self.snapshot_id,
            "entity_counts": self.counts,
            "unmapped_kinds": self.unmapped_kinds,
            "skipped_kinds": self.skipped_kinds,
            "translations": len(translations),
        }

    def _load_translations(self, records: Iterable[SourceRecord]) -> dict[str, str]:
        """Store translation records and build a key -> value lookup."""
        lookup: dict[str, str] = {}
        for record in records:
            if record.entity_kind != "translation":
                continue
            raw = record.raw
            key = raw.get("key", record.source_id)
            value = raw.get("value")
            if not isinstance(value, str) or not key:
                continue
            namespace = raw.get("namespace") or record.source_scope or "default"
            language = raw.get("language") or "en"
            lookup[str(key)] = value
            insert_row(
                self.conn,
                "translations",
                {
                    "translation_id": hashlib.sha256(
                        "\x1f".join(
                            [record.source_snapshot_id, namespace, language, str(key)]
                        ).encode("utf-8")
                    ).hexdigest()[:32],
                    "source": self.source,
                    "source_snapshot_id": record.source_snapshot_id,
                    "namespace": namespace,
                    "language": language,
                    "key": str(key),
                    "value": value,
                },
                or_ignore=True,
            )
        return lookup

    def map_record(self, record: SourceRecord) -> EntityPlan | None:
        skip_kinds = getattr(self.mapping_module, "SKIP_KINDS", set())
        if record.entity_kind in skip_kinds:
            self.skipped_kinds[record.entity_kind] = (
                self.skipped_kinds.get(record.entity_kind, 0) + 1
            )
            return None
        mapper: MappingFn | None = getattr(
            self.mapping_module, f"map_{record.entity_kind}", None
        )
        if mapper is not None:
            return mapper(record)
        generic = getattr(self.mapping_module, "map_generic", None)
        if generic is not None:
            return generic(record)
        return self._generic_plan(record)

    def _generic_plan(self, record: SourceRecord) -> EntityPlan:
        """Fallback: preserve an unknown source entity instead of dropping it."""
        plan = EntityPlan(
            source_record=record,
            kind=record.entity_kind,
            entity_id=make_entity_id(
                record.entity_kind, self.source, record.source_scope, record.source_id
            ),
            record_status="provisional",
            extensions={"raw": record.raw},
        )
        plan.issues.append(
            {
                "issue_type": "unmapped_entity_kind",
                "severity": "info",
                "message": f"no typed mapping for kind {record.entity_kind!r}; stored generically",
                "locator": record.locator,
            }
        )
        return plan

    # -- persistence --------------------------------------------------------
    def persist(self, plan: EntityPlan) -> None:
        record = plan.source_record
        # Crosswalk members are keyed by the *source* entity kind/scope/id
        # (e.g. Tunklab "skill_tree_node"), while plan.kind is the canonical
        # kind after mapping (e.g. "node"), so the lookup must use the record.
        canonical = self.crosswalk.canonical_for(
            self.source, record.entity_kind, record.source_scope, record.source_id
        )
        merged_entity_id = canonical or plan.entity_id
        if canonical and canonical != plan.entity_id:
            plan.record_status = "merged"

        # When this record is a secondary member of a crosswalk merge the
        # typed tables already come from the primary record; only the id map,
        # names and evidence are added here.
        is_secondary = bool(canonical and canonical != plan.entity_id)

        existing = self.conn.execute(
            "SELECT entity_id FROM entities WHERE entity_id = ?", (merged_entity_id,)
        ).fetchone()
        if existing is None:
            insert_row(
                self.conn,
                "entities",
                {
                    "entity_id": merged_entity_id,
                    "kind": plan.kind,
                    "canonical_name": plan.canonical_name,
                    "record_status": plan.record_status,
                    "primary_source": self.source,
                    "extensions": json.dumps(plan.extensions, ensure_ascii=False)
                    if plan.extensions
                    else None,
                    "created_at": now_utc(),
                },
            )
        elif plan.canonical_name and not is_secondary:
            self.conn.execute(
                "UPDATE entities SET canonical_name = COALESCE(canonical_name, ?) "
                "WHERE entity_id = ?",
                (plan.canonical_name, merged_entity_id),
            )

        for name in plan.names:
            insert_row(
                self.conn,
                "entity_names",
                {
                    "entity_id": merged_entity_id,
                    "language": name.get("language", "en"),
                    "name": name["name"],
                    "is_alias": name.get("is_alias", False),
                    "source": name.get("source", self.source),
                    "name_key": name.get("name_key"),
                },
                or_ignore=True,
            )

        insert_row(
            self.conn,
            "source_id_map",
            {
                "mapping_id": hashlib.sha256(
                    "\x1f".join(
                        [record.source_snapshot_id, record.entity_kind,
                         record.source_scope, record.source_id]
                    ).encode("utf-8")
                ).hexdigest()[:32],
                "source_snapshot_id": record.source_snapshot_id,
                "source": self.source,
                "kind": record.entity_kind,
                "source_scope": record.source_scope,
                "source_id": record.source_id,
                "entity_id": merged_entity_id,
                "mapping_method": "crosswalk" if canonical else "source_authoritative",
                "evidence": record.locator,
                "created_at": now_utc(),
            },
            or_ignore=True,
        )

        if not is_secondary:
            for table, row in plan.typed_rows:
                insert_row(self.conn, table, row)
            for relationship in plan.relationships:
                insert_row(self.conn, "entity_relationships", relationship, or_ignore=True)

        for evidence in plan.evidence:
            self._insert_evidence(evidence, merged_entity_id, record)
        for issue in plan.issues:
            self._insert_issue(issue, record, merged_entity_id)

        self.counts[plan.kind] = self.counts.get(plan.kind, 0) + 1

    def _insert_evidence(
        self, evidence: dict[str, Any], entity_id: str, record: SourceRecord
    ) -> None:
        row = {
            "entity_id": entity_id,
            "owner_type": evidence.get("owner_type", "entity"),
            "owner_id": evidence.get("owner_id", entity_id),
            "field_path": evidence["field_path"],
            "source_record_id": record.record_id,
            "source": self.source,
            "source_snapshot_id": record.source_snapshot_id,
            "raw_value": _jsonish(evidence.get("raw_value")),
            "normalized_value": _jsonish(evidence.get("normalized_value")),
            "unit": evidence.get("unit"),
            "extraction_method": evidence.get("extraction_method", "parsed"),
            "evidence_status": evidence.get("evidence_status", "known"),
            "locator": evidence.get("locator", record.locator),
            "notes": evidence.get("notes"),
        }
        insert_row(self.conn, "field_evidence", row)

    def _insert_issue(
        self, issue: dict[str, Any], record: SourceRecord, entity_id: str
    ) -> None:
        severity = issue.get("severity", "warning")
        iid = issue_id(
            self.source,
            record.source_snapshot_id,
            record.entity_kind,
            issue.get("issue_type", "issue"),
            entity_id,
            issue.get("field_path", ""),
            issue.get("message", ""),
        )
        insert_row(
            self.conn,
            "data_issues",
            {
                "issue_id": iid,
                "issue_type": issue.get("issue_type", "unknown"),
                "severity": severity,
                "source": self.source,
                "source_snapshot_id": record.source_snapshot_id,
                "entity_id": entity_id,
                "field_path": issue.get("field_path"),
                "message": issue.get("message"),
                "expected": issue.get("expected"),
                "actual": issue.get("actual"),
                "locator": issue.get("locator", record.locator),
                "status": "open",
                "created_at": now_utc(),
            },
            or_ignore=True,
        )

    def persist_relations_only(self, plan: EntityPlan, entity_id: str) -> None:
        for relationship in plan.relationships:
            insert_row(self.conn, "entity_relationships", relationship, or_ignore=True)


def _jsonish(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list, bool, int, float)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)
