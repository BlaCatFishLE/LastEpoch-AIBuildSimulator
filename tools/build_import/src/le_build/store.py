"""Immutable revision store for imported builds.

Layout (per the phase-2 plan)::

    data/builds/letools/<shareId>/
      raw/<response-sha256>.json
      fetches/<attempt-id>.json
      revisions/<revision-id>/{build.json, import-report.json, import-report.zh-CN.md, ...}
      current.json

A revision is generated in a temporary directory, validated, then renamed into place;
the build ``current.json`` pointer is replaced atomically afterwards.  The revision id is
derived from the build content (all fields except volatile import metadata), so importing
the same payload twice is idempotent while any content change produces a new revision.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
from pathlib import Path

from .paths import BuildPaths, canonical_json_bytes, write_json

VOLATILE_META_KEYS = ("revision", "created_at", "import_attempt", "previous_revision")
VOLATILE_SOURCE_PAYLOAD_KEYS = (
    "attempt_id",
    "raw_path",
    "fetched_at",
    "fetch_mode",
    "http_status",
    "api_url",
)
# The content identity keeps only the stable meta fields; local transport details
# (attempt ids, archive paths, endpoint used for this attempt) never change it.
CONTENT_META_KEYS = (
    "build_id",
    "share_id",
    "share_url",
    "source",
    "protocol",
    "source_payload",
    "codec",
    "idempotency",
)
CONTENT_SOURCE_PAYLOAD_KEYS = ("raw_sha256", "data_field_sha256")


class StoreError(RuntimeError):
    pass


class RevisionConflict(StoreError):
    pass


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def content_view(spec: dict) -> dict:
    """Return the part of a BuildSpec that defines the revision content.

    Volatile import metadata (fetch attempt id, timestamp, local archive path, local
    file path, edition URL) is excluded so re-importing the same payload is idempotent;
    the source payload hash and the source-field hash remain part of the content.
    """
    view = json.loads(json.dumps(spec))  # deep copy without depending on copy semantics
    meta = view.get("meta") or {}
    for key in VOLATILE_META_KEYS:
        meta.pop(key, None)
    view["meta"] = {
        key: value
        for key, value in meta.items()
        if key in CONTENT_META_KEYS
    }
    source_payload = view["meta"].get("source_payload")
    if isinstance(source_payload, dict):
        view["meta"]["source_payload"] = {
            key: value
            for key, value in source_payload.items()
            if key in CONTENT_SOURCE_PAYLOAD_KEYS
        }
    return view


def content_hash(spec: dict) -> str:
    return hashlib.sha256(canonical_json_bytes(content_view(spec))).hexdigest()


def revision_id_for(spec: dict) -> str:
    return "rev-" + content_hash(spec)[:16]


def build_ref_parts(build_ref: str) -> tuple[str, str]:
    if ":" in build_ref:
        source, share_id = build_ref.split(":", 1)
        return source, share_id
    return "letools", build_ref


def load_current(paths: BuildPaths, share_id: str) -> dict | None:
    path = paths.current_file(share_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise StoreError(f"current pointer is corrupt: {path}: {exc}") from exc


def list_revisions(paths: BuildPaths, share_id: str) -> list[str]:
    directory = paths.revisions_dir(share_id)
    if not directory.is_dir():
        return []
    return sorted(
        entry.name
        for entry in directory.iterdir()
        if entry.is_dir() and entry.name.startswith("rev-")
    )


def load_revision(paths: BuildPaths, share_id: str, revision_id: str) -> tuple[dict, Path]:
    directory = paths.revision_dir(share_id, revision_id)
    build_path = directory / "build.json"
    if not build_path.is_file():
        raise StoreError(f"revision not found: {build_path}")
    spec = json.loads(build_path.read_text(encoding="utf-8"))
    return spec, directory


def load_build(paths: BuildPaths, build_ref: str, *, revision_id: str | None = None) -> dict:
    """Load the current (or a named) revision of a build and attach storage metadata."""
    source, share_id = build_ref_parts(build_ref)
    paths = BuildPaths(paths.root, source=source)
    if revision_id:
        spec, directory = load_revision(paths, share_id, revision_id)
        pointer = load_current(paths, share_id)
        return {
            "spec": spec,
            "revision_id": revision_id,
            "revision_dir": directory,
            "current": pointer,
            "is_current": bool(pointer and pointer.get("revision_id") == revision_id),
        }
    pointer = load_current(paths, share_id)
    if not pointer:
        raise StoreError(f"build {source}:{share_id} has no published revision")
    spec, directory = load_revision(paths, share_id, pointer["revision_id"])
    return {
        "spec": spec,
        "revision_id": pointer["revision_id"],
        "revision_dir": directory,
        "current": pointer,
        "is_current": True,
    }


def _write_atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    write_json(temp, value)
    os.replace(temp, path)


def publish_revision(
    paths: BuildPaths,
    share_id: str,
    spec: dict,
    *,
    artifacts: dict[str, dict] | None = None,
    published_at: str | None = None,
) -> dict:
    """Publish one BuildSpec revision immutably and update the current pointer.

    ``artifacts`` maps a relative filename (e.g. ``import-report.json``) to a JSON value
    written next to ``build.json``.  Returns a summary dict.
    """
    source = spec.get("meta", {}).get("source") or paths.source
    paths = BuildPaths(paths.root, source=source)
    digest = content_hash(spec)
    revision_id = "rev-" + digest[:16]
    stamp = published_at or _utc_now()
    revisions_dir = paths.revisions_dir(share_id)
    revisions_dir.mkdir(parents=True, exist_ok=True)

    existing_dir = revisions_dir / revision_id
    if existing_dir.is_dir():
        existing_build = existing_dir / "build.json"
        if not existing_build.is_file():
            raise StoreError(f"revision directory exists without build.json: {existing_dir}")
        existing = json.loads(existing_build.read_text(encoding="utf-8"))
        if content_hash(existing) != digest:
            raise RevisionConflict(
                f"revision {revision_id} already exists with different content; refusing to overwrite"
            )
        pointer = load_current(paths, share_id)
        unchanged = pointer is None or pointer.get("revision_id") != revision_id
        existing_meta = (existing.get("meta") or {}).get("revision") or {}
        if unchanged:
            revision_seq = existing_meta.get("revision_seq") or len(list_revisions(paths, share_id))
            spec.setdefault("meta", {})["revision"] = {
                "revision_id": revision_id,
                "content_sha256": digest,
                "revision_seq": revision_seq,
                "published_at": existing_meta.get("published_at") or stamp,
            }
            _update_pointer(paths, share_id, spec, digest, revision_id, revision_seq, stamp)
        return {
            "revision_id": revision_id,
            "content_sha256": digest,
            "revision_seq": existing_meta.get("revision_seq") or len(list_revisions(paths, share_id)),
            "state": "exists",
            "published_at": existing_meta.get("published_at"),
        }

    previous_pointer = load_current(paths, share_id)
    revision_seq = (int(previous_pointer["revision_seq"]) + 1) if previous_pointer and previous_pointer.get("revision_seq") else 1
    revision_meta = {
        "revision_id": revision_id,
        "content_sha256": digest,
        "revision_seq": revision_seq,
        "published_at": stamp,
        "previous_revision_id": previous_pointer.get("revision_id") if previous_pointer else None,
    }
    spec.setdefault("meta", {})["revision"] = revision_meta

    temp_dir = revisions_dir / f".tmp-{revision_id}-{os.getpid()}-{stamp.replace(':', '').replace('+', '')}"
    if temp_dir.exists():  # pragma: no cover - defensive
        shutil.rmtree(temp_dir)
    try:
        temp_dir.mkdir(parents=True)
        write_json(temp_dir / "build.json", spec)
        for name, value in (artifacts or {}).items():
            target = temp_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(value, str):
                target.write_text(value, encoding="utf-8", newline="\n")
            else:
                write_json(target, value)
        # Final integrity gate before the rename: the stored revision must round-trip.
        stored = json.loads((temp_dir / "build.json").read_text(encoding="utf-8"))
        if content_hash(stored) != digest:
            raise StoreError("revision content hash changed between generation and validation")
        os.rename(temp_dir, existing_dir)
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    _update_pointer(paths, share_id, spec, digest, revision_id, revision_seq, stamp)
    return {
        "revision_id": revision_id,
        "content_sha256": digest,
        "revision_seq": revision_seq,
        "state": "published",
        "published_at": stamp,
    }


def _update_pointer(paths: BuildPaths, share_id: str, spec: dict, digest: str, revision_id: str,
                    revision_seq: int, stamp: str) -> None:
    previous = load_current(paths, share_id)
    pointer = {
        "build_id": spec.get("meta", {}).get("build_id"),
        "share_id": share_id,
        "source": spec.get("meta", {}).get("source"),
        "revision_id": revision_id,
        "content_sha256": digest,
        "revision_seq": revision_seq,
        "dataset_id": (spec.get("catalog") or {}).get("dataset_id"),
        "dataset_logical_sha256": (spec.get("catalog") or {}).get("logical_content_sha256"),
        "source_raw_sha256": ((spec.get("meta") or {}).get("source_payload") or {}).get("raw_sha256"),
        "statuses": spec.get("statuses"),
        "published_at": stamp,
        "previous_revision_id": (previous or {}).get("revision_id"),
        "updated_at": _utc_now(),
    }
    _write_atomic_json(paths.current_file(share_id), pointer)


def write_failure_report(paths: BuildPaths, share_id: str, attempt_id: str, report: dict) -> Path:
    directory = paths.imports_dir(share_id) / (attempt_id or "unknown")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "failure-report.json"
    write_json(path, report)
    return path


def cleanup_temp_revisions(paths: BuildPaths, share_id: str) -> list[str]:
    """Remove orphaned ``.tmp-*`` revision directories from interrupted publishes."""
    revisions_dir = paths.revisions_dir(share_id)
    removed: list[str] = []
    if not revisions_dir.is_dir():
        return removed
    for entry in revisions_dir.iterdir():
        if entry.is_dir() and entry.name.startswith(".tmp-"):
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed
