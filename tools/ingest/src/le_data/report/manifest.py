"""Dataset manifest and publish helpers (workguide sections 6.5 and 12.1).

Publishing is immutable (phase-1 review P1-5):

1. a publish lock (``data/.publish.lock``) serializes publishers;
2. the database and manifest are written to a staging directory inside the
   release root, verified (file SHA-256, logical content fingerprint, SQLite
   integrity) and only then atomically renamed to ``releases/<dataset_id>``;
3. a release with the same ``dataset_id`` is never overwritten: identical
   content is idempotent, different content is rejected (publish under a new
   dataset id);
4. ``data/current.json`` is switched last, atomically.  A failure before the
   switch leaves the previous current pointer and every previous release
   untouched.

Partial datasets (``dataset_meta.status == 'partial'``) are only released when
the caller passes ``allow_partial=True``; by default such a release does not
replace an existing ``current.json`` unless ``force_current=True``.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from ..paths import NORMALIZED_DIR, RELEASES_DIR
from ..store import (
    checkpoint,
    connect,
    integrity_report,
    logical_fingerprint,
    table_counts,
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path) -> str:
    try:
        from ..paths import PROJECT_ROOT

        return str(Path(path).resolve().relative_to(PROJECT_ROOT.resolve()))
    except Exception:
        return str(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def build_manifest(
    conn,
    dataset_db: Path,
    *,
    dataset_id: str,
    run_id: str | None = None,
    generation_command: str = "",
    config_file: str | None = None,
    limitations: list[str] | None = None,
    compatibility: str | None = None,
    content_fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = conn.execute("SELECT * FROM dataset_meta").fetchone()
    snapshots = []
    for row in conn.execute("SELECT * FROM source_snapshots ORDER BY source"):
        resources = [
            {
                "resource_name": resource["resource_name"],
                "url": resource["url"],
                "role": resource["role"],
                "sha256": resource["sha256"],
                "size_bytes": resource["size_bytes"],
                "http_status": resource["http_status"],
                "fetch_status": resource["fetch_status"],
                "local_path": _relative(Path(resource["local_path"]))
                if resource["local_path"]
                else None,
            }
            for resource in conn.execute(
                "SELECT * FROM raw_resources WHERE source_snapshot_id = ? ORDER BY resource_name",
                (row["source_snapshot_id"],),
            )
        ]
        record_counts = {
            record["entity_kind"]: record["n"]
            for record in conn.execute(
                "SELECT entity_kind, COUNT(*) AS n FROM source_records "
                "WHERE source_snapshot_id = ? GROUP BY entity_kind ORDER BY entity_kind",
                (row["source_snapshot_id"],),
            )
        }
        snapshots.append(
            {
                "source": row["source"],
                "source_snapshot_id": row["source_snapshot_id"],
                "entry_url": row["entry_url"],
                "claimed_version": row["claimed_version"],
                "version_evidence": json.loads(row["version_evidence"])
                if row["version_evidence"]
                else None,
                "fetch_mode": row["fetch_mode"],
                "fetch_started_at": row["fetch_started_at"],
                "fetch_finished_at": row["fetch_finished_at"],
                "status": row["status"],
                "local_game_build": row["local_game_build"],
                "resources": resources,
                "source_record_counts": record_counts,
            }
        )

    counts = table_counts(conn)
    issue_counts = {
        (row[0] or "unknown"): row[1]
        for row in conn.execute("SELECT severity, COUNT(*) FROM data_issues GROUP BY severity")
    }
    open_issues = conn.execute(
        "SELECT COUNT(*) FROM data_issues WHERE status = 'open'"
    ).fetchone()[0]
    unresolved = conn.execute(
        "SELECT COUNT(*) FROM entity_relationships WHERE resolution_status = 'unresolved'"
    ).fetchone()[0]
    fingerprint = content_fingerprint or logical_fingerprint(conn)
    checkpoint(conn)

    return {
        "dataset_id": dataset_id,
        "run_id": run_id,
        "schema_version": meta["schema_version"],
        "normalizer_version": meta["normalizer_version"],
        "mapping_revision": meta["mapping_revision"],
        "parser_versions": {"jsdata_literal_parser": "1", "letools": "1", "tunklab": "1"},
        "exported_at": _now(),
        "generation_command": generation_command,
        "config_file": config_file,
        "target_game_version": meta["target_game_version"],
        "target_season": meta["target_season"],
        "dataset_status": meta["status"],
        "compatibility_conclusion": compatibility or meta["compatibility_conclusion"],
        "sources": snapshots,
        "database": {
            "path": _relative(dataset_db),
            "sha256": sha256_file(dataset_db),
            "size_bytes": dataset_db.stat().st_size,
            "logical_content_sha256": fingerprint["logical_sha256"],
            "logical_fingerprint": fingerprint,
        },
        "counts": counts,
        "issue_counts": issue_counts,
        "open_issues": open_issues,
        "unresolved_relationships": unresolved,
        "quality_state": "pass" if open_issues == 0 else "issues_open",
        "limitations": limitations or [],
    }


# -- publish internals ------------------------------------------------------
@contextmanager
def _hold_publish_lock(data_dir: Path) -> Iterator[Path]:
    """Create ``data/.publish.lock`` with O_EXCL; remove it on exit."""
    data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = data_dir / ".publish.lock"
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise RuntimeError(
            f"publish lock is held: {lock_path} exists. Another publish is running, or a "
            f"previous run crashed; inspect the lock and remove it manually only then."
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"pid": os.getpid(), "created_at": _now()}, ensure_ascii=False
                )
            )
        yield lock_path
    finally:
        lock_path.unlink(missing_ok=True)


def _read_current(data_dir: Path) -> dict[str, Any] | None:
    current_path = data_dir / "current.json"
    if not current_path.exists():
        return None
    try:
        return json.loads(current_path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _stage_database(dataset_db: Path, staging_dir: Path) -> Path:
    """Copy the source database into the staging directory.

    The copy is converted to a rollback-journal (DELETE) database so that the
    published release is exactly two files (``data.sqlite`` + ``manifest.json``):
    a WAL-mode database produces ``-wal``/``-shm`` side files as soon as a
    read-only reader opens it, which would mutate the immutable release
    directory after publish.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged_db = staging_dir / "data.sqlite"
    shutil.copyfile(dataset_db, staged_db)
    raw = sqlite3.connect(staged_db)
    try:
        raw.execute("PRAGMA journal_mode = DELETE")
        raw.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        raw.commit()
    finally:
        raw.close()
    return staged_db


