"""Workflow contract data types (``le-workflow-v1`` family).

This module is the executable form of :file:`docs/contracts/workflow-interface.md`.
It is deliberately dependency-free (stdlib only) and free of ``le_calc`` imports so
that callers (CLI, local orchestration, future graph nodes) can serialize and
validate workflow payloads without loading the damage engine.

Design rules (see the contract document):

* payloads are plain JSON objects; no Python objects, file handles or sessions;
* ``schema_version`` is validated on load, unknown versions fail explicitly;
* ``execution_status`` and ``readiness`` are separate fields;
* ``evidence_state`` / ``implementation_state`` stay independent from readiness;
* repository-relative paths only; artifact references carry a sha256;
* business functions return results or raise explicit errors (no print/sys.exit).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

CONTRACT_ID = "le-workflow-v1"

SCHEMA_STAGE_REQUEST = "le-stage-request-v1"
SCHEMA_STAGE_RESULT = "le-stage-result-v1"
SCHEMA_MECHANISM_QUERY = "le-mechanism-query-v1"
SCHEMA_MECHANISM_RESULT = "le-mechanism-result-v1"
SCHEMA_RESOLVE_BUILD_REQUEST = "le-resolve-build-request-v1"
SCHEMA_RESOLVE_BUILD_RESULT = "le-resolve-build-result-v1"
SCHEMA_ANALYSIS_PLAN = "le-analysis-plan-v1"
SCHEMA_COVERAGE = "le-coverage-v1"
SCHEMA_RESEARCH_QUEUE = "le-research-queue-v1"
SCHEMA_SIMULATE_REQUEST = "le-simulate-request-v1"
SCHEMA_SIMULATE_RESULT = "le-simulate-result-v1"
SCHEMA_VALIDATION_RESULT = "le-validation-result-v1"
SCHEMA_RESEARCH_TASK = "le-research-task-v1"
SCHEMA_RESEARCH_PROPOSAL = "le-research-proposal-v1"
SCHEMA_COMMIT_REQUEST = "le-commit-request-v1"
SCHEMA_COMMIT_RESULT = "le-commit-result-v1"
SCHEMA_PIPELINE_STATE = "le-pipeline-state-v1"

EXECUTION_COMPLETED = "completed"
EXECUTION_WAITING = "waiting"
EXECUTION_FAILED = "failed"
EXECUTION_STATUSES = frozenset({EXECUTION_COMPLETED, EXECUTION_WAITING, EXECUTION_FAILED})

READY = "ready"
PARTIAL = "partial"
NEEDS_RESEARCH = "needs_research"
NEEDS_INPUT = "needs_input"
READINESS = frozenset({READY, PARTIAL, NEEDS_RESEARCH, NEEDS_INPUT})

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"
SEVERITIES = frozenset({SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO})

CACHE_HIT = "hit"
CACHE_MISS = "miss"
CACHE_DISABLED = "disabled"
CACHE_STATUSES = frozenset({CACHE_HIT, CACHE_MISS, CACHE_DISABLED})

OUTCOME_PROPOSED_CHANGE = "proposed_change"
OUTCOME_UNRESOLVED = "unresolved"
OUTCOME_NEEDS_INPUT = "needs_input"
OUTCOMES = frozenset({OUTCOME_PROPOSED_CHANGE, OUTCOME_UNRESOLVED, OUTCOME_NEEDS_INPUT})

COMMIT_COMMITTED = "committed"
COMMIT_REPLAYED = "replayed"
COMMIT_CONFLICT = "conflict"
COMMIT_FAILED = "failed"
COMMIT_STATUSES = frozenset({COMMIT_COMMITTED, COMMIT_REPLAYED, COMMIT_CONFLICT, COMMIT_FAILED})

OPERATION_KINDS = frozenset(
    {
        "new_revision",
        "publish_dataset",
        "write_run",
        "publish_rule",
        "register_mechanism",
        "update_pointer",
    }
)

MATCH_EXACT = "exact"
MATCH_CANDIDATE = "candidate"

APPLICABILITY_SATISFIED = "satisfied"
APPLICABILITY_UNKNOWN = "unknown"
APPLICABILITY_NOT_APPLICABLE = "not_applicable"
APPLICABILITY_STATUSES = frozenset(
    {APPLICABILITY_SATISFIED, APPLICABILITY_UNKNOWN, APPLICABILITY_NOT_APPLICABLE}
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_PREFIX_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class ContractError(ValueError):
    """Raised when a workflow payload violates the contract."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    h = hashlib.sha256()
    with p.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(payload: Any) -> str:
    """Deterministic content fingerprint for JSON-compatible payloads."""
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


