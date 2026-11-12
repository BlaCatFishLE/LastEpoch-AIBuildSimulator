"""Configuration loading with explicit validation.

The checked-in example is ``config/sources.example.json``.  A run may use a
local ``config/sources.json`` (not required) that overrides entry points and
policies; the composition is recorded in the run manifest.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import CONFIG_DIR


@dataclass
class SourcesConfig:
    data: dict[str, Any]
    path: Path | None = None

    @property
    def sources(self) -> dict[str, Any]:
        return self.data.get("sources", {})

    def get(self, source: str) -> dict[str, Any]:
        try:
            return self.sources[source]
        except KeyError:
            raise KeyError(f"unknown source {source!r}; known: {sorted(self.sources)}") from None

    def enabled_sources(self) -> list[str]:
        return [name for name, cfg in self.sources.items() if cfg.get("enabled")]


def load_sources(path: Path | None = None) -> SourcesConfig:
    candidates = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates.extend([CONFIG_DIR / "sources.json", CONFIG_DIR / "sources.example.json"])
    for candidate in candidates:
        if candidate.exists():
            data = json.loads(candidate.read_text(encoding="utf-8"))
            if "sources" not in data:
                raise ValueError(f"{candidate} has no 'sources' object")
            return SourcesConfig(data=data, path=candidate)
    raise FileNotFoundError(
        f"no source configuration found; looked for {[str(c) for c in candidates]}"
    )


def load_coverage_scope(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else CONFIG_DIR / "coverage-scope.json"
    return json.loads(Path(target).read_text(encoding="utf-8"))
