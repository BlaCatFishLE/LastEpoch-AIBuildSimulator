# -*- coding: utf-8 -*-
r"""Shared helpers for the formal save-import pipeline scripts.

These four scripts were promoted from the one-off ``.scratch/save-import``
prototypes.  They are the *formal* running chain behind
``scripts/le.ps1 reproduce`` and must therefore be:

* **portable** — every path resolves from the repository root, which is derived
  from this file's location (``__file__``) or from ``config/project.json``; no
  absolute machine path and no dependence on the current working directory;
* **case driven** — sample-specific numbers live in ``examples/<case>/`` (case
  configuration, corrections file, refraction selection); this module contains
  no per-case constants;
* **deterministic** — no wall-clock timestamps in data outputs, JSON is written
  with a stable key order and UTF-8 encoding.

Overrides (all optional, for receivers who unpacked the project elsewhere):

* ``LE_REPO_ROOT`` — project root directory;
* ``LE_CASE``     — default case configuration when ``--case`` is omitted.

Usage from the repository root::

    $env:PYTHONPATH='tools/save_import/src'
    & .\.venv\Scripts\python.exe tools\save_import\scripts\pipeline\make_snapshot.py --case examples\<case>\case.json --out <file>
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

SCHEMA_CASE = "le-case-v1"
SCHEMA_CORRECTIONS = "le-case-corrections-v1"


class PipelineError(RuntimeError):
    """Fatal, user-facing pipeline error (missing input, contract violation)."""


# ---------------------------------------------------------------------------
# Repository / configuration discovery
# ---------------------------------------------------------------------------


def repo_root(start: str | Path | None = None) -> Path:
    """Return the repository root, independent of the current working directory.

    Resolution order: ``LE_REPO_ROOT`` → walk up from ``__file__`` until a
    directory that contains both ``config/project.json`` and
    ``data/current.json`` is found.
    """
    env = os.environ.get("LE_REPO_ROOT")
    if env:
        candidate = Path(env).expanduser().resolve()
        if (candidate / "data" / "current.json").is_file():
            return candidate
        raise PipelineError(f"LE_REPO_ROOT does not look like the project root: {candidate}")
    here = Path(start if start is not None else __file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "data" / "current.json").is_file() and (
            candidate / "config" / "project.json"
        ).is_file():
            return candidate
    raise PipelineError(
        "cannot locate the project root: expected config/project.json and data/current.json "
        "in this directory or an ancestor (set LE_REPO_ROOT to override)"
    )


def load_project_config(root: Path) -> dict[str, Any]:
    """Load ``config/project.json`` (portable template; optional)."""
    path = root / "config" / "project.json"
    if not path.is_file():
        return {}
    return load_json(path, what="project configuration")


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def load_json(path: str | Path, *, what: str = "JSON file") -> Any:
    path = Path(path)
    if not path.is_file():
        raise PipelineError(f"{what} not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"{what} is not valid JSON ({path}): {exc}") from exc


def write_json(path: str | Path, data: Any) -> Path:
    """Write deterministic UTF-8 JSON (indent=1, insertion order preserved)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=1)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return path


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(path: str | Path, root: Path) -> str:
    """Return a repo-root relative POSIX path (or the absolute path if outside)."""
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return Path(path).resolve().as_posix()


