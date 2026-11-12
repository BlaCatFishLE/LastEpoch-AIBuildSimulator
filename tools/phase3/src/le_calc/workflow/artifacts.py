"""Analysis artifacts writer (``le-workflow-v1`` / P2).

Turns a :func:`le_calc.workflow.domain.resolve_build` result into the three
structured artifacts required by the implementation plan §9:

* ``analysis-plan.json`` (``le-analysis-plan-v1``)
* ``coverage.json`` (``le-coverage-v1``) — included / excluded_with_reason /
  unresolved / out_of_scope + selected scope
* ``research-queue.json`` (``le-research-queue-v1``) — one ``ResearchTask``-shaped
  entry per gap (question / sources / related mechanisms / already researched /
  affected blocks / deliverable / verification requirements)

Properties: deterministic JSON, repository-relative paths only, no absolute paths
in the payloads, and every artifact carries ``schema_version``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from . import contract
from .contract import (
    ContractError,
    SCHEMA_ANALYSIS_PLAN,
    SCHEMA_COVERAGE,
    SCHEMA_RESEARCH_QUEUE,
    SCHEMA_RESEARCH_TASK,
    deterministic_id,
    make_artifact_ref,
)

DEFAULT_ROOT = Path(__file__).resolve().parents[5]

ARTIFACT_NAMES = ("analysis-plan.json", "coverage.json", "research-queue.json")
_ABS_WINDOWS_RE = re.compile(r"^[A-Za-z]:[\\/]")
_ABS_POSIX_RE = re.compile(r"^/(?:home|Users|root|mnt|tmp|var|opt|srv)/")


def _root(root: Optional[str | Path]) -> Path:
    return Path(root).resolve() if root is not None else DEFAULT_ROOT


def _relative(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _resolve(path: str | Path, root: Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()
    try:
        p.relative_to(root)
    except ValueError as exc:
        raise ContractError(f"path outside repository root: {path}") from exc
    return p


def find_absolute_paths(payload: Any) -> list[str]:
    """Return JSON-pointer-ish paths of strings that look like machine paths."""

    found: list[str] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")
        elif isinstance(node, str):
            if _ABS_WINDOWS_RE.match(node) or _ABS_POSIX_RE.match(node) or node.startswith("\\\\"):
                found.append(f"{path} = {node}")

    visit(payload, "")
    return found


# ---------------------------------------------------------------------------
# research queue
# ---------------------------------------------------------------------------
def build_research_queue(resolve_result: Mapping[str, Any]) -> dict[str, Any]:
    """Build the ``le-research-queue-v1`` payload from a resolve-build result."""

    case_id = str(resolve_result.get("case_id") or "")
    input_fingerprint = str(resolve_result.get("input_fingerprint") or "")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for gap in resolve_result.get("gaps") or []:
        if not isinstance(gap, Mapping):
            continue
        gap_id = str(gap.get("gap_id") or deterministic_id("gap", dict(gap)))
        task_seed = {"gap_id": gap_id, "input_fingerprint": input_fingerprint}
        task_id = deterministic_id("research_task", task_seed)
        if task_id in seen:
            continue
        seen.add(task_id)
        affected = gap.get("affected") or {}
        components = [str(item) for item in affected.get("components") or []]
        if not components:
            # Enrich from the bound mechanisms so a queue entry always carries a
            # concrete impact scope; `affected.components == []` alone would not
            # satisfy the research-task contract (see research._collect_task).
            bound = {
                str(item.get("mechanism_id")): item
                for item in (resolve_result.get("bound_mechanisms") or [])
                if isinstance(item, Mapping)
            }
            for ref in gap.get("related_mechanism_refs") or []:
                mech = bound.get(str(ref))
                if not mech:
                    continue
                for component in mech.get("components") or []:
                    text = str(component)
                    if text and text not in components:
                        components.append(text)
        calculation_blocks = [str(item) for item in affected.get("calculation_blocks") or []]
        entry = {
            "schema_version": SCHEMA_RESEARCH_TASK,
            "task_id": task_id,
            "gap_id": gap_id,
            "input_fingerprint": input_fingerprint,
            "question": str(gap.get("question") or ""),
            "source_refs": [dict(ref) for ref in gap.get("source_refs") or []],
            "related_mechanism_refs": [str(item) for item in gap.get("related_mechanism_refs") or []],
            "researched_already": [str(item) for item in gap.get("researched_already") or []],
            "affected": {
                "components": components,
                "calculation_blocks": calculation_blocks,
                "blocks_target": bool(affected.get("blocks_target")),
            },
            "deliverable_kind": str(gap.get("deliverable_kind") or "mapping"),
            "verification_required": [str(item) for item in gap.get("verification_required") or []],
        }
        entries.append(entry)
    entries.sort(key=lambda entry: entry["task_id"])
    readiness = str(resolve_result.get("readiness") or "")
    blocking = sum(
        1
        for gap in resolve_result.get("gaps") or []
        if isinstance(gap, Mapping) and gap.get("blocking")
    )
    return {
        "schema_version": SCHEMA_RESEARCH_QUEUE,
        "case_id": case_id,
        "input_fingerprint": input_fingerprint,
        "readiness": readiness,
        "summary": {
            "tasks": len(entries),
            "blocking": blocking,
            "by_deliverable_kind": _count(entries, "deliverable_kind"),
        },
        "entries": entries,
    }


def _count(entries: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        value = str(entry.get(key) or "")
        counts[value] = counts.get(value, 0) + 1
    return dict(sorted(counts.items()))


# ---------------------------------------------------------------------------
# writer
# ---------------------------------------------------------------------------
def write_analysis_artifacts(
    resolve_result: Mapping[str, Any],
    output_dir: str | Path,
    *,
    root: Optional[str | Path] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Write analysis-plan / coverage / research-queue into ``output_dir``.

    Returns a small manifest with repository-relative paths, sha256 digests and
    artifact refs.  Refuses to overwrite existing artifact files unless
    ``overwrite=True`` (same protection as historical run directories).
    """

    root_path = _root(root)
    if not isinstance(resolve_result, Mapping):
        raise ContractError("resolve_result must be an object")
    plan = resolve_result.get("analysis_plan")
    coverage = resolve_result.get("coverage")
    if not isinstance(plan, Mapping) or not isinstance(coverage, Mapping):
        raise ContractError("resolve_result must contain analysis_plan and coverage objects")
    if plan.get("schema_version") != SCHEMA_ANALYSIS_PLAN:
        raise ContractError(f"analysis_plan schema mismatch: {plan.get('schema_version')!r}")
    if coverage.get("schema_version") != SCHEMA_COVERAGE:
        raise ContractError(f"coverage schema mismatch: {coverage.get('schema_version')!r}")

    out = _resolve(output_dir, root_path)
    files: dict[str, str] = {
        "analysis-plan.json": plan,
        "coverage.json": coverage,
        "research-queue.json": build_research_queue(resolve_result),
    }
    for payload in files.values():
        absolute = find_absolute_paths(payload)
        if absolute:
            raise ContractError(
                "artifact payload contains machine absolute paths: " + "; ".join(absolute[:5])
            )
    out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, payload in files.items():
        path = out / name
        if path.exists() and not overwrite:
            raise ContractError(f"artifact already exists (refuse to overwrite): {_relative(path, root_path)}")
        contract.write_json(path, payload)
        written[name] = _relative(path, root_path)

    refs = [
        make_artifact_ref(
            out / name,
            root=root_path,
            artifact_id=f"analysis:{resolve_result.get('case_id')}:{name}",
            schema_version=_schema_of(name),
        )
        for name in ARTIFACT_NAMES
    ]
    return {
        "schema_version": "le-analysis-artifact-set-v1",
        "case_id": resolve_result.get("case_id"),
        "output_dir": _relative(out, root_path),
        "files": written,
        "artifact_refs": refs,
        "counts": {
            "checklist_items": len((plan.get("checklist") or [])),
            "gaps": len(resolve_result.get("gaps") or []),
            "research_tasks": len((files["research-queue.json"].get("entries") or [])),
            "coverage_included": ((coverage.get("counts") or {}).get("included")),
            "coverage_unresolved": ((coverage.get("counts") or {}).get("unresolved")),
        },
    }


def _schema_of(name: str) -> str:
    return {
        "analysis-plan.json": SCHEMA_ANALYSIS_PLAN,
        "coverage.json": SCHEMA_COVERAGE,
        "research-queue.json": SCHEMA_RESEARCH_QUEUE,
    }[name]


def read_analysis_artifacts(output_dir: str | Path, *, root: Optional[str | Path] = None) -> dict[str, Any]:
    """Read back the three artifacts (used by round-trip tests / other stages)."""

    root_path = _root(root)
    out = _resolve(output_dir, root_path)
    payloads: dict[str, Any] = {}
    for name in ARTIFACT_NAMES:
        path = out / name
        if not path.is_file():
            raise ContractError(f"artifact missing: {_relative(path, root_path)}")
        try:
            payloads[name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ContractError(f"artifact is not valid JSON: {name}: {exc}") from exc
    return payloads


__all__ = [
    "ARTIFACT_NAMES",
    "DEFAULT_ROOT",
    "build_research_queue",
    "find_absolute_paths",
    "read_analysis_artifacts",
    "write_analysis_artifacts",
]
