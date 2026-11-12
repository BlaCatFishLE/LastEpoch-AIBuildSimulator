"""Idempotent, atomic workflow operation commits (P4).

Implements the commit protocol from ``docs/contracts/workflow-interface.md``
§7:

* one operation record per logical ``operation_id`` under
  ``<operations_root>/<operation_id>.json`` (atomic write);
* same operation + same input fingerprint => verify published artefacts and
  return ``replayed`` without publishing again;
* same operation + different fingerprint => ``conflict`` (never overwrite);
* failed preconditions / corrupt records / missing published artefacts =>
  ``failed`` with a stable issue code;
* candidates are published through same-volume staging paths with
  ``os.replace``; a crash before recording is recovered by re-verifying the
  published target.

This module is a library: no CLI imports, no printing, no process exit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from . import atomic_io, contract

OPERATION_RECORD_SCHEMA = "le-operation-record-v1"
OPERATION_RECORD_FILE = "{operation_id}.json"
LOCK_DIR = ".locks"
TARGET_LOCK_DIR = ".target-locks"
TARGET_CLAIM_DIR = ".targets"
INTENT_DIR = ".intents"
INTENT_SCHEMA = "le-operation-intent-v1"
CURRENT_REVISION_FILE = "current-revision.json"
_POINTER_BUILD_RE = re.compile(r"^data/builds/[^/]+/[^/]+/current\.json$")

_MAX_OPERATION_ID_CHARS = 512
_FILE_SAFE_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_ENCODED_STEM_PREFIX = "~op-"

ISSUE_ERROR = "error"
ISSUE_WARNING = "warning"
ISSUE_INFO = "info"


class CommitError(ValueError):
    """Invalid request/path or unsafe invocation (never a business conflict)."""


class OperationStoreError(CommitError):
    """The operation record exists but cannot be trusted."""


class CommitLockTimeout(CommitError):
    """Another process/thread held the operation lock past the timeout."""


@dataclass
class _Candidate:
    candidate_id: str
    kind: str
    candidate_path: Path
    target_path: str
    target_abs: Path
    sha256: str
    artifact_id: Optional[str] = None


def compute_input_fingerprint(payload: Any) -> str:
    """Deterministic sha256 of a JSON-able operation input payload."""

    return atomic_io.canonical_payload_sha256(payload)


# ---------------------------------------------------------------------------
# path / record helpers
# ---------------------------------------------------------------------------


def _resolve_root(root: str | Path | None) -> Path:
    return Path(root).resolve() if root is not None else Path.cwd().resolve()


def _resolve_operations_root(operations_root: str | Path, root: Path) -> Path:
    path = Path(operations_root)
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def operation_file_stem(operation_id: str) -> str:
    """Windows-safe filesystem stem for a logical operation id.

    ``:`` and other characters that are legal in the logical id but invalid in
    Windows filenames are replaced by a deterministic digest; simple ids such
    as ``op-1`` keep their raw name for backwards compatibility.
    """

    text = _validate_operation_id(operation_id)
    if _FILE_SAFE_OPERATION_ID.match(text) and text not in (".", ".."):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"{_ENCODED_STEM_PREFIX}{digest}"


def operation_record_path(operation_id: str, operations_root: str | Path) -> Path:
    stem = operation_file_stem(operation_id)
    return Path(operations_root) / OPERATION_RECORD_FILE.format(operation_id=stem)


def _validate_operation_id(operation_id: str) -> str:
    """Validate a logical operation id without imposing filesystem restrictions.

    The contract only requires a stable string.  Paths are never built from the
    raw value (a hashed safe stem is used when needed), so ``:``, ``/``,
    ``\\``, ``..``, CJK and long ids are all legitimate.
    """

    if not isinstance(operation_id, str):
        raise CommitError("operation_id 必须是字符串")
    text = operation_id
    if not text or len(text) > _MAX_OPERATION_ID_CHARS:
        raise CommitError(
            f"operation_id 长度必须在 1..{_MAX_OPERATION_ID_CHARS}：{operation_id!r}"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in text):
        raise CommitError("operation_id 不得包含控制字符")
    return text


def _normalise_relative_target(root: Path, value: Any) -> tuple[str, Path]:
    text = str(value or "").replace("\\", "/").strip()
    if not text:
        raise CommitError("candidate_ref 缺少正式目标路径（path/target）")
    relative = Path(text)
    if relative.is_absolute():
        raise CommitError(f"正式目标路径必须是仓库相对路径：{value!r}")
    parts = [part for part in relative.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise CommitError(f"正式目标路径不得包含 ..：{value!r}")
    resolved = (root / Path(*parts)).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CommitError(f"正式目标路径逃出仓库根：{value!r}") from exc
    return Path(*parts).as_posix(), resolved


def _normalise_candidate(root: Path, ref: Mapping[str, Any], index: int) -> _Candidate:
    data = dict(ref or {})
    target_value = data.get("path") or data.get("target") or data.get("target_path")
    target_path, target_abs = _normalise_relative_target(root, target_value)
    expected = data.get("sha256")
    raw_candidate = data.get("candidate_path") or data.get("candidate") or data.get("source")
    kind = str(data.get("kind") or ("dir" if target_abs.is_dir() else "file"))
    if kind not in ("file", "dir"):
        raise CommitError(f"candidate_refs[{index}].kind 只支持 file/dir")
    if raw_candidate:
        candidate = Path(str(raw_candidate))
        if not candidate.is_absolute():
            candidate = root / candidate
        candidate = candidate.resolve()
    elif expected and target_abs.exists():
        # Recovery path: after a crash the staging candidate may be gone; the
        # caller can still reference the published target + expected sha.
        candidate = target_abs
    else:
        raise CommitError(f"candidate_refs[{index}] 缺少 candidate_path（且目标不存在/无 sha 可恢复）")
    if kind == "dir":
        actual = atomic_io.tree_sha256(candidate)
    else:
        actual = atomic_io.sha256_file(candidate)
    if expected is not None and str(expected) != actual:
        raise CommitError(
            f"candidate_refs[{index}] sha256 与候选产物不一致："
            f"expected={expected} actual={actual}"
        )
    return _Candidate(
        candidate_id=str(data.get("candidate_id") or f"candidate-{index}"),
        kind=kind,
        candidate_path=candidate,
        target_path=target_path,
        target_abs=target_abs,
        sha256=actual,
        artifact_id=data.get("artifact_id"),
    )


def _record_public_ref(candidate: _Candidate) -> dict[str, Any]:
    ref = {
        "path": candidate.target_path,
        "sha256": candidate.sha256,
        "kind": candidate.kind,
        "candidate_id": candidate.candidate_id,
    }
    if candidate.artifact_id:
        ref["artifact_id"] = str(candidate.artifact_id)
    return ref


def _record_display_path(ref: Mapping[str, Any]) -> str:
    return str(ref.get("path") or "")


def _read_record_file(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationStoreError(f"操作记录损坏（不是合法 JSON）：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise OperationStoreError(f"操作记录损坏（不是 JSON 对象）：{path}")
    if payload.get("schema_version") != OPERATION_RECORD_SCHEMA:
        raise OperationStoreError(
            f"操作记录 schema_version 不受支持：{payload.get('schema_version')!r}"
        )
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not operation_id:
        raise OperationStoreError("操作记录缺少 operation_id")
    _validate_operation_id(operation_id)
    if payload.get("status") not in ("committed", "publishing"):
        raise OperationStoreError(f"操作记录 status 非法：{payload.get('status')!r}")
    if not isinstance(payload.get("published_refs"), list):
        raise OperationStoreError("操作记录缺少 published_refs")
    return payload


def read_operation_record(
    operation_id: str, operations_root: str | Path
) -> Optional[dict[str, Any]]:
    """Read one operation record; ``None`` when absent, raise when corrupt."""

    path = operation_record_path(operation_id, operations_root)
    if not path.is_file():
        return None
    payload = _read_record_file(path)
    if payload is not None and payload.get("operation_id") != operation_id:
        raise OperationStoreError("操作记录 operation_id 与查询参数不一致")
    return payload


def _write_operation_record(
    operation_id: str, operations_root: Path, record: Mapping[str, Any]
) -> dict[str, str]:
    operations_root.mkdir(parents=True, exist_ok=True)
    path = operation_record_path(operation_id, operations_root)
    return atomic_io.atomic_write_json(path, record)


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------


class OperationLock:
    """Cross-process O_EXCL lock for one operation id.

    Windows notes: ``os.open(..., O_CREAT|O_EXCL)`` is atomic on NTFS and works
    across processes; stale locks are reclaimed by age only (PID reuse is not
    solved).  Callers that need stronger semantics should keep operation ids
    unique per logical operation or use a service-level lock.
    """

    def __init__(
        self,
        operations_root: Path,
        operation_id: str,
        *,
        timeout_seconds: float = 10.0,
        stale_seconds: float = 300.0,
        poll_seconds: float = 0.02,
    ):
        self.lock_dir = operations_root / LOCK_DIR
        self.path = self.lock_dir / f"{operation_file_stem(operation_id)}.lock"
        self.operation_id = operation_id
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.poll_seconds = poll_seconds
        self._acquired = False

    def _is_stale(self) -> bool:
        try:
            age = time.time() - self.path.stat().st_mtime
        except OSError:
            return False
        return age > self.stale_seconds

    def acquire(self) -> None:
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                descriptor = os.open(
                    str(self.path),
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o644,
                )
            except FileExistsError:
                if self._is_stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise CommitLockTimeout(
                        f"等待 operation 锁超时：{self.operation_id}"
                    )
                time.sleep(self.poll_seconds)
                continue
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "operation_id": self.operation_id,
                                "pid": os.getpid(),
                                "created_at": _utc_now(),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            except BaseException:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                try:
                    self.path.unlink()
                except OSError:
                    pass
                raise
            self._acquired = True
            return

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "OperationLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# verification / recovery
# ---------------------------------------------------------------------------


def _verify_published_ref(root: Path, ref: Mapping[str, Any]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    path_value = _record_display_path(ref)
    expected = str(ref.get("sha256") or "")
    if not path_value or not expected:
        return [
            {
                "code": "published_artifact_missing",
                "message": "操作记录中的 published_ref 缺少 path/sha256",
                "severity": ISSUE_ERROR,
                "retryable": False,
                "details": {"ref": dict(ref)},
            }
        ]
    path = root / Path(path_value)
    kind = str(ref.get("kind") or "file")
    if kind == "dir":
        if not path.is_dir():
            problems.append(
                {
                    "code": "published_artifact_missing",
                    "message": f"已发布目录不存在：{path_value}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": path_value},
                }
            )
        else:
            try:
                actual = atomic_io.tree_sha256(path)
            except OSError as exc:
                problems.append(
                    {
                        "code": "published_artifact_sha_mismatch",
                        "message": f"已发布目录无法校验：{path_value}：{exc}",
                        "severity": ISSUE_ERROR,
                        "retryable": False,
                        "details": {"path": path_value},
                    }
                )
            else:
                if actual != expected:
                    problems.append(
                        {
                            "code": "published_artifact_sha_mismatch",
                            "message": f"已发布目录 sha256 不一致：{path_value}",
                            "severity": ISSUE_ERROR,
                            "retryable": False,
                            "details": {"path": path_value, "expected": expected, "actual": actual},
                        }
                    )
    else:
        if not path.is_file():
            problems.append(
                {
                    "code": "published_artifact_missing",
                    "message": f"已发布文件不存在：{path_value}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": path_value},
                }
            )
        else:
            actual = atomic_io.sha256_file(path)
            if actual != expected:
                problems.append(
                    {
                        "code": "published_artifact_sha_mismatch",
                        "message": f"已发布文件 sha256 不一致：{path_value}",
                        "severity": ISSUE_ERROR,
                        "retryable": False,
                        "details": {"path": path_value, "expected": expected, "actual": actual},
                    }
                )
    return problems


def _target_owner(
    repo_root: Path,
    operations_root: Path,
    candidates: list[_Candidate],
    operation_id: str,
) -> Optional[str]:
    """Return the operation id that already claims one of these targets."""

    targets = {candidate.target_path for candidate in candidates}
    if not operations_root.is_dir():
        return None
    for entry in operations_root.iterdir():
        if entry.suffix != ".json":
            continue
        try:
            record = _read_record_file(entry)
        except OperationStoreError:
            continue
        if not record or record.get("operation_id") == operation_id:
            continue
        for ref in record.get("published_refs") or []:
            if str(ref.get("path") or "") in targets:
                return str(record.get("operation_id"))
    return None


def _published_matches_candidates(
    root: Path, candidates: list[_Candidate]
) -> bool:
    if not candidates:
        return False
    for candidate in candidates:
        if candidate.kind == "dir":
            if not candidate.target_abs.is_dir():
                return False
            if atomic_io.tree_sha256(candidate.target_abs) != candidate.sha256:
                return False
        else:
            if not candidate.target_abs.is_file():
                return False
            if atomic_io.sha256_file(candidate.target_abs) != candidate.sha256:
                return False
    return True


def _cleanup_candidate_temps(candidates: list[_Candidate], operation_id: str) -> list[str]:
    removed: list[str] = []
    parents = sorted({candidate.target_abs.parent for candidate in candidates})
    for parent in parents:
        removed.extend(atomic_io.cleanup_operation_temps(parent, operation_id))
    return removed


def recover_operations(
    operations_root: str | Path, *, root: str | Path | None = None
) -> dict[str, Any]:
    """Inspect operation records and orphan staging entries (explicit recovery).

    Does not mutate published artefacts.  Returns a report consumed by tests /
    integration diagnostics; ``commit_operation`` performs the actual
    publish-before-record recovery automatically.
    """

    repo_root = _resolve_root(root)
    ops_root = _resolve_operations_root(operations_root, repo_root)
    report: dict[str, Any] = {
        "operations_root": str(ops_root),
        "records": [],
        "orphan_temps": [],
        "invalid_records": [],
    }
    if not ops_root.is_dir():
        return report
    for entry in sorted(ops_root.iterdir(), key=lambda item: item.name):
        if not entry.is_file() or entry.suffix != ".json":
            continue
        try:
            record = _read_record_file(entry)
        except OperationStoreError as exc:
            report["invalid_records"].append({"file": entry.name, "error": str(exc)})
            continue
        if not record:
            continue
        operation_id = str(record.get("operation_id"))
        problems = []
        for ref in record.get("published_refs") or []:
            problems.extend(_verify_published_ref(repo_root, ref))
        report["records"].append(
            {
                "operation_id": operation_id,
                "status": record.get("status"),
                "problems": problems,
                "published_refs": record.get("published_refs") or [],
            }
        )
        parents = {
            (repo_root / Path(str(ref.get("path")))).parent
            for ref in record.get("published_refs") or []
            if ref.get("path")
        }
        for parent in parents:
            prefix = f"{atomic_io.TMP_PREFIX}{operation_file_stem(operation_id)}-"
            if parent.is_dir():
                for item in parent.iterdir():
                    if item.name.startswith(prefix):
                        report["orphan_temps"].append(str(item))
    return report


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------


def _request_to_mapping(request: "contract.CommitRequest | Mapping[str, Any]") -> dict[str, Any]:
    if isinstance(request, contract.CommitRequest):
        return request.to_dict()
    if not isinstance(request, Mapping):
        raise CommitError("commit_operation 需要 CommitRequest 或 mapping")
    data = dict(request)
    data.setdefault("schema_version", contract.SCHEMA_COMMIT_REQUEST)
    if data.get("operation_kind") in ("write_file", "write_dir"):
        # Library-level generic helper kinds map to the closest contract kind;
        # the original requested kind is retained in the operation record.
        data.setdefault("requested_operation_kind", data["operation_kind"])
        data["operation_kind"] = "write_run"
    return data


def _normalise_request(
    request: "contract.CommitRequest | Mapping[str, Any]",
) -> tuple[contract.CommitRequest, Optional[str]]:
    data = _request_to_mapping(request)
    requested_kind = data.pop("requested_operation_kind", None)
    try:
        normalised = contract.CommitRequest.from_dict(data)
    except (KeyError, contract.ContractError) as exc:
        raise CommitError(f"非法 CommitRequest：{exc}") from exc
    _validate_operation_id(normalised.operation_id)
    if not normalised.attempt_id:
        raise CommitError("CommitRequest.attempt_id 不能为空")
    return normalised, requested_kind


def _preconditions_payload(
    request: contract.CommitRequest, overrides: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    payload = dict(request.preconditions or {})
    if overrides:
        payload.update(dict(overrides))
    return payload


def _check_preconditions(
    root: Path,
    candidate_targets: list[_Candidate],
    preconditions: Mapping[str, Any],
    operations_root: Path,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    must_not_exist = preconditions.get("must_not_exist") or []
    for value in must_not_exist:
        try:
            _path, resolved = _normalise_relative_target(root, value)
        except CommitError as exc:
            issues.append(
                {
                    "code": "malformed_precondition",
                    "message": str(exc),
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": value},
                }
            )
            continue
        if resolved.exists():
            issues.append(
                {
                    "code": "precondition_failed",
                    "message": f"前置条件要求路径不存在：{value}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": value, "precondition": "must_not_exist"},
                }
            )
    for value in preconditions.get("require_exists") or []:
        try:
            _path, resolved = _normalise_relative_target(root, value)
        except CommitError as exc:
            issues.append(
                {
                    "code": "malformed_precondition",
                    "message": str(exc),
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": value},
                }
            )
            continue
        if not resolved.exists():
            issues.append(
                {
                    "code": "precondition_failed",
                    "message": f"前置条件要求路径存在：{value}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"path": value, "precondition": "require_exists"},
                }
            )
    expected_revision = preconditions.get("expected_current_revision")
    if expected_revision is not None:
        current = _read_current_revision(operations_root)
        if str(expected_revision) != str(current):
            issues.append(
                {
                    "code": "current_revision_mismatch",
                    "message": f"期望当前 revision={expected_revision!r}，实际={current!r}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {"expected": expected_revision, "actual": current},
                }
            )
    target_absent = preconditions.get("target_absent", True)
    if not target_absent:
        issues.append(
            {
                "code": "unsupported_precondition",
                "message": "V1 不支持覆盖已存在的正式路径（target_absent=false）",
                "severity": ISSUE_ERROR,
                "retryable": False,
                "details": {"precondition": "target_absent"},
            }
        )
    return issues


def _read_current_revision(operations_root: Path) -> Optional[str]:
    path = operations_root / CURRENT_REVISION_FILE
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(payload, dict):
        return payload.get("revision")
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def _target_key(target_path: str) -> str:
    return hashlib.sha256(target_path.encode("utf-8")).hexdigest()


def _target_lock_path(operations_root: Path, target_path: str) -> Path:
    return operations_root / TARGET_LOCK_DIR / f"{_target_key(target_path)}.lock"


def _target_claim_path(operations_root: Path, target_path: str) -> Path:
    return operations_root / TARGET_CLAIM_DIR / f"{_target_key(target_path)}.json"


class TargetLockSet:
    """Acquire one stable O_EXCL reservation per target path.

    Multiple operations with overlapping targets serialize here.  Targets are
    always acquired in sorted path order, so two multi-target commits cannot
    deadlock each other.  Windows ``O_CREAT|O_EXCL`` is atomic; stale locks are
    reclaimed by age like :class:`OperationLock`.
    """

    def __init__(
        self,
        operations_root: Path,
        target_paths: list[str],
        *,
        operation_id: str,
        timeout_seconds: float = 10.0,
        stale_seconds: float = 300.0,
        poll_seconds: float = 0.02,
    ):
        self.operations_root = operations_root
        self.target_paths = sorted(set(target_paths))
        self.operation_id = operation_id
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.poll_seconds = poll_seconds
        self.paths: list[Path] = []
        self._acquired: list[Path] = []

    def _stale(self, path: Path) -> bool:
        try:
            return time.time() - path.stat().st_mtime > self.stale_seconds
        except OSError:
            return False

    def acquire(self) -> None:
        try:
            self._acquire_all()
        except BaseException:
            self.release()
            raise

    def _acquire_all(self) -> None:
        deadline = time.monotonic() + self.timeout_seconds
        for target_path in self.target_paths:
            path = _target_lock_path(self.operations_root, target_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            while True:
                try:
                    descriptor = os.open(
                        str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644
                    )
                except FileExistsError:
                    if self._stale(path):
                        try:
                            path.unlink()
                        except OSError:
                            pass
                        continue
                    if time.monotonic() >= deadline:
                        raise CommitLockTimeout(
                            f"等待目标锁超时：{target_path}（operation={self.operation_id}）"
                        )
                    time.sleep(self.poll_seconds)
                    continue
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(
                            {
                                "target_path": target_path,
                                "operation_id": self.operation_id,
                                "pid": os.getpid(),
                                "created_at": _utc_now(),
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                self._acquired.append(path)
                break

    def release(self) -> None:
        while self._acquired:
            path = self._acquired.pop()
            try:
                path.unlink()
            except OSError:
                pass

    def __enter__(self) -> "TargetLockSet":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _read_claim(operations_root: Path, target_path: str) -> Optional[dict[str, Any]]:
    path = _target_claim_path(operations_root, target_path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _write_claim(
    operations_root: Path,
    target_path: str,
    operation_id: str,
    sha256: str,
) -> None:
    path = _target_claim_path(operations_root, target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_io.atomic_write_json(
        path,
        {
            "schema_version": "le-target-claim-v1",
            "target_path": target_path,
            "operation_id": operation_id,
            "sha256": sha256,
            "created_at": _utc_now(),
        },
    )


def _remove_claim(operations_root: Path, target_path: str) -> None:
    try:
        _target_claim_path(operations_root, target_path).unlink()
    except OSError:
        pass


def _claim_owner(operations_root: Path, target_path: str) -> Optional[str]:
    claim = _read_claim(operations_root, target_path)
    if claim:
        owner = claim.get("operation_id")
        if isinstance(owner, str) and owner:
            return owner
    return None


def _rollback_created_targets(
    repo_root: Path,
    operations_root: Path,
    candidates: list[_Candidate],
    operation_id: str,
) -> list[str]:
    """Remove targets this call created, only while they still match our sha.

    Never touches a target claimed/recorded by another operation, and never
    removes a directory/file whose bytes changed since publication.
    """

    removed: list[str] = []
    for candidate in reversed(candidates):
        owner = _target_owner(repo_root, operations_root, [candidate], operation_id)
        claim_owner = _claim_owner(operations_root, candidate.target_path)
        if owner or (claim_owner and claim_owner != operation_id):
            continue
        try:
            if candidate.kind == "dir" and candidate.target_abs.is_dir():
                if atomic_io.tree_sha256(candidate.target_abs) == candidate.sha256:
                    shutil.rmtree(candidate.target_abs)
                    removed.append(candidate.target_path)
            elif candidate.kind == "file" and candidate.target_abs.is_file():
                if atomic_io.sha256_file(candidate.target_abs) == candidate.sha256:
                    candidate.target_abs.unlink()
                    removed.append(candidate.target_path)
        except OSError:
            pass
        _remove_claim(operations_root, candidate.target_path)
    return removed


def _intent_path(operations_root: Path, operation_id: str) -> Path:
    return operations_root / INTENT_DIR / f"{operation_file_stem(operation_id)}.json"


def _read_intent(
    operations_root: Path,
    operation_id: str,
    *,
    expected_target_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    path = _intent_path(operations_root, operation_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OperationStoreError(f"operation intent 损坏：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise OperationStoreError(f"operation intent 损坏：{path}")
    if payload.get("schema_version") != INTENT_SCHEMA:
        raise OperationStoreError("operation intent schema_version 不受支持")
    if payload.get("operation_id") != operation_id:
        raise OperationStoreError("operation intent operation_id 不匹配")
    if not isinstance(payload.get("input_fingerprint"), str):
        raise OperationStoreError("operation intent 缺少 input_fingerprint")
    candidates = payload.get("candidate_refs")
    if not isinstance(candidates, list) or not candidates:
        raise OperationStoreError("operation intent 缺少 candidate_refs")
    for ref in candidates:
        if not isinstance(ref, dict):
            raise OperationStoreError("operation intent candidate_refs 非法")
        if not isinstance(ref.get("path"), str) or not isinstance(ref.get("sha256"), str):
            raise OperationStoreError("operation intent candidate_ref 缺少 path/sha256")
        if ref.get("kind") not in ("file", "dir"):
            raise OperationStoreError("operation intent candidate_ref.kind 非法")
    top_target = payload.get("target_path")
    if top_target is not None:
        if not isinstance(top_target, str) or not top_target:
            raise OperationStoreError("operation intent target_path 非法")
        if len(candidates) != 1 or candidates[0].get("path") != top_target:
            raise OperationStoreError(
                "operation intent target_path 与 candidate_refs[0].path 不一致"
            )
    if expected_target_path is not None:
        if len(candidates) != 1 or candidates[0].get("path") != expected_target_path:
            raise OperationStoreError(
                "operation intent candidate path 与当前指针目标不一致（疑似路径重定向）"
            )
        if top_target is not None and top_target != expected_target_path:
            raise OperationStoreError(
                "operation intent target_path 与当前指针目标不一致（疑似路径重定向）"
            )
    return payload


def _write_intent(
    operations_root: Path,
    request: contract.CommitRequest,
    candidates: list[_Candidate],
    timestamp: str,
    previous_sha256: Optional[str] = None,
) -> dict[str, str]:
    path = _intent_path(operations_root, request.operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": INTENT_SCHEMA,
        "operation_id": request.operation_id,
        "attempt_id": request.attempt_id,
        "operation_kind": request.operation_kind,
        "input_fingerprint": request.input_fingerprint,
        "candidate_refs": [
            {
                "candidate_id": candidate.candidate_id,
                "kind": candidate.kind,
                "path": candidate.target_path,
                "sha256": candidate.sha256,
                "artifact_id": candidate.artifact_id,
            }
            for candidate in candidates
        ],
        "created_at": timestamp,
        **({"target_path": candidates[0].target_path} if len(candidates) == 1 else {}),
        **({"previous_sha256": previous_sha256} if previous_sha256 else {}),
    }
    return atomic_io.atomic_write_json(path, payload)


def _remove_intent(operations_root: Path, operation_id: str) -> None:
    try:
        _intent_path(operations_root, operation_id).unlink()
    except OSError:
        pass


def _intent_candidates(
    repo_root: Path, intent: Mapping[str, Any]
) -> list[tuple[str, str, str, Path, str, Optional[str]]]:
    rows = []
    for raw in intent.get("candidate_refs") or []:
        target_path, target_abs = _normalise_relative_target(repo_root, raw.get("path"))
        rows.append(
            (
                str(raw.get("candidate_id") or "candidate"),
                str(raw.get("kind")),
                target_path,
                target_abs,
                str(raw.get("sha256")),
                raw.get("artifact_id"),
            )
        )
    return rows


def _verify_intent_targets(repo_root: Path, intent: Mapping[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for candidate_id, kind, target_path, target_abs, expected, _artifact in _intent_candidates(
        repo_root, intent
    ):
        if kind == "dir":
            if not target_abs.is_dir():
                issues.append(
                    {
                        "code": "published_artifact_missing",
                        "message": f"发布意图中的正式目录不存在：{target_path}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {"path": target_path, "candidate_id": candidate_id},
                    }
                )
                continue
            actual = atomic_io.tree_sha256(target_abs)
        else:
            if not target_abs.is_file():
                issues.append(
                    {
                        "code": "published_artifact_missing",
                        "message": f"发布意图中的正式文件不存在：{target_path}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {"path": target_path, "candidate_id": candidate_id},
                    }
                )
                continue
            actual = atomic_io.sha256_file(target_abs)
        if actual != expected:
            issues.append(
                {
                    "code": "intent_target_mismatch",
                    "message": f"正式目标与发布意图摘要不一致：{target_path}",
                    "severity": ISSUE_ERROR,
                    "retryable": False,
                    "details": {
                        "path": target_path,
                        "expected": expected,
                        "actual": actual,
                        "candidate_id": candidate_id,
                    },
                }
            )
    return issues


def _record_from_intent(
    repo_root: Path,
    request: contract.CommitRequest,
    requested_kind: Optional[str],
    intent: Mapping[str, Any],
    preconditions: Mapping[str, Any],
    timestamp: str,
) -> dict[str, Any]:
    published_refs = []
    candidates: list[_Candidate] = []
    for candidate_id, kind, target_path, target_abs, sha, artifact in _intent_candidates(
        repo_root, intent
    ):
        # target_abs was resolved against the caller's root in the caller; only
        # public refs are needed here, candidate paths are not used.
        published_refs.append(
            {
                "path": target_path,
                "sha256": sha,
                "kind": kind,
                "candidate_id": candidate_id,
                **({"artifact_id": artifact} if artifact else {}),
            }
        )
        candidates.append(
            _Candidate(
                candidate_id=candidate_id,
                kind=kind,
                candidate_path=target_abs,
                target_path=target_path,
                target_abs=target_abs,
                sha256=sha,
                artifact_id=artifact,
            )
        )
    record = _build_record(
        request,
        requested_kind,
        candidates,
        preconditions,
        timestamp,
        published_refs=published_refs,
        recovered=True,
    )
    return record


def _is_pointer_operation(operation_kind: str) -> bool:
    return operation_kind == "update_pointer"


def _validate_pointer_target(target_path: str) -> None:
    if target_path == "data/current.json":
        return
    if _POINTER_BUILD_RE.match(target_path):
        return
    raise CommitError(f"update_pointer 目标不在白名单：{target_path!r}")


def _read_pointer_document(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommitError(f"指针文件不是合法 JSON：{path}：{exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise CommitError(f"指针文件必须是完整 JSON 对象：{path}")
    return payload


def _validate_pointer_candidate(path: Path) -> None:
    _read_pointer_document(path)


def _pointer_current_sha(path: Path) -> str:
    return atomic_io.sha256_file(path)


def _pointer_expected_value(
    request: contract.CommitRequest, preconditions: Mapping[str, Any]
) -> str:
    expected = preconditions.get("expected_current_sha256")
    if not expected:
        expected = request.expected_current_revision or preconditions.get("expected_current_revision")
    if not isinstance(expected, str) or not expected.strip():
        raise CommitError("update_pointer 需要 expected_current_revision 或 expected_current_sha256")
    return expected.strip()


def _parse_pointer_bytes(data: bytes, label: object = "pointer") -> dict[str, Any]:
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CommitError(f"指针文件不是合法 JSON：{label}：{exc}") from exc
    if not isinstance(document, dict) or not document:
        raise CommitError(f"指针文件必须是完整 JSON 对象：{label}")
    return document


def _pointer_expected_matches_bytes(
    expected: str, data: bytes, document: dict[str, Any]
) -> bool:
    if re.match(r"^[0-9a-f]{64}$", expected):
        return hashlib.sha256(data).hexdigest() == expected
    keys = (
        "dataset_id", "revision_id", "build_id", "revision", "content_sha256",
        "file_sha256", "logical_content_sha256", "share_id",
    )
    for key in keys:
        if str(document.get(key)) == expected:
            return True
    return False


_POINTER_SNAPSHOT = threading.local()


def _stash_pointer_snapshot(data: bytes, document: dict[str, Any], target: Path) -> str:
    sha = hashlib.sha256(data).hexdigest()
    _POINTER_SNAPSHOT.data = data
    _POINTER_SNAPSHOT.document = document
    _POINTER_SNAPSHOT.target = str(target)
    _POINTER_SNAPSHOT.sha256 = sha
    return sha


def _stashed_pointer_sha(target: Path) -> Optional[str]:
    stashed = getattr(_POINTER_SNAPSHOT, "sha256", None)
    if stashed and getattr(_POINTER_SNAPSHOT, "target", None) == str(target):
        return stashed
    return None


def _clear_pointer_snapshot() -> None:
    for name in ("data", "document", "target", "sha256"):
        try:
            delattr(_POINTER_SNAPSHOT, name)
        except AttributeError:
            pass


def _pointer_expected_matches(expected: str, target: Path) -> bool:
    """Single-read check hook used by the review race scripts.

    If the caller pre-read the pointer bytes (production flow), that exact
    snapshot is used, so a paused writer cannot later resample a new pointer.
    """

    stashed_sha = _stashed_pointer_sha(target)
    if stashed_sha is not None:
        data = _POINTER_SNAPSHOT.data
        document = _POINTER_SNAPSHOT.document
    else:
        if not target.is_file():
            return False
        try:
            data = target.read_bytes()
            document = _parse_pointer_bytes(data, target)
        except (OSError, CommitError):
            return False
        _stash_pointer_snapshot(data, document, target)
    return _pointer_expected_matches_bytes(expected, data, document)


def _intent_target_ref(intent: Mapping[str, Any], target_path: str) -> Optional[dict[str, Any]]:
    for ref in intent.get("candidate_refs") or []:
        if isinstance(ref, dict) and str(ref.get("path") or "") == target_path:
            return ref
    return None


def _intent_recovery_request(
    intent: Mapping[str, Any], target_ref: Mapping[str, Any]
) -> contract.CommitRequest:
    return contract.CommitRequest(
        operation_id=str(intent["operation_id"]),
        attempt_id=str(intent.get("attempt_id") or "recovered"),
        operation_kind=str(intent.get("operation_kind") or "update_pointer"),
        input_fingerprint=str(intent["input_fingerprint"]),
        candidate_refs=[
            {
                "candidate_id": target_ref.get("candidate_id"),
                "kind": target_ref.get("kind") or "file",
                "path": target_ref.get("path"),
                "sha256": target_ref.get("sha256"),
                "artifact_id": target_ref.get("artifact_id"),
            }
        ],
        preconditions={},
    )


def _recover_pending_pointer_intents(
    repo_root: Path,
    operations_root: Path,
    *,
    target_path: str,
    current_sha: str,
    except_operation_id: str,
    lock_timeout_seconds: float,
    now: str,
) -> Optional[contract.CommitResult]:
    """Preflight: recover other operations' pointer intents matching current bytes.

    Returns a failed CommitResult when a same-target intent is corrupt or can
    neither be recovered nor safely classified; the caller must not advance.
    """

    intent_dir = operations_root / INTENT_DIR
    if not intent_dir.is_dir():
        return None
    pending: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    cleanups: list[str] = []
    problems: list[dict[str, Any]] = []
    for entry in sorted(intent_dir.glob("*.json"), key=lambda item: item.name):
        try:
            payload = json.loads(entry.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(
                {
                    "code": "intent_corrupt",
                    "message": f"同目标 pointer intent 损坏：{entry.name}：{exc}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"intent_file": entry.name},
                }
            )
            continue
        if not isinstance(payload, dict):
            problems.append(
                {
                    "code": "intent_corrupt",
                    "message": f"同目标 pointer intent 不是对象：{entry.name}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"intent_file": entry.name},
                }
            )
            continue
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            problems.append(
                {
                    "code": "intent_corrupt",
                    "message": f"同目标 pointer intent 缺少 operation_id：{entry.name}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"intent_file": entry.name},
                }
            )
            continue
        if operation_id == except_operation_id:
            continue
        # Fail-closed target binding: examine every pointer intent that either
        # names the current target at top level OR references it in a candidate
        # path.  A redirected/mismatched intent must block B rather than being
        # silently filtered out.
        top_target = payload.get("target_path")
        raw_candidates = payload.get("candidate_refs")
        if not isinstance(raw_candidates, list) or not raw_candidates:
            problems.append(
                {
                    "code": "intent_corrupt",
                    "message": f"pointer intent 缺少 candidate_refs：{entry.name}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"intent_file": entry.name, "operation_id": operation_id},
                }
            )
            continue

        def _path_of(ref: Any) -> Optional[str]:
            return ref.get("path") if isinstance(ref, dict) else None

        candidate_paths = [_path_of(ref) for ref in raw_candidates]
        relevant = top_target == target_path or target_path in candidate_paths
        if not relevant:
            # Legacy intents without top-level target can only be associated via
            # candidate path; unrelated candidate paths are intentionally skipped
            # (limitation: cannot prove they belong to this pointer).
            continue

        bad_reason: Optional[str] = None
        if payload.get("schema_version") != INTENT_SCHEMA:
            bad_reason = "intent schema_version 不受支持"
        elif payload.get("operation_kind") != "update_pointer":
            bad_reason = "intent operation_kind 不是 update_pointer"
        elif not isinstance(payload.get("input_fingerprint"), str):
            bad_reason = "intent 缺少 input_fingerprint"
        elif len(raw_candidates) != 1:
            bad_reason = "pointer intent 必须且只能有一个 candidate_ref"
        elif not all(isinstance(ref, dict) for ref in raw_candidates):
            bad_reason = "intent candidate_refs 结构非法"
        elif not isinstance(candidate_paths[0], str) or not candidate_paths[0]:
            bad_reason = "intent candidate path 非法"
        elif not isinstance(raw_candidates[0].get("sha256"), str) or not re.match(
            r"^[0-9a-f]{64}$", str(raw_candidates[0].get("sha256"))
        ):
            bad_reason = "intent candidate sha256 非法"
        elif raw_candidates[0].get("kind") != "file":
            bad_reason = "pointer intent candidate kind 必须为 file"
        elif top_target is not None:
            if not isinstance(top_target, str) or not top_target:
                bad_reason = "intent target_path 非法"
            elif top_target != candidate_paths[0]:
                bad_reason = "intent target_path 与 candidate path 不一致"
            elif top_target != target_path:
                bad_reason = "intent target_path 指向其他目标"
        else:
            # Legacy compatibility: no top-level target_path; require the sole
            # candidate path to be exactly the current target.
            if candidate_paths[0] != target_path:
                bad_reason = "legacy intent candidate path 与当前目标不一致"
        if candidate_paths[0] != target_path:
            bad_reason = bad_reason or "intent candidate path 指向其他目标"
        previous_sha = payload.get("previous_sha256")
        if previous_sha is not None and not isinstance(previous_sha, str):
            bad_reason = bad_reason or "intent previous_sha256 非法"

        if bad_reason:
            problems.append(
                {
                    "code": "intent_target_mismatch",
                    "message": (
                        f"同目标 pointer intent 被重定向或自相矛盾，拒绝恢复：{entry.name}：{bad_reason}"
                    ),
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {
                        "intent_file": entry.name,
                        "operation_id": operation_id,
                        "target_path": top_target,
                        "candidate_paths": candidate_paths,
                        "current_target": target_path,
                    },
                }
            )
            continue

        target_ref = raw_candidates[0]
        try:
            record = read_operation_record(operation_id, operations_root)
        except OperationStoreError as exc:
            problems.append(
                {
                    "code": "pending_pointer_recovery",
                    "message": f"待恢复 pointer 操作记录损坏：{operation_id}：{exc}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"operation_id": operation_id},
                }
            )
            continue
        if record is not None:
            cleanups.append(operation_id)
            continue
        ref_sha = str(target_ref.get("sha256") or "")
        if ref_sha == current_sha:
            pending.append((operation_id, payload, target_ref))
            continue
        if payload.get("previous_sha256") == current_sha:
            cleanups.append(operation_id)
            continue
        problems.append(
            {
                "code": "intent_target_mismatch",
                "message": (
                    f"同目标 pointer intent 与当前内容不符，拒绝自动恢复：{operation_id}"
                ),
                "severity": ISSUE_ERROR,
                "retryable": True,
                "details": {
                    "operation_id": operation_id,
                    "intent_sha256": ref_sha,
                    "current_sha256": current_sha,
                },
            }
        )

    if problems:
        return contract.CommitResult(
            operation_id=except_operation_id,
            attempt_id="pending-recovery",
            status=contract.COMMIT_FAILED,
            issues=[
                {
                    "code": "pending_pointer_recovery",
                    "message": "存在需要人工/重试处理的同目标 pointer 意图，暂不推进",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"problems": problems},
                }
            ],
        )

    # Clean stale intents whose operation record already exists or that are
    # provably pre-publish (target still previous sha).
    for operation_id in cleanups:
        lock = OperationLock(
            operations_root,
            operation_id,
            timeout_seconds=min(0.2, max(0.02, lock_timeout_seconds)),
        )
        try:
            lock.acquire()
        except CommitLockTimeout:
            # Housekeeping only: the owner is currently active (possibly paused
            # inside its own publish).  Skip cleanup; it will clean its own
            # intent on success, or be classified as pending next time.
            continue
        try:
            # The scan is only a hint.  The owner may have published while we
            # waited for its lock.  Reclassify under both the operation lock
            # and the same target mutex used by atomic_replace_file.
            target_abs = repo_root / Path(target_path)
            try:
                with atomic_io._PublishLock(
                    target_abs, timeout_seconds=lock_timeout_seconds
                ):
                    record_now = read_operation_record(operation_id, operations_root)
                    intent_now = _read_intent(
                        operations_root, operation_id,
                        expected_target_path=target_path,
                    )
                    if record_now is not None or intent_now is None:
                        _remove_intent(operations_root, operation_id)
                    elif intent_now.get("previous_sha256") == atomic_io.sha256_file(target_abs):
                        _remove_intent(operations_root, operation_id)
                    else:
                        # Keep the recovery evidence; a retry can classify this
                        # as published and complete its operation record.
                        raise OperationStoreError("pointer changed while waiting to clean intent")
            except (OSError, OperationStoreError) as exc:
                return contract.CommitResult(
                    operation_id=except_operation_id,
                    attempt_id="pending-recovery",
                    status=contract.COMMIT_FAILED,
                    issues=[{
                        "code": "pending_pointer_recovery",
                        "message": f"pointer 意图清理状态已变化，保留意图等待恢复：{exc}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {"operation_id": operation_id},
                    }],
                )
        finally:
            lock.release()

    # Recover each matching pending intent under its own operation lock; write
    # the permanent record, then remove the intent.
    for operation_id, payload, target_ref in pending:
        lock = OperationLock(
            operations_root,
            operation_id,
            timeout_seconds=lock_timeout_seconds,
        )
        try:
            lock.acquire()
        except CommitLockTimeout as exc:
            return contract.CommitResult(
                operation_id=except_operation_id,
                attempt_id="pending-recovery",
                status=contract.COMMIT_FAILED,
                issues=[
                    {
                        "code": "pending_pointer_recovery",
                        "message": f"恢复 pointer 记录锁超时：{operation_id}：{exc}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {"operation_id": operation_id},
                    }
                ],
            )
        try:
            try:
                intent_now = _read_intent(operations_root, operation_id)
                record_now = read_operation_record(operation_id, operations_root)
            except OperationStoreError as exc:
                return contract.CommitResult(
                    operation_id=except_operation_id,
                    attempt_id="pending-recovery",
                    status=contract.COMMIT_FAILED,
                    issues=[
                        {
                            "code": "pending_pointer_recovery",
                            "message": f"待恢复意图/记录损坏：{operation_id}：{exc}",
                            "severity": ISSUE_ERROR,
                            "retryable": True,
                            "details": {"operation_id": operation_id},
                        }
                    ],
                )
            if record_now is not None:
                _remove_intent(operations_root, operation_id)
                continue
            if intent_now is None:
                continue
            ref_now = _intent_target_ref(intent_now, target_path)
            if ref_now is None or str(ref_now.get("sha256") or "") != current_sha:
                return contract.CommitResult(
                    operation_id=except_operation_id,
                    attempt_id="pending-recovery",
                    status=contract.COMMIT_FAILED,
                    issues=[
                        {
                            "code": "pending_pointer_recovery",
                            "message": f"待恢复意图在锁内已变化：{operation_id}",
                            "severity": ISSUE_ERROR,
                            "retryable": True,
                            "details": {"operation_id": operation_id},
                        }
                    ],
                )
            target_abs = repo_root / Path(target_path)
            try:
                if atomic_io.sha256_file(target_abs) != current_sha:
                    raise OSError("pointer target changed before recovery")
                recovery_request = _intent_recovery_request(intent_now, ref_now)
                recovery_record = _record_from_intent(
                    repo_root, recovery_request, None, intent_now, {}, now
                )
                _write_operation_record(operation_id, operations_root, recovery_record)
                _remove_intent(operations_root, operation_id)
            except Exception as exc:
                return contract.CommitResult(
                    operation_id=except_operation_id,
                    attempt_id="pending-recovery",
                    status=contract.COMMIT_FAILED,
                    issues=[
                        {
                            "code": "pending_pointer_recovery",
                            "message": f"恢复 pointer 操作记录失败：{operation_id}：{exc}",
                            "severity": ISSUE_ERROR,
                            "retryable": True,
                            "details": {"operation_id": operation_id},
                        }
                    ],
                )
        finally:
            lock.release()
    return None


def _pointer_result_failed(
    operation_id: str, attempt_id: str, code: str, message: str, details: Mapping[str, Any] | None = None
) -> contract.CommitResult:
    return contract.CommitResult(
        operation_id=operation_id,
        attempt_id=attempt_id,
        status=contract.COMMIT_FAILED,
        issues=[
            {
                "code": code,
                "message": message,
                "severity": ISSUE_ERROR,
                "retryable": False,
                "details": dict(details or {}),
            }
        ],
    )


def commit_operation(
    request: "contract.CommitRequest | Mapping[str, Any]",
    operations_root: str | Path | None = None,
    *,
    root: str | Path | None = None,
    repo_root: str | Path | None = None,
    operation_store_root: str | Path | None = None,
    preconditions: Optional[Mapping[str, Any]] = None,
    lock_timeout_seconds: float = 10.0,
    stale_lock_seconds: float = 300.0,
    target_lock_timeout_seconds: float = 30.0,
    now: Optional[datetime] = None,
    op_store_root: str | Path | None = None,
) -> contract.CommitResult:
    """Commit one logical operation idempotently.

    Never raises for business conflicts; returns a :class:`CommitResult` with
    ``committed`` / ``replayed`` / ``conflict`` / ``failed``.
    """

    repo_root = _resolve_root(root if root is not None else repo_root)
    if operations_root is None:
        operations_root = operation_store_root or op_store_root or "data/cache/operations"
    ops_root = _resolve_operations_root(operations_root, repo_root)
    normalised, requested_kind = _normalise_request(request)
    operation_id = normalised.operation_id
    candidates = [
        _normalise_candidate(repo_root, ref, index)
        for index, ref in enumerate(normalised.candidate_refs)
    ]
    if not candidates:
        raise CommitError("CommitRequest.candidate_refs 不能为空")
    pointer_mode = _is_pointer_operation(normalised.operation_kind)
    if pointer_mode:
        if len(candidates) != 1:
            return _pointer_result_failed(
                normalised.operation_id,
                normalised.attempt_id,
                "invalid_pointer_request",
                "update_pointer 只允许一个 candidate_ref",
            )
        try:
            _validate_pointer_target(candidates[0].target_path)
        except CommitError as exc:
            return _pointer_result_failed(
                normalised.operation_id, normalised.attempt_id,
                "pointer_target_not_allowed", str(exc),
                {"path": candidates[0].target_path},
            )
        try:
            _validate_pointer_candidate(candidates[0].candidate_path)
        except CommitError as exc:
            return _pointer_result_failed(
                normalised.operation_id, normalised.attempt_id,
                "invalid_pointer_document", str(exc),
                {"path": candidates[0].target_path},
            )
    merged_preconditions = _preconditions_payload(normalised, preconditions)
    timestamp = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()

    lock = OperationLock(
        ops_root,
        operation_id,
        timeout_seconds=lock_timeout_seconds,
        stale_seconds=stale_lock_seconds,
    )
    try:
        lock.acquire()
    except CommitLockTimeout as exc:
        return contract.CommitResult(
            operation_id=operation_id,
            attempt_id=normalised.attempt_id,
            status=contract.COMMIT_FAILED,
            issues=[
                {
                    "code": "operation_locked",
                    "message": f"等待 operation 锁超时：{exc}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"operation_id": operation_id},
                }
            ],
        )
    except OSError as exc:
        return contract.CommitResult(
            operation_id=operation_id,
            attempt_id=normalised.attempt_id,
            status=contract.COMMIT_FAILED,
            issues=[
                {
                    "code": "operation_lock_failed",
                    "message": f"无法获取 operation 锁：{exc}",
                    "severity": ISSUE_ERROR,
                    "retryable": True,
                    "details": {"operation_id": operation_id},
                }
            ],
        )
    try:
        try:
            record = read_operation_record(operation_id, ops_root)
        except OperationStoreError as exc:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                committed_refs=[],
                current_revision=None,
                issues=[
                    {
                        "code": "operation_record_corrupt",
                        "message": str(exc),
                        "severity": ISSUE_ERROR,
                        "retryable": False,
                        "details": {},
                    }
                ],
            )

        if record is not None:
            if str(record.get("input_fingerprint")) != normalised.input_fingerprint:
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_CONFLICT,
                    committed_refs=list(record.get("published_refs") or []),
                    current_revision=record.get("current_revision"),
                    issues=[
                        {
                            "code": "input_fingerprint_conflict",
                            "message": "同一 operation_id 的输入指纹不同，拒绝覆盖",
                            "severity": ISSUE_ERROR,
                            "retryable": False,
                            "details": {
                                "existing": record.get("input_fingerprint"),
                                "incoming": normalised.input_fingerprint,
                            },
                        }
                    ],
                )
            if pointer_mode:
                refs = list(record.get("published_refs") or [])
                ref = refs[0] if refs else {}
                target_path = _record_display_path(ref)
                target_abs = repo_root / Path(target_path) if target_path else None
                if not target_abs or not target_abs.is_file():
                    return contract.CommitResult(
                        operation_id=operation_id,
                        attempt_id=normalised.attempt_id,
                        status=contract.COMMIT_FAILED,
                        committed_refs=refs,
                        current_revision=record.get("current_revision"),
                        issues=[
                            {
                                "code": "published_artifact_missing",
                                "message": f"指针操作记录对应的正式指针缺失：{target_path}",
                                "severity": ISSUE_ERROR,
                                "retryable": False,
                                "details": {"path": target_path},
                            }
                        ],
                    )
                try:
                    current_bytes = target_abs.read_bytes()
                    current_document = _parse_pointer_bytes(current_bytes, target_abs)
                except (OSError, CommitError) as exc:
                    return contract.CommitResult(
                        operation_id=operation_id,
                        attempt_id=normalised.attempt_id,
                        status=contract.COMMIT_FAILED,
                        committed_refs=refs,
                        current_revision=record.get("current_revision"),
                        issues=[
                            {
                                "code": "pointer_target_corrupt",
                                "message": f"当前指针无法校验：{exc}",
                                "severity": ISSUE_ERROR,
                                "retryable": False,
                                "details": {"path": target_path},
                            }
                        ],
                    )
                current_sha = hashlib.sha256(current_bytes).hexdigest()
                advanced = current_sha != str(ref.get("sha256") or "")
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_REPLAYED,
                    committed_refs=refs,
                    current_revision=record.get("current_revision"),
                    issues=[
                        {
                            "code": (
                                "pointer_advanced_after_commit"
                                if advanced else "operation_replayed"
                            ),
                            "message": (
                                "指针已在本次操作后合法推进；旧操作重放不覆盖当前指针"
                                if advanced else "同一操作与输入已提交，指针内容一致"
                            ),
                            "severity": ISSUE_WARNING if advanced else ISSUE_INFO,
                            "retryable": False,
                            "details": {
                                "recorded_sha256": ref.get("sha256"),
                                "current_sha256": current_sha,
                                "current_dataset_id": current_document.get("dataset_id"),
                                "current_revision_id": current_document.get("revision_id"),
                            },
                        }
                    ],
                )
            problems: list[dict[str, Any]] = []
            for ref in record.get("published_refs") or []:
                problems.extend(_verify_published_ref(repo_root, ref))
            if problems:
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_FAILED,
                    committed_refs=list(record.get("published_refs") or []),
                    current_revision=record.get("current_revision"),
                    issues=problems,
                )
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_REPLAYED,
                committed_refs=list(record.get("published_refs") or []),
                current_revision=record.get("current_revision"),
                issues=[
                    {
                        "code": "operation_replayed",
                        "message": "同一操作与输入已提交，校验正式产物后返回",
                        "severity": ISSUE_INFO,
                        "retryable": False,
                        "details": {"attempts": [normalised.attempt_id]},
                    }
                ],
            )

        precondition_input = dict(merged_preconditions)
        if pointer_mode:
            # Pointer updates validate expected_current_revision against the
            # pointer file itself below, not the operations revision marker.
            precondition_input.pop("expected_current_revision", None)
        precondition_issues = _check_preconditions(
            repo_root, candidates, precondition_input, ops_root
        )
        if precondition_issues:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                issues=precondition_issues,
            )

        _cleanup_candidate_temps(candidates, operation_file_stem(operation_id))

        # Legacy/foreign target reservation compatibility: external tooling may
        # still create the old `.target-locks/<hash>.lock` file.  Our new
        # publish mutex is inside atomic_io; this only reports a foreign hold.
        for candidate in candidates:
            legacy_lock = _target_lock_path(ops_root, candidate.target_path)
            if legacy_lock.exists():
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_FAILED,
                    issues=[
                        {
                            "code": "target_locked",
                            "message": f"目标正被其他操作预留：{candidate.target_path}",
                            "severity": ISSUE_ERROR,
                            "retryable": True,
                            "details": {"path": candidate.target_path},
                        }
                    ],
                )

        owner = _target_owner(repo_root, ops_root, candidates, operation_id)
        if owner and not pointer_mode:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                issues=[
                    {
                        "code": "target_exists",
                        "message": f"正式目标已由其他操作发布：{owner}",
                        "severity": ISSUE_ERROR,
                        "retryable": False,
                        "details": {"owner_operation_id": owner},
                    }
                ],
            )

        # R2: a publish intent is written before the formal target; it records
        # the exact tree/file sha that is about to be published.  This is the
        # authoritative recovery source when the record write fails afterwards
        # (the retry's candidate tree may differ in timestamps/metadata).
        try:
            intent = _read_intent(
                ops_root,
                operation_id,
                expected_target_path=(
                    candidates[0].target_path if pointer_mode else None
                ),
            )
        except OperationStoreError as exc:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                issues=[
                    {
                        "code": "intent_corrupt",
                        "message": str(exc),
                        "severity": ISSUE_ERROR,
                        "retryable": False,
                        "details": {},
                    }
                ],
            )
        if intent is not None:
            if intent["input_fingerprint"] != normalised.input_fingerprint:
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_CONFLICT,
                    committed_refs=[],
                    issues=[
                        {
                            "code": "input_fingerprint_conflict",
                            "message": "同一 operation_id 的发布意图输入指纹不同，拒绝覆盖",
                            "severity": ISSUE_ERROR,
                            "retryable": False,
                            "details": {
                                "existing": intent["input_fingerprint"],
                                "incoming": normalised.input_fingerprint,
                            },
                        }
                    ],
                )
            try:
                intent_rows = _intent_candidates(repo_root, intent)
                any_target = any(
                    target_abs.exists()
                    for _id, _kind, _path, target_abs, _sha, _art in intent_rows
                )
            except CommitError as exc:
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_FAILED,
                    issues=[
                        {
                            "code": "intent_corrupt",
                            "message": f"operation intent 目标路径非法：{exc}",
                            "severity": ISSUE_ERROR,
                            "retryable": False,
                            "details": {},
                        }
                    ],
                )
            if pointer_mode and intent is not None and any_target:
                pointer_row = next(
                    row for row in intent_rows if row[3].exists()
                )
                intended_sha = pointer_row[4]
                current_sha = _pointer_current_sha(pointer_row[3])
                if current_sha != intended_sha:
                    previous_sha = intent.get("previous_sha256")
                    if previous_sha and current_sha == previous_sha:
                        _remove_intent(ops_root, operation_id)
                        intent = None
                        any_target = False
            if intent is not None and any_target:
                problems = _verify_intent_targets(repo_root, intent)
                if problems:
                    return contract.CommitResult(
                        operation_id=operation_id,
                        attempt_id=normalised.attempt_id,
                        status=contract.COMMIT_FAILED,
                        issues=problems,
                    )
                record = _record_from_intent(
                    repo_root,
                    normalised,
                    requested_kind,
                    intent,
                    merged_preconditions,
                    timestamp,
                )
                try:
                    _write_operation_record(operation_id, ops_root, record)
                except Exception as exc:
                    return contract.CommitResult(
                        operation_id=operation_id,
                        attempt_id=normalised.attempt_id,
                        status=contract.COMMIT_FAILED,
                        committed_refs=list(record.get("published_refs") or []),
                        issues=[
                            {
                                "code": "record_write_failed",
                                "message": f"正式产物已发布，但操作记录写入失败（意图已保留，可重试恢复）：{exc}",
                                "severity": ISSUE_ERROR,
                                "retryable": True,
                                "details": {},
                            }
                        ],
                    )
                _remove_intent(ops_root, operation_id)
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_REPLAYED,
                    committed_refs=list(record["published_refs"]),
                    current_revision=record.get("current_revision"),
                    issues=[
                        {
                            "code": "recovered_record_after_publish",
                            "message": "检测到意图对应的正式产物，补齐操作记录后返回 replayed",
                            "severity": ISSUE_WARNING,
                            "retryable": False,
                            "details": {"intent_attempt_id": intent.get("attempt_id")},
                        }
                    ],
                )
            # No target exists yet: the intent is a stale pre-publish marker.
            _remove_intent(ops_root, operation_id)

        pointer_expected_current_sha: Optional[str] = None
        if pointer_mode:
            pointer = candidates[0]
            if not pointer.target_abs.is_file():
                return _pointer_result_failed(
                    operation_id, normalised.attempt_id,
                    "pointer_target_missing",
                    f"指针目标不存在：{pointer.target_path}",
                    {"path": pointer.target_path},
                )
            if pointer.target_abs.is_symlink():
                return _pointer_result_failed(
                    operation_id, normalised.attempt_id,
                    "pointer_target_not_allowed",
                    f"指针目标不得是 symlink：{pointer.target_path}",
                    {"path": pointer.target_path},
                )
            try:
                pointer_bytes = pointer.target_abs.read_bytes()
                pointer_document = _parse_pointer_bytes(
                    pointer_bytes, pointer.target_path
                )
            except (OSError, CommitError) as exc:
                return _pointer_result_failed(
                    operation_id, normalised.attempt_id,
                    "pointer_target_corrupt", str(exc),
                    {"path": pointer.target_path},
                )
            pointer_snapshot_sha = _stash_pointer_snapshot(
                pointer_bytes, pointer_document, pointer.target_abs
            )
            try:
                expected_current = _pointer_expected_value(
                    normalised, merged_preconditions
                )
            except CommitError as exc:
                _clear_pointer_snapshot()
                return _pointer_result_failed(
                    operation_id, normalised.attempt_id,
                    "pointer_expected_current_required", str(exc),
                    {"path": pointer.target_path},
                )
            # Call the review-visible hook; it must use the stashed snapshot.
            matched = _pointer_expected_matches(expected_current, pointer.target_abs)
            _clear_pointer_snapshot()
            if not matched:
                return _pointer_result_failed(
                    operation_id, normalised.attempt_id,
                    "current_revision_mismatch",
                    f"指针当前内容与 expected_current_revision 不一致：{pointer.target_path}",
                    {
                        "path": pointer.target_path,
                        "expected": expected_current,
                        "actual_sha256": pointer_snapshot_sha,
                    },
                )
            # R2: bind the same snapshot to intent previous_sha256 and to the
            # lock-protected replace expectation; never resample after checking.
            pointer_expected_current_sha = pointer_snapshot_sha

            # R3: before advancing A -> B, recover any other pointer operation
            # whose published intent matches the current pointer bytes (A's
            # record write may have failed).  Never advance if recovery is
            # ambiguous or unsafe.
            pending_failure = _recover_pending_pointer_intents(
                repo_root,
                ops_root,
                target_path=pointer.target_path,
                current_sha=pointer_snapshot_sha,
                except_operation_id=operation_id,
                lock_timeout_seconds=target_lock_timeout_seconds,
                now=timestamp,
            )
            if pending_failure is not None:
                return pending_failure
        else:
            # Foreign target that is not backed by our intent: never adopt it.
            for candidate in candidates:
                if candidate.target_abs.exists():
                    return contract.CommitResult(
                        operation_id=operation_id,
                        attempt_id=normalised.attempt_id,
                        status=contract.COMMIT_FAILED,
                        issues=[
                            {
                                "code": "target_exists",
                                "message": f"正式目标已存在且不存在本操作发布意图：{candidate.target_path}",
                                "severity": ISSUE_ERROR,
                                "retryable": False,
                                "details": {"path": candidate.target_path},
                            }
                        ],
                    )

        try:
            _write_intent(
                ops_root,
                normalised,
                candidates,
                timestamp,
                previous_sha256=pointer_expected_current_sha if pointer_mode else None,
            )
        except Exception as exc:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                issues=[
                    {
                        "code": "intent_write_failed",
                        "message": f"发布意图持久化失败，未发布正式产物：{exc}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {},
                    }
                ],
            )

        published_refs: list[dict[str, Any]] = []
        created_candidates: list[_Candidate] = []
        try:
            for candidate in candidates:
                if pointer_mode:
                    reference = atomic_io.atomic_replace_file(
                        candidate.candidate_path,
                        candidate.target_abs,
                        expected_sha256=candidate.sha256,
                        expected_current_sha256=pointer_expected_current_sha,
                        operation_id=operation_file_stem(operation_id),
                        lock_timeout_seconds=target_lock_timeout_seconds,
                        stale_lock_seconds=stale_lock_seconds,
                    )
                elif candidate.kind == "dir":
                    reference = atomic_io.atomic_publish_dir(
                        candidate.candidate_path,
                        candidate.target_abs,
                        expected_tree_sha256=candidate.sha256,
                        operation_id=operation_file_stem(operation_id),
                        lock_timeout_seconds=target_lock_timeout_seconds,
                        stale_lock_seconds=stale_lock_seconds,
                    )
                else:
                    reference = atomic_io.atomic_publish_file(
                        candidate.candidate_path,
                        candidate.target_abs,
                        expected_sha256=candidate.sha256,
                        operation_id=operation_file_stem(operation_id),
                        lock_timeout_seconds=target_lock_timeout_seconds,
                        stale_lock_seconds=stale_lock_seconds,
                    )
                created_candidates.append(candidate)
                public_ref = _record_public_ref(candidate)
                public_ref["file_count"] = reference.get("file_count")
                published_refs.append(public_ref)
        except BaseException as exc:
            _cleanup_candidate_temps(candidates, operation_file_stem(operation_id))
            if pointer_mode:
                pointer = candidates[0]
                replaced = False
                try:
                    replaced = (
                        pointer.target_abs.is_file()
                        and atomic_io.sha256_file(pointer.target_abs) == pointer.sha256
                    )
                except OSError:
                    replaced = False
                if not replaced:
                    _remove_intent(ops_root, operation_id)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                lock_conflict = "changed under lock" in str(exc)
                return contract.CommitResult(
                    operation_id=operation_id,
                    attempt_id=normalised.attempt_id,
                    status=contract.COMMIT_FAILED,
                    committed_refs=[],
                    issues=[
                        {
                            "code": (
                                "current_revision_mismatch"
                                if lock_conflict else "publish_failed"
                            ),
                            "message": (
                                f"指针在锁内已被其他操作推进，拒绝覆盖：{exc}"
                                if lock_conflict else
                                "指针替换过程异常；"
                                + ("目标已替换，意图已保留，可重试补记录" if replaced else "目标未替换")
                                + f"：{exc}"
                            ),
                            "severity": ISSUE_ERROR,
                            "retryable": not lock_conflict,
                            "details": {"pointer_replaced": replaced},
                        }
                    ],
                )
            _remove_intent(ops_root, operation_id)
            # Only targets successfully created by this call are eligible for
            # rollback; a failed publish may be failing because another
            # operation's target already exists.
            _rollback_created_targets(
                repo_root, ops_root, created_candidates, operation_id
            )
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                issues=[
                    {
                        "code": "publish_failed",
                        "message": f"原子发布失败：{exc}",
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {},
                    }
                ],
            )

        record = _build_record(
            normalised,
            requested_kind,
            candidates,
            merged_preconditions,
            timestamp,
            published_refs=published_refs,
        )
        try:
            _write_operation_record(operation_id, ops_root, record)
        except Exception as exc:
            return contract.CommitResult(
                operation_id=operation_id,
                attempt_id=normalised.attempt_id,
                status=contract.COMMIT_FAILED,
                committed_refs=published_refs,
                issues=[
                    {
                        "code": "record_write_failed",
                        "message": (
                            "正式产物已发布，但操作记录写入失败（记录未完成，"
                            f"意图已保留，可重试恢复）：{exc}"
                        ),
                        "severity": ISSUE_ERROR,
                        "retryable": True,
                        "details": {"published_refs": published_refs},
                    }
                ],
            )
        _remove_intent(ops_root, operation_id)
        return contract.CommitResult(
            operation_id=operation_id,
            attempt_id=normalised.attempt_id,
            status=contract.COMMIT_COMMITTED,
            committed_refs=list(published_refs),
            current_revision=record.get("current_revision"),
            issues=[],
        )
    finally:
        lock.release()


def _build_record(
    request: contract.CommitRequest,
    requested_kind: Optional[str],
    candidates: list[_Candidate],
    preconditions: Mapping[str, Any],
    timestamp: str,
    *,
    published_refs: Optional[list[dict[str, Any]]] = None,
    recovered: bool = False,
) -> dict[str, Any]:
    if published_refs is None:
        published_refs = []
        for candidate in candidates:
            ref = _record_public_ref(candidate)
            published_refs.append(ref)
    return {
        "schema_version": OPERATION_RECORD_SCHEMA,
        "operation_id": request.operation_id,
        "attempt_id": request.attempt_id,
        "operation_kind": request.operation_kind,
        "requested_operation_kind": requested_kind,
        "input_fingerprint": request.input_fingerprint,
        "candidate_refs": [
            {
                "candidate_id": candidate.candidate_id,
                "kind": candidate.kind,
                "path": candidate.target_path,
                "sha256": candidate.sha256,
                "artifact_id": candidate.artifact_id,
            }
            for candidate in candidates
        ],
        "preconditions": dict(preconditions),
        "status": "committed",
        "published_refs": published_refs,
        "current_revision": request.expected_current_revision,
        "created_at": timestamp,
        "recovered": bool(recovered),
    }


def publish_run_dir(
    candidate_dir: str | Path,
    target_run_dir: str | Path,
    *,
    operation_id: str,
    attempt_id: str,
    input_fingerprint: str,
    operations_root: str | Path = "data/cache/operations",
    root: str | Path | None = None,
    repo_root: str | Path | None = None,
    operation_store_root: str | Path | None = None,
    preconditions: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    op_store_root: str | Path | None = None,
    lock_timeout_seconds: float = 10.0,
    target_lock_timeout_seconds: float = 30.0,
) -> contract.CommitResult:
    """Publish a candidate run directory with the idempotent commit protocol."""

    repo_root = _resolve_root(root if root is not None else repo_root)
    if operation_store_root is not None or op_store_root is not None:
        operations_root = operation_store_root or op_store_root
    candidate = Path(candidate_dir)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if not candidate.is_dir():
        raise CommitError(f"candidate run directory not found: {candidate}")
    target = Path(str(target_run_dir).replace("\\", "/"))
    if target.is_absolute():
        raise CommitError("target_run_dir 必须是仓库相对路径")
    request = {
        "schema_version": contract.SCHEMA_COMMIT_REQUEST,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "operation_kind": "write_run",
        "input_fingerprint": input_fingerprint,
        "candidate_refs": [
            {
                "candidate_id": "run",
                "kind": "dir",
                "candidate_path": str(candidate),
                "path": target.as_posix(),
                "artifact_id": "run",
            }
        ],
        "preconditions": dict(preconditions or {}),
    }
    return commit_operation(
        request,
        operations_root=operations_root,
        root=repo_root,
        now=now,
        lock_timeout_seconds=lock_timeout_seconds,
        target_lock_timeout_seconds=target_lock_timeout_seconds,
    )


def publish_file(
    candidate_file: str | Path,
    target_file: str | Path,
    *,
    operation_id: str,
    attempt_id: str,
    input_fingerprint: str,
    operation_kind: str = "publish_rule",
    artifact_id: Optional[str] = None,
    operations_root: str | Path = "data/cache/operations",
    root: str | Path | None = None,
    repo_root: str | Path | None = None,
    operation_store_root: str | Path | None = None,
    preconditions: Optional[Mapping[str, Any]] = None,
    now: Optional[datetime] = None,
    op_store_root: str | Path | None = None,
    lock_timeout_seconds: float = 10.0,
    target_lock_timeout_seconds: float = 30.0,
) -> contract.CommitResult:
    """Publish a candidate file with the same idempotent commit protocol."""

    repo_root = _resolve_root(root if root is not None else repo_root)
    if operation_store_root is not None or op_store_root is not None:
        operations_root = operation_store_root or op_store_root
    candidate = Path(candidate_file)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if not candidate.is_file():
        raise CommitError(f"candidate file not found: {candidate}")
    target = Path(str(target_file).replace("\\", "/"))
    if target.is_absolute():
        raise CommitError("target_file 必须是仓库相对路径")
    request = {
        "schema_version": contract.SCHEMA_COMMIT_REQUEST,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "operation_kind": operation_kind,
        "input_fingerprint": input_fingerprint,
        "candidate_refs": [
            {
                "candidate_id": "file",
                "kind": "file",
                "candidate_path": str(candidate),
                "path": target.as_posix(),
                "artifact_id": artifact_id,
            }
        ],
        "preconditions": dict(preconditions or {}),
    }
    return commit_operation(
        request,
        operations_root=operations_root,
        root=repo_root,
        now=now,
        lock_timeout_seconds=lock_timeout_seconds,
        target_lock_timeout_seconds=target_lock_timeout_seconds,
    )


def update_pointer(
    candidate_file: str | Path,
    target_pointer: str | Path,
    *,
    operation_id: str,
    attempt_id: str,
    input_fingerprint: str,
    expected_current_revision: str | None = None,
    expected_current_sha256: str | None = None,
    operations_root: str | Path = "data/cache/operations",
    root: str | Path | None = None,
    repo_root: str | Path | None = None,
    operation_store_root: str | Path | None = None,
    preconditions: Optional[Mapping[str, Any]] = None,
    lock_timeout_seconds: float = 10.0,
    target_lock_timeout_seconds: float = 30.0,
    now: Optional[datetime] = None,
) -> contract.CommitResult:
    """Atomically update a whitelisted pointer file (``update_pointer``)."""

    repo_root = _resolve_root(root if root is not None else repo_root)
    if operation_store_root is not None:
        operations_root = operation_store_root
    candidate = Path(candidate_file)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    if not candidate.is_file():
        raise CommitError(f"candidate pointer file not found: {candidate}")
    target = Path(str(target_pointer).replace("\\", "/"))
    if target.is_absolute():
        raise CommitError("target_pointer 必须是仓库相对路径")
    merged_preconditions = dict(preconditions or {})
    if expected_current_sha256:
        merged_preconditions["expected_current_sha256"] = expected_current_sha256
    if expected_current_revision:
        merged_preconditions["expected_current_revision"] = expected_current_revision
    request = {
        "schema_version": contract.SCHEMA_COMMIT_REQUEST,
        "operation_id": operation_id,
        "attempt_id": attempt_id,
        "operation_kind": "update_pointer",
        "input_fingerprint": input_fingerprint,
        "candidate_refs": [
            {
                "candidate_id": "pointer",
                "kind": "file",
                "candidate_path": str(candidate),
                "path": target.as_posix(),
                "artifact_id": "pointer",
            }
        ],
        "expected_current_revision": expected_current_revision,
        "preconditions": merged_preconditions,
    }
    return commit_operation(
        request,
        operations_root=operations_root,
        root=repo_root,
        lock_timeout_seconds=lock_timeout_seconds,
        target_lock_timeout_seconds=target_lock_timeout_seconds,
        now=now,
    )