def deterministic_id(prefix: str, payload: Any) -> str:
    """Stable id like ``research_task-<16 hex>`` derived from content.

    Used for gap/task/operation ids so the same input does not create duplicates.
    """
    if not _ID_PREFIX_RE.match(prefix):
        raise ContractError(f"invalid id prefix: {prefix!r}")
    return f"{prefix}-{fingerprint(payload)[:16]}"


def normalize_relative(path: str | Path, root: str | Path) -> str:
    """Return a repository-relative POSIX path; raise for paths outside root."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    try:
        rel = p.resolve().relative_to(Path(root).resolve())
    except ValueError as exc:  # pragma: no cover - host specific
        raise ContractError(f"path outside repository root: {path}") from exc
    return rel.as_posix()


# ---------------------------------------------------------------------------
# artifact references
# ---------------------------------------------------------------------------


def make_artifact_ref(
    path: str | Path,
    *,
    root: str | Path,
    artifact_id: Optional[str] = None,
    schema_version: Optional[str] = None,
) -> dict[str, Any]:
    """Build an ``artifact_ref``; computes sha256 from the file on disk."""
    p = Path(path)
    if not p.is_absolute():
        p = Path(root) / p
    if not p.is_file():
        raise ContractError(f"artifact not found: {path}")
    rel = normalize_relative(p, root)
    ref: dict[str, Any] = {
        "artifact_id": artifact_id or rel,
        "path": rel,
        "asset_id": None,
        "schema_version": schema_version,
        "sha256": sha256_file(p),
    }
    return ref


def verify_artifact_ref(ref: Mapping[str, Any], *, root: str | Path) -> dict[str, Any]:
    """Verify an ``artifact_ref`` against the file on disk.

    Returns a dict ``{"ok": bool, "reason": str|None, "path": str|None}``; never
    raises for a merely stale reference (callers map it to a ``stale_ref`` issue).
    """
    path = ref.get("path")
    if not path:
        return {"ok": False, "reason": "asset_id_or_missing_path", "path": None}
    p = Path(root) / str(path)
    if not p.is_file():
        return {"ok": False, "reason": "missing", "path": str(path)}
    actual = sha256_file(p)
    if actual != ref.get("sha256"):
        return {"ok": False, "reason": "sha256_mismatch", "path": str(path), "actual": actual}
    return {"ok": True, "reason": None, "path": str(path)}


# ---------------------------------------------------------------------------
# issues / cache
# ---------------------------------------------------------------------------


def make_issue(
    code: str,
    message: str,
    *,
    severity: str = SEVERITY_ERROR,
    related_ids: Optional[Iterable[str]] = None,
    retryable: bool = False,
    required_input: Optional[Mapping[str, Any]] = None,
    details: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    if severity not in SEVERITIES:
        raise ContractError(f"invalid severity: {severity!r}")
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "related_ids": list(related_ids or []),
        "retryable": bool(retryable),
        "required_input": dict(required_input) if required_input else None,
        "details": dict(details or {}),
    }


def make_cache_info(
    status: str, *, key: Optional[str] = None, reason: str = "", invalidated_by: Optional[Iterable[str]] = None
) -> dict[str, Any]:
    if status not in CACHE_STATUSES:
        raise ContractError(f"invalid cache status: {status!r}")
    return {"status": status, "key": key, "reason": reason, "invalidated_by": list(invalidated_by or [])}


# ---------------------------------------------------------------------------
# stage payloads
# ---------------------------------------------------------------------------


@dataclass
class StageRequest:
    stage_id: str
    case_id: str
    run_id: str
    input_refs: list[dict[str, Any]] = field(default_factory=list)
    dependency_fingerprints: dict[str, Any] = field(default_factory=dict)
    parameters: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_STAGE_REQUEST

    def to_dict(self) -> dict[str, Any]:
        self._validate()
        return {
            "schema_version": self.schema_version,
            "stage_id": self.stage_id,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "input_refs": [dict(ref) for ref in self.input_refs],
            "dependency_fingerprints": dict(self.dependency_fingerprints),
            "parameters": dict(self.parameters),
        }

    def _validate(self) -> None:
        if self.schema_version != SCHEMA_STAGE_REQUEST:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        for name in ("stage_id", "case_id", "run_id"):
            if not getattr(self, name):
                raise ContractError(f"{name} is required")
        for ref in self.input_refs:
            sha = ref.get("sha256")
            if sha is not None and not _SHA256_RE.match(str(sha)):
                raise ContractError(f"bad artifact sha256: {sha!r}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageRequest":
        data = dict(payload)
        version = data.get("schema_version")
        if version != SCHEMA_STAGE_REQUEST:
            raise ContractError(f"unsupported schema_version: {version!r}")
        return cls(
            stage_id=str(data["stage_id"]),
            case_id=str(data["case_id"]),
            run_id=str(data["run_id"]),
            input_refs=list(data.get("input_refs") or []),
            dependency_fingerprints=dict(data.get("dependency_fingerprints") or {}),
            parameters=dict(data.get("parameters") or {}),
        )


@dataclass
class StageResult:
    stage_id: str
    execution_status: str
    readiness: Optional[str]
    output_refs: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    cache: dict[str, Any] = field(default_factory=lambda: make_cache_info(CACHE_MISS))
    schema_version: str = SCHEMA_STAGE_RESULT

    def to_dict(self) -> dict[str, Any]:
        self._validate()
        return {
            "schema_version": self.schema_version,
            "stage_id": self.stage_id,
            "execution_status": self.execution_status,
            "readiness": self.readiness,
            "output_refs": [dict(ref) for ref in self.output_refs],
            "issues": [dict(issue) for issue in self.issues],
            "metrics": dict(self.metrics),
            "cache": dict(self.cache),
        }

    def _validate(self) -> None:
        if self.schema_version != SCHEMA_STAGE_RESULT:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if self.execution_status not in EXECUTION_STATUSES:
            raise ContractError(f"invalid execution_status: {self.execution_status!r}")
        if self.readiness is not None and self.readiness not in READINESS:
            raise ContractError(f"invalid readiness: {self.readiness!r}")
        if self.execution_status == EXECUTION_WAITING and not any(
            issue.get("required_input") for issue in self.issues
        ):
            raise ContractError("waiting stage must carry an issue with required_input")
        for issue in self.issues:
            if issue.get("severity") not in SEVERITIES:
                raise ContractError(f"invalid issue severity: {issue.get('severity')!r}")
        if self.cache.get("status") not in CACHE_STATUSES:
            raise ContractError(f"invalid cache status: {self.cache.get('status')!r}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageResult":
        data = dict(payload)
        version = data.get("schema_version")
        if version != SCHEMA_STAGE_RESULT:
            raise ContractError(f"unsupported schema_version: {version!r}")
        return cls(
            stage_id=str(data["stage_id"]),
            execution_status=str(data["execution_status"]),
            readiness=data.get("readiness"),
            output_refs=list(data.get("output_refs") or []),
            issues=list(data.get("issues") or []),
            metrics=dict(data.get("metrics") or {}),
            cache=dict(data.get("cache") or make_cache_info(CACHE_MISS)),
        )


@dataclass
class StageContext:
    """Explicit execution context; deliberately *not* serialized or persisted."""

    repo_root: Path
    output_root: Path
    adapters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.repo_root = Path(self.repo_root)
        self.output_root = Path(self.output_root)

    def adapter(self, name: str) -> Any:
        try:
            return self.adapters[name]
        except KeyError as exc:  # pragma: no cover - caller configuration error
            raise ContractError(f"missing stage context adapter: {name!r}") from exc


# ---------------------------------------------------------------------------
# research hand-off payloads
# ---------------------------------------------------------------------------


@dataclass
class ResearchTask:
    task_id: str
    gap_id: str
    input_fingerprint: str
    question: str
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    related_mechanism_refs: list[str] = field(default_factory=list)
    researched_already: list[str] = field(default_factory=list)
    affected: dict[str, Any] = field(default_factory=dict)
    deliverable_kind: str = "mapping"
    verification_required: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_RESEARCH_TASK

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_RESEARCH_TASK:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if not _SHA256_RE.match(self.input_fingerprint):
            raise ContractError("input_fingerprint must be a sha256 hex digest")
        if not self.question:
            raise ContractError("question is required")
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "gap_id": self.gap_id,
            "input_fingerprint": self.input_fingerprint,
            "question": self.question,
            "source_refs": [dict(ref) for ref in self.source_refs],
            "related_mechanism_refs": list(self.related_mechanism_refs),
            "researched_already": list(self.researched_already),
            "affected": dict(self.affected),
            "deliverable_kind": self.deliverable_kind,
            "verification_required": list(self.verification_required),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchTask":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_RESEARCH_TASK:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            task_id=str(data["task_id"]),
            gap_id=str(data["gap_id"]),
            input_fingerprint=str(data["input_fingerprint"]),
            question=str(data["question"]),
            source_refs=list(data.get("source_refs") or []),
            related_mechanism_refs=list(data.get("related_mechanism_refs") or []),
            researched_already=list(data.get("researched_already") or []),
            affected=dict(data.get("affected") or {}),
            deliverable_kind=str(data.get("deliverable_kind") or "mapping"),
            verification_required=list(data.get("verification_required") or []),
        )


@dataclass
class ResearchProposal:
    proposal_id: str
    task_id: str
    based_on_fingerprint: str
    outcome: str
    mechanism_refs: list[str] = field(default_factory=list)
    mapping_refs: list[str] = field(default_factory=list)
    code_change_refs: list[str] = field(default_factory=list)
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    assumptions: list[dict[str, Any]] = field(default_factory=list)
    verification_refs: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_RESEARCH_PROPOSAL

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_RESEARCH_PROPOSAL:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if self.outcome not in OUTCOMES:
            raise ContractError(f"invalid outcome: {self.outcome!r}")
        if not _SHA256_RE.match(self.based_on_fingerprint):
            raise ContractError("based_on_fingerprint must be a sha256 hex digest")
        return {
            "schema_version": self.schema_version,
            "proposal_id": self.proposal_id,
            "task_id": self.task_id,
            "based_on_fingerprint": self.based_on_fingerprint,
            "outcome": self.outcome,
            "mechanism_refs": list(self.mechanism_refs),
            "mapping_refs": list(self.mapping_refs),
            "code_change_refs": list(self.code_change_refs),
            "evidence_refs": [dict(ref) for ref in self.evidence_refs],
            "assumptions": [dict(item) for item in self.assumptions],
            "verification_refs": list(self.verification_refs),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResearchProposal":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_RESEARCH_PROPOSAL:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            proposal_id=str(data["proposal_id"]),
            task_id=str(data["task_id"]),
            based_on_fingerprint=str(data["based_on_fingerprint"]),
            outcome=str(data["outcome"]),
            mechanism_refs=list(data.get("mechanism_refs") or []),
            mapping_refs=list(data.get("mapping_refs") or []),
            code_change_refs=list(data.get("code_change_refs") or []),
            evidence_refs=list(data.get("evidence_refs") or []),
            assumptions=list(data.get("assumptions") or []),
            verification_refs=list(data.get("verification_refs") or []),
        )


@dataclass
class ValidationResult:
    kind: str
    passed: bool
    accepted: list[str] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    outcome_effective: Optional[bool] = None
    blocking: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = SCHEMA_VALIDATION_RESULT

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_VALIDATION_RESULT:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if self.kind not in ("result", "proposal"):
            raise ContractError(f"invalid validation kind: {self.kind!r}")
        return {
            "schema_version": self.schema_version,
            "kind": self.kind,
            "passed": bool(self.passed),
            "accepted": list(self.accepted),
            "errors": [dict(item) for item in self.errors],
            "checks": [dict(item) for item in self.checks],
            "blocking": [dict(item) for item in self.blocking],
            "next_actions": list(self.next_actions),
            "outcome_effective": self.outcome_effective,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ValidationResult":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_VALIDATION_RESULT:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            kind=str(data["kind"]),
            passed=bool(data["passed"]),
            accepted=list(data.get("accepted") or []),
            errors=list(data.get("errors") or []),
            checks=list(data.get("checks") or []),
            next_actions=list(data.get("next_actions") or []),
            outcome_effective=data.get("outcome_effective"),
            blocking=list(data.get("blocking") or []),
        )


# ---------------------------------------------------------------------------
# commit / idempotent write payloads
# ---------------------------------------------------------------------------


@dataclass
class CommitRequest:
    operation_id: str
    attempt_id: str
    operation_kind: str
    input_fingerprint: str
    candidate_refs: list[dict[str, Any]] = field(default_factory=list)
    expected_current_revision: Optional[str] = None
    preconditions: dict[str, Any] = field(default_factory=dict)
    schema_version: str = SCHEMA_COMMIT_REQUEST

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_COMMIT_REQUEST:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if self.operation_kind not in OPERATION_KINDS:
            raise ContractError(f"invalid operation_kind: {self.operation_kind!r}")
        if not _SHA256_RE.match(self.input_fingerprint):
            raise ContractError("input_fingerprint must be a sha256 hex digest")
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "operation_kind": self.operation_kind,
            "input_fingerprint": self.input_fingerprint,
            "candidate_refs": [dict(ref) for ref in self.candidate_refs],
            "expected_current_revision": self.expected_current_revision,
            "preconditions": dict(self.preconditions),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CommitRequest":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_COMMIT_REQUEST:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            operation_id=str(data["operation_id"]),
            attempt_id=str(data["attempt_id"]),
            operation_kind=str(data["operation_kind"]),
            input_fingerprint=str(data["input_fingerprint"]),
            candidate_refs=list(data.get("candidate_refs") or []),
            expected_current_revision=data.get("expected_current_revision"),
            preconditions=dict(data.get("preconditions") or {}),
        )


@dataclass
class CommitResult:
    operation_id: str
    status: str
    attempt_id: str
    committed_refs: list[dict[str, Any]] = field(default_factory=list)
    current_revision: Optional[str] = None
    issues: list[dict[str, Any]] = field(default_factory=list)
    schema_version: str = SCHEMA_COMMIT_RESULT

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_COMMIT_RESULT:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        if self.status not in COMMIT_STATUSES:
            raise ContractError(f"invalid commit status: {self.status!r}")
        return {
            "schema_version": self.schema_version,
            "operation_id": self.operation_id,
            "attempt_id": self.attempt_id,
            "status": self.status,
            "committed_refs": [dict(ref) for ref in self.committed_refs],
            "current_revision": self.current_revision,
            "issues": [dict(item) for item in self.issues],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "CommitResult":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_COMMIT_RESULT:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            operation_id=str(data["operation_id"]),
            attempt_id=str(data["attempt_id"]),
            status=str(data["status"]),
            committed_refs=list(data.get("committed_refs") or []),
            current_revision=data.get("current_revision"),
            issues=list(data.get("issues") or []),
        )


# ---------------------------------------------------------------------------
# execution state (persisted via StateStore; minimal shape for V1)
# ---------------------------------------------------------------------------


@dataclass
class StageState:
    stage_id: str
    execution_status: str
    readiness: Optional[str]
    input_fingerprint: Optional[str] = None
    implementation_fingerprint: Optional[str] = None
    parameters_fingerprint: Optional[str] = None
    cache_key: Optional[str] = None
    output_refs: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: Optional[float] = None
    cache_status: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        if self.execution_status not in EXECUTION_STATUSES:
            raise ContractError(f"invalid execution_status: {self.execution_status!r}")
        if self.readiness is not None and self.readiness not in READINESS:
            raise ContractError(f"invalid readiness: {self.readiness!r}")
        return {
            "stage_id": self.stage_id,
            "execution_status": self.execution_status,
            "readiness": self.readiness,
            "input_fingerprint": self.input_fingerprint,
            "implementation_fingerprint": self.implementation_fingerprint,
            "parameters_fingerprint": self.parameters_fingerprint,
            "cache_key": self.cache_key,
            "output_refs": [dict(ref) for ref in self.output_refs],
            "elapsed_ms": self.elapsed_ms,
            "cache_status": self.cache_status,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "StageState":
        data = dict(payload)
        return cls(
            stage_id=str(data["stage_id"]),
            execution_status=str(data["execution_status"]),
            readiness=data.get("readiness"),
            input_fingerprint=data.get("input_fingerprint"),
            implementation_fingerprint=data.get("implementation_fingerprint"),
            parameters_fingerprint=data.get("parameters_fingerprint"),
            cache_key=data.get("cache_key"),
            output_refs=list(data.get("output_refs") or []),
            elapsed_ms=data.get("elapsed_ms"),
            cache_status=data.get("cache_status"),
        )


@dataclass
class PipelineState:
    case_id: str
    run_id: str
    stages: list[StageState] = field(default_factory=list)
    pending_input: list[dict[str, Any]] = field(default_factory=list)
    last_commit: Optional[dict[str, Any]] = None
    schema_version: str = SCHEMA_PIPELINE_STATE

    @property
    def run_key(self) -> str:
        return f"{self.case_id}:{self.run_id}"

    def to_dict(self) -> dict[str, Any]:
        if self.schema_version != SCHEMA_PIPELINE_STATE:
            raise ContractError(f"unsupported schema_version: {self.schema_version!r}")
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "run_id": self.run_id,
            "stages": [stage.to_dict() for stage in self.stages],
            "pending_input": [dict(item) for item in self.pending_input],
            "last_commit": dict(self.last_commit) if self.last_commit else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PipelineState":
        data = dict(payload)
        if data.get("schema_version") != SCHEMA_PIPELINE_STATE:
            raise ContractError(f"unsupported schema_version: {data.get('schema_version')!r}")
        return cls(
            case_id=str(data["case_id"]),
            run_id=str(data["run_id"]),
            stages=[StageState.from_dict(item) for item in data.get("stages") or []],
            pending_input=list(data.get("pending_input") or []),
            last_commit=data.get("last_commit"),
        )


# ---------------------------------------------------------------------------
# JSON round-trip helpers
# ---------------------------------------------------------------------------


def dumps(payload: Any) -> str:
    """Serialize a contract payload deterministically (UTF-8 JSON text)."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)


