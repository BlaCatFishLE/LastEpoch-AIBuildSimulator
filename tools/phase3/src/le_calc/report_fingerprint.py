"""Authoritative renderer/asset fingerprint (O-C single source).

``renderer_fingerprint(repo_root)`` is the **only** implementation of the
renderer fingerprint used by:

* the player report page (``report_ui.document`` fallback, delegated by the
  renderer owner),
* :mod:`le_calc.report_capabilities` (``report-capabilities.json``, page
  capabilities, bindings),
* the report cache key (``le_calc.recalc.report_cache_key``),
* the recompute services (served pages always carry the same value).

Coverage: the whole ``report_ui`` package (all Python pages/components, every
asset under ``report_ui/assets`` — including ``app.js``, ``shell.js``,
``recalc_contract.js``, ``style.css`` — and anything else shipped in the
package) plus the model renderers ``report.py`` / ``hit_report.py`` / ``html.py``.
The hash is content-addressed over repository-relative POSIX paths, so it is
portable across machines/checkouts as long as the bytes are identical.

Bump :data:`FINGERPRINT_FORMAT` when the fingerprint *algorithm* (not renderer
content) changes, so downstream caches invalidate explicitly.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Fingerprint algorithm/format identity (not renderer content).
FINGERPRINT_FORMAT = "le-renderer-fingerprint-v2"

#: Model renderer files that are part of the player report output.
MODEL_RENDERER_PATHS: tuple[str, ...] = (
    "tools/phase3/src/le_calc/report.py",
    "tools/phase3/src/le_calc/hit_report.py",
    "tools/phase3/src/le_calc/html.py",
)

#: Shared renderer package (all files are fingerprinted, not just *.py).
REPORT_UI_DIR = "tools/phase3/src/le_calc/report_ui"

#: File suffixes that are never renderer inputs (byte-code caches etc.).
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".pyd", ".tmp", ".swp"})


def _iter_renderer_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for entry in MODEL_RENDERER_PATHS:
        path = root / entry
        if path.is_file():
            files.append(path)
    report_ui = root / REPORT_UI_DIR
    if report_ui.is_dir():
        for candidate in sorted(report_ui.rglob("*")):
            if not candidate.is_file():
                continue
            relative_parts = candidate.relative_to(report_ui).parts
            if "__pycache__" in relative_parts:
                continue
            if candidate.suffix.lower() in _IGNORED_SUFFIXES:
                continue
            files.append(candidate)
    return sorted(set(files), key=lambda item: item.relative_to(root).as_posix())


def renderer_sources(repo_root: str | Path) -> list[Path]:
    """Return every renderer/asset file covered by the fingerprint (sorted)."""
    root = Path(repo_root)
    return _iter_renderer_files(root)


def renderer_source_paths(repo_root: str | Path) -> list[str]:
    """Repository-relative POSIX paths of :func:`renderer_sources`."""
    root = Path(repo_root)
    return [path.relative_to(root).as_posix() for path in renderer_sources(root)]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def renderer_fingerprint(repo_root: str | Path) -> str:
    """Content hash of every renderer/asset file plus the format identity.

    Missing files are simply absent from the hash; callers that require a
    complete checkout must check :func:`renderer_source_paths` themselves.
    """
    root = Path(repo_root)
    rows: list[str] = []
    for path in renderer_sources(root):
        try:
            relative = path.relative_to(root).as_posix()
        except ValueError:  # pragma: no cover - sources are inside the repo
            continue
        rows.append(f"{relative}\t{_sha256_file(path)}")
    payload = FINGERPRINT_FORMAT + "\n" + "\n".join(rows)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def assert_single_source(primary: str, delegated: str) -> None:
    """Raise ``ValueError`` when two fingerprint implementations disagree."""
    if primary != delegated:
        raise ValueError(
            "renderer_fingerprint 存在双实现（O-C）："
            f"authoritative={primary[:16]}… delegated={delegated[:16]}…"
        )


__all__ = [
    "FINGERPRINT_FORMAT",
    "MODEL_RENDERER_PATHS",
    "REPORT_UI_DIR",
    "renderer_fingerprint",
    "renderer_source_paths",
    "renderer_sources",
]
