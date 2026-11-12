"""Atomic filesystem primitives for workflow commits (P4).

The helpers here are intentionally small and dependency-free:

* content digests (file / bytes / directory tree);
* atomic text/bytes writes (same-directory temp file + ``os.replace``);
* atomic publication of a candidate file or directory, with a verified sha;
* cleanup of ``.tmp-<operation_id>-*`` staging entries.

No CLI, no printing, no global state.  Callers own the semantic commit
protocol (see :mod:`le_calc.workflow.commit`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

TMP_PREFIX = ".tmp-"
PUBLISH_LOCK_PREFIX = ".le-publish-"


class AtomicIOError(OSError):
    """Raised when a candidate/target cannot be published atomically."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    p = Path(path)
    digest = hashlib.sha256()
    with p.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: str | Path, *, ignore_tmp: bool = True) -> str:
    """Deterministic digest of a directory tree.

    Entries are sorted by POSIX relative path; each regular file contributes
    its relative path, a NUL separator and its content sha256.  Symlinks are
    rejected so a publish never follows a candidate out of its tree.
    """

    base = Path(root)
    if not base.is_dir():
        raise AtomicIOError(f"directory tree not found: {base}")
    digest = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for current, dirs, files in os.walk(base):
        dirs.sort()
        files.sort()
        for name in files:
            path = Path(current) / name
            if path.is_symlink():
                raise AtomicIOError(f"symlink in candidate tree: {path}")
            relative = path.relative_to(base).as_posix()
            if ignore_tmp and Path(relative).name.startswith(TMP_PREFIX):
                continue
            entries.append((relative, path))
    for relative, path in sorted(entries, key=lambda item: item[0]):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def canonical_payload_sha256(payload: Any) -> str:
    return sha256_bytes(canonical_json_bytes(payload))


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    except OSError:  # pragma: no cover - best effort on platforms/filesystems
        pass


