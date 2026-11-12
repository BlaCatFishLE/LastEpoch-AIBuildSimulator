"""P4 stage fingerprint + on-disk stage cache (``le-stage-cache-v1``).

Design (workflow-interface §4.1 / §7.4 / §10.4 / §11 9+14):

* a stage's cache key is derived from four canonical fingerprints:
  ``input`` (declared input refs content + dependency fingerprints),
  ``implementation`` (stage handler source + related implementation/rules files),
  ``parameters`` (stage parameters with repository-relative paths), and
  ``dependencies`` (upstream stage cache keys, so a change cascades downstream);
* entries live at ``<cache_root>/<stage_id>/<cache_key>.json`` plus a payload
  directory ``<cache_root>/<stage_id>/<cache_key>/output-*`` holding byte copies
  of the stage outputs; hits are validated against those copies and then
  materialised back into the current run directory (a hit never executes the
  stage handler/subprocess);
* cache writes are atomic (temp file + ``os.replace``) and never fail the main
  pipeline: errors are returned to the caller and recorded in metrics;
* no machine absolute paths are stored: repo-internal paths are POSIX-relative,
  external paths become ``<external>/<basename>``.

Test-only hooks (documented for acceptance):

* ``LE_PIPELINE_CACHE_ROOT`` — override cache root (absolute or repo-relative);
* ``LE_PIPELINE_CACHE_DISABLE=1`` — same as ``--no-cache`` (skip reads, still
  refresh writes);
* ``LE_PIPELINE_IMPL_SALT`` — salt every stage implementation fingerprint;
* ``LE_PIPELINE_IMPL_OVERRIDE`` — JSON ``{stage_id: token}`` implementing a
  stage-specific implementation override (cascade tests without editing source).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

SCHEMA_STAGE_CACHE = "le-stage-cache-v1"
SCHEMA_STAGE_IMPLEMENTATION = "le-stage-implementation-v1"
PRODUCER_NAME = "le_calc.workflow.stage_cache"
PRODUCER_VERSION = "1.0.0"
DEFAULT_CACHE_ROOT = "data/cache/pipeline"

CACHE_ROOT_ENV = "LE_PIPELINE_CACHE_ROOT"
CACHE_DISABLE_ENV = "LE_PIPELINE_CACHE_DISABLE"
IMPL_SALT_ENV = "LE_PIPELINE_IMPL_SALT"
IMPL_OVERRIDE_ENV = "LE_PIPELINE_IMPL_OVERRIDE"

#: Workflow executor/contract files that affect every stage's implementation.
_SHARED_IMPLEMENTATION_FILES: tuple[str, ...] = (
    "tools/phase3/src/le_calc/workflow/stage.py",
    "tools/phase3/src/le_calc/workflow/stage_cache.py",
    "tools/phase3/src/le_calc/workflow/contract.py",
)

#: Per-stage implementation dependency groups (directories or files).
_STAGE_IMPLEMENTATION_FILES: dict[str, tuple[str, ...]] = {
    "freeze_input": (
        "tools/save_import/src",
        "tools/build_import/src",
    ),
    "parse_input": (
        "tools/save_import/src",
        "tools/build_import/src",
    ),
    "normalize_build": (
        "tools/build_import/src",
        "tools/phase3/rules",
    ),
    "export_calc_input": (
        "tools/build_import/src",
        "tools/phase3/rules",
    ),
    "apply_affix_ranges": (
        "tools/save_import/scripts/pipeline",
        "tools/save_import/src",
        "tools/phase3/rules",
    ),
    "resolve_skills": (
        "tools/phase3/src/le_calc/workflow/domain.py",
        "tools/phase3/src/le_calc/workflow/components.py",
        "tools/build_import/src",
        "tools/knowledge/src",
        "knowledge",
    ),
    "prepare_snapshot": (
        "tools/build_import/src",
        "tools/phase3/rules",
        "tools/phase3/src/le_calc",
    ),
    "compile_plan": (
        "tools/phase3/src/le_calc/calculation_route.py",
        "tools/phase3/src/le_calc/calculation_plan.py",
        "tools/phase3/src/le_calc/plan_compiler.py",
        "tools/phase3/src/le_calc/plan_handlers.py",
        "tools/phase3/src/le_calc/effect_binding.py",
        "tools/phase3/src/le_calc/contribution_mapping.py",
        "tools/phase3/src/le_calc/condition_codes.py",
        "tools/phase3/src/le_calc/workflow/unified.py",
        "tools/phase3/rules",
        "knowledge/schemas",
    ),
    "min_validate": (
        "tools/phase3/src/le_calc",
        "tools/phase3/rules",
    ),
    "simulate": ("tools/phase3/src/le_calc",),
    "validate_result": ("tools/phase3/src/le_calc", "tools/phase3/rules"),
    "render_report": ("tools/phase3/src/le_calc",),
    "commit": ("tools/phase3/src/le_calc/workflow/commit.py",),
}

#: Renderer/UI-only files that must not invalidate numeric/validation stages.
_RENDER_ONLY_EXCLUDES: tuple[str, ...] = (
    "tools/phase3/src/le_calc/report_ui",
    "tools/phase3/src/le_calc/html.py",
    "tools/phase3/src/le_calc/dps_report.py",
    "tools/phase3/src/le_calc/report.py",
    "tools/phase3/src/le_calc/hit_report.py",
    "tools/phase3/src/le_calc/report_fingerprint.py",
    "tools/phase3/src/le_calc/report_capabilities.py",
    "tools/phase3/src/le_calc/serve.py",
    "tools/phase3/src/le_calc/hit_serve.py",
    "tools/phase3/src/le_calc/recalc.py",
)

#: Per-stage implementation exclusions (real dependency groups; R4).
_STAGE_IMPLEMENTATION_EXCLUDES: dict[str, tuple[str, ...]] = {
    "simulate": _RENDER_ONLY_EXCLUDES + ("tools/phase3/src/le_calc/workflow/expected_check.py",),
    "validate_result": _RENDER_ONLY_EXCLUDES + (
        "tools/phase3/src/le_calc/workflow/checklist.py",
        "tools/phase3/src/le_calc/report.py",
        "tools/phase3/src/le_calc/hit_report.py",
    ),
    "render_report": (),
}

_IGNORED_PARTS = frozenset({"__pycache__", ".git"})
_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".tmp", ".log"})
_MAX_TREE_FILES = 5000


class StageCacheError(ValueError):
    """Invalid cache layout/arguments (never used for a cache miss)."""


# ---------------------------------------------------------------------------
# canonical hashing / path normalisation
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fingerprint(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(canonical.encode("utf-8"))


_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")


def _is_absolute_path_text(value: str) -> bool:
    if not value or not _ABSOLUTE_RE.match(value):
        return False
    try:
        return Path(value).is_absolute()
    except (OSError, ValueError):  # pragma: no cover - malformed path
        return False


def display_path(path: Path, root: Path) -> str:
    """Repository-relative POSIX path; external paths become ``<external>/<name>``."""
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return "<external>/" + candidate.name


def normalize_value(value: Any, root: Path) -> Any:
    """Recursively normalise paths in parameters/fingerprints (no absolute paths)."""
    if isinstance(value, Path):
        return display_path(value, root)
    if isinstance(value, str):
        if _is_absolute_path_text(value):
            return display_path(Path(value), root)
        return value
    if isinstance(value, Mapping):
        return {str(key): normalize_value(item, root) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [normalize_value(item, root) for item in value]
    return value


def _iter_implementation_files(
    root: Path, entries: Iterable[str], excludes: Iterable[str] = ()
) -> list[Path]:
    files: list[Path] = []
    excluded = tuple(str(item).strip("/").replace("\\", "/") for item in excludes)
    def is_excluded(relative_posix: str) -> bool:
        return any(
            relative_posix == item or relative_posix.startswith(item + "/")
            for item in excluded
            if item
        )
    for entry in entries:
        path = root / entry
        entry_posix = display_path(path, root)
        if is_excluded(entry_posix):
            continue
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if len(files) >= _MAX_TREE_FILES:
                    break
                if not candidate.is_file():
                    continue
                parts = set(candidate.relative_to(root).parts)
                if _IGNORED_PARTS & parts:
                    continue
                if candidate.suffix.lower() in _IGNORED_SUFFIXES:
                    continue
                if is_excluded(display_path(candidate, root)):
                    continue
                files.append(candidate)
    return sorted(set(files), key=lambda item: display_path(item, root))


def implementation_manifest(
    stage_id: str,
    root: str | Path,
    *,
    handler: Optional[Callable[..., Any]] = None,
    excludes: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Files + handler source that define one stage implementation.

    ``excludes`` defaults to the stage's real dependency boundary: numeric and
    validation stages exclude report_ui/renderer-only files, while
    render_report includes them (R4).
    """
    repo_root = Path(root).resolve()
    entries = list(dict.fromkeys(_SHARED_IMPLEMENTATION_FILES + _STAGE_IMPLEMENTATION_FILES.get(stage_id, ())))
    effective_excludes = excludes if excludes is not None else _STAGE_IMPLEMENTATION_EXCLUDES.get(stage_id, ())
    files = _iter_implementation_files(repo_root, entries, effective_excludes)
    rows = []
    for path in files:
        rows.append(
            {
                "path": display_path(path, repo_root),
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        )
    source = ""
    if handler is not None:
        try:
            source = inspect.getsource(handler)
        except (OSError, TypeError):  # pragma: no cover - dynamic handlers
            source = f"{getattr(handler, '__module__', '')}:{getattr(handler, '__qualname__', repr(handler))}"
    source = "\n".join(line.rstrip() for line in source.splitlines()).strip()
    payload = {
        "schema_version": SCHEMA_STAGE_IMPLEMENTATION,
        "stage_id": stage_id,
        "handler_source_sha256": sha256_bytes(source.encode("utf-8")),
        "files": rows,
    }
    return payload


def implementation_fingerprint(
    stage_id: str,
    root: str | Path,
    *,
    handler: Optional[Callable[..., Any]] = None,
    salt: Optional[str] = None,
    override: Optional[str] = None,
    excludes: Optional[Iterable[str]] = None,
) -> dict[str, Any]:
    """Return ``{fingerprint, manifest, salt, override}`` for one stage."""
    manifest = implementation_manifest(stage_id, root, handler=handler, excludes=excludes)
    env_salt = salt if salt is not None else os.environ.get(IMPL_SALT_ENV, "")
    override_value = override
    if override_value is None:
        raw = os.environ.get(IMPL_OVERRIDE_ENV, "")
        if raw:
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                parsed = {}
            if isinstance(parsed, Mapping):
                override_value = parsed.get(stage_id)
    payload = {
        "manifest": manifest,
        "salt": str(env_salt or ""),
        "override": str(override_value or ""),
    }
    return {
        "fingerprint": fingerprint(payload),
        "manifest": manifest,
        "salt": str(env_salt or ""),
        "override": str(override_value or ""),
    }


# ---------------------------------------------------------------------------
# request fingerprints
# ---------------------------------------------------------------------------


def normalize_input_refs(input_refs: Iterable[Mapping[str, Any]], root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for ref in input_refs or []:
        if not isinstance(ref, Mapping):
            continue
        value = str(ref.get("path") or "")
        sha = ref.get("sha256")
        row: dict[str, Any] = {
            "artifact_id": str(ref.get("artifact_id") or value),
            "path": display_path(Path(value), root) if value else "",
            "asset_id": ref.get("asset_id"),
            "sha256": str(sha) if sha else None,
        }
        if not row["sha256"] and value:
            candidate = Path(value)
            if not candidate.is_absolute():
                candidate = root / candidate
            if candidate.is_file():
                row["sha256"] = sha256_file(candidate)
        rows.append(row)
    return sorted(rows, key=lambda item: (item["path"], item["artifact_id"]))


def _dependency_chain(dependency_fingerprints: Mapping[str, Any]) -> dict[str, str]:
    chain = dependency_fingerprints.get("dependencies")
    if not isinstance(chain, Mapping):
        return {}
    return {str(key): str(value) for key, value in sorted(chain.items())}


def request_fingerprints(
    request: Any,
    root: str | Path,
    *,
    handler: Optional[Callable[..., Any]] = None,
    implementation: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Compute all fingerprints/cache key for one StageRequest (no execution)."""
    repo_root = Path(root).resolve()
    input_refs = normalize_input_refs(getattr(request, "input_refs", []) or [], repo_root)
    parameters = normalize_value(dict(getattr(request, "parameters", {}) or {}), repo_root)
    dependency_raw = dict(getattr(request, "dependency_fingerprints", {}) or {})
    dependencies = _dependency_chain(dependency_raw)
    input_payload = {
        "input_refs": input_refs,
        "dependency_fingerprints": normalize_value(
            {key: value for key, value in dependency_raw.items() if key != "dependencies"}, repo_root
        ),
    }
    input_fp = fingerprint(input_payload)
    parameters_fp = fingerprint(parameters)
    implementation_data = dict(implementation or implementation_fingerprint(
        str(getattr(request, "stage_id", "")), repo_root, handler=handler
    ))
    implementation_fp = str(implementation_data["fingerprint"])
    cache_key = fingerprint(
        {
            "schema_version": SCHEMA_STAGE_CACHE,
            "stage_id": str(getattr(request, "stage_id", "")),
            "input_fingerprint": input_fp,
            "implementation_fingerprint": implementation_fp,
            "parameters_fingerprint": parameters_fp,
            "dependencies": dependencies,
        }
    )
    return {
        "input_fingerprint": input_fp,
        "implementation_fingerprint": implementation_fp,
        "parameters_fingerprint": parameters_fp,
        "dependencies": dependencies,
        "cache_key": cache_key,
        "normalized_input_refs": input_refs,
        "normalized_parameters": parameters,
        "implementation": implementation_data.get("manifest"),
        "implementation_salt": implementation_data.get("salt", ""),
        "implementation_override": implementation_data.get("override", ""),
    }


# ---------------------------------------------------------------------------
# cache storage
# ---------------------------------------------------------------------------


def resolve_cache_root(root: str | Path, explicit: Optional[str | Path] = None) -> Path:
    repo_root = Path(root).resolve()
    raw = explicit if explicit is not None else os.environ.get(CACHE_ROOT_ENV)
    if raw:
        candidate = Path(str(raw))
        return candidate if candidate.is_absolute() else (repo_root / candidate)
    return repo_root / DEFAULT_CACHE_ROOT


def cache_disabled_by_env() -> bool:
    return str(os.environ.get(CACHE_DISABLE_ENV, "")).strip().lower() in {"1", "true", "yes", "on"}


def snapshot_tree(output_root: str | Path) -> dict[str, dict[str, Any]]:
    """Snapshot files under a run directory as ``{relative: {sha256, bytes}}``."""
    root = Path(output_root)
    snapshot: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return snapshot
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = set(path.relative_to(root).parts)
        if "__pycache__" in parts or path.suffix.lower() in _IGNORED_SUFFIXES:
            continue
        try:
            snapshot[path.relative_to(root).as_posix()] = {
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
            }
        except OSError:  # pragma: no cover - transient file
            continue
    return snapshot


class StageCache:
    """Validated on-disk cache for one pipeline execution."""

    def __init__(
        self,
        root: str | Path,
        *,
        cache_root: Optional[str | Path] = None,
        enabled: bool = True,
    ) -> None:
        self.root = Path(root).resolve()
        self.cache_root = resolve_cache_root(self.root, cache_root)
        self.enabled = bool(enabled) and not cache_disabled_by_env()
        self.last_error: Optional[str] = None

    # -- paths -----------------------------------------------------------
    def stage_dir(self, stage_id: str) -> Path:
        return self.cache_root / _safe_component(stage_id)

    def entry_path(self, stage_id: str, cache_key: str) -> Path:
        return self.stage_dir(stage_id) / f"{_safe_component(cache_key)}.json"

    def payload_dir(self, stage_id: str, cache_key: str) -> Path:
        return self.stage_dir(stage_id) / _safe_component(cache_key)

    # -- public API ------------------------------------------------------
    def load(self, stage_id: str, cache_key: str) -> Optional[dict[str, Any]]:
        """Return a validated cache entry, or ``None`` for miss/disabled/corrupt."""
        if not self.enabled:
            return None
        path = self.entry_path(stage_id, cache_key)
        if not path.is_file():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.last_error = f"cache entry unreadable: {exc}"
            return None
        validation = self.validate_entry(stage_id, cache_key, entry)
        if not validation["ok"]:
            self.last_error = validation["reason"]
            return None
        return entry

    def validate_entry(self, stage_id: str, cache_key: str, entry: Any) -> dict[str, Any]:
        if not isinstance(entry, Mapping):
            return {"ok": False, "reason": "cache_entry_not_object"}
        if entry.get("schema_version") != SCHEMA_STAGE_CACHE:
            return {"ok": False, "reason": "cache_entry_schema_mismatch"}
        if str(entry.get("stage_id")) != stage_id:
            return {"ok": False, "reason": "cache_entry_stage_mismatch"}
        if str(entry.get("cache_key")) != cache_key:
            return {"ok": False, "reason": "cache_entry_key_mismatch"}
        outputs = entry.get("outputs")
        if not isinstance(outputs, list):
            return {"ok": False, "reason": "cache_entry_outputs_missing"}
        payload_dir = self.payload_dir(stage_id, cache_key)
        for output in outputs:
            if not isinstance(output, Mapping):
                return {"ok": False, "reason": "cache_output_not_object"}
            cached_path = str(output.get("cached_path") or "")
            expected = str(output.get("sha256") or "")
            if not cached_path or not expected:
                return {"ok": False, "reason": "cache_output_missing_path_or_sha"}
            path = payload_dir / cached_path
            if not path.is_file():
                return {"ok": False, "reason": "cache_output_missing"}
            if sha256_file(path) != expected:
                return {"ok": False, "reason": "cache_output_sha_mismatch"}
        return {"ok": True, "reason": None}

    def materialize(
        self,
        entry: Mapping[str, Any],
        output_root: str | Path,
        *,
        artifact_ref: Callable[..., dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Copy cached payloads into the current run dir; return artifact refs."""
        stage_id = str(entry["stage_id"])
        cache_key = str(entry["cache_key"])
        payload_dir = self.payload_dir(stage_id, cache_key)
        target_root = Path(output_root).resolve()
        refs: list[dict[str, Any]] = []
        for output in entry.get("outputs") or []:
            relative_path = str(output.get("relative_path") or "")
            if not relative_path:
                raise StageCacheError("cache output missing relative_path")
            cached_path = payload_dir / str(output["cached_path"])
            target = target_root / relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            expected = str(output["sha256"])
            if not target.is_file() or sha256_file(target) != expected:
                _atomic_copy(cached_path, target)
            if sha256_file(target) != expected:
                raise StageCacheError(f"materialized output sha mismatch: {relative_path}")
            refs.append(
                artifact_ref(target, artifact_id=str(output.get("name") or relative_path))
            )
        return refs

    def store(
        self,
        *,
        stage_id: str,
        cache_key: str,
        fingerprints: Mapping[str, Any],
        outputs: Iterable[str | Path],
        output_root: str | Path,
        artifact_ref: Callable[..., dict[str, Any]],
        readiness: Optional[str] = None,
        issues: Iterable[Mapping[str, Any]] = (),
        metrics: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write one cache entry + payload copies atomically; never raises."""
        try:
            return self._store(
                stage_id=stage_id,
                cache_key=cache_key,
                fingerprints=fingerprints,
                outputs=outputs,
                output_root=output_root,
                artifact_ref=artifact_ref,
                readiness=readiness,
                issues=issues,
                metrics=metrics or {},
            )
        except Exception as exc:  # noqa: BLE001 - cache must never break the pipeline
            self.last_error = f"cache_store_failed: {exc}"
            return {"ok": False, "reason": self.last_error}

    def _store(self, **kwargs: Any) -> dict[str, Any]:
        stage_id = str(kwargs["stage_id"])
        cache_key = str(kwargs["cache_key"])
        output_root = Path(kwargs["output_root"]).resolve()
        payload_dir = self.payload_dir(stage_id, cache_key)
        payload_dir.mkdir(parents=True, exist_ok=True)
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(kwargs["outputs"] or []):
            path = Path(str(raw))
            if not path.is_absolute():
                path = self.root / path
            if not path.is_file():
                return {"ok": False, "reason": f"cache_output_not_file: {path.name}"}
            try:
                relative = path.resolve().relative_to(output_root).as_posix()
            except ValueError:
                return {"ok": False, "reason": f"cache_output_outside_run_dir: {path.name}"}
            data = path.read_bytes()
            digest = sha256_bytes(data)
            cached_name = f"output-{index:02d}-{_safe_component(path.name)}"
            _atomic_write_bytes(payload_dir / cached_name, data)
            rows.append(
                {
                    "name": f"{stage_id}:{path.name}",
                    "relative_path": relative,
                    "sha256": digest,
                    "bytes": len(data),
                    "cached_path": cached_name,
                }
            )
        fingerprints = dict(kwargs["fingerprints"])
        entry = {
            "schema_version": SCHEMA_STAGE_CACHE,
            "stage_id": stage_id,
            "cache_key": cache_key,
            "input_fingerprint": fingerprints.get("input_fingerprint"),
            "implementation_fingerprint": fingerprints.get("implementation_fingerprint"),
            "parameters_fingerprint": fingerprints.get("parameters_fingerprint"),
            "dependencies": dict(fingerprints.get("dependencies") or {}),
            "outputs": rows,
            "readiness": kwargs.get("readiness"),
            "issues": [dict(issue) for issue in kwargs.get("issues") or []],
            "metrics": dict(kwargs.get("metrics") or {}),
            "producer": {"name": PRODUCER_NAME, "version": PRODUCER_VERSION},
            "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        _atomic_write_json(self.entry_path(stage_id, cache_key), entry)
        return {"ok": True, "entry": entry, "path": self.entry_path(stage_id, cache_key).as_posix()}

    # -- maintenance -----------------------------------------------------
    def stats(self) -> dict[str, Any]:
        by_stage: dict[str, int] = {}
        total_bytes = 0
        stage_dirs = sorted(p for p in self.cache_root.iterdir() if p.is_dir()) if self.cache_root.is_dir() else []
        for stage_dir in stage_dirs:
            for path in sorted(stage_dir.glob("*.json")):
                if not path.is_file():
                    continue
                by_stage[stage_dir.name] = by_stage.get(stage_dir.name, 0) + 1
                total_bytes += path.stat().st_size
        payload_bytes = 0
        if self.cache_root.is_dir():
            for path in self.cache_root.rglob("output-*"):
                if path.is_file():
                    payload_bytes += path.stat().st_size
        return {
            "cache_root": display_path(self.cache_root, self.root),
            "enabled": self.enabled,
            "entries": sum(by_stage.values()),
            "by_stage": dict(sorted(by_stage.items())),
            "entry_bytes": total_bytes,
            "payload_bytes": payload_bytes,
        }

    def clear(self) -> dict[str, Any]:
        removed = 0
        if self.cache_root.is_dir():
            for path in sorted(self.cache_root.iterdir(), reverse=True):
                try:
                    if path.is_dir():
                        shutil.rmtree(path)
                    else:
                        path.unlink()
                    removed += 1
                except OSError as exc:  # pragma: no cover - best effort
                    self.last_error = str(exc)
        return {"removed": removed, "cache_root": display_path(self.cache_root, self.root)}


# ---------------------------------------------------------------------------
# atomic file helpers (cache-local; commit.py owns publish semantics)
# ---------------------------------------------------------------------------


def _safe_component(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")
    return text[:180] or "entry"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, tmp_name = tempfile.mkstemp(prefix=".tmp-stage-cache-", dir=str(path.parent))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - best effort
            pass
        raise


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    data = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, data)


def _atomic_copy(source: Path, target: Path) -> None:
    _atomic_write_bytes(target, source.read_bytes())


__all__ = [
    "CACHE_DISABLE_ENV",
    "CACHE_ROOT_ENV",
    "DEFAULT_CACHE_ROOT",
    "IMPL_OVERRIDE_ENV",
    "IMPL_SALT_ENV",
    "PRODUCER_NAME",
    "PRODUCER_VERSION",
    "SCHEMA_STAGE_CACHE",
    "SCHEMA_STAGE_IMPLEMENTATION",
    "StageCache",
    "StageCacheError",
    "cache_disabled_by_env",
    "display_path",
    "fingerprint",
    "implementation_fingerprint",
    "implementation_manifest",
    "normalize_input_refs",
    "normalize_value",
    "request_fingerprints",
    "snapshot_tree",
    "resolve_cache_root",
    "sha256_bytes",
    "sha256_file",
]
