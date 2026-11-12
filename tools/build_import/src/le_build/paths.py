"""Filesystem layout and small hashing helpers for the phase-2 build importer."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

DEFAULT_BUILD_NAMESPACE = "letools"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upwards until the phase-1 project root (README.md + data/current.json) is found."""
    here = Path(start) if start is not None else Path(__file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "data" / "current.json").is_file() and (candidate / "README.md").is_file():
            return candidate
    raise RuntimeError(
        "cannot locate project root (expected data/current.json and README.md in an ancestor directory)"
    )


def repo_default() -> Path:
    env = os.environ.get("LE_REPO_ROOT")
    if env:
        return Path(env).resolve()
    return find_repo_root()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value) -> bytes:
    """Deterministic JSON used for content hashing (sorted keys, compact, UTF-8)."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value, *, indent: int = 2, sort_keys: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(value, ensure_ascii=False, indent=indent, sort_keys=sort_keys)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")


def pointer_for(*parts: str) -> str:
    """Build an RFC6901-ish JSON pointer for source traceability (``data.equipment.head``)."""
    if not parts:
        return ""
    return ".".join(str(part) for part in parts)


class BuildPaths:
    """Paths for one build inside ``data/builds/<source>/<share_id>/``."""

    def __init__(self, root: Path | None = None, source: str = DEFAULT_BUILD_NAMESPACE):
        self.root = Path(root) if root is not None else repo_default()
        self.source = source

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def current_pointer(self) -> Path:
        return self.data_dir / "current.json"

    def build_dir(self, share_id: str) -> Path:
        return self.data_dir / "builds" / self.source / share_id

    def raw_dir(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "raw"

    def fetches_dir(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "fetches"

    def revisions_dir(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "revisions"

    def revision_dir(self, share_id: str, revision_id: str) -> Path:
        return self.revisions_dir(share_id) / revision_id

    def current_file(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "current.json"

    def imports_dir(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "imports"

    def inputs_dir(self, share_id: str) -> Path:
        return self.build_dir(share_id) / "inputs"

    def inputs_file(self, share_id: str) -> Path:
        return self.inputs_dir(share_id) / "local-inputs.json"

    def root_path(self, relative_path: str | Path) -> Path:
        """Resolve a repo-relative path recorded in a revision (e.g. archived raw payload)."""
        path = Path(relative_path)
        return path if path.is_absolute() else self.root / path

    def reports_dir(self) -> Path:
        return self.root / "reports" / "phase-2"
