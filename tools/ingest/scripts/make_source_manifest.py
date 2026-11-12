"""Write a source/artifact hash manifest without requiring git.

The review noted that "no git" is a transient environment statement, not a
project property.  This script produces a deterministic inventory of the
tracked source/config/docs plus the key data artifacts, so a released dataset
can always be tied to the exact input files that produced it.

Usage:
  python tools/ingest/scripts/make_source_manifest.py [--out docs/source-manifest.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

SKIP_DIRS = {
    "data",
    "reports",
    ".venv",
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
}

# Bundled external runtimes and local scratch workspaces are not project sources.
SKIP_RELATIVE_PREFIXES = (
    "tools/node",
    "tools/jdk-21",
    "tools/ghidra_",
    ".scratch",
    "_verify",
)
SOURCE_ROOTS = ["README.md", "AGENTS.md", "docs", "config", "scripts", "tools", "examples", "knowledge"]
ARTIFACTS = [
    "data/current.json",
    "data/mappings/crosswalk.json",
    "data/mappings/game-build.json",
    "data/mappings/version-compat.json",
    "data/releases/phase1-dataset-003/data.sqlite",
    "data/releases/phase1-dataset-003/manifest.json",
    "data/affix-db/v1/normalized/affix-db.sqlite",
    "data/affix-db/v1/manifest.json",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_source_files():
    for root in SOURCE_ROOTS:
        path = PROJECT_ROOT / root
        if path.is_file():
            yield path
            continue
        if not path.exists():
            continue
        for candidate in path.rglob("*"):
            if not candidate.is_file():
                continue
            if any(part in SKIP_DIRS for part in candidate.parts):
                continue
            relative = candidate.relative_to(PROJECT_ROOT).as_posix()
            if relative.startswith(SKIP_RELATIVE_PREFIXES):
                continue
            if candidate.suffix in {".pyc", ".pyo"}:
                continue
            yield candidate


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", default=str(PROJECT_ROOT / "docs" / "source-manifest.json")
    )
    args = parser.parse_args()
    out_path = Path(args.out).resolve()
    files = []
    for path in sorted(set(iter_source_files())):
        if path.resolve() == out_path:
            # Never hash the manifest being generated: it would be circular.
            continue
        files.append(
            {
                "path": str(path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    artifacts = []
    for relative in ARTIFACTS:
        path = PROJECT_ROOT / relative
        if not path.exists():
            continue
        artifacts.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generator": "tools/ingest/scripts/make_source_manifest.py",
        "note": "No git metadata is required: this manifest pins the exact source/config/docs "
        "and key data artifacts. Regenerate it before publishing a dataset.",
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            # Deliberately no absolute interpreter path: this manifest must be
            # portable and must not leak machine-specific locations.
            "interpreter_name": Path(sys.executable).name,
        },
        "file_count": len(files),
        "files": files,
        "artifacts": artifacts,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {out} ({len(files)} source files, {len(artifacts)} artifacts)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
