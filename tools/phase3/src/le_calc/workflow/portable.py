"""Portable artefact normalisation shared by ``reproduce`` and ``prepare`` (P3).

Generated run artefacts must stay repository-portable: no drive-letter or UNC
machine paths.  The primitives live in the canonical pipeline helper module
``tools/save_import/scripts/pipeline/_common.py`` (``portable_text`` /
``portable_document`` / ``TEXT_SCAN_SUFFIXES``); this module only provides the
*directory walk* used by both entry points so the semantics cannot drift
(reproduce previously had its own copy in ``scripts/le_commands.py``).

Frozen inputs are never passed through this helper — only generated artefacts.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[5]
_PIPELINE_SCRIPTS = ROOT / "tools" / "save_import" / "scripts" / "pipeline"


def _common():
    """Lazy import of the canonical portability primitives (same implementation)."""
    scripts = str(_PIPELINE_SCRIPTS)
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import _common  # noqa: PLC0415

    return _common


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except ValueError:
        return "<external-path>"


def sanitize_directory(directory: str | Path, root: str | Path) -> list[dict[str, Any]]:
    """Normalise machine absolute paths in the text artefacts under ``directory``.

    Returns one ``{"path": <repo-relative>, "replacements": <count>}`` entry per
    rewritten file (empty list when nothing needed rewriting), matching the
    previous ``scripts/le_commands._sanitize_directory`` contract.
    """

    common = _common()
    directory_path = Path(directory)
    root_path = Path(root)
    results: list[dict[str, Any]] = []
    if not directory_path.is_dir():
        return results
    for path in sorted(directory_path.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in common.TEXT_SCAN_SUFFIXES:
            continue
        suffix = path.suffix.lower()
        if suffix == ".json":
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            cleaned, count = common.portable_document(document, root_path)
            if count:
                path.write_text(
                    json.dumps(cleaned, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                results.append({"path": _display(path, root_path), "replacements": count})
        else:
            text_value = path.read_text(encoding="utf-8")
            cleaned, count = common.portable_text(text_value, root_path)
            if count:
                path.write_text(cleaned, encoding="utf-8", newline="\n")
                results.append({"path": _display(path, root_path), "replacements": count})
    return results


__all__ = ["ROOT", "sanitize_directory"]
