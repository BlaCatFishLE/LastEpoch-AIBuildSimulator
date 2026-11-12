"""Adapter registry.

Adapters are imported lazily so that a missing optional dependency for one
source (for example the local game asset tool) never blocks the others.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import BaseAdapter


def get_adapter(
    name: str,
    config: dict[str, Any],
    raw_root: Path,
    logger=None,
) -> BaseAdapter:
    if name == "letools":
        from .letools import LeToolsAdapter

        return LeToolsAdapter(config=config, raw_root=raw_root, logger=logger)
    if name == "tunklab":
        from .tunklab import TunklabAdapter

        return TunklabAdapter(config=config, raw_root=raw_root, logger=logger)
    raise KeyError(f"unknown adapter {name!r}")


def adapter_names() -> list[str]:
    return ["letools", "tunklab"]
