"""Research hand-off contract (``le-research-task-v1`` / ``le-research-proposal-v1``).

Implements :file:`docs/contracts/workflow-interface.md` §6 on top of
:mod:`le_calc.workflow.contract`:

* :func:`make_research_task` — turn one structured gap (an analysis-plan /
  research-queue candidate, or a minimal dict) into a :class:`ResearchTask`.
  ``task_id`` / ``gap_id`` are deterministic: same gap content + same input
  fingerprint produce the same ids; a changed input fingerprint produces new ids.
* :func:`validate_proposal` — validate a :class:`ResearchProposal` against the
  task it claims to answer. A changed fingerprint yields an explicit
  ``stale_proposal`` error instead of silently applying the proposal. ``outcome``
  stays independent from evidence strength: ``unresolved`` / ``needs_input``
  close the research round without requiring change candidates, while
  ``proposed_change`` needs candidates + evidence + verification declarations.
* file exchange helpers (``write_task_file`` / ``read_task_file`` /
  ``write_proposal_file`` / ``read_proposal_file``) for the V1 "hand-off via
  files" requirement: UTF-8 JSON, ``schema_version`` always present, repository
  relative paths only.

Hard boundaries (do not weaken when wiring P3/P4):

* this module never turns research text into executable rules and never imports
  or evaluates proposal contents; passing validation only means "accepted for a
  future commit", the actual publish path is ``commit_operation`` (P4);
* no ``print`` / ``sys.exit`` control flow: business functions return payloads
  or raise :class:`~le_calc.workflow.contract.ContractError`;
* no dependency on the CLI, the current case, chat context or global state.

Field paths in errors use the same ``$.field`` style as the knowledge loader so
callers can map them to the JSON schemas under ``tools/phase3/schemas/workflow-v1/``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping

from .contract import (
    OUTCOME_PROPOSED_CHANGE,
    OUTCOME_UNRESOLVED,
    OUTCOMES,
    SCHEMA_RESEARCH_PROPOSAL,
    SCHEMA_RESEARCH_TASK,
    SEVERITY_ERROR,
    ContractError,
    ResearchProposal,
    ResearchTask,
    ValidationResult,
    deterministic_id,
    dumps,
    fingerprint,
    make_issue,
    sha256_file,
    write_json,
)

#: ``deliverable_kind`` values from workflow-interface §6.1.
DELIVERABLE_KINDS = ("mapping", "parameter_rule", "handler", "bounded_assumption")

#: ``assumptions[].status`` shares the knowledge ``evidence_state`` vocabulary.
ASSUMPTION_STATUSES = ("confirmed", "derived", "assumed", "unresolved")

#: JSON-schema ids / file names supplied alongside this module.
SCHEMA_TASK_FILE = "research-task-v1.json"
SCHEMA_PROPOSAL_FILE = "research-proposal-v1.json"

TASK_FIELDS = (
    "schema_version",
    "task_id",
    "gap_id",
    "input_fingerprint",
    "question",
    "source_refs",
    "related_mechanism_refs",
    "researched_already",
    "affected",
    "deliverable_kind",
    "verification_required",
)

PROPOSAL_FIELDS = (
    "schema_version",
    "proposal_id",
    "task_id",
    "based_on_fingerprint",
    "outcome",
    "mechanism_refs",
    "mapping_refs",
    "code_change_refs",
    "evidence_refs",
    "assumptions",
    "verification_refs",
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GAP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
_TASK_ID_RE = re.compile(r"^research_task-[0-9a-f]{16}$")
_WINDOWS_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_POSIX_ABS_RE = re.compile(
    r"^/(?:home|Users|root|tmp|mnt|opt|var|etc|usr|srv|system|private|Volumes)(?:/|$)"
)
_URL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_SCHEME_RE = re.compile(r"^([a-z][a-z0-9_]*):(.+)$")


# ---------------------------------------------------------------------------
# field errors / portability
# ---------------------------------------------------------------------------


def _field_error(code: str, field: str, message: str) -> dict[str, str]:
    return {"code": code, "field": field, "message": message}


def _format_field_errors(errors: Iterable[Mapping[str, str]]) -> str:
    parts = [f"{item.get('field', '$')}: {item.get('message', '')}" for item in errors]
    return "; ".join(parts)


def _raise_field_errors(errors: list[dict[str, str]], *, what: str) -> None:
    if errors:
        raise ContractError(f"{what} 校验失败：{_format_field_errors(errors)}")


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.match(value))


def _is_absolute_path(value: str) -> bool:
    text = str(value)
    return bool(_WINDOWS_ABS_RE.match(text) or _POSIX_ABS_RE.match(text))


def _iter_strings(value: Any, pointer: str = "$") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield pointer, value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _iter_strings(item, f"{pointer}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            yield from _iter_strings(item, f"{pointer}[{index}]")


def _portable_problems(payload: Any) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    for pointer, value in _iter_strings(payload):
        if _is_absolute_path(value):
            problems.append(
                _field_error(
                    "non_portable_path",
                    pointer,
                    f"不允许机器绝对路径：{value!r}（请使用仓库根相对路径）",
                )
            )
    return problems


def _assert_portable(payload: Any, *, what: str) -> None:
    _raise_field_errors(_portable_problems(payload), what=what)


# ---------------------------------------------------------------------------
# generic field normalisation
# ---------------------------------------------------------------------------


def _string_list(
    value: Any,
    field: str,
    errors: list[dict[str, str]],
    *,
    tolerate_scalar: bool = False,
) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not tolerate_scalar:
            errors.append(_field_error("incomplete_payload", field, "应为字符串数组"))
            return []
        value = [value]
    if not isinstance(value, (list, tuple)):
        errors.append(_field_error("incomplete_payload", field, "应为字符串数组"))
        return []
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            errors.append(
                _field_error("incomplete_payload", f"{field}[{index}]", "数组项必须是非空字符串")
            )
            continue
        result.append(item.strip())
    return result


def _normalize_source_refs(
    value: Any,
    field: str,
    errors: list[dict[str, str]],
    *,
    tolerate_scalar: bool = False,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, str):
        if not tolerate_scalar:
            errors.append(_field_error("incomplete_payload", field, "应为对象数组"))
            return []
        value = [value]
    if not isinstance(value, (list, tuple)):
        errors.append(_field_error("incomplete_payload", field, "应为对象数组"))
        return []
    refs: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        pointer = f"{field}[{index}]"
        if isinstance(item, str) and item.strip():
            refs.append({"locator": item.strip()})
            continue
        if not isinstance(item, Mapping) or not item:
            errors.append(
                _field_error("incomplete_payload", pointer, "来源引用必须是字符串或非空对象")
            )
            continue
        ref = {str(key): value for key, value in item.items()}
        if not any(key in ref for key in ("locator", "path", "artifact_id", "field")):
            errors.append(
                _field_error(
                    "incomplete_payload",
                    pointer,
                    "来源引用至少需要 locator / path / artifact_id / field 之一",
                )
            )
            continue
        refs.append(ref)
    return refs


def _normalize_affected(value: Any, field: str, errors: list[dict[str, str]]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        errors.append(
            _field_error("incomplete_payload", field, "affected 必须是对象（组件/计算块/是否阻塞）")
        )
        return {"components": [], "calculation_blocks": [], "blocks_target": False}
    components = _string_list(value.get("components"), f"{field}.components", errors)
    calculation_blocks = _string_list(
        value.get("calculation_blocks"), f"{field}.calculation_blocks", errors
    )
    blocks_target = value.get("blocks_target", False)
    if not isinstance(blocks_target, bool):
        errors.append(
            _field_error("incomplete_payload", f"{field}.blocks_target", "blocks_target 必须是布尔值")
        )
        blocks_target = False
    if (
        not components
        and not calculation_blocks
        and "components" not in value
        and "calculation_blocks" not in value
    ):
        # Explicit empty scope (both keys present but empty) is accepted: the
        # producing queue states "scope not narrowed yet" and the question /
        # source_refs carry the actionable context. Only a missing scope
        # declaration is a contract error.
        errors.append(
            _field_error(
                "incomplete_payload",
                field,
                "affected 至少需要 components 或 calculation_blocks 之一（说明受影响范围）",
            )
        )
    normalised: dict[str, Any] = {
        "components": components,
        "calculation_blocks": calculation_blocks,
        "blocks_target": blocks_target,
    }
    for key, item in value.items():
        key = str(key)
        if key not in normalised:
            normalised[key] = item
    return normalised


# ---------------------------------------------------------------------------
# ResearchTask
# ---------------------------------------------------------------------------


def _collect_task(payload: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Normalise a ``ResearchTask`` JSON payload; returns ``(data, field_errors)``."""
    errors: list[dict[str, str]] = []
    if isinstance(payload, ResearchTask):
        try:
            payload = payload.to_dict()
        except ContractError as exc:  # pragma: no cover - dataclass already normalised
            return None, [_field_error("incomplete_payload", "$", str(exc))]
    if not isinstance(payload, Mapping):
        return None, [_field_error("incomplete_payload", "$", "ResearchTask 必须是 JSON 对象")]

    data: dict[str, Any] = {"schema_version": SCHEMA_RESEARCH_TASK}
    version = payload.get("schema_version")
    if version is None:
        errors.append(_field_error("invalid_schema_version", "$.schema_version", "缺少 schema_version"))
    elif version != SCHEMA_RESEARCH_TASK:
        return None, [
            _field_error(
                "invalid_schema_version",
                "$.schema_version",
                f"不支持的 schema_version：{version!r}（需要 {SCHEMA_RESEARCH_TASK}）",
            )
        ]

    for name in ("task_id", "gap_id"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(_field_error("incomplete_payload", f"$.{name}", "缺少必填字段或为空"))
        else:
            data[name] = value.strip()
    if data.get("task_id") and not _TASK_ID_RE.match(data["task_id"]):
        errors.append(
            _field_error(
                "incomplete_payload",
                "$.task_id",
                f"task_id 必须形如 research_task-<16 位十六进制>，实际 {data['task_id']!r}",
            )
        )
    if data.get("gap_id") and not _GAP_ID_RE.match(data["gap_id"]):
        errors.append(
            _field_error(
                "incomplete_payload",
                "$.gap_id",
                f"gap_id 只能包含字母/数字/_.:-，实际 {data['gap_id']!r}",
            )
        )

    fingerprint_value = payload.get("input_fingerprint")
    if not _is_sha256(fingerprint_value):
        errors.append(
            _field_error(
                "invalid_fingerprint",
                "$.input_fingerprint",
                "input_fingerprint 必须是 64 位 sha256 十六进制字符串",
            )
        )
    else:
        data["input_fingerprint"] = fingerprint_value

    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        errors.append(_field_error("incomplete_payload", "$.question", "缺少必填字段或为空"))
    else:
        data["question"] = question.strip()

    data["source_refs"] = _normalize_source_refs(
        payload.get("source_refs"), "$.source_refs", errors
    )
    data["related_mechanism_refs"] = _string_list(
        payload.get("related_mechanism_refs"), "$.related_mechanism_refs", errors
    )
    data["researched_already"] = _string_list(
        payload.get("researched_already"), "$.researched_already", errors
    )
    data["affected"] = _normalize_affected(payload.get("affected"), "$.affected", errors)
    deliverable_kind = payload.get("deliverable_kind")
    if deliverable_kind not in DELIVERABLE_KINDS:
        errors.append(
            _field_error(
                "incomplete_payload",
                "$.deliverable_kind",
                f"deliverable_kind 必须是 {', '.join(DELIVERABLE_KINDS)} 之一，实际 {deliverable_kind!r}",
            )
        )
    else:
        data["deliverable_kind"] = deliverable_kind
    data["verification_required"] = _string_list(
        payload.get("verification_required"), "$.verification_required", errors
    )

    errors.extend(_portable_problems(payload))
    if errors:
        return data, errors
    return data, []


def _task_payload(task: Any) -> dict[str, Any]:
    """Strictly normalise a task payload for file exchange; raises ContractError."""
    if isinstance(task, ResearchTask):
        try:
            data = task.to_dict()
        except ContractError as exc:
            raise ContractError(f"ResearchTask 校验失败：{exc}") from exc
        _assert_portable(data, what="ResearchTask")
        return data
    data, errors = _collect_task(task)
    _raise_field_errors(errors, what="ResearchTask")
    assert data is not None
    payload = ResearchTask(
        task_id=data["task_id"],
        gap_id=data["gap_id"],
        input_fingerprint=data["input_fingerprint"],
        question=data["question"],
        source_refs=data["source_refs"],
        related_mechanism_refs=data["related_mechanism_refs"],
        researched_already=data["researched_already"],
        affected=data["affected"],
        deliverable_kind=data["deliverable_kind"],
        verification_required=data["verification_required"],
    ).to_dict()
    _assert_portable(payload, what="ResearchTask")
    return payload


def _gap_identity(
    *,
    question: str,
    source_refs: list[dict[str, Any]],
    related_mechanism_refs: list[str],
    researched_already: list[str],
    affected: dict[str, Any],
    deliverable_kind: str,
    verification_required: list[str],
) -> dict[str, Any]:
    """Order-insensitive identity of a gap (used for deterministic ids)."""
    return {
        "question": question,
        "source_refs": sorted(source_refs, key=lambda ref: dumps(ref)),
        "related_mechanism_refs": sorted(related_mechanism_refs),
        "researched_already": sorted(researched_already),
        "affected": {
            "components": sorted(str(item) for item in affected.get("components") or []),
            "calculation_blocks": sorted(
                str(item) for item in affected.get("calculation_blocks") or []
            ),
            "blocks_target": bool(affected.get("blocks_target", False)),
        },
        "deliverable_kind": deliverable_kind,
        "verification_required": sorted(verification_required),
    }


def _resolve_input_fingerprint(gap: Mapping[str, Any], explicit: Any, identity: Mapping[str, Any]) -> str:
    if explicit is not None:
        if not _is_sha256(explicit):
            raise ContractError(
                "$.input_fingerprint: 必须是 64 位 sha256 十六进制字符串，"
                f"实际 {explicit!r}"
            )
        return str(explicit)
    from_gap = gap.get("input_fingerprint", gap.get("fingerprint"))
    if from_gap is not None:
        if not _is_sha256(from_gap):
            raise ContractError(
                "$.input_fingerprint: 缺口提供的 input_fingerprint 不是 sha256，"
                f"实际 {from_gap!r}"
            )
        return str(from_gap)
    # Fallback: deterministic content fingerprint of the gap itself. Callers
    # should pass the real input/rules fingerprint; this keeps the hand-off
    # reproducible when they do not.
    return fingerprint({"gap": identity})


def make_research_task(gap: Mapping[str, Any], *, input_fingerprint: Any = None) -> dict[str, Any]:
    """Build a deterministic :class:`ResearchTask` dict from one structured gap.

    Minimal accepted gap: ``{"question": str, "affected": {...}}``.  Optional
    fields (canonical or research-queue aliases):

    ``source_refs`` / ``sources``, ``related_mechanism_refs`` /
    ``related_mechanisms`` / ``mechanism_refs``, ``researched_already`` /
    ``researched``, ``deliverable_kind`` / ``required_deliverable`` /
    ``deliverable``, ``verification_required`` / ``verification`` /
    ``next_verification``, ``gap_id``, ``task_id``, ``input_fingerprint``.

    ``input_fingerprint`` resolution order: explicit argument → gap field →
    content fingerprint of the normalised gap (explicit fallback, documented).
    ``task_id``/``gap_id`` are ``deterministic_id`` values over the gap identity
    and the fingerprint; the same pair never creates two different tasks.  If the
    incoming gap is already ``ResearchTask``-shaped (e.g. one entry read from
    ``research-queue.json``) its ``task_id`` / ``gap_id`` are preserved so the
    hand-off keeps the same identity across files.
    """
    if not isinstance(gap, Mapping):
        raise ContractError("$: gap 必须是 JSON 对象（来自 analysis-plan / research-queue 的候选）")

    errors: list[dict[str, str]] = []
    question = gap.get("question")
    if not isinstance(question, str) or not question.strip():
        errors.append(_field_error("incomplete_payload", "$.question", "缺少必填字段或为空"))
        question_text = ""
    else:
        question_text = question.strip()

    affected_source = gap.get("affected")
    # research-queue 条目有时把 blocks_target 放在顶层；接受这一别名并归入 affected。
    if (
        isinstance(affected_source, Mapping)
        and "blocks_target" not in affected_source
        and "blocks_target" in gap
    ):
        affected_source = dict(affected_source, blocks_target=gap["blocks_target"])
    affected = _normalize_affected(affected_source, "$.affected", errors)

    deliverable_kind = gap.get(
        "deliverable_kind", gap.get("required_deliverable", gap.get("deliverable", "mapping"))
    )
    if deliverable_kind not in DELIVERABLE_KINDS:
        errors.append(
            _field_error(
                "incomplete_payload",
                "$.deliverable_kind",
                f"deliverable_kind 必须是 {', '.join(DELIVERABLE_KINDS)} 之一，"
                f"实际 {deliverable_kind!r}",
            )
        )
        deliverable_kind = "mapping"

    source_refs = _normalize_source_refs(
        gap.get("source_refs", gap.get("sources")), "$.source_refs", errors, tolerate_scalar=True
    )
    related_mechanism_refs = _string_list(
        gap.get(
            "related_mechanism_refs",
            gap.get("related_mechanisms", gap.get("mechanism_refs")),
        ),
        "$.related_mechanism_refs",
        errors,
        tolerate_scalar=True,
    )
    researched_already = _string_list(
        gap.get("researched_already", gap.get("researched")),
        "$.researched_already",
        errors,
        tolerate_scalar=True,
    )
    verification_required = _string_list(
        gap.get(
            "verification_required",
            gap.get("verification", gap.get("next_verification")),
        ),
        "$.verification_required",
        errors,
        tolerate_scalar=True,
    )

    explicit_gap_id = gap.get("gap_id")
    if explicit_gap_id is not None:
        if not isinstance(explicit_gap_id, str) or not explicit_gap_id.strip():
            errors.append(_field_error("incomplete_payload", "$.gap_id", "gap_id 必须是非空字符串"))
        elif not _GAP_ID_RE.match(explicit_gap_id.strip()):
            errors.append(
                _field_error(
                    "incomplete_payload",
                    "$.gap_id",
                    f"gap_id 只能包含字母/数字/_.:-，实际 {explicit_gap_id!r}",
                )
            )

    # 与并行工作流（domain/artifacts 的 research-queue）对齐：候选 dict 可能已带
    # ResearchTask 形状的 task_id；若提供则沿用（保证 queue 条目 → task 文件不换 ID）。
    explicit_task_id = gap.get("task_id")
    if explicit_task_id is not None:
        if not isinstance(explicit_task_id, str) or not _TASK_ID_RE.match(explicit_task_id.strip()):
            errors.append(
                _field_error(
                    "incomplete_payload",
                    "$.task_id",
                    "task_id 必须形如 research_task-<16 位十六进制>，"
                    f"实际 {explicit_task_id!r}",
                )
            )

    # Portability: the gap may come from a report written on another machine.
    errors.extend(_portable_problems(gap))
    _raise_field_errors(errors, what="ResearchGap")

    identity = _gap_identity(
        question=question_text,
        source_refs=source_refs,
        related_mechanism_refs=related_mechanism_refs,
        researched_already=researched_already,
        affected=affected,
        deliverable_kind=deliverable_kind,
        verification_required=verification_required,
    )
    resolved_fingerprint = _resolve_input_fingerprint(gap, input_fingerprint, identity)

    if isinstance(explicit_gap_id, str) and explicit_gap_id.strip():
        gap_id = explicit_gap_id.strip()
    else:
        gap_id = deterministic_id(
            "gap", {"input_fingerprint": resolved_fingerprint, "gap": identity}
        )
    if isinstance(explicit_task_id, str) and explicit_task_id.strip():
        task_id = explicit_task_id.strip()
    else:
        task_id = deterministic_id(
            "research_task",
            {
                "gap_id": gap_id,
                "input_fingerprint": resolved_fingerprint,
                "gap": identity,
            },
        )

    task = ResearchTask(
        task_id=task_id,
        gap_id=gap_id,
        input_fingerprint=resolved_fingerprint,
        question=question_text,
        source_refs=source_refs,
        related_mechanism_refs=related_mechanism_refs,
        researched_already=researched_already,
        affected=affected,
        deliverable_kind=deliverable_kind,
        verification_required=verification_required,
    ).to_dict()
    _assert_portable(task, what="ResearchTask")
    return task


# ---------------------------------------------------------------------------
# ResearchProposal
# ---------------------------------------------------------------------------


def _collect_assumptions(
    value: Any, field: str, errors: list[dict[str, str]]
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        errors.append(_field_error("incomplete_payload", field, "assumptions 必须是对象数组"))
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        pointer = f"{field}[{index}]"
        if not isinstance(item, Mapping):
            errors.append(_field_error("incomplete_payload", pointer, "假设必须是对象"))
            continue
        assumption_id = item.get("assumption_id")
        if not isinstance(assumption_id, str) or not assumption_id.strip():
            errors.append(
                _field_error("incomplete_payload", f"{pointer}.assumption_id", "缺少 assumption_id")
            )
            continue
        if assumption_id in seen:
            errors.append(
                _field_error("incomplete_payload", f"{pointer}.assumption_id", "assumption_id 重复")
            )
            continue
        seen.add(assumption_id)
        normalised = {str(key): value for key, value in item.items()}
        status = normalised.get("status")
        if status is not None and status not in ASSUMPTION_STATUSES:
            errors.append(
                _field_error(
                    "incomplete_payload",
                    f"{pointer}.status",
                    f"status 必须是 {', '.join(ASSUMPTION_STATUSES)} 之一，实际 {status!r}",
                )
            )
        result.append(normalised)
    return result


def _collect_evidence_refs(
    value: Any, field: str, errors: list[dict[str, str]]
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        errors.append(_field_error("incomplete_payload", field, "evidence_refs 必须是对象数组"))
        return []
    result: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        pointer = f"{field}[{index}]"
        if not isinstance(item, Mapping) or not item:
            errors.append(_field_error("incomplete_payload", pointer, "证据引用必须是非空对象"))
            continue
        ref = {str(key): value for key, value in item.items()}
        if not any(key in ref for key in ("locator", "path", "artifact_id")):
            errors.append(
                _field_error(
                    "incomplete_payload",
                    pointer,
                    "证据引用至少需要 locator / path / artifact_id 之一（sha256 仅作完整性声明）",
                )
            )
            continue
        sha = ref.get("sha256")
        if sha is not None and not _is_sha256(sha):
            errors.append(
                _field_error("invalid_fingerprint", f"{pointer}.sha256", "sha256 必须是 64 位十六进制")
            )
        result.append(ref)
    return result


def _collect_proposal(payload: Any) -> tuple[dict[str, Any] | None, list[dict[str, str]]]:
    """Normalise a ``ResearchProposal`` JSON payload; returns ``(data, field_errors)``."""
    if isinstance(payload, ResearchProposal):
        try:
            payload = payload.to_dict()
        except ContractError as exc:
            message = str(exc)
            field = "$.outcome" if "outcome" in message else "$.based_on_fingerprint"
            return None, [_field_error("incomplete_payload", field, message)]
    if not isinstance(payload, Mapping):
        return None, [_field_error("incomplete_payload", "$", "ResearchProposal 必须是 JSON 对象")]

    errors: list[dict[str, str]] = []
    data: dict[str, Any] = {"schema_version": SCHEMA_RESEARCH_PROPOSAL}
    version = payload.get("schema_version")
    if version is None:
        errors.append(_field_error("invalid_schema_version", "$.schema_version", "缺少 schema_version"))
    elif version != SCHEMA_RESEARCH_PROPOSAL:
        return None, [
            _field_error(
                "invalid_schema_version",
                "$.schema_version",
                f"不支持的 schema_version：{version!r}（需要 {SCHEMA_RESEARCH_PROPOSAL}）",
            )
        ]

    for name in ("proposal_id", "task_id"):
        value = payload.get(name)
        if not isinstance(value, str) or not value.strip():
            errors.append(_field_error("incomplete_payload", f"$.{name}", "缺少必填字段或为空"))
        else:
            data[name] = value.strip()

    based_on = payload.get("based_on_fingerprint")
    if not _is_sha256(based_on):
        errors.append(
            _field_error(
                "invalid_fingerprint",
                "$.based_on_fingerprint",
                "based_on_fingerprint 必须是 64 位 sha256 十六进制字符串",
            )
        )
    else:
        data["based_on_fingerprint"] = based_on

    outcome = payload.get("outcome")
    if outcome is None:
        errors.append(_field_error("incomplete_payload", "$.outcome", "缺少必填字段"))
    elif outcome not in OUTCOMES:
        errors.append(
            _field_error(
                "invalid_outcome",
                "$.outcome",
                f"outcome 必须是 {', '.join(sorted(OUTCOMES))} 之一，实际 {outcome!r}",
            )
        )
    else:
        data["outcome"] = outcome

    data["mechanism_refs"] = _string_list(payload.get("mechanism_refs"), "$.mechanism_refs", errors)
    data["mapping_refs"] = _string_list(payload.get("mapping_refs"), "$.mapping_refs", errors)
    data["code_change_refs"] = _string_list(
        payload.get("code_change_refs"), "$.code_change_refs", errors
    )
    data["evidence_refs"] = _collect_evidence_refs(
        payload.get("evidence_refs"), "$.evidence_refs", errors
    )
    data["assumptions"] = _collect_assumptions(payload.get("assumptions"), "$.assumptions", errors)
    data["verification_refs"] = _string_list(
        payload.get("verification_refs"), "$.verification_refs", errors
    )

    errors.extend(_portable_problems(payload))
    if errors:
        return data if data else None, errors
    return data, []


def _proposal_payload(proposal: Any) -> dict[str, Any]:
    """Strictly normalise a proposal payload for file exchange; raises ContractError."""
    if isinstance(proposal, ResearchProposal):
        try:
            payload = proposal.to_dict()
        except ContractError as exc:
            raise ContractError(f"ResearchProposal 校验失败：{exc}") from exc
        _assert_portable(payload, what="ResearchProposal")
        return payload
    data, errors = _collect_proposal(proposal)
    _raise_field_errors(errors, what="ResearchProposal")
    assert data is not None
    payload = ResearchProposal(
        proposal_id=data["proposal_id"],
        task_id=data["task_id"],
        based_on_fingerprint=data["based_on_fingerprint"],
        outcome=data["outcome"],
        mechanism_refs=data["mechanism_refs"],
        mapping_refs=data["mapping_refs"],
        code_change_refs=data["code_change_refs"],
        evidence_refs=data["evidence_refs"],
        assumptions=data["assumptions"],
        verification_refs=data["verification_refs"],
    ).to_dict()
    _assert_portable(payload, what="ResearchProposal")
    return payload


# ---------------------------------------------------------------------------
# validate_proposal
# ---------------------------------------------------------------------------


def _check(check_id: str, status: str, detail: str, *, field: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"id": check_id, "status": status, "detail": detail}
    if field:
        item["field"] = field
    return item


def _issue(
    code: str,
    message: str,
    *,
    field: str | None = None,
    related_ids: Iterable[str] | None = None,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = dict(details or {})
    if field:
        payload["field"] = field
    label = f"{field}: {message}" if field else message
    return make_issue(
        code,
        label,
        severity=SEVERITY_ERROR,
        related_ids=related_ids,
        details=payload,
    )


def _issue_from_field_error(error: Mapping[str, str]) -> dict[str, Any]:
    return _issue(
        str(error.get("code") or "incomplete_payload"),
        str(error.get("message") or ""),
        field=str(error.get("field") or "$"),
    )


def _normalise_tasks(tasks_by_id: Any) -> Mapping[str, Any] | None:
    if tasks_by_id is None:
        return None
    if isinstance(tasks_by_id, Mapping):
        return {str(key): value for key, value in tasks_by_id.items()}
    if isinstance(tasks_by_id, (list, tuple)):
        mapping: dict[str, Any] = {}
        for index, item in enumerate(tasks_by_id):
            if isinstance(item, ResearchTask):
                task_id = item.task_id
            elif isinstance(item, Mapping):
                task_id = item.get("task_id")
            else:
                raise ContractError(f"$.tasks_by_id[{index}]: 必须是 ResearchTask 或 JSON 对象")
            if not isinstance(task_id, str) or not task_id.strip():
                raise ContractError(f"$.tasks_by_id[{index}]: 缺少 task_id")
            mapping[task_id.strip()] = item
        return mapping
    raise ContractError("$.tasks_by_id: 必须是 task_id → task 的映射或任务数组")


def _resolve_task(
    proposal: Mapping[str, Any], *, task: Any, tasks_by_id: Any
) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    """Return ``(task_data, field_error)``; the caller maps it to a ValidationResult."""
    if task is not None:
        data, errors = _collect_task(task)
        if errors:
            return None, _field_error(
                "invalid_task", "$.task", f"提供的 task 内容不合法：{_format_field_errors(errors)}"
            )
        assert data is not None
        if data["task_id"] != proposal["task_id"]:
            return None, _field_error(
                "invalid_task_id",
                "$.task_id",
                f"proposal.task_id={proposal['task_id']!r} 与提供 task.task_id={data['task_id']!r} 不一致",
            )
        return data, None

    mapping = _normalise_tasks(tasks_by_id)
    if mapping is None:
        return None, _field_error(
            "invalid_task_id",
            "$.task_id",
            "未提供 task 或 tasks_by_id，无法确认 task 存在（V1 文件交换请先传递/读取 ResearchTask 文件）",
        )
    if proposal["task_id"] not in mapping:
        return None, _field_error(
            "invalid_task_id",
            "$.task_id",
            f"tasks_by_id 中不存在 task_id={proposal['task_id']!r}",
        )
    data, errors = _collect_task(mapping[proposal["task_id"]])
    if errors:
        return None, _field_error(
            "invalid_task",
            "$.task",
            f"tasks_by_id 中的 task 内容不合法：{_format_field_errors(errors)}",
        )
    return data, None


def _relative_locator_path(value: str) -> str | None:
    """Return a repository-relative path candidate from an evidence locator."""
    text = str(value).strip()
    if not text or _URL_RE.match(text) or _is_absolute_path(text):
        return None
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    scheme = _SCHEME_RE.match(text)
    if scheme:
        text = scheme.group(2).strip()
    if "/" not in text and "\\" not in text:
        return None
    return text.replace("\\", "/")


def _check_evidence_refs(
    refs: list[dict[str, Any]], evidence_root: Any
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    if not refs:
        return [_check("evidence_existence", "skip", "未提供 evidence_refs")], errors

    root = Path(evidence_root) if evidence_root is not None else None
    checkable = 0
    verified = 0
    skipped = 0
    for index, ref in enumerate(refs):
        field = f"$.evidence_refs[{index}]"
        candidates: list[tuple[str, str]] = []
        for key in ("locator", "path", "artifact_id"):
            value = ref.get(key)
            if not isinstance(value, str) or not value.strip():
                continue
            if _is_absolute_path(value):
                errors.append(
                    _issue(
                        "non_portable_path",
                        _relative_hint(value),
                        field=f"{field}.{key}",
                    )
                )
                continue
            candidate = _relative_locator_path(value)
            if candidate:
                candidates.append((key, candidate))
        if not candidates:
            skipped += 1
            continue
        if root is None:
            skipped += 1
            continue
        for key, candidate in candidates:
            checkable += 1
            target = root / candidate
            if not target.is_file():
                errors.append(
                    _issue(
                        "evidence_missing",
                        f"证据定位在 evidence_root 下不存在：{candidate}",
                        field=f"{field}.{key}",
                        details={"locator": candidate},
                    )
                )
                continue
            declared_sha = ref.get("sha256")
            if declared_sha is not None:
                actual = sha256_file(target)
                if actual != declared_sha:
                    errors.append(
                        _issue(
                            "evidence_sha_mismatch",
                            f"证据文件 sha256 与声明不一致（实际 {actual[:16]}…）",
                            field=f"{field}.sha256",
                            details={"locator": candidate},
                        )
                    )
                    continue
            verified += 1

    if checkable:
        status = "pass" if not _evidence_errors(errors) else "fail"
        detail = f"检查 {checkable} 个可定位证据引用，{verified} 个存在"
        if skipped:
            detail += f"，{skipped} 条无法解析为相对路径已跳过"
        checks.append(_check("evidence_existence", status, detail))
    else:
        reason = "evidence_root 未提供" if root is None else "无可用相对路径定位"
        checks.append(
            _check(
                "evidence_existence",
                "skip",
                f"{len(refs)} 条证据引用未做存在性校验（{reason}）",
            )
        )
    return checks, errors


def _relative_hint(value: str) -> str:
    """Describe an absolute path without echoing the machine path."""
    name = str(value).replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    return f"不允许机器绝对路径（{name}）"


def _evidence_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    evidence_codes = ("evidence_missing", "evidence_sha_mismatch", "non_portable_path")
    return [error for error in errors if error.get("code") in evidence_codes]


def _outcome_next_actions(outcome: str, *, passed: bool) -> list[str]:
    if not passed:
        return [
            "按 errors[].details.field 修正 proposal 后，使用同一 task 重新提交校验",
            "提案不得绕过校验直接生效（发布走 P4 commit_operation）",
        ]
    if outcome == OUTCOME_PROPOSED_CHANGE:
        return [
            "校验通过只表示提案可提交，不等于已发布；正式发布走 P4 commit_operation，本函数不写规则/知识/案例",
            "按 verification_refs 执行正向 + 边界/反向验证后再提交",
            "提交时一并携带 evidence_refs 与 assumptions，保持声明与证据强度一致",
        ]
    if outcome == OUTCOME_UNRESOLVED:
        return [
            "本轮研究结束：记录 unresolved 结论与已查来源，相同资料条件下不重复调查",
            "用户提出重查、新资料到达或相关语义变化时，创建新的 ResearchTask",
            "不要把 unresolved 结论写成规则/已支持机制（保持未解决项可见）",
        ]
    return [
        "补齐提案声明缺少的输入后，使用同一 task 重新提交 proposal",
        "若输入无法获得，改为 outcome=unresolved + 显式假设，结束本轮而非无限重试",
    ]


def validate_proposal(
    proposal: Mapping[str, Any] | ResearchProposal,
    *,
    task: Mapping[str, Any] | ResearchTask | None = None,
    tasks_by_id: Any = None,
    current_fingerprint: str | None = None,
    evidence_root: str | Path | None = None,
) -> ValidationResult:
    """Validate one research proposal; never publishes, never executes research text.

    Checks performed (each reported in ``checks[]``):

    * ``proposal_contract`` — schema_version + required fields / value vocabularies;
    * ``task_lookup`` — task exists (``task`` or ``tasks_by_id``) and ids match;
    * ``fingerprint_consistency`` — ``based_on_fingerprint`` equals the task input
      fingerprint and, when given, ``current_fingerprint`` → otherwise the explicit
      ``stale_proposal`` error (the proposal must be re-derived, not re-applied);
    * ``evidence_existence`` — when ``evidence_root`` is given, repository-relative
      evidence locators must exist (sha256 verified when declared);
    * ``candidate_completeness`` / ``verification_declared`` — required for
      ``outcome=proposed_change``.

    ``outcome`` and evidence strength stay independent: ``unresolved`` /
    ``needs_input`` pass with a complete contract and a fresh fingerprint even
    without change candidates, which is how a research round ends without an
    infinite loop. ``outcome_effective`` records whether the declared outcome is
    consistent with the supplied evidence strength.
    """
    if current_fingerprint is not None and not _is_sha256(current_fingerprint):
        raise ContractError(
            "$.current_fingerprint: 必须是 64 位 sha256 十六进制字符串，"
            f"实际 {current_fingerprint!r}"
        )

    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    next_actions: list[str] = []
    accepted: list[str] = []
    outcome_effective: bool | None = None

    proposal_data, contract_errors = _collect_proposal(proposal)
    if contract_errors:
        errors.extend(_issue_from_field_error(error) for error in contract_errors)
        checks.append(
            _check(
                "proposal_contract",
                "fail",
                f"{len(contract_errors)} 个契约字段错误",
            )
        )
        next_actions = _outcome_next_actions(
            str(proposal_data.get("outcome") if proposal_data else ""), passed=False
        )
        return ValidationResult(
            kind="proposal",
            passed=False,
            accepted=[],
            errors=errors,
            checks=checks,
            next_actions=next_actions,
            outcome_effective=None,
        )
    assert proposal_data is not None
    outcome = proposal_data["outcome"]
    checks.append(
        _check(
            "proposal_contract",
            "pass",
            f"proposal_id={proposal_data['proposal_id']} outcome={outcome}",
        )
    )

    task_data, task_error = _resolve_task(proposal_data, task=task, tasks_by_id=tasks_by_id)
    if task_error is not None:
        errors.append(_issue_from_field_error(task_error))
        checks.append(_check("task_lookup", "fail", task_error["message"]))
        next_actions = [
            "先通过 make_research_task 生成（或 read_task_file 读取）task，再按 task_id 提交 proposal",
            "proposal.task_id 必须指向已存在的 ResearchTask",
        ]
        return ValidationResult(
            kind="proposal",
            passed=False,
            accepted=[],
            errors=errors,
            checks=checks,
            next_actions=next_actions,
            outcome_effective=None,
        )
    assert task_data is not None
    checks.append(
        _check("task_lookup", "pass", f"task_id={task_data['task_id']} gap_id={task_data['gap_id']}")
    )

    stale_reasons: list[str] = []
    if proposal_data["based_on_fingerprint"] != task_data["input_fingerprint"]:
        stale_reasons.append(
            "based_on_fingerprint 与 task.input_fingerprint 不一致"
            f"（proposal={proposal_data['based_on_fingerprint'][:16]}…，"
            f"task={task_data['input_fingerprint'][:16]}…）"
        )
    if current_fingerprint is not None and proposal_data["based_on_fingerprint"] != current_fingerprint:
        stale_reasons.append(
            "based_on_fingerprint 与当前输入指纹不一致"
            f"（proposal={proposal_data['based_on_fingerprint'][:16]}…，"
            f"current={current_fingerprint[:16]}…）"
        )
    if current_fingerprint is not None and task_data["input_fingerprint"] != current_fingerprint:
        stale_reasons.append("task.input_fingerprint 与当前输入指纹不一致（task 已过期）")
    if stale_reasons:
        errors.append(
            _issue(
                "stale_proposal",
                "；".join(stale_reasons) + "；不得静默套用旧提案，请按当前输入重新解析/调查",
                field="$.based_on_fingerprint",
                related_ids=[proposal_data["task_id"]],
            )
        )
        checks.append(_check("fingerprint_consistency", "fail", "；".join(stale_reasons)))
        return ValidationResult(
            kind="proposal",
            passed=False,
            accepted=[],
            errors=errors,
            checks=checks,
            next_actions=[
                "输入指纹已变化：按当前输入重新生成 ResearchTask（新 task_id）并重新调查",
                "不要套用旧提案；编排会按新指纹重新检查适用性",
            ],
            outcome_effective=None,
        )
    checks.append(
        _check("fingerprint_consistency", "pass", "based_on_fingerprint 与 task/当前输入一致")
    )

    evidence_checks, evidence_check_errors = _check_evidence_refs(
        proposal_data["evidence_refs"], evidence_root
    )
    checks.extend(evidence_checks)
    errors.extend(evidence_check_errors)

    candidates = (
        list(proposal_data["mechanism_refs"])
        + list(proposal_data["mapping_refs"])
        + list(proposal_data["code_change_refs"])
    )
    if outcome == OUTCOME_PROPOSED_CHANGE:
        candidate_ok = bool(candidates)
        evidence_ok = bool(proposal_data["evidence_refs"])
        verification_ok = bool(proposal_data["verification_refs"])
        if not candidate_ok:
            errors.append(
                _issue(
                    "missing_candidates",
                    "outcome=proposed_change 至少需要 mechanism_refs / mapping_refs / "
                    "code_change_refs 之一（候选以产物/变更引用提交）",
                    field="$.mechanism_refs",
                )
            )
        if not evidence_ok:
            errors.append(
                _issue(
                    "missing_evidence",
                    "outcome=proposed_change 必须提供 evidence_refs（证据定位）",
                    field="$.evidence_refs",
                )
            )
        if not verification_ok:
            errors.append(
                _issue(
                    "missing_verification",
                    "outcome=proposed_change 必须提供 verification_refs（正向 + 边界/反向验证）",
                    field="$.verification_refs",
                )
            )
        checks.append(
            _check(
                "candidate_completeness",
                "pass" if candidate_ok else "fail",
                f"候选变更 {len(candidates)} 条"
                if candidate_ok
                else "缺少候选变更引用",
            )
        )
        checks.append(
            _check(
                "verification_declared",
                "pass" if verification_ok else "fail",
                f"验证项 {len(proposal_data['verification_refs'])} 条"
                if verification_ok
                else "缺少 verification_refs",
            )
        )
        outcome_effective = bool(
            candidate_ok and evidence_ok and verification_ok and not _evidence_errors(errors)
        )
        if not errors:
            accepted = [f"outcome:{outcome}"] + candidates
    else:
        consistency = not proposal_data["code_change_refs"]
        checks.append(
            _check(
                "outcome_evidence_consistency",
                "pass" if consistency else "warn",
                "outcome 声明不再提出变更" if consistency else (
                    "outcome 声明为 " + outcome + "，但仍提交了 code_change_refs（需人工复核）"
                ),
            )
        )
        outcome_effective = consistency
        if not errors:
            accepted = [f"outcome:{outcome}"]

    passed = not errors
    if passed:
        next_actions = _outcome_next_actions(outcome, passed=True)
        if not accepted:
            accepted = [f"outcome:{outcome}"]
    else:
        next_actions = _outcome_next_actions(outcome, passed=False)

    return ValidationResult(
        kind="proposal",
        passed=passed,
        accepted=accepted if passed else [],
        errors=errors,
        checks=checks,
        next_actions=next_actions,
        outcome_effective=outcome_effective,
    )


# ---------------------------------------------------------------------------
# file exchange
# ---------------------------------------------------------------------------


def _read_json_file(path: str | Path, *, what: str) -> dict[str, Any]:
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"{what} 文件读取失败（{p.name}）：{exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ContractError(f"{what} 文件不是合法 JSON（{p.name}）：{exc}") from exc
    if not isinstance(payload, Mapping):
        raise ContractError(f"{what} 文件必须是 JSON 对象（{p.name}）")
    return {str(key): value for key, value in payload.items()}


def write_task_file(path: str | Path, task: Mapping[str, Any] | ResearchTask) -> Path:
    """Write one ``ResearchTask`` JSON file (UTF-8, schema_version included)."""
    payload = _task_payload(task)
    target = Path(path)
    write_json(target, payload)
    return target


def read_task_file(path: str | Path) -> dict[str, Any]:
    """Read and strictly validate one ``ResearchTask`` JSON file."""
    payload = _read_json_file(path, what="ResearchTask")
    version = payload.get("schema_version")
    if version != SCHEMA_RESEARCH_TASK:
        raise ContractError(
            f"ResearchTask 文件 schema_version 不支持：{version!r}（需要 {SCHEMA_RESEARCH_TASK}）"
        )
    data, errors = _collect_task(payload)
    _raise_field_errors(errors, what="ResearchTask")
    assert data is not None
    return _task_payload(data)


def write_proposal_file(
    path: str | Path, proposal: Mapping[str, Any] | ResearchProposal
) -> Path:
    """Write one ``ResearchProposal`` JSON file (UTF-8, schema_version included)."""
    payload = _proposal_payload(proposal)
    target = Path(path)
    write_json(target, payload)
    return target


def read_proposal_file(path: str | Path) -> dict[str, Any]:
    """Read and strictly validate one ``ResearchProposal`` JSON file."""
    payload = _read_json_file(path, what="ResearchProposal")
    version = payload.get("schema_version")
    if version != SCHEMA_RESEARCH_PROPOSAL:
        raise ContractError(
            "ResearchProposal 文件 schema_version 不支持："
            f"{version!r}（需要 {SCHEMA_RESEARCH_PROPOSAL}）"
        )
    data, errors = _collect_proposal(payload)
    _raise_field_errors(errors, what="ResearchProposal")
    assert data is not None
    return _proposal_payload(data)


__all__ = [
    "ASSUMPTION_STATUSES",
    "DELIVERABLE_KINDS",
    "PROPOSAL_FIELDS",
    "SCHEMA_PROPOSAL_FILE",
    "SCHEMA_TASK_FILE",
    "TASK_FIELDS",
    "make_research_task",
    "read_proposal_file",
    "read_task_file",
    "validate_proposal",
    "write_proposal_file",
    "write_task_file",
]
