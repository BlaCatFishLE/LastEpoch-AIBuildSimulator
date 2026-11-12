"""Offline single-file HTML damage report (redesigned)."""

from __future__ import annotations

__all__ = ["render", "write_html", "build_view"]

from .present import build_view  # noqa: E402

try:  # renderer is imported lazily so present.py can be used/tested alone
    from .shell import render, write_html  # noqa: E402
except Exception:  # pragma: no cover - during staged development
    def render(result):  # type: ignore
        raise RuntimeError("report_ui.shell is not available yet")

    def write_html(result, path):  # type: ignore
        raise RuntimeError("report_ui.shell is not available yet")