def _write_staged_manifest(staging_dir: Path, manifest: dict[str, Any]) -> None:
    manifest_path = staging_dir / "manifest.json"
    manifest_tmp = staging_dir / "manifest.json.tmp"
    manifest_tmp.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest_tmp.replace(manifest_path)


def _open_readonly_immutable(path: Path) -> sqlite3.Connection:
    """Open a read-only, immutable connection (creates no -wal/-shm side files)."""
    uri = f"file:{Path(path).as_posix()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _verify_staged_release(staging_dir: Path, manifest: dict[str, Any]) -> None:
    """Verify the staged database and the staged manifest before activation."""
    staged_db = staging_dir / "data.sqlite"
    staged_manifest_path = staging_dir / "manifest.json"
    if not staged_db.exists():
        raise RuntimeError("staged release has no database")
    if not staged_manifest_path.exists():
        raise RuntimeError("staged release has no manifest")
    expected_sha = manifest["database"]["sha256"]
    actual_sha = sha256_file(staged_db)
    if actual_sha != expected_sha:
        raise RuntimeError(
            "published database hash does not match manifest: "
            f"expected {expected_sha}, got {actual_sha}"
        )
    expected_logical = manifest["database"]["logical_content_sha256"]
    staged_conn = _open_readonly_immutable(staged_db)
    try:
        report = integrity_report(staged_conn)
        fingerprint = logical_fingerprint(staged_conn)
    finally:
        staged_conn.close()
    if not report["integrity_ok"]:
        raise RuntimeError(
            f"staged database failed PRAGMA integrity_check: {report['integrity_check'][:5]}"
        )
    if not report["foreign_keys_ok"]:
        raise RuntimeError(
            "staged database has foreign key violations: "
            f"{report['foreign_key_violations'][:5]}"
        )
    if fingerprint["logical_sha256"] != expected_logical:
        raise RuntimeError(
            "staged database logical content does not match manifest: "
            f"expected {expected_logical}, got {fingerprint['logical_sha256']}"
        )
    written = json.loads(staged_manifest_path.read_text(encoding="utf-8"))
    if written.get("dataset_id") != manifest.get("dataset_id"):
        raise RuntimeError("staged manifest dataset_id does not match the requested dataset")
    if written.get("database", {}).get("sha256") != expected_sha:
        raise RuntimeError("staged manifest database hash does not match the staged database")
    if written.get("database", {}).get("logical_content_sha256") != expected_logical:
        raise RuntimeError("staged manifest logical hash does not match the staged database")