def resolve(base: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (base / path).resolve()


# ---------------------------------------------------------------------------
# Case configuration
# ---------------------------------------------------------------------------


def load_case(case_path: str | Path | None, *, root: Path | None = None) -> dict[str, Any]:
    """Load and lightly validate a ``le-case-v1`` case configuration.

    The returned dict contains three private helpers (not JSON):

    * ``_root``  — absolute project root;
    * ``_path``  — absolute path of ``case.json``;
    * ``_dir``   — directory containing ``case.json``.
    """
    base = (root or repo_root()).resolve()
    requested = case_path or os.environ.get("LE_CASE")
    if not requested:
        raise PipelineError(
            "no case configuration given: pass --case <path/to/case.json> "
            "(or set LE_CASE); the public release has no default case"
        )
    path = resolve(base, requested)
    data = load_json(path, what="case configuration")
    if not isinstance(data, dict):
        raise PipelineError(f"case configuration must be a JSON object: {path}")
    schema = data.get("schema")
    if schema != SCHEMA_CASE:
        raise PipelineError(f"case configuration schema must be {SCHEMA_CASE!r}, got {schema!r}: {path}")
    if not data.get("case_id"):
        raise PipelineError(f"case configuration has no case_id: {path}")
    data["_root"] = base
    data["_path"] = path
    data["_dir"] = path.parent
    return data


def case_path(case: dict[str, Any], dotted: str) -> Path:
    """Resolve one dotted case field to an absolute path under the project root."""
    node: Any = case
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise PipelineError(f"case configuration field missing: {dotted!r}")
        node = node[part]
    if not isinstance(node, str) or not node:
        raise PipelineError(f"case configuration field {dotted!r} must be a non-empty path string")
    return resolve(case["_root"], node)


def optional_case_path(case: dict[str, Any], dotted: str) -> Path | None:
    try:
        return case_path(case, dotted)
    except PipelineError:
        return None


def load_corrections(case_or_path: dict[str, Any] | str | Path, *, root: Path | None = None) -> dict[str, Any]:
    """Load the explicit, data-driven corrections file (``le-case-corrections-v1``)."""
    if isinstance(case_or_path, dict):
        path = case_path(case_or_path, "inputs.corrections")
    else:
        path = resolve((root or repo_root()), case_or_path)
    data = load_json(path, what="corrections file")
    if not isinstance(data, dict):
        raise PipelineError(f"corrections file must be a JSON object: {path}")
    if data.get("schema") != SCHEMA_CORRECTIONS:
        raise PipelineError(
            f"corrections schema must be {SCHEMA_CORRECTIONS!r}, got {data.get('schema')!r}: {path}"
        )
    data["_path"] = path
    return data


def case_counts(case: dict[str, Any]) -> dict[str, int]:
    """Declared structural expectation for the case's calculation input."""
    expectations = case.get("snapshot_expectations")
    if not isinstance(expectations, dict):
        raise PipelineError("case configuration has no snapshot_expectations block")
    required = (
        "items",
        "mods",
        "passive_nodes",
        "passive_points",
        "specializations",
        "bh2_points_total",
    )
    missing = [key for key in required if key not in expectations]
    if missing:
        raise PipelineError(f"snapshot_expectations missing keys: {', '.join(missing)}")
    return {key: int(expectations[key]) for key in required}


def calculation_input_counts(document: dict[str, Any]) -> dict[str, int]:
    """Count the structural rows the snapshot builder validates."""
    passive = document.get("passive_tree") or {}
    skills = document.get("skills") or {}
    specs = skills.get("specializations") if isinstance(skills, dict) else skills
    bh2_points = 0
    for spec in specs or []:
        tree_entity = str(((spec.get("tree") or {}).get("entity_id")) or "")
        if tree_entity.endswith(":bh2"):
            bh2_points = int(spec.get("points_total") or 0)
    return {
        "items": len(document.get("items") or []),
        "mods": len(document.get("mods") or []),
        "passive_nodes": len(passive.get("nodes") or []),
        "passive_points": int(passive.get("points_total") or 0),
        "specializations": len(specs or []),
        "bh2_points_total": bh2_points,
    }


def print_step(message: str) -> None:
    print(f"[pipeline] {message}", flush=True)


# ---------------------------------------------------------------------------
# Portability: machine absolute path normalisation
# ---------------------------------------------------------------------------

#: Placeholder used for absolute paths outside the project (e.g. a third-party
#: tool path recorded in frozen source data).  Keeps generated run artefacts
#: portable without inventing a fake local path.
EXTERNAL_PATH_PLACEHOLDER = "<external-path>"

_MACHINE_PATH_RE = __import__("re").compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|,;)]*"
)


def _root_variants(root: Path) -> list[str]:
    """Raw / forward-slash / backslash / JSON-escaped spellings of the root."""
    import json as _json

    raw_posix = root.as_posix()
    raw_native = str(root)
    variants = {raw_native, raw_posix, raw_posix.replace("/", "\\")}
    for value in list(variants):
        variants.add(_json.dumps(value)[1:-1])
    return sorted((value for value in variants if value), key=len, reverse=True)


def portable_text(text: str, root: Path) -> tuple[str, int]:
    """Replace machine absolute paths so generated artefacts stay portable.

    * project-root prefixes become repository-relative (a path under the project
      root loses its machine prefix and keeps the remaining relative part);
    * remaining drive-absolute paths (external tools, other machines) become
      :data:`EXTERNAL_PATH_PLACEHOLDER`.

    Returns ``(new_text, replacement_count)``.  Frozen inputs are never passed
    through this helper; only generated run artefacts are.
    """
    import re as _re

    count = 0
    for variant in _root_variants(root):
        pattern = _re.compile(_re.escape(variant) + r"[\\/]*")
        text, replaced = pattern.subn("", text)
        count += replaced
    text, replaced = _MACHINE_PATH_RE.subn(EXTERNAL_PATH_PLACEHOLDER, text)
    count += replaced
    return text, count


def portable_document(value: Any, root: Path) -> tuple[Any, int]:
    """Recursively apply :func:`portable_text` to every string in a JSON value."""
    if isinstance(value, dict):
        total = 0
        result = {}
        for key, item in value.items():
            cleaned, replaced = portable_document(item, root)
            result[key] = cleaned
            total += replaced
        return result, total
    if isinstance(value, list):
        total = 0
        result = []
        for item in value:
            cleaned, replaced = portable_document(item, root)
            result.append(cleaned)
            total += replaced
        return result, total
    if isinstance(value, str):
        return portable_text(value, root)
    return value, 0


#: Text suffixes whose generated copies are scanned/normalised for portability.
TEXT_SCAN_SUFFIXES = {
    ".json",
    ".md",
    ".html",
    ".htm",
    ".txt",
    ".py",
    ".ps1",
    ".js",
    ".css",
}
