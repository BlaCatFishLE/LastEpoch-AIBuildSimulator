"""Run state: one execution of discover/fetch/parse/normalize/validate.

A ``run_id`` is not a dataset id (workguide 7.1).  The run records which
source snapshots it used so that a dataset can be rebuilt from them later.
State lives in ``data/staging/<run-id>/run.json`` and events are appended to
``reports/<run-id>/events.jsonl`` for debugging.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .paths import raw_source_dir, report_dir, staging_dir
from .store import now_utc


@dataclass
class RunState:
    run_id: str
    created_at: str
    updated_at: str
    snapshots: dict[str, str] = field(default_factory=dict)
    source_status: dict[str, str] = field(default_factory=dict)
    stages: dict[str, dict[str, str]] = field(default_factory=dict)
    notes: dict[str, Any] = field(default_factory=dict)
    dataset_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "snapshots": self.snapshots,
            "source_status": self.source_status,
            "stages": self.stages,
            "notes": self.notes,
            "dataset_id": self.dataset_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunState":
        return cls(**data)


class RunStore:
    def __init__(self, run_id: str, project_root: Path | None = None) -> None:
        self.run_id = run_id
        self.path = staging_dir(run_id) / "run.json"
        self.events_path = report_dir(run_id) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RunState | None:
        if not self.path.exists():
            return None
        return RunState.from_dict(json.loads(self.path.read_text(encoding="utf-8")))

    def create_or_load(self, sources: list[str] | None = None) -> RunState:
        state = self.load()
        if state is not None:
            return state
        stamp = now_utc()
        state = RunState(run_id=self.run_id, created_at=stamp, updated_at=stamp)
        if sources:
            for source in sources:
                state.source_status[source] = "pending"
        self.save(state)
        self.event("run_created", {"sources": sources or []})
        return state

    def save(self, state: RunState) -> None:
        state.updated_at = now_utc()
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def set_snapshot(self, source: str, snapshot_id: str, status: str = "discovered") -> RunState:
        state = self.create_or_load()
        state.snapshots[source] = snapshot_id
        state.source_status[source] = status
        self.save(state)
        return state

    def mark_stage(self, stage: str, source: str, status: str, detail: str | None = None) -> RunState:
        state = self.create_or_load()
        state.stages.setdefault(stage, {})[source] = status
        if source in state.source_status:
            state.source_status[source] = status
        self.save(state)
        self.event(f"{stage}_{status}", {"source": source, "detail": detail})
        return state

    def event(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": now_utc(), "event": event, "detail": detail or {}}
        with open(self.events_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    def resource_dir(self, source: str) -> Path:
        snapshot_id = self.create_or_load().snapshots.get(source)
        if not snapshot_id:
            raise KeyError(f"run {self.run_id} has no snapshot for source {source}")
        return raw_source_dir(source, snapshot_id)

    def source_records_path(self, source: str) -> Path:
        return staging_dir(self.run_id) / "source_records" / f"{source}.jsonl"

    def summary_path(self, source: str) -> Path:
        return staging_dir(self.run_id) / f"{source}_summary.json"

    def staging_db_path(self) -> Path:
        return staging_dir(self.run_id) / "data.sqlite"
