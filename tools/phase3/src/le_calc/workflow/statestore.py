"""Minimal file-backed pipeline state store (``le-pipeline-state-v1`` / P3).

Responsibilities (workflow-interface §7.4, P3 scope only):

* persist one ``pipeline-state.json`` per run next to the run artefacts;
* record every :class:`~le_calc.workflow.contract.StageState` at stage boundaries;
* on resume, **verify the actual outputs** (re-hash artifact refs) instead of
  trusting the progress flags — a broken stage invalidates that stage and every
  downstream stage;
* atomic save (temp file + ``os.replace``) so a crash cannot leave half state.

No business logic, no printing, no hidden global state.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from . import contract

STATE_FILE = "pipeline-state.json"


class StateStoreError(ValueError):
    """Raised when a pipeline state document exists but cannot be trusted."""


class FileStateStore:
    """Load/save ``pipeline-state.json`` and verify recorded stage outputs."""

    def __init__(self, run_dir: str | Path, *, root: Optional[str | Path] = None):
        self.run_dir = Path(run_dir)
        self.root = Path(root).resolve() if root is not None else Path.cwd().resolve()
        if not self.run_dir.is_absolute():
            self.run_dir = self.root / self.run_dir
        self.run_dir = self.run_dir.resolve()
        self.path = self.run_dir / STATE_FILE

    # -- io -------------------------------------------------------------
    def load(self) -> Optional[contract.PipelineState]:
        """Load state; ``None`` when absent, raise on corrupt/incompatible state."""

        if not self.path.is_file():
            return None
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StateStoreError(f"pipeline-state.json 不是合法 JSON：{exc}") from exc
        if not isinstance(document, dict):
            raise StateStoreError("pipeline-state.json 必须是 JSON 对象")
        if document.get("schema_version") != contract.SCHEMA_PIPELINE_STATE:
            raise StateStoreError(
                f"pipeline-state schema_version 不受支持：{document.get('schema_version')!r}"
            )
        try:
            return contract.PipelineState.from_dict(document)
        except contract.ContractError as exc:
            raise StateStoreError(f"pipeline-state 校验失败：{exc}") from exc

    def save(self, state: contract.PipelineState) -> dict[str, Any]:
        """Atomically persist ``state`` and return its artifact-ref style record."""

        if not isinstance(state, contract.PipelineState):
            raise StateStoreError("save 需要 contract.PipelineState")
        payload = state.to_dict()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(
            prefix=".pipeline-state-", suffix=".json.tmp", dir=str(self.run_dir)
        )
        try:
            with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:  # pragma: no cover - best effort
                pass
            raise
        return {
            "path": self._display(self.path),
            "sha256": contract.sha256_file(self.path),
            "schema_version": contract.SCHEMA_PIPELINE_STATE,
        }

    # -- stage bookkeeping ---------------------------------------------
    def record(self, state: contract.PipelineState, result: contract.StageResult) -> None:
        """Insert/replace one stage entry from a :class:`StageResult` (no save)."""

        entry = contract.StageState(
            stage_id=result.stage_id,
            execution_status=result.execution_status,
            readiness=result.readiness,
            input_fingerprint=(
                result.metrics.get("input_fingerprint") if isinstance(result.metrics, dict) else None
            ),
            implementation_fingerprint=(
                result.metrics.get("implementation_fingerprint")
                if isinstance(result.metrics, dict)
                else None
            ),
            parameters_fingerprint=(
                result.metrics.get("parameters_fingerprint")
                if isinstance(result.metrics, dict)
                else None
            ),
            cache_key=(result.cache or {}).get("key"),
            output_refs=list(result.output_refs),
            elapsed_ms=(result.metrics or {}).get("elapsed_ms"),
            cache_status=(result.cache or {}).get("status"),
        )
        for index, existing in enumerate(state.stages):
            if existing.stage_id == result.stage_id:
                state.stages[index] = entry
                return
        state.stages.append(entry)

    # -- verification / invalidation -----------------------------------
    def verify(self, state: contract.PipelineState) -> dict[str, Any]:
        """Re-hash every recorded output ref and invalidate broken stages + downstream.

        Returns ``{"valid": [...], "invalidated": [{"stage_id","reason",...}],
        "downstream_invalidated": [...]}``.  The state object is **not** mutated
        here; callers decide whether to persist the invalidation.
        """

        valid: list[str] = []
        invalidated: list[dict[str, Any]] = []
        downstream: list[str] = []
        broken_seen = False
        for stage in state.stages:
            if broken_seen:
                downstream.append(stage.stage_id)
                continue
            problems: list[dict[str, Any]] = []
            for ref in stage.output_refs:
                path_value = str((ref or {}).get("path") or "")
                expected = str((ref or {}).get("sha256") or "")
                if not path_value or not expected:
                    problems.append({"path": path_value, "reason": "output_ref_missing_path_or_sha"})
                    continue
                path = Path(path_value)
                if not path.is_absolute():
                    path = self.root / path
                if not path.is_file():
                    problems.append({"path": path_value, "reason": "output_missing"})
                elif contract.sha256_file(path) != expected:
                    problems.append({"path": path_value, "reason": "sha256_mismatch"})
            if problems:
                broken_seen = True
                invalidated.append(
                    {
                        "stage_id": stage.stage_id,
                        "execution_status": stage.execution_status,
                        "reason": problems[0]["reason"],
                        "problems": problems,
                    }
                )
            elif stage.execution_status != contract.EXECUTION_COMPLETED:
                broken_seen = True
                invalidated.append(
                    {
                        "stage_id": stage.stage_id,
                        "execution_status": stage.execution_status,
                        "reason": f"stage status {stage.execution_status!r} is not completed",
                        "problems": [],
                    }
                )
            else:
                valid.append(stage.stage_id)
        return {
            "valid": valid,
            "invalidated": invalidated,
            "downstream_invalidated": downstream,
        }

    def verify_fingerprints(
        self,
        state: contract.PipelineState,
        current_fingerprints: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, Any]:
        """Compare recorded stage fingerprints/cache keys with the current inputs.

        ``current_fingerprints`` maps ``stage_id`` to the freshly computed
        ``input/implementation/parameters/cache_key`` values (see
        :func:`le_calc.workflow.stage.stage_fingerprints`).  The first changed
        stage invalidates itself and every downstream stage.
        """

        valid: list[str] = []
        invalidated: list[dict[str, Any]] = []
        downstream: list[str] = []
        broken = False
        for stage in state.stages:
            if broken:
                downstream.append(stage.stage_id)
                continue
            expected = current_fingerprints.get(stage.stage_id)
            if expected is None:
                valid.append(stage.stage_id)
                continue
            problems: list[dict[str, Any]] = []
            for field in ("input_fingerprint", "implementation_fingerprint",
                          "parameters_fingerprint", "cache_key"):
                recorded = getattr(stage, field)
                current = expected.get(field)
                if recorded is not None and current is not None and str(recorded) != str(current):
                    problems.append({"field": field, "recorded": str(recorded)[:16], "current": str(current)[:16]})
            if problems:
                broken = True
                invalidated.append(
                    {
                        "stage_id": stage.stage_id,
                        "execution_status": stage.execution_status,
                        "reason": "fingerprint_changed",
                        "problems": problems,
                    }
                )
            else:
                valid.append(stage.stage_id)
        return {"valid": valid, "invalidated": invalidated, "downstream_invalidated": downstream}

    def resume_plan(
        self,
        state: contract.PipelineState,
        current_fingerprints: Optional[Mapping[str, Mapping[str, Any]]] = None,
    ) -> dict[str, Any]:
        """Return the verification report plus the first stage to re-run."""

        report = self.verify(state)
        if current_fingerprints is not None:
            fp_report = self.verify_fingerprints(state, current_fingerprints)
            # Combine in stage order: first invalid (outputs or fingerprints) plus downstream.
            output_invalid = {item["stage_id"]: item for item in report["invalidated"]}
            fp_invalid = {item["stage_id"]: item for item in fp_report["invalidated"]}
            valid: list[str] = []
            invalidated: list[dict[str, Any]] = []
            downstream: list[str] = []
            broken = False
            for stage in state.stages:
                if broken:
                    downstream.append(stage.stage_id)
                elif stage.stage_id in output_invalid or stage.stage_id in fp_invalid:
                    broken = True
                    invalidated.append(output_invalid.get(stage.stage_id)
                                       or fp_invalid.get(stage.stage_id))
                else:
                    valid.append(stage.stage_id)
            report = {
                "valid": valid,
                "invalidated": invalidated,
                "downstream_invalidated": downstream,
            }
        restart_after = report["valid"][-1] if report["valid"] else None
        broken = [item["stage_id"] for item in report["invalidated"]]
        return {
            "valid": report["valid"],
            "invalidated": report["invalidated"],
            "downstream_invalidated": report["downstream_invalidated"],
            "restart_from": broken[0] if broken else None,
            "resume_stage": restart_after,
        }

    def invalidate(self, state: contract.PipelineState, stage_ids: Iterable[str]) -> list[str]:
        """Reset matching stages (and keep downstream) so they will re-run."""

        targets = set(stage_ids)
        removed: list[str] = []
        kept: list[contract.StageState] = []
        for stage in state.stages:
            if stage.stage_id in targets:
                removed.append(stage.stage_id)
            else:
                kept.append(stage)
        state.stages = kept
        return removed

    def _display(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return "<external-path>"


__all__ = ["FileStateStore", "STATE_FILE", "StateStoreError"]