def _activate_release(staging_dir: Path, release_dir: Path) -> None:
    """Atomically move the complete staging directory into place."""
    try:
        staging_dir.rename(release_dir)
    except FileExistsError:
        raise RuntimeError(
            f"release directory {release_dir} appeared during publish; refusing to overwrite "
            f"an existing release"
        ) from None


def _set_current(
    data_dir: Path,
    dataset_id: str,
    release_dir: Path,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    database = release_dir / "data.sqlite"
    manifest_path = release_dir / "manifest.json"
    current = {
        "dataset_id": dataset_id,
        "database": _relative(database),
        "manifest": _relative(manifest_path),
        "logical_content_sha256": manifest["database"]["logical_content_sha256"],
        "file_sha256": manifest["database"]["sha256"],
        "schema_version": manifest["schema_version"],
        "target_game_version": manifest["target_game_version"],
        "dataset_status": manifest.get("dataset_status"),
        "partial": manifest.get("dataset_status") == "partial",
        "published_at": _now(),
    }
    data_dir.mkdir(parents=True, exist_ok=True)
    current_tmp = data_dir / "current.json.tmp"
    current_tmp.write_text(
        json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    current_tmp.replace(data_dir / "current.json")
    return current


def _verify_existing_release(release_dir: Path, dataset_id: str, logical_sha: str) -> dict[str, Any]:
    """Validate an existing release and return its manifest."""
    manifest_path = release_dir / "manifest.json"
    release_db = release_dir / "data.sqlite"
    if not manifest_path.exists() or not release_db.exists():
        raise RuntimeError(
            f"release {dataset_id!r} exists but is incomplete (missing manifest.json or "
            f"data.sqlite); refusing to overwrite an immutable release"
        )
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise RuntimeError(
            f"release {dataset_id!r} has an unreadable manifest: {exc}"
        ) from None
    database = existing.get("database") or {}
    if database.get("logical_content_sha256") != logical_sha:
        raise RuntimeError(
            f"dataset {dataset_id!r} already has a release with different content; releases "
            f"are immutable -- publish the change under a new dataset id"
        )
    if sha256_file(release_db) != database.get("sha256"):
        raise RuntimeError(
            f"release {dataset_id!r} database hash does not match its manifest; refusing to "
            f"treat a corrupted release as an idempotent publish"
        )
    existing_conn = _open_readonly_immutable(release_db)
    try:
        fingerprint = logical_fingerprint(existing_conn)
    finally:
        existing_conn.close()
    if fingerprint["logical_sha256"] != logical_sha:
        raise RuntimeError(
            f"release {dataset_id!r} logical content does not match its manifest; refusing to "
            f"treat a corrupted release as an idempotent publish"
        )
    return existing


def _publish_new_release(
    dataset_id: str,
    db_path: Path,
    conn,
    fingerprint: dict[str, Any],
    data_dir: Path,
    *,
    run_id: str | None,
    generation_command: str,
    limitations: list[str] | None,
    is_partial: bool,
    force_current: bool,
) -> dict[str, Any]:
    release_root = Path(RELEASES_DIR)
    release_root.mkdir(parents=True, exist_ok=True)
    release_dir = release_root / dataset_id
    staging_dir = Path(
        tempfile.mkdtemp(dir=release_root, prefix=f".staging-{dataset_id}-")
    )
    activated = False
    try:
        staged_db = _stage_database(db_path, staging_dir)
        manifest = build_manifest(
            conn,
            staged_db,
            dataset_id=dataset_id,
            run_id=run_id,
            generation_command=generation_command,
            limitations=limitations,
            content_fingerprint=fingerprint,
        )
        # The release path is part of the immutable identity, not the staging path.
        manifest["database"]["path"] = _relative(release_dir / "data.sqlite")
        _write_staged_manifest(staging_dir, manifest)
        _verify_staged_release(staging_dir, manifest)
        _activate_release(staging_dir, release_dir)
        activated = True
        if is_partial and not force_current:
            return {
                "result": "partial_released",
                "dataset_id": dataset_id,
                "release": _relative(release_dir),
                "manifest": manifest,
                "current": _read_current(data_dir),
                "switched_current": False,
            }
        current = _set_current(data_dir, dataset_id, release_dir, manifest)
        return {
            "result": "published",
            "dataset_id": dataset_id,
            "release": _relative(release_dir),
            "manifest": manifest,
            "current": current,
            "switched_current": True,
        }
    except BaseException:
        shutil.rmtree(staging_dir, ignore_errors=True)
        (data_dir / "current.json.tmp").unlink(missing_ok=True)
        if activated:
            # The release was created by this call and current was not switched:
            # roll it back so a retry starts from the previous consistent state.
            shutil.rmtree(Path(RELEASES_DIR) / dataset_id, ignore_errors=True)
        raise


def publish_dataset(
    dataset_id: str,
    *,
    conn=None,
    dataset_db: Path | None = None,
    run_id: str | None = None,
    generation_command: str = "",
    limitations: list[str] | None = None,
    allow_partial: bool = False,
    force_current: bool = False,
) -> dict[str, Any]:
    """Publish a validated dataset as an immutable release, then switch current.

    Parameters
    ----------
    dataset_id:
        Immutable release identity.  Re-publishing the same content is
        idempotent; different content raises instead of overwriting.
    conn:
        Optional open connection to the dataset database (used for the logical
        fingerprint and manifest).  When omitted the database is opened
        read-only.
    dataset_db:
        Source database path; defaults to ``data/normalized/<dataset_id>/data.sqlite``.
    allow_partial:
        Required when ``dataset_meta.status == 'partial'``.  A partial release is
        written but ``current.json`` is kept unchanged unless ``force_current``.
    force_current:
        Allow a partial release to replace ``current.json``.

    Returns
    -------
    dict with ``result`` in ``{"published", "partial_released", "idempotent"}``,
    plus ``manifest``, ``current`` (the effective current pointer, which may be
    the previous one), ``release`` and ``switched_current``.
    """
    from ..paths import DATA_DIR

    db_path = Path(dataset_db) if dataset_db else Path(NORMALIZED_DIR) / dataset_id / "data.sqlite"
    if not db_path.exists():
        raise FileNotFoundError(f"dataset database not found: {db_path}")
    close_conn = conn is None
    if conn is None:
        conn = connect(db_path, readonly=True)
    try:
        meta = conn.execute("SELECT * FROM dataset_meta").fetchone()
        if meta is None:
            raise RuntimeError("dataset has no dataset_meta row")
        recorded_id = meta["dataset_id"] if "dataset_id" in meta.keys() else None
        if recorded_id and str(recorded_id) != dataset_id:
            raise RuntimeError(
                f"dataset_meta.dataset_id is {recorded_id!r} but publish_dataset was called "
                f"with dataset_id {dataset_id!r}"
            )
        status = meta["status"]
        if status == "partial":
            if not allow_partial:
                raise RuntimeError(
                    f"dataset {dataset_id} status is 'partial'; pass allow_partial=True to "
                    f"publish a partial release that keeps current.json unchanged"
                )
            is_partial = True
        elif status in {"valid", "published"}:
            is_partial = False
        else:
            raise RuntimeError(
                f"dataset {dataset_id} status is {status!r}; validate it first"
            )
        fingerprint = logical_fingerprint(conn)
        data_dir = Path(DATA_DIR)
        with _hold_publish_lock(data_dir):
            release_dir = Path(RELEASES_DIR) / dataset_id
            if release_dir.exists():
                existing = _verify_existing_release(
                    release_dir, dataset_id, fingerprint["logical_sha256"]
                )
                if is_partial and force_current:
                    current = _set_current(data_dir, dataset_id, release_dir, existing)
                    return {
                        "result": "published",
                        "dataset_id": dataset_id,
                        "release": _relative(release_dir),
                        "manifest": existing,
                        "current": current,
                        "switched_current": True,
                    }
                return {
                    "result": "partial_released" if is_partial else "idempotent",
                    "dataset_id": dataset_id,
                    "release": _relative(release_dir),
                    "manifest": existing,
                    "current": _read_current(data_dir),
                    "switched_current": False,
                }
            return _publish_new_release(
                dataset_id,
                db_path,
                conn,
                fingerprint,
                data_dir,
                run_id=run_id,
                generation_command=generation_command,
                limitations=limitations,
                is_partial=is_partial,
                force_current=force_current,
            )
    finally:
        if close_conn:
            conn.close()


def published_dataset_paths() -> dict[str, str] | None:
    from ..paths import DATA_DIR

    current_path = Path(DATA_DIR) / "current.json"
    if not current_path.exists():
        return None
    return json.loads(current_path.read_text(encoding="utf-8"))
