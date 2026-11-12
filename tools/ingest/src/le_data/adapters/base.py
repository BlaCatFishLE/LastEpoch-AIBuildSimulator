"""Adapter base class and registry.

Each source adapter implements three independent steps so that they can be
tested and re-run separately:

  * ``discover``  -> inventory of resources + entity index (network or cache)
  * ``fetch``     -> exact raw responses on disk (delegated to Fetch)
  * ``parse``     -> SourceRecord stream from local raw files only

Parsers must be able to run with ``offline=True``: they only read files that
fetch already produced.  Adapters never write to the normalized database.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

from ..fetch import Fetcher
from ..records import SourceIssue, SourceRecord


class BaseAdapter:
    name: str = "base"

    def __init__(self, config: dict[str, Any], raw_root: Path, logger=None) -> None:
        self.config = config
        self.raw_root = Path(raw_root)
        self.logger = logger or (lambda msg: None)
        self.issues: list[SourceIssue] = []

    # -- lifecycle ---------------------------------------------------------
    def fetcher(self, snapshot_id: str, offline: bool = False) -> Fetcher:
        policy = self.config.get("policy", {})
        from ..fetch import SourcePolicy

        return Fetcher(
            source=self.name,
            raw_root=self.raw_root,
            snapshot_id=snapshot_id,
            policy=SourcePolicy(**policy),
            offline=offline,
            logger=self.logger,
        )

    def discover(self, snapshot_id: str, offline: bool = False) -> dict[str, Any]:
        raise NotImplementedError

    def fetch(self, snapshot_id: str, offline: bool = False, inventory: dict | None = None) -> list[dict]:
        raise NotImplementedError

    def parse(self, snapshot_id: str) -> Iterator[SourceRecord]:
        raise NotImplementedError

    # -- helpers -----------------------------------------------------------
    def add_issue(self, **kwargs: Any) -> SourceIssue:
        issue = SourceIssue(source=self.name, **kwargs)
        self.issues.append(issue)
        return issue

    def resource_path(self, snapshot_id: str, resource_name: str) -> Path:
        return self.raw_root / self.name / snapshot_id / "responses" / f"{resource_name}.body"
