"""Workflow layer (``le-workflow-v1``).

This package is the executable boundary described by
:file:`docs/contracts/workflow-interface.md`. It deliberately does **not** import
LangGraph or any orchestration framework: the current callers are the local CLI /
orchestration, and the interfaces stay plain JSON in / JSON out.

Modules:

* :mod:`le_calc.workflow.contract` — payload types, vocabularies, JSON round-trip,
  artifact-ref helpers. Stdlib only, no damage-engine imports.
* ``domain`` (P2) — ``resolve_build`` / ``simulate`` / ``validate_result`` thin
  adapters over the existing engine and the knowledge catalog.
* ``checklist`` (P2) — the fixed nine-item DPS inspection checklist.
* ``artifacts`` (P2) — analysis-plan / coverage / research-queue writers.
* ``research`` (P2) — ``make_research_task`` / ``validate_proposal`` hand-off.
* ``commit`` / ``statestore`` (P4) — idempotent commit and ``StateStore``.

Only :mod:`contract` is imported eagerly. The heavier modules (``stage``,
``statestore``, ``domain``, ``artifacts``, ``checklist``, ``research``,
``expected_check``) are resolved lazily through ``__getattr__`` so importing the
package for the payload types does not pull the damage engine or subprocess layer.
"""

from __future__ import annotations

import importlib

from . import contract
from .contract import (  # noqa: F401  (re-exported for convenient imports)
    CONTRACT_ID,
    ContractError,
    CommitRequest,
    CommitResult,
    PipelineState,
    ResearchProposal,
    ResearchTask,
    StageContext,
    StageRequest,
    StageResult,
    StageState,
    ValidationResult,
    deterministic_id,
    dumps,
    fingerprint,
    loads,
    make_artifact_ref,
    make_cache_info,
    make_issue,
    read_json,
    sha256_bytes,
    sha256_file,
    verify_artifact_ref,
    write_json,
)

_LAZY_MODULES = frozenset(
    {"artifacts", "checklist", "domain", "expected_check", "research", "stage", "statestore"}
)


def __getattr__(name: str):  # pragma: no cover - thin import shim
    if name in _LAZY_MODULES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "contract",
    "CONTRACT_ID",
    "ContractError",
    "CommitRequest",
    "CommitResult",
    "PipelineState",
    "ResearchProposal",
    "ResearchTask",
    "StageContext",
    "StageRequest",
    "StageResult",
    "StageState",
    "ValidationResult",
    "deterministic_id",
    "dumps",
    "fingerprint",
    "loads",
    "make_artifact_ref",
    "make_cache_info",
    "make_issue",
    "read_json",
    "sha256_bytes",
    "sha256_file",
    "verify_artifact_ref",
    "write_json",
]
