"""Validation checks for a normalized dataset.

A validation run produces machine-readable results; ``fail`` blocks publish,
``warn`` is recorded but does not block (data can be legitimately partial).
Structural checks (integrity/foreign keys) are hard requirements.

The gate layering follows the phase-1 review (P1-3/P1-4):

* structural integrity (schema, foreign keys, references);
* acquisition completeness (required domains, executable ``field_requirements``,
  ``source_index`` records that must be mapped or issue-tracked);
* semantic normalization completeness (effect->stat links, buff relationships,
  buff source configs that must not be silently dropped);
* version compatibility (target / claimed / page / module versions, with an
  explicit approval file as the only bypass).

A check is only as strong as its SQL: a failing query (for example because the
schema is older than the semantic post-pass) is reported as ``fail``, never
silently skipped.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import MAPPINGS_DIR, PROJECT_ROOT
from ..store import integrity_report

SCHEMA_VERSION = "1"

#: A dotted game version such as ``1.5.2``.  Page/module evidence is scanned for
#: these tokens; version directories such as ``version152`` are deliberately
#: not treated as evidence because they encode the version ambiguously.
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+)\b")

#: Semantic normalization gates that are always executed, even when a caller
#: passes a scope file without ``semantic_requirements``.  The review item must
#: not be switchable off by editing one config file.
DEFAULT_SEMANTIC_REQUIREMENTS: list[dict[str, Any]] = [
    {
        "id": "stat_links_present",
        "label": "effect_values are linked to stat definitions (P1-2 result)",
        "sql": "SELECT COUNT(*) FROM effect_values WHERE stat_link_status = 'linked'",
        "minimum": 1,
        "severity": "fail",
    },
    {
        "id": "buff_relationships_present",
        "label": "Buff-granting/modifying relationships are normalized (P1-1 result)",
        "sql": "SELECT COUNT(*) FROM entity_relationships WHERE relationship_type LIKE '%buff%'",
        "minimum": 1,
        "severity": "fail",
    },
]

#: Key names that carry buff configuration inside a source record payload.
DEFAULT_BUFF_CONFIG_KEYS = ("buffs", "buffAbilities", "masteryBuffs")

VALID_EVIDENCE_STATUS = {
    "known",
    "absent",
    "not_applicable",
    "unparsed",
    "conflict",
    "inferred",
}
VALID_PARSE_STATUS = {"parsed", "unparsed", "failed", "skipped"}

#: Machine-readable gate stages (phase-1 review P1-3 suggestion: separate
#: structural integrity, acquisition completeness, semantic normalization and
#: phase-2 import readiness instead of one flat pass count).
STAGE_STRUCTURAL = "structural_integrity"
STAGE_ACQUISITION = "acquisition_completeness"
STAGE_SEMANTIC = "semantic_normalization"
STAGE_VERSION = "version_compatibility"
STAGE_PHASE2 = "phase2_import_readiness"
STAGES = (STAGE_STRUCTURAL, STAGE_ACQUISITION, STAGE_SEMANTIC, STAGE_VERSION, STAGE_PHASE2)

_STRUCTURAL_PREFIXES = (
    "schema_",
    "dataset_schema_version",
    "dataset_meta_",
    "source_snapshots_",
    "source_records_",
    "entities_",
    "crosswalk_",
    "relationships_",
    "nodes_",
    "affix_",
    "field_evidence",
    "sqlite_integrity",
    "foreign_keys",
)
_ACQUISITION_PREFIXES = ("domain_", "field_", "source_index_", "threshold_")
_SEMANTIC_PREFIXES = ("semantic_", "buff_source_configs")
_VERSION_PREFIXES = ("version_",)


def check_stage(check_id: str) -> str:
    """Classify a check id into one of the gate stages (P1-3 separation)."""
    for prefix in _STRUCTURAL_PREFIXES:
        if check_id.startswith(prefix):
            return STAGE_STRUCTURAL
    for prefix in _ACQUISITION_PREFIXES:
        if check_id.startswith(prefix):
            return STAGE_ACQUISITION
    for prefix in _SEMANTIC_PREFIXES:
        if check_id.startswith(prefix):
            return STAGE_SEMANTIC
    for prefix in _VERSION_PREFIXES:
        if check_id.startswith(prefix):
            return STAGE_VERSION
    return STAGE_STRUCTURAL


@dataclass
class CheckResult:
    check_id: str
    status: str  # pass | warn | fail
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "message": self.message,
            "detail": self.detail,
        }


class Validator:
    def __init__(
        self,
        conn,
        coverage_scope: dict[str, Any] | None = None,
        *,
        version_compat_path: Path | str | None = None,
    ) -> None:
        self.conn = conn
        self.scope = coverage_scope or {}
        self.results: list[CheckResult] = []
        self.version_compat_path = (
            Path(version_compat_path) if version_compat_path is not None else None
        )

    def add(self, check_id: str, status: str, message: str, **detail: Any) -> None:
        self.results.append(CheckResult(check_id, status, message, detail))

    # -- checks -------------------------------------------------------------
    def run_all(self) -> dict[str, Any]:
        self.check_schema()
        self.check_integrity()
        self.check_dataset_meta()
        self.check_source_snapshots()
        self.check_source_records()
        self.check_entities()
        self.check_crosswalk()
        self.check_domains()
        self.check_source_index()
        self.check_field_evidence()
        self.check_field_requirements()
        self.check_relationships()
        self.check_node_values()
        self.check_affix_tiers()
        self.check_coverage_thresholds()
        self.check_semantic_requirements()
        self.check_buff_source_configs()
        self.check_version_gate()
        return self.summary()

    # -- small helpers ------------------------------------------------------
    def _scalar(self, sql: str, params: tuple | list = ()) -> Any:
        row = self.conn.execute(sql, params).fetchone()
        if row is None:
            return None
        return row[0]

    def _table_exists(self, table: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    def _column_exists(self, table: str, column: str) -> bool:
        try:
            return any(
                row["name"] == column for row in self.conn.execute(f"PRAGMA table_info({table})")
            )
        except sqlite3.Error:
            return False

    def _open_issue_for_types(self, issue_types: list[str]) -> dict[str, Any] | None:
        """Return the first open/wontfix issue with one of ``issue_types``."""
        types = [str(item) for item in issue_types if item]
        if not types:
            return None
        placeholders = ",".join("?" for _ in types)
        try:
            row = self.conn.execute(
                "SELECT issue_id, issue_type, severity, status, entity_id, message "
                f"FROM data_issues WHERE status IN ('open', 'wontfix') "
                f"AND issue_type IN ({placeholders}) ORDER BY issue_id LIMIT 1",
                types,
            ).fetchone()
        except sqlite3.Error:
            return None
        return dict(row) if row is not None else None

    # -- structural checks --------------------------------------------------
    def check_schema(self) -> None:
        row = self.conn.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        version = row[0] if row else None
        meta = self.conn.execute(
            "SELECT schema_version FROM dataset_meta LIMIT 1"
        ).fetchone()
        recorded = meta["schema_version"] if meta else None
        self.add(
            "schema_migrations",
            "pass" if version and int(version) >= 1 else "fail",
            f"latest applied migration = {version}",
        )
        self.add(
            "dataset_schema_version",
            "pass" if recorded == SCHEMA_VERSION else "fail",
            f"dataset schema_version = {recorded}",
            expected=SCHEMA_VERSION,
            actual=recorded,
        )

    def check_integrity(self) -> None:
        report = integrity_report(self.conn)
        self.add(
            "sqlite_integrity",
            "pass" if report["integrity_ok"] else "fail",
            "PRAGMA integrity_check",
            result=report["integrity_check"][:5],
        )
        self.add(
            "foreign_keys",
            "pass" if report["foreign_keys_ok"] else "fail",
            f"{len(report['foreign_key_violations'])} foreign key violations",
            sample=report["foreign_key_violations"][:10],
        )

    def check_dataset_meta(self) -> None:
        rows = list(self.conn.execute("SELECT * FROM dataset_meta"))
        if len(rows) != 1:
            self.add("dataset_meta_single", "fail", f"expected exactly 1 dataset_meta row, got {len(rows)}")
            return
        row = rows[0]
        self.add("dataset_meta_single", "pass", f"dataset_id={row['dataset_id']}")
        self.add(
            "dataset_meta_status",
            "pass" if row["status"] in {"staging", "valid", "partial", "published"} else "warn",
            f"dataset status = {row['status']}",
        )
        self.add(
            "dataset_meta_versions",
            "pass" if row["schema_version"] and row["normalizer_version"] else "fail",
            "schema/normalizer versions recorded",
            schema_version=row["schema_version"],
            normalizer_version=row["normalizer_version"],
            mapping_revision=row["mapping_revision"],
        )

    def check_source_snapshots(self) -> None:
        rows = list(
            self.conn.execute(
                "SELECT source, source_snapshot_id, status, claimed_version FROM source_snapshots"
            )
        )
        if not rows:
            self.add("source_snapshots_present", "fail", "no source snapshots recorded")
            return
        self.add("source_snapshots_present", "pass", f"{len(rows)} source snapshots")
        failed = [row for row in rows if row["status"] == "failed"]
        self.add(
            "source_snapshots_status",
            "warn" if failed else "pass",
            f"{len(failed)} source snapshot(s) failed",
            failed=[dict(row) for row in failed],
        )

    def check_source_records(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) FROM source_records").fetchone()[0]
        self.add("source_records_present", "pass" if total else "fail", f"{total} source records")
        rows = list(
            self.conn.execute(
                "SELECT parse_status, COUNT(*) AS n FROM source_records GROUP BY parse_status"
            )
        )
        statuses = {row["parse_status"]: row["n"] for row in rows}
        invalid = set(statuses) - VALID_PARSE_STATUS
        self.add(
            "source_records_parse_status",
            "fail" if invalid else "pass",
            f"parse_status distribution: {statuses}",
            invalid=sorted(invalid),
        )
        unparsed = sum(statuses.get(key, 0) for key in ("unparsed", "failed"))
        self.add(
            "source_records_unparsed",
            "warn" if unparsed else "pass",
            f"{unparsed} source record(s) not parsed",
        )

    def check_entities(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        self.add("entities_present", "pass" if total else "fail", f"{total} entities")
        unnamed = self.conn.execute(
            "SELECT COUNT(*) FROM entities e WHERE NOT EXISTS "
            "(SELECT 1 FROM entity_names n WHERE n.entity_id = e.entity_id)"
        ).fetchone()[0]
        self.add(
            "entities_named",
            "warn" if unnamed else "pass",
            f"{unnamed} entities without any name",
        )
        unmapped = self.conn.execute(
            "SELECT COUNT(*) FROM entities e WHERE NOT EXISTS "
            "(SELECT 1 FROM source_id_map m WHERE m.entity_id = e.entity_id)"
        ).fetchone()[0]
        self.add(
            "entities_source_mapped",
            "pass" if unmapped == 0 else "fail",
            f"{unmapped} entities without a source id mapping",
        )
        details = list(
            self.conn.execute(
                "SELECT kind, COUNT(*) AS n FROM entities GROUP BY kind ORDER BY kind"
            )
        )
        self.add(
            "entities_by_kind",
            "pass",
            "entity counts by kind",
            counts={row["kind"]: row["n"] for row in details},
        )

    def check_crosswalk(self) -> None:
        path = MAPPINGS_DIR / "crosswalk.json"
        if not path.exists():
            self.add("crosswalk_present", "warn", f"no crosswalk file at {path}")
            return
        data = json.loads(path.read_text(encoding="utf-8"))
        merges = data.get("merges", [])
        missing: list[str] = []
        for merge in merges:
            canonical = merge.get("canonical_entity_id")
            if not canonical:
                missing.append("<empty canonical id>")
                continue
            row = self.conn.execute(
                "SELECT 1 FROM entities WHERE entity_id = ?", (canonical,)
            ).fetchone()
            if row is None:
                missing.append(canonical)
        self.add(
            "crosswalk_targets_exist",
            "fail" if missing else "pass",
            f"{len(merges)} merges, {len(missing)} canonical ids missing from the dataset",
            revision=data.get("revision"),
            missing_sample=missing[:10],
        )

    # -- acquisition completeness ------------------------------------------
    def check_domains(self) -> None:
        required = self.scope.get("required_domains", [])
        for domain in required:
            domain_id = domain.get("id", "unknown")
            kinds = list(domain.get("entity_kinds") or [])
            minimum = int(domain.get("minimum_entities", 1) or 0)
            label = domain.get("label", domain_id)
            check_id = f"domain_{domain_id}"
            count_sql = domain.get("count_sql")
            if count_sql:
                try:
                    count = self.conn.execute(count_sql).fetchone()[0]
                except sqlite3.Error as exc:
                    self.add(
                        check_id,
                        "fail",
                        f"domain {label}: count_sql could not be executed: {exc}",
                        domain_id=domain_id,
                        count_sql=count_sql,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
            elif not kinds and domain_id == "relationships":
                count = self.conn.execute(
                    "SELECT COUNT(*) FROM entity_relationships"
                ).fetchone()[0]
            elif not kinds:
                self.add(
                    check_id,
                    "fail",
                    f"domain {label}: configured without entity_kinds or a counting rule",
                    domain_id=domain_id,
                )
                continue
            else:
                placeholders = ",".join("?" for _ in kinds)
                count = self.conn.execute(
                    f"SELECT COUNT(*) FROM entities WHERE kind IN ({placeholders})", kinds
                ).fetchone()[0]
            status = "pass" if count >= minimum else "fail"
            detail: dict[str, Any] = {
                "domain_id": domain_id,
                "count": count,
                "minimum": minimum,
                "entity_kinds": kinds,
                "priority": domain.get("priority"),
            }
            if count_sql:
                detail["count_sql"] = count_sql
                detail["count_note"] = domain.get("count_note")
            message = f"domain {label}: {count} entities (minimum {minimum})"
            if status == "fail":
                issue_types = list(domain.get("absence_issue_types") or [])
                issue = self._open_issue_for_types(issue_types)
                if issue is not None:
                    status = "warn"
                    detail["absence_issue"] = issue
                    message += (
                        "; absence is explicitly recorded by data_issue "
                        f"{issue['issue_id']} ({issue['issue_type']})"
                    )
                elif issue_types:
                    detail["absence_issue_types"] = issue_types
                    message += (
                        "; absence is NOT recorded by an allowed data_issues row "
                        f"({', '.join(issue_types)})"
                    )
            self.add(check_id, status, message, **detail)

    def check_source_index(self) -> None:
        """Raw source families must be mapped or explicitly issue-tracked.

        This is the guard behind the P1-3 review statement: an aggregate domain
        count (for example idols+blessings) must not hide a silently dropped
        structured source family (for example the weaver/season records).
        """
        entries = self.scope.get("source_index") or []
        if not entries:
            return
        for entry in entries:
            entry_id = entry.get("id", "unknown")
            check_id = f"source_index_{entry_id}"
            kinds = list(entry.get("raw_entity_kinds") or [])
            if not kinds:
                self.add(check_id, "fail", f"source_index rule {entry_id!r} has no raw_entity_kinds")
                continue
            statuses = list(entry.get("parse_statuses") or ["parsed"])
            kind_placeholders = ",".join("?" for _ in kinds)
            status_placeholders = ",".join("?" for _ in statuses)
            base_params = kinds + statuses
            raw_total = self.conn.execute(
                "SELECT COUNT(*) FROM source_records "
                f"WHERE entity_kind IN ({kind_placeholders}) "
                f"AND parse_status IN ({status_placeholders})",
                base_params,
            ).fetchone()[0]
            unmapped = self.conn.execute(
                "SELECT COUNT(*) FROM source_records sr "
                f"WHERE sr.entity_kind IN ({kind_placeholders}) "
                f"AND sr.parse_status IN ({status_placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM source_id_map m "
                "WHERE m.source_snapshot_id = sr.source_snapshot_id "
                "AND m.source = sr.source AND m.kind = sr.entity_kind "
                "AND m.source_scope = sr.source_scope AND m.source_id = sr.source_id)",
                base_params,
            ).fetchone()[0]
            minimum_raw = int(entry.get("minimum_raw_records", 0) or 0)
            minimum_ratio = float(entry.get("minimum_mapped_ratio", 1.0))
            mapped = raw_total - unmapped
            detail = {
                "domain": entry.get("domain"),
                "raw_entity_kinds": kinds,
                "raw_records": raw_total,
                "mapped_records": mapped,
                "unmapped_records": unmapped,
                "minimum_raw_records": minimum_raw,
                "minimum_mapped_ratio": minimum_ratio,
            }
            label = entry.get("label", entry_id)
            if raw_total == 0 and minimum_raw > 0:
                issue = self._open_issue_for_types(list(entry.get("absence_issue_types") or []))
                if issue is not None:
                    self.add(
                        check_id,
                        "warn",
                        f"source index {label}: no raw records found, but absence is "
                        f"explicitly recorded by data_issue {issue['issue_id']} ({issue['issue_type']})",
                        absence_issue=issue,
                        **detail,
                    )
                else:
                    self.add(
                        check_id,
                        "fail",
                        f"source index {label}: expected at least {minimum_raw} raw record(s) "
                        f"of kinds {kinds} but found none and no absence issue was recorded",
                        **detail,
                    )
                continue
            ratio = (mapped / raw_total) if raw_total else 1.0
            if unmapped and ratio + 1e-9 < minimum_ratio:
                issue = self._open_issue_for_types(list(entry.get("unmapped_issue_types") or []))
                if issue is not None:
                    self.add(
                        check_id,
                        "warn",
                        f"source index {label}: {unmapped}/{raw_total} raw records unmapped "
                        f"(ratio {ratio:.4f} < {minimum_ratio}); an issue "
                        f"{issue['issue_id']} ({issue['issue_type']}) is recorded but per-record "
                        f"attribution is not available",
                        issue=issue,
                        **detail,
                    )
                else:
                    self.add(
                        check_id,
                        "fail",
                        f"source index {label}: {unmapped}/{raw_total} parsed raw records "
                        f"of kinds {kinds} have no source_id_map entry (mapped ratio "
                        f"{ratio:.4f} < {minimum_ratio}); structured source data is silently dropped",
                        **detail,
                    )
            else:
                self.add(
                    check_id,
                    "pass",
                    f"source index {label}: {mapped}/{raw_total} raw records mapped "
                    f"(ratio {ratio:.4f}, minimum {minimum_ratio})",
                    **detail,
                )

    def check_field_requirements(self) -> None:
        entries = self.scope.get("field_requirements")
        if not entries:
            self.add(
                "field_requirements_config",
                "warn",
                "no field_requirements configured; field-level review items are not enforced",
            )
            return
        if isinstance(entries, dict):
            # Legacy schema_version 1 format: prose strings that were never executed.
            self.add(
                "field_requirements_config",
                "warn",
                "field_requirements uses the legacy prose mapping and cannot be executed; "
                "migrate it to executable SQL entries",
                entity_kinds=sorted(entries),
            )
            return
        self.add(
            "field_requirements_config",
            "pass",
            f"{len(entries)} executable field requirement(s) configured",
        )
        for requirement in entries:
            if not isinstance(requirement, dict):
                self.add("field_requirement_invalid", "fail", "field_requirements entry is not an object")
                continue
            requirement_id = requirement.get("id") or "unknown"
            check_id = f"field_{requirement_id}"
            label = requirement.get("label", requirement_id)
            severity = requirement.get("severity", "fail")
            if severity not in {"fail", "warn"}:
                severity = "fail"
            sql = requirement.get("sql")
            denominator_sql = requirement.get("denominator_sql")
            try:
                satisfied = int(self._scalar(sql) or 0) if sql else 0
                denominator = (
                    int(self._scalar(denominator_sql) or 0) if denominator_sql else None
                )
            except (sqlite3.Error, TypeError) as exc:
                self.add(
                    check_id,
                    "fail",
                    f"field requirement {label}: SQL could not be executed: {exc}",
                    sql=sql,
                    denominator_sql=denominator_sql,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if denominator is None:
                minimum = int(requirement.get("minimum", 1) or 1)
                ok = satisfied >= minimum
                detail = {"entity_kind": requirement.get("entity_kind"), "satisfied": satisfied, "minimum": minimum}
                message = f"field requirement {label}: {satisfied} row(s) (minimum {minimum})"
                ratio = None
            else:
                minimum = float(requirement.get("minimum_ratio", 1.0))
                ratio = (satisfied / denominator) if denominator else None
                ok = denominator > 0 and ratio is not None and ratio + 1e-9 >= minimum
                detail = {
                    "entity_kind": requirement.get("entity_kind"),
                    "satisfied": satisfied,
                    "applicable": denominator,
                    "ratio": round(ratio, 4) if ratio is not None else None,
                    "minimum_ratio": minimum,
                }
                if denominator == 0:
                    self.add(
                        check_id,
                        "warn",
                        f"field requirement {label}: no applicable rows (denominator=0); "
                        f"cannot evaluate, see the domain check",
                        **detail,
                    )
                    continue
                message = (
                    f"field requirement {label}: {satisfied}/{denominator} "
                    f"(ratio {ratio:.4f}, minimum {minimum})"
                )
            status = "pass" if ok else severity
            self.add(check_id, status, message, **detail)

    # -- semantic normalization completeness --------------------------------
    def check_semantic_requirements(self) -> None:
        configured: dict[str, dict[str, Any]] = {}
        for entry in self.scope.get("semantic_requirements") or []:
            if isinstance(entry, dict) and entry.get("id"):
                configured[str(entry["id"])] = entry
        merged: dict[str, dict[str, Any]] = {
            str(item["id"]): dict(item) for item in DEFAULT_SEMANTIC_REQUIREMENTS
        }
        merged.update(configured)
        for requirement_id, requirement in merged.items():
            check_id = f"semantic_{requirement_id}"
            label = requirement.get("label", requirement_id)
            severity = requirement.get("severity", "fail")
            if severity not in {"fail", "warn"}:
                severity = "fail"
            sql = requirement.get("sql")
            minimum = int(requirement.get("minimum", 1) or 1)
            try:
                count = int(self._scalar(sql) or 0) if sql else 0
            except (sqlite3.Error, TypeError) as exc:
                self.add(
                    check_id,
                    "fail",
                    f"semantic requirement {label}: SQL could not be executed (missing table "
                    f"or the semantic post-pass column is absent): {exc}",
                    sql=sql,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            status = "pass" if count >= minimum else severity
            self.add(
                check_id,
                status,
                f"semantic requirement {label}: {count} (minimum {minimum})",
                sql=sql,
                count=count,
                minimum=minimum,
            )

    def check_buff_source_configs(self) -> None:
        """Non-empty buff configs in the raw sources must not be silently lost.

        The check counts source_records whose raw payload carries a non-empty
        ``buffs``/``buffAbilities``/``masteryBuffs`` value, then requires that
        the dataset contains structured buff relationships or explicit buff
        issues as an accounting fallback.  Per-record attribution is not
        available in the schema yet, so a partial accounting is a warning and a
        fully silent loss (zero relationships and zero issues) is a failure --
        exactly the phase-1 review case where 209 non-empty configs coexisted
        with 0 buff relationships.
        """
        config = self.scope.get("buff_source_configs") or {}
        patterns = list(config.get("source_record_patterns") or [])
        config_keys = tuple(config.get("config_keys") or DEFAULT_BUFF_CONFIG_KEYS)
        relationship_patterns = list(config.get("relationship_patterns") or ["%buff%"])
        issue_patterns = list(config.get("issue_type_patterns") or ["%buff%"])
        severity = config.get("severity", "fail")
        if severity not in {"fail", "warn"}:
            severity = "fail"

        raw_records: list[tuple[str, str]] = []
        try:
            if patterns:
                where = " OR ".join("raw_payload LIKE ?" for _ in patterns)
                rows = self.conn.execute(
                    f"SELECT record_id, entity_kind, raw_payload FROM source_records WHERE {where}",
                    patterns,
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT record_id, entity_kind, raw_payload FROM source_records"
                ).fetchall()
        except sqlite3.Error as exc:
            self.add(
                "buff_source_configs",
                "fail",
                f"could not scan source_records for buff configs: {exc}",
                error=str(exc),
            )
            return
        for row in rows:
            try:
                payload = json.loads(row["raw_payload"])
            except (TypeError, ValueError):
                continue
            if self._has_non_empty_key(payload, config_keys):
                raw_records.append((row["record_id"], row["entity_kind"]))

        if not raw_records:
            self.add(
                "buff_source_configs",
                "pass",
                "no non-empty buff config found in source records",
                raw_config_records=0,
            )
            return

        def _count_like(table: str, column: str, like_patterns: list[str]) -> int:
            if not like_patterns:
                return 0
            where = " OR ".join(f"{column} LIKE ?" for _ in like_patterns)
            return int(
                self.conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {where}", like_patterns
                ).fetchone()[0]
            )

        try:
            relationship_count = _count_like(
                "entity_relationships", "relationship_type", relationship_patterns
            )
            issue_count = _count_like("data_issues", "issue_type", issue_patterns)
        except sqlite3.Error as exc:
            self.add(
                "buff_source_configs",
                "fail",
                f"could not count buff relationships/issues: {exc}",
                error=str(exc),
            )
            return

        accounted = relationship_count + issue_count
        detail = {
            "raw_config_records": len(raw_records),
            "raw_config_entity_kinds": self._kind_counts(raw_records),
            "buff_relationships": relationship_count,
            "buff_issues": issue_count,
            "accounted": accounted,
            "sample_records": [record_id for record_id, _ in raw_records[:10]],
        }
        if accounted == 0:
            self.add(
                "buff_source_configs",
                severity,
                f"{len(raw_records)} source record(s) contain non-empty buff configuration "
                f"but the dataset has 0 buff relationships and 0 buff issues: the config is "
                f"silently dropped",
                **detail,
            )
        elif accounted < len(raw_records):
            self.add(
                "buff_source_configs",
                "warn",
                f"{len(raw_records)} non-empty buff config record(s); {accounted} buff "
                f"relationship(s)+issue(s) account for them (partial per-record attribution, "
                f"schema has no record link)",
                **detail,
            )
        else:
            self.add(
                "buff_source_configs",
                "pass",
                f"{len(raw_records)} non-empty buff config record(s) accounted for by "
                f"{relationship_count} relationship(s) and {issue_count} issue(s)",
                **detail,
            )

    @staticmethod
    def _has_non_empty_key(node: Any, keys: tuple[str, ...]) -> bool:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in keys and value not in (None, "", [], {}):
                    return True
                if Validator._has_non_empty_key(value, keys):
                    return True
        elif isinstance(node, list):
            for item in node:
                if Validator._has_non_empty_key(item, keys):
                    return True
        return False

    @staticmethod
    def _kind_counts(records: list[tuple[str, str]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, kind in records:
            counts[kind] = counts.get(kind, 0) + 1
        return dict(sorted(counts.items()))

    # -- version compatibility gate (P1-4) ----------------------------------
    def _version_compat_file(self) -> Path:
        if self.version_compat_path is not None:
            return self.version_compat_path
        gate = self.scope.get("version_gate") or {}
        configured = gate.get("compat_file")
        if configured:
            path = Path(str(configured))
            return path if path.is_absolute() else PROJECT_ROOT / path
        return MAPPINGS_DIR / "version-compat.json"

    def _load_version_approvals(self) -> tuple[list[dict[str, Any]], str | None]:
        path = self._version_compat_file()
        try:
            if not path.exists():
                return [], None
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            return [], f"version compat file {path} could not be read: {type(exc).__name__}: {exc}"
        approvals = data.get("approvals") if isinstance(data, dict) else data
        if isinstance(data, dict) and approvals is None:
            approvals = data.get("approved_pairs")
        if not isinstance(approvals, list):
            return [], (
                f"version compat file {path} must contain an 'approvals' (or "
                f"'approved_pairs') list"
            )
        return [item for item in approvals if isinstance(item, dict)], None

    @staticmethod
    def _approval_field(approval: dict[str, Any], *names: str) -> str:
        for name in names:
            value = approval.get(name)
            if value not in (None, ""):
                return str(value).strip()
        return ""

    @staticmethod
    def _approved_version(
        source: str,
        target: str,
        observed: str,
        approvals: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        for approval in approvals:
            if (
                Validator._approval_field(approval, "decision").strip().lower()
                != "compatible"
            ):
                continue
            if Validator._approval_field(
                approval, "target_game_version", "target_version"
            ) != target:
                continue
            if Validator._approval_field(
                approval, "observed_version", "version", "game_version"
            ) != observed:
                continue
            approval_source = approval.get("source")
            if approval_source not in (None, "", source):
                continue
            if not str(approval.get("evidence") or "").strip():
                continue
            if not (
                str(approval.get("approved_by") or "").strip()
                or str(approval.get("approved_at") or "").strip()
            ):
                continue
            return approval
        return None

    @staticmethod
    def _versions_in_evidence(
        evidence: Any,
    ) -> tuple[set[str], dict[str, list[str]], list[str]]:
        versions: set[str] = set()
        module_versions: dict[str, list[str]] = {}
        unparsed_modules: list[str] = []

        def walk(node: Any, path: str) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "module_versions" and isinstance(value, dict):
                        for module_key in value:
                            matches = VERSION_RE.findall(str(module_key))
                            if matches:
                                module_versions[str(module_key)] = matches
                                versions.update(matches)
                            else:
                                unparsed_modules.append(f"module_versions[{module_key!r}]")
                        continue
                    walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for index, item in enumerate(node):
                    walk(item, f"{path}[{index}]")
            elif isinstance(node, str):
                versions.update(VERSION_RE.findall(node))

        walk(evidence, "")
        return versions, module_versions, unparsed_modules

    def check_version_gate(self) -> None:
        gate = self.scope.get("version_gate") or {}
        meta = self.conn.execute("SELECT * FROM dataset_meta LIMIT 1").fetchone()
        target = (meta["target_game_version"] or "").strip() if meta is not None else ""
        self.add(
            "version_target_present",
            "pass" if target else "fail",
            f"dataset_meta.target_game_version = {target or None!r}",
            target_game_version=target or None,
        )

        approvals, compat_error = self._load_version_approvals()
        self.add(
            "version_compat_config",
            "fail" if compat_error else "pass",
            compat_error
            or (
                f"version compat approvals loaded: {len(approvals)}"
                if approvals
                else "no approved version pairs configured (exact version match required)"
            ),
            compat_file=str(self._version_compat_file()),
            approvals=len(approvals),
        )

        # -- dataset_meta.compatibility_conclusion --------------------------
        conclusion_problems: list[str] = []
        raw_conclusion = meta["compatibility_conclusion"] if meta is not None else None
        conclusion: dict[str, Any] | None = None
        if gate.get("require_compatibility_conclusion", True):
            if not raw_conclusion or not str(raw_conclusion).strip():
                conclusion_problems.append(
                    "compatibility_conclusion is empty; run normalize with the version gate"
                )
            else:
                try:
                    parsed = json.loads(raw_conclusion)
                except ValueError:
                    parsed = None
                    conclusion_problems.append(
                        "compatibility_conclusion is not JSON; a plain 'compatible' text is "
                        "not a version decision"
                    )
                if isinstance(parsed, dict):
                    conclusion = parsed
                    if str(parsed.get("decision") or "").strip().lower() != "compatible":
                        conclusion_problems.append(
                            f"decision is {parsed.get('decision')!r}, expected 'compatible'"
                        )
                    if not target or str(parsed.get("target_game_version") or "").strip() != target:
                        conclusion_problems.append(
                            f"conclusion target_game_version {parsed.get('target_game_version')!r} "
                            f"does not match dataset target {target!r}"
                        )
                    sources = parsed.get("sources")
                    if not isinstance(sources, dict) or not sources:
                        conclusion_problems.append("conclusion.sources is missing")
                    else:
                        for source, version in sources.items():
                            version_text = str(version or "").strip()
                            if not version_text:
                                conclusion_problems.append(f"{source}: empty claimed version")
                            elif target and version_text != target:
                                approval = self._approved_version(
                                    str(source), target, version_text, approvals
                                )
                                if approval is None:
                                    conclusion_problems.append(
                                        f"{source}: claimed version {version_text} != target "
                                        f"{target} and no approved version pair exists"
                                    )
                    if not str(parsed.get("evidence") or "").strip():
                        conclusion_problems.append("conclusion.evidence is missing")
                elif parsed is not None:
                    conclusion_problems.append("compatibility_conclusion is not a JSON object")
        self.add(
            "version_compatibility_conclusion",
            "fail" if conclusion_problems else "pass",
            "compatibility conclusion accepted"
            if not conclusion_problems
            else "; ".join(conclusion_problems),
            decision=(conclusion or {}).get("decision"),
            sources=(conclusion or {}).get("sources"),
            problems=conclusion_problems,
        )

        # -- claimed versions + page/module evidence ------------------------
        rows = list(
            self.conn.execute(
                "SELECT source, source_snapshot_id, claimed_version, version_evidence "
                "FROM source_snapshots ORDER BY source"
            )
        )
        claimed_problems: list[str] = []
        evidence_problems: list[str] = []
        per_source: dict[str, Any] = {}
        if not rows:
            claimed_problems.append("no source snapshots recorded")
        for row in rows:
            source = str(row["source"])
            claimed = str(row["claimed_version"] or "").strip()
            entry: dict[str, Any] = {
                "source_snapshot_id": row["source_snapshot_id"],
                "claimed_version": claimed or None,
                "claimed_approved": False,
                "evidence_versions": [],
                "evidence_approved_versions": [],
                "module_versions": {},
                "evidence_ok": False,
            }
            if not claimed:
                claimed_problems.append(f"{source}: claimed_version is missing/unknown")
            elif target and claimed != target:
                approval = self._approved_version(source, target, claimed, approvals)
                if approval is None:
                    claimed_problems.append(
                        f"{source}: claimed_version {claimed} != target {target} and no "
                        f"approved version pair exists"
                    )
                else:
                    entry["claimed_approved"] = True
                    entry["claimed_approval"] = approval.get("evidence")

            raw_evidence = row["version_evidence"]
            if not raw_evidence or not str(raw_evidence).strip():
                if gate.get("require_version_evidence", True):
                    evidence_problems.append(f"{source}: version_evidence is missing")
                per_source[source] = entry
                continue
            try:
                evidence = json.loads(raw_evidence)
            except ValueError:
                evidence_problems.append(f"{source}: version_evidence is not valid JSON")
                per_source[source] = entry
                continue
            versions, module_versions, unparsed_modules = self._versions_in_evidence(evidence)
            entry["evidence_versions"] = sorted(versions)
            entry["module_versions"] = module_versions
            if not versions and gate.get("require_version_evidence", True):
                evidence_problems.append(
                    f"{source}: no page/module game version (x.y.z) found in version_evidence"
                )
            for version in sorted(versions):
                if target and version == target:
                    continue
                approval = self._approved_version(source, target, version, approvals)
                if approval is None:
                    evidence_problems.append(
                        f"{source}: version_evidence mentions {version} which conflicts with "
                        f"target {target} and has no approved version pair"
                    )
                else:
                    entry["evidence_approved_versions"].append(version)
            for module_key in unparsed_modules:
                evidence_problems.append(
                    f"{source}: {module_key} has no comparable x.y.z version"
                )
            entry["evidence_ok"] = not any(
                problem.startswith(f"{source}:") for problem in evidence_problems
            )
            per_source[source] = entry

        self.add(
            "version_claimed_versions",
            "fail" if claimed_problems else "pass",
            "all source_snapshots.claimed_version values are known and match the target"
            if not claimed_problems
            else "; ".join(claimed_problems),
            target_game_version=target or None,
            sources=per_source,
            problems=claimed_problems,
        )
        self.add(
            "version_evidence_versions",
            "fail" if evidence_problems else "pass",
            "page/module version evidence matches the target"
            if not evidence_problems
            else "; ".join(evidence_problems),
            sources=per_source,
            problems=evidence_problems,
        )

    # -- referential / value checks -----------------------------------------
    def check_field_evidence(self) -> None:
        total = self.conn.execute("SELECT COUNT(*) FROM field_evidence").fetchone()[0]
        self.add("field_evidence_present", "pass" if total else "warn", f"{total} evidence rows")
        rows = list(
            self.conn.execute(
                "SELECT evidence_status, COUNT(*) AS n FROM field_evidence GROUP BY evidence_status"
            )
        )
        statuses = {row["evidence_status"]: row["n"] for row in rows}
        invalid = set(statuses) - VALID_EVIDENCE_STATUS
        self.add(
            "field_evidence_status",
            "fail" if invalid else "pass",
            f"evidence status distribution: {statuses}",
            invalid=sorted(invalid),
        )
        # Known numeric fields must carry a normalized value.
        missing = self.conn.execute(
            "SELECT COUNT(*) FROM field_evidence WHERE evidence_status = 'known' "
            "AND normalized_value IS NULL"
        ).fetchone()[0]
        self.add(
            "field_evidence_known_values",
            "warn" if missing else "pass",
            f"{missing} known evidence rows without normalized_value",
        )

    def check_relationships(self) -> None:
        rows = list(
            self.conn.execute(
                "SELECT resolution_status, COUNT(*) AS n FROM entity_relationships "
                "GROUP BY resolution_status"
            )
        )
        statuses = {row["resolution_status"]: row["n"] for row in rows}
        self.add(
            "relationships_present",
            "pass" if statuses else "warn",
            f"relationship status distribution: {statuses}",
        )
        resolved_without_target = self.conn.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE resolution_status = 'resolved' "
            "AND to_entity_id IS NULL"
        ).fetchone()[0]
        self.add(
            "relationships_resolved_have_target",
            "fail" if resolved_without_target else "pass",
            f"{resolved_without_target} resolved relationships without target entity",
        )
        unresolved_without_ref = self.conn.execute(
            "SELECT COUNT(*) FROM entity_relationships WHERE resolution_status = 'unresolved' "
            "AND (raw_target_ref IS NULL OR raw_target_ref = '')"
        ).fetchone()[0]
        self.add(
            "relationships_unresolved_have_ref",
            "fail" if unresolved_without_ref else "pass",
            f"{unresolved_without_ref} unresolved relationships without raw target reference",
        )

    def check_node_values(self) -> None:
        bad = self.conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE max_points IS NOT NULL AND max_points < 0"
        ).fetchone()[0]
        self.add("nodes_max_points", "fail" if bad else "pass", f"{bad} nodes with negative max_points")
        missing_tree = self.conn.execute(
            "SELECT COUNT(*) FROM nodes n WHERE NOT EXISTS "
            "(SELECT 1 FROM trees t WHERE t.entity_id = n.tree_entity_id)"
        ).fetchone()[0]
        self.add(
            "nodes_tree_reference",
            "fail" if missing_tree else "pass",
            f"{missing_tree} nodes without a tree row",
        )

    def check_affix_tiers(self) -> None:
        bad = self.conn.execute(
            "SELECT COUNT(*) FROM affix_tiers WHERE roll_min IS NOT NULL "
            "AND roll_max IS NOT NULL AND roll_min > roll_max"
        ).fetchone()[0]
        self.add(
            "affix_tier_ranges",
            "fail" if bad else "pass",
            f"{bad} affix tiers with roll_min > roll_max",
        )
        no_tiers = self.conn.execute(
            "SELECT COUNT(*) FROM affixes a WHERE NOT EXISTS "
            "(SELECT 1 FROM affix_tiers t WHERE t.affix_entity_id = a.entity_id)"
        ).fetchone()[0]
        self.add("affix_tiers_present", "warn" if no_tiers else "pass", f"{no_tiers} affixes without tiers")

    def check_coverage_thresholds(self) -> None:
        thresholds = self.scope.get("quality_thresholds", {})
        counts = self._table_counts()
        for table, minimum in thresholds.items():
            actual = counts.get(table, 0)
            self.add(
                f"threshold_{table}",
                "pass" if actual >= int(minimum) else "fail",
                f"{table}={actual} (quality threshold {minimum})",
                actual=actual,
                minimum=minimum,
            )

    def _table_counts(self) -> dict[str, int]:
        tables = [
            "entities", "source_records", "skills", "skill_components", "trees",
            "nodes", "buffs", "ailments", "items", "affixes", "affix_tiers",
            "idols", "blessings", "minions", "entity_relationships",
        ]
        counts = {}
        for table in tables:
            try:
                counts[table] = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                counts[table] = 0
        return counts

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {"pass": 0, "warn": 0, "fail": 0}
        by_stage: dict[str, dict[str, int]] = {
            stage: {"pass": 0, "warn": 0, "fail": 0} for stage in STAGES
        }
        for result in self.results:
            by_status[result.status] = by_status.get(result.status, 0) + 1
            stage = check_stage(result.check_id)
            by_stage.setdefault(stage, {"pass": 0, "warn": 0, "fail": 0})
            by_stage[stage][result.status] = by_stage[stage].get(result.status, 0) + 1
        ok = by_status["fail"] == 0
        return {
            "ok": ok,
            "status": "pass" if ok and by_status["warn"] == 0 else ("pass_with_warnings" if ok else "fail"),
            "counts": by_status,
            "stages": by_stage,
            "checks": [result.to_dict() for result in self.results],
        }


def write_validation_reports(result: dict[str, Any], report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "validation.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Validation report",
        "",
        f"- overall: **{result['status']}**",
        f"- pass: {result['counts'].get('pass', 0)}",
        f"- warn: {result['counts'].get('warn', 0)}",
        f"- fail: {result['counts'].get('fail', 0)}",
    ]
    stages = result.get("stages") or {}
    if stages:
        lines.extend(["", "## Gate stages", "", "| stage | pass | warn | fail |", "|---|---|---|---|"])
        for stage, counts in stages.items():
            lines.append(
                f"| {stage} | {counts.get('pass', 0)} | {counts.get('warn', 0)} | "
                f"{counts.get('fail', 0)} |"
            )
    lines.extend(["", "| check | status | message |", "|---|---|---|"])
    for check in result["checks"]:
        message = check["message"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {check['check_id']} | {check['status']} | {message} |")
    md_path = report_dir / "validation.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "md": md_path}