def _fsync_dir(path: Path) -> None:
    if os.name == "nt":  # Windows cannot fsync a directory handle portably.
        return
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:  # pragma: no cover - best effort
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_bytes(path: str | Path, data: bytes) -> dict[str, str]:
    """Write ``data`` to ``path`` via a same-directory temp file + replace."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=".tmp-write-", suffix=".part", dir=str(target.parent)
    )
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, target)
        _fsync_dir(target.parent)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:  # pragma: no cover - best effort
            pass
        raise
    return {"path": str(target), "sha256": sha256_bytes(data)}


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> dict[str, str]:
    text = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    ).encode("utf-8")
    return atomic_write_bytes(path, text)


def publish_lock_path(target: str | Path) -> Path:
    destination = Path(target)
    key = hashlib.sha256(str(destination.resolve()).encode("utf-8")).hexdigest()[:32]
    return destination.parent / f"{PUBLISH_LOCK_PREFIX}{key}.lock"


class _PublishLock:
    """Target-scoped O_EXCL mutex held *inside* the publish primitive."""

    def __init__(self, target: Path, *, timeout_seconds: float = 60.0,
                 stale_seconds: float = 300.0, poll_seconds: float = 0.02):
        self.path = publish_lock_path(target)
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.poll_seconds = poll_seconds
        self._acquired = False

    def _stale(self) -> bool:
        try:
            return time.time() - self.path.stat().st_mtime > self.stale_seconds
        except OSError:
            return False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                if self._stale():
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise AtomicIOError(f"等待目标发布锁超时：{self.path.name}")
                time.sleep(self.poll_seconds)
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write('{"target": "%s", "pid": %d}\n' % (self.path.parent, os.getpid()))
            self._acquired = True
            return

    def release(self) -> None:
        if not self._acquired:
            return
        self._acquired = False
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "_PublishLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def _staging_name(target: Path, operation_id: str, suffix: str) -> Path:
    token = uuid.uuid4().hex[:12]
    return target.parent / f"{TMP_PREFIX}{operation_id}-{token}{suffix}"


def cleanup_operation_temps(parent: str | Path, operation_id: str) -> list[str]:
    """Remove orphan staging entries for one operation under ``parent``."""

    directory = Path(parent)
    removed: list[str] = []
    if not directory.is_dir():
        return removed
    prefix = f"{TMP_PREFIX}{operation_id}-"
    for entry in sorted(directory.iterdir(), key=lambda item: item.name):
        if not entry.name.startswith(prefix):
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed.append(str(entry))
        except OSError:  # pragma: no cover - best effort
            continue
    return removed



def atomic_replace_file(
    source: str | Path,
    target: str | Path,
    *,
    expected_sha256: Optional[str] = None,
    expected_current_sha256: Optional[str] = None,
    operation_id: str = "commit",
    lock_timeout_seconds: float = 60.0,
    stale_lock_seconds: float = 300.0,
) -> dict[str, str]:
    """Atomically replace an existing pointer file (no half-written target).

    The target mutex is acquired inside this primitive.  ``expected_current_sha256``
    is verified while holding the mutex immediately before the replace, so two
    pointer updates with the same expected old content cannot both win.
    """

    candidate = Path(source)
    destination = Path(target)
    if not candidate.is_file():
        raise AtomicIOError(f"candidate file not found: {candidate}")
    if destination.is_symlink():
        raise AtomicIOError(f"refusing to replace symlink pointer: {destination}")
    actual = sha256_file(candidate)
    if expected_sha256 and actual != str(expected_sha256):
        raise AtomicIOError(
            f"candidate sha256 mismatch: expected {expected_sha256}, got {actual}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _PublishLock(
        destination,
        timeout_seconds=lock_timeout_seconds,
        stale_seconds=stale_lock_seconds,
    ):
        if not destination.is_file():
            raise AtomicIOError(f"pointer target does not exist: {destination}")
        current = sha256_file(destination)
        if expected_current_sha256 and current != str(expected_current_sha256):
            raise AtomicIOError(
                f"pointer target changed under lock: expected {expected_current_sha256}, got {current}"
            )
        staging = _staging_name(destination, operation_id, ".pointer")
        try:
            shutil.copyfile(candidate, staging)
            _fsync_file(staging)
            copied = sha256_file(staging)
            if copied != actual:
                raise AtomicIOError("staged pointer sha256 mismatch after copy")
            os.replace(staging, destination)
            _fsync_dir(destination.parent)
        except BaseException:
            try:
                staging.unlink()
            except OSError:
                pass
            raise
    return {
        "path": str(destination),
        "sha256": actual,
        "kind": "file",
        "previous_sha256": current,
    }




def atomic_publish_file(
    source: str | Path,
    target: str | Path,
    *,
    expected_sha256: Optional[str] = None,
    operation_id: str = "commit",
    lock_timeout_seconds: float = 60.0,
    stale_lock_seconds: float = 300.0,
) -> dict[str, str]:
    """Copy a candidate file into a target via verified temp + replace.

    The target mutex is acquired *inside* this primitive, after any caller or
    test wrapper has entered the publish call.
    """

    candidate = Path(source)
    destination = Path(target)
    if not candidate.is_file():
        raise AtomicIOError(f"candidate file not found: {candidate}")
    actual = sha256_file(candidate)
    if expected_sha256 and actual != str(expected_sha256):
        raise AtomicIOError(
            f"candidate sha256 mismatch: expected {expected_sha256}, got {actual}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _PublishLock(
        destination,
        timeout_seconds=lock_timeout_seconds,
        stale_seconds=stale_lock_seconds,
    ):
        if destination.exists():
            raise AtomicIOError(f"target file already exists: {destination}")
        staging = _staging_name(destination, operation_id, ".part")
        try:
            shutil.copyfile(candidate, staging)
            _fsync_file(staging)
            copied = sha256_file(staging)
            if copied != actual:
                raise AtomicIOError("staged file sha256 mismatch after copy")
            os.replace(staging, destination)
            _fsync_dir(destination.parent)
        except BaseException:
            try:
                staging.unlink()
            except OSError:  # pragma: no cover - best effort
                pass
            raise
    return {
        "path": str(destination),
        "sha256": actual,
        "kind": "file",
    }


def atomic_publish_dir(
    source: str | Path,
    target: str | Path,
    *,
    expected_tree_sha256: Optional[str] = None,
    operation_id: str = "commit",
    lock_timeout_seconds: float = 60.0,
    stale_lock_seconds: float = 300.0,
) -> dict[str, Any]:
    """Publish a candidate directory to an absent target path.

    The target mutex is acquired inside this primitive; existing targets are
    never replaced.
    """

    candidate = Path(source)
    destination = Path(target)
    if not candidate.is_dir():
        raise AtomicIOError(f"candidate directory not found: {candidate}")
    actual = tree_sha256(candidate)
    if expected_tree_sha256 and actual != str(expected_tree_sha256):
        raise AtomicIOError(
            f"candidate tree sha256 mismatch: expected {expected_tree_sha256}, got {actual}"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with _PublishLock(
        destination,
        timeout_seconds=lock_timeout_seconds,
        stale_seconds=stale_lock_seconds,
    ):
        if destination.exists():
            raise AtomicIOError(f"target directory already exists: {destination}")
        staging = _staging_name(destination, operation_id, ".dir")
        try:
            shutil.copytree(candidate, staging, symlinks=False)
            for current, _dirs, files in os.walk(staging):
                for name in files:
                    _fsync_file(Path(current) / name)
            staged = tree_sha256(staging)
            if staged != actual:
                raise AtomicIOError("staged tree sha256 mismatch after copy")
            os.replace(staging, destination)
            _fsync_dir(destination.parent)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
    return {
        "path": str(destination),
        "sha256": actual,
        "kind": "dir",
        "file_count": sum(len(files) for _root, _dirs, files in os.walk(destination)),
    }