def loads(text: str, *, expected_schemas: Optional[Iterable[str]] = None) -> dict[str, Any]:
    """Parse JSON text and (optionally) check its ``schema_version``."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise ContractError("payload must be a JSON object")
    allowed = set(expected_schemas or [])
    if allowed and payload.get("schema_version") not in allowed:
        raise ContractError(
            f"unsupported schema_version: {payload.get('schema_version')!r} (expected one of {sorted(allowed)})"
        )
    return payload


def read_json(path: str | Path, *, expected_schemas: Optional[Iterable[str]] = None) -> dict[str, Any]:
    return loads(Path(path).read_text(encoding="utf-8"), expected_schemas=expected_schemas)


def write_json(path: str | Path, payload: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps(payload) + "\n", encoding="utf-8")


__all__ = [
    "CONTRACT_ID",
    "ContractError",
    # schema ids
    "SCHEMA_STAGE_REQUEST",
    "SCHEMA_STAGE_RESULT",
    "SCHEMA_ANALYSIS_PLAN",
    "SCHEMA_COVERAGE",
    "SCHEMA_RESEARCH_QUEUE",
    "SCHEMA_VALIDATION_RESULT",
    "SCHEMA_RESEARCH_TASK",
    "SCHEMA_RESEARCH_PROPOSAL",
    "SCHEMA_COMMIT_REQUEST",
    "SCHEMA_COMMIT_RESULT",
    "SCHEMA_PIPELINE_STATE",
    # vocabularies
    "EXECUTION_COMPLETED",
    "EXECUTION_WAITING",
    "EXECUTION_FAILED",
    "EXECUTION_STATUSES",
    "READY",
    "PARTIAL",
    "NEEDS_RESEARCH",
    "NEEDS_INPUT",
    "READINESS",
    "SEVERITY_ERROR",
    "SEVERITY_WARNING",
    "SEVERITY_INFO",
    "SEVERITIES",
    "CACHE_HIT",
    "CACHE_MISS",
    "CACHE_DISABLED",
    "CACHE_STATUSES",
    "OUTCOME_PROPOSED_CHANGE",
    "OUTCOME_UNRESOLVED",
    "OUTCOME_NEEDS_INPUT",
    "OUTCOMES",
    "COMMIT_COMMITTED",
    "COMMIT_REPLAYED",
    "COMMIT_CONFLICT",
    "COMMIT_FAILED",
    "COMMIT_STATUSES",
    "OPERATION_KINDS",
    "MATCH_EXACT",
    "MATCH_CANDIDATE",
    "APPLICABILITY_SATISFIED",
    "APPLICABILITY_UNKNOWN",
    "APPLICABILITY_NOT_APPLICABLE",
    "APPLICABILITY_STATUSES",
    # helpers
    "sha256_bytes",
    "sha256_file",
    "fingerprint",
    "deterministic_id",
    "normalize_relative",
    "make_artifact_ref",
    "verify_artifact_ref",
    "make_issue",
    "make_cache_info",
    "dumps",
    "loads",
    "read_json",
    "write_json",
    # types
    "StageRequest",
    "StageResult",
    "StageContext",
    "ResearchTask",
    "ResearchProposal",
    "ValidationResult",
    "CommitRequest",
    "CommitResult",
    "StageState",
    "PipelineState",
]
