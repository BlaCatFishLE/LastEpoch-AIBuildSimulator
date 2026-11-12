# -*- coding: utf-8 -*-
r"""Build the minimal public package from an **explicit manifest**.

The public release contains runtime code, knowledge, base data, docs, tests and
test fixtures only.  It deliberately excludes every author-local artifact:

* cases (``examples/`` except the template), saves, build revisions and reports;
* the 250+ MiB offline dataset database (distributed as a GitHub Release asset
  and fetched by ``scripts/fetch_dataset.ps1``);
* regenerable caches, virtualenvs, scratch workspaces and bundled external tools.

``package-manifest.json`` records per-file SHA-256 plus the dataset/version
bindings and is written next to the package (it is not part of its own file
list, so it cannot contain its own hash).  Git is optional: when unavailable the
manifest records ``available: false`` and packaging still succeeds.

Usage::

    & .\.venv\Scripts\python.exe scripts\package.py
    & .\.venv\Scripts\python.exe scripts\package.py --out dist\my-release --zip dist\my-release.zip
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_NAME = "package-manifest.json"
MANIFEST_SCHEMA = "le-package-manifest-v1"
ZIP_FIXED_TIME = (1980, 1, 1, 0, 0, 0)

#: Explicit release content.  ``file`` = single file, ``dir`` = recursive.
INCLUDE: list[tuple[str, str]] = [
    ("README.md", "file"),
    ("LICENSE", "file"),
    ("THIRD_PARTY_NOTICES.md", "file"),
    ("AGENTS.md", "file"),
    (".gitignore", "file"),
    (".gitattributes", "file"),
    ("config", "dir"),
    ("docs", "dir"),
    ("knowledge", "dir"),
    ("examples/_template", "dir"),
    ("scripts", "dir"),
    ("tools/affix_db", "dir"),
    ("tools/build_import/README.md", "file"),
    ("tools/build_import/schemas", "dir"),
    ("tools/build_import/src", "dir"),
    ("tools/build_import/tests", "dir"),
    ("tools/extract-game/README.md", "file"),
    ("tools/ingest/pyproject.toml", "file"),
    ("tools/ingest/fixtures", "dir"),
    ("tools/ingest/migrations", "dir"),
    ("tools/ingest/scripts", "dir"),
    ("tools/ingest/src", "dir"),
    ("tools/ingest/tests", "dir"),
    ("tools/knowledge/README.md", "file"),
    ("tools/knowledge/src", "dir"),
    ("tools/knowledge/tests", "dir"),
    ("tools/phase3/README.md", "file"),
    ("tools/phase3/data", "dir"),
    ("tools/phase3/fixtures", "dir"),
    ("tools/phase3/rules", "dir"),
    ("tools/phase3/schemas", "dir"),
    ("tools/phase3/src", "dir"),
    ("tools/phase3/tests", "dir"),
    ("tools/save_import/README.md", "file"),
    ("tools/save_import/data", "dir"),
    ("tools/save_import/scripts/pipeline", "dir"),
    ("tools/save_import/scripts/tool_native", "dir"),
    ("tools/save_import/scripts/render_bd_html.py", "file"),
    ("tools/save_import/src", "dir"),
    ("tools/save_import/tests", "dir"),
    ("data/current.json", "file"),
    ("data/affix-db/v1", "dir"),
    ("data/mappings", "dir"),
    ("data/releases/phase1-dataset-003/manifest.json", "file"),
]

#: Explicit exclusions (safety net while walking INCLUDE dirs).  Prefixes end
#: with ``/``; glob patterns are matched against the basename or relative path.
EXCLUDE: list[tuple[str, str]] = [
    (".git/", "版本历史不随发布包分发；版本由 manifest 的 commit/tag 标识"),
    (".venv/", "本机 Python 环境，接收方自行创建（见 README）"),
    (".scratch/", "本地临时工作区"),
    ("_verify/", "本地验证临时输出"),
    ("reports/", "用户本地运行产物（不随包分发）"),
    ("data/cache/", "可删除重建的知识查询索引与阶段缓存"),
    ("data/builds/", "用户构筑 revision（本地产物）"),
    ("data/raw/", "用户原始存档/来源快照（本地产物）"),
    ("data/staging/", "可再生的规范化 staging 数据"),
    ("data/normalized/", "可再生的规范化中间数据"),
    ("data/reference/", "作者本机游戏参考数据，不随公开版分发"),
    ("data/releases/*/data.sqlite", "离线 dataset 走 Release 附件，由 scripts/fetch_dataset.ps1 获取"),
    ("tools/node/", "外部 Node 运行时，普通运行不需要"),
    ("tools/jdk-*/", "外部 JDK，普通运行不需要"),
    ("tools/ghidra_*/", "逆向分析工具，普通运行不需要"),
    ("tools/phase3/work/", "生成的中间快照/演示报告"),
    ("tools/extract-game/data/", "作者本机游戏数据提取快照，不随包分发"),
    ("dist/", "本地打包输出"),
    ("**/__pycache__/", "Python 字节码缓存"),
    ("*.pyc", "Python 字节码缓存"),
    ("**/.DS_Store", "操作系统元数据"),
    ("**/*.log", "日志"),
]

#: Files whose absence makes the package unusable (reported before writing).
REQUIRED_FILES: list[str] = [
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "AGENTS.md",
    "config/project.json",
    "config/sources.example.json",
    "scripts/le.ps1",
    "scripts/le_commands.py",
    "scripts/fetch_dataset.ps1",
    "examples/_template/case.json",
    "knowledge/README.md",
    "knowledge/definitions/index.json",
    "data/current.json",
    "data/releases/phase1-dataset-003/manifest.json",
    "data/affix-db/v1/manifest.json",
    "data/affix-db/v1/normalized/affix-db.sqlite",
    "tools/phase3/src/le_calc/__init__.py",
    "tools/build_import/src/le_build/__init__.py",
    "tools/save_import/src/le_save_import/__init__.py",
    "tools/knowledge/src/le_knowledge/__init__.py",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def load_config() -> dict[str, Any]:
    path = ROOT / "config" / "project.json"
    if not path.is_file():
        return {}
    return load_json(path)


def _match_exclusion(relative_posix: str, name: str) -> str | None:
    """Return the exclusion reason if this path matches an explicit exclusion."""
    import fnmatch

    for pattern, reason in EXCLUDE:
        if pattern.endswith("/"):
            prefix = pattern[:-1]
            if relative_posix == prefix or relative_posix.startswith(prefix + "/"):
                return reason
            if name == prefix:
                return reason
        elif any(ch in pattern for ch in "*?["):
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(relative_posix, pattern.replace("**/", "")):
                return reason
        elif relative_posix == pattern or relative_posix.endswith("/" + pattern):
            return reason
    return None


def _iter_included_files() -> Iterable[Path]:
    for entry, kind in INCLUDE:
        path = ROOT / entry
        if not path.exists():
            yield path  # reported as missing by the caller
            continue
        if kind == "file":
            yield path
            continue
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            relative_posix = child.relative_to(ROOT).as_posix()
            if _match_exclusion(relative_posix, child.name):
                continue
            yield child


def _git_metadata() -> dict[str, Any]:
    config = load_config()
    candidates: list[str] = []
    if os.environ.get("LE_GIT"):
        candidates.append(os.environ["LE_GIT"])
    candidates.extend((config.get("git") or {}).get("candidates") or ["git"])
    executable = None
    for candidate in candidates:
        if not candidate:
            continue
        if Path(candidate).is_absolute():
            if Path(candidate).is_file():
                executable = candidate
                break
        else:
            found = shutil.which(candidate)
            if found:
                executable = found
                break
    if executable is None:
        return {
            "available": False,
            "commit": None,
            "tag": None,
            "describe": None,
            "worktree_clean": None,
            "status": [],
            "note": "git 不可用（未在 PATH/LE_GIT）：版本信息记为 unknown；打包不依赖 git。",
        }

    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [executable, *args], cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace"
        )

    commit = git("rev-parse", "HEAD")
    tag = git("describe", "--tags", "--always")
    status = git("status", "--porcelain")
    available = commit.returncode == 0
    note: str | None = None
    if not available:
        detail = (commit.stderr or commit.stdout or "").strip()
        if "not a git repository" in detail.lower():
            note = "当前工作目录尚未建立 Git 仓库：版本信息记为 unknown；打包不依赖 git。"
        else:
            note = f"git 命令执行失败：{detail[:200] or 'unknown error'}"
    return {
        "available": available,
        "commit": commit.stdout.strip() if available else None,
        "tag": tag.stdout.strip() if tag.returncode == 0 else None,
        "describe": tag.stdout.strip() if tag.returncode == 0 else None,
        "worktree_clean": (not status.stdout.strip()) if status.returncode == 0 else None,
        "status": status.stdout.strip().splitlines()[:50] if status.returncode == 0 else [],
        "note": note,
    }


def _data_bindings() -> dict[str, Any]:
    bindings: dict[str, Any] = {}
    current_path = ROOT / "data" / "current.json"
    if current_path.is_file():
        current = load_json(current_path)
        bindings["dataset"] = {
            "dataset_id": current.get("dataset_id"),
            "target_game_version": current.get("target_game_version"),
            "logical_content_sha256": current.get("logical_content_sha256"),
            "file_sha256": current.get("file_sha256"),
            "database": current.get("database"),
            "distribution": "GitHub Release asset (data.sqlite); fetch with scripts/fetch_dataset.ps1",
        }
    affix_db = ROOT / "data" / "affix-db" / "v1" / "normalized" / "affix-db.sqlite"
    if affix_db.is_file():
        bindings["affix_db"] = {
            "path": "data/affix-db/v1/normalized/affix-db.sqlite",
            "sha256": sha256_file(affix_db),
        }
    template = ROOT / "examples" / "_template" / "case.json"
    if template.is_file():
        bindings["case_template"] = {
            "path": "examples/_template/case.json",
            "sha256": sha256_file(template),
        }
    return bindings


def _clean_dir(path: Path, *, overwrite: bool) -> None:
    if path.exists():
        if not path.is_dir():
            raise SystemExit(f"错误：输出路径不是目录：{path}")
        if any(path.iterdir()):
            if not overwrite:
                raise SystemExit(f"错误：输出目录非空，拒绝覆盖：{path}（如确需覆盖请加 --overwrite）")
            shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def _write_deterministic_zip(directory: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            info = zipfile.ZipInfo(path.relative_to(directory).as_posix(), date_time=ZIP_FIXED_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def build(out_dir: Path, zip_path: Path | None, *, overwrite: bool) -> int:
    config = load_config()
    project = config.get("project") or {}
    name = str(project.get("name") or "LastEpoch-BuildSimulator")
    version = str(project.get("version") or "0.0.0")
    print(f"打包 {name} {version} → {out_dir}", flush=True)

    missing_required = [entry for entry in REQUIRED_FILES if not (ROOT / entry).is_file()]
    if missing_required:
        print("错误：公开包必需文件缺失：", file=sys.stderr)
        for entry in missing_required:
            print(f"  - {entry}", file=sys.stderr)
        return 1

    included: list[tuple[Path, str]] = []
    missing: list[str] = []
    for path in _iter_included_files():
        if not path.exists():
            missing.append(str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path))
            continue
        included.append((path, path.relative_to(ROOT).as_posix()))
    if missing:
        print("错误：以下清单条目不存在：", file=sys.stderr)
        for entry in missing:
            print(f"  - {entry}", file=sys.stderr)
        return 1

    # deterministic order and de-duplication
    unique: dict[str, Path] = {}
    for path, relative_posix in included:
        unique.setdefault(relative_posix, path)
    ordered = sorted(unique.items())

    excluded_files = 0
    for entry, kind in INCLUDE:
        source_dir = ROOT / entry
        if kind != "dir" or not source_dir.is_dir():
            continue
        for child in source_dir.rglob("*"):
            if not child.is_file():
                continue
            relative_posix = child.relative_to(ROOT).as_posix()
            if _match_exclusion(relative_posix, child.name):
                excluded_files += 1
    print(
        f"清单：包含条目 {len(INCLUDE)} 条（file/dir），排除规则 {len(EXCLUDE)} 条，"
        f"遍历时排除文件 {excluded_files} 个",
        flush=True,
    )

    _clean_dir(out_dir, overwrite=overwrite)
    files: list[dict[str, Any]] = []
    total_bytes = 0
    for relative_posix, source in ordered:
        destination = out_dir / relative_posix
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        size = destination.stat().st_size
        total_bytes += size
        files.append(
            {
                "path": relative_posix,
                "size": size,
                "sha256": sha256_file(destination),
            }
        )
        if len(files) % 200 == 0:
            print(f"  已复制 {len(files)} 个文件……", flush=True)

    manifest = {
        "schema": MANIFEST_SCHEMA,
        "manifest_name": MANIFEST_NAME,
        "project": {"name": name, "version": version},
        "generated_at": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat(),
        "source_project_root_name": ROOT.name,
        "git": _git_metadata(),
        "dependencies": {
            "runtime": [
                "Python 3.12（标准库即可运行计算、测试与打包）",
                "Windows PowerShell 5.1+（scripts/le.ps1、scripts/fetch_dataset.ps1）",
            ],
            "database_rebuild": ["Python 3.12；affix-db 冻结快照随包（data/affix-db/v1）"],
            "dataset": [
                "离线 dataset（data.sqlite）不随包分发：从 GitHub Release 附件获取，"
                "用 scripts/fetch_dataset.ps1 校验 data/current.json 的 file_sha256",
            ],
            "external_optional": [
                "Node.js + lastepochsupertools：仅 tool-native 新存档完整首接需要，不随包分发；"
                "未配置时 prepare 返回 needs_input",
            ],
            "python_version_used_to_build": sys.version.split()[0],
        },
        "bindings": _data_bindings(),
        "application": {
            "entry": "scripts/le.ps1",
            "dataset_fetch": "scripts/fetch_dataset.ps1",
            "case_template": "examples/_template/case.json",
            "case": None,
            "expected_summary": None,
        },
        "counts": {
            "files": len(files),
            "bytes": total_bytes,
        },
        "files": files,
        "exclusions": [{"pattern": pattern, "reason": reason} for pattern, reason in EXCLUDE],
        "notes": [
            "本清单不包含 package-manifest.json 自身的哈希（避免自引用）；校验器以清单内容为准。",
            "文件清单按仓库根相对 POSIX 路径排序，sha256 为打包副本的实际哈希。",
            "打包不依赖 git；git 不可用时 manifest.git.available=false。",
            "公开包不含案例/存档/构筑 revision/历史报告；dataset 走 Release 附件。",
            "已发布 dataset / affix-db 为只读绑定，接收方不得就地改写。",
        ],
    }
    manifest_path = out_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n"
    )

    final_zip = zip_path or out_dir.with_suffix(".zip")
    _write_deterministic_zip(out_dir, final_zip)

    print()
    print(f"文件数：{len(files)}；总大小：{total_bytes / (1024 * 1024):.1f} MiB")
    print(f"清单：{manifest_path}（schema {MANIFEST_SCHEMA}；不含自身哈希）")
    print(f"发布目录：{out_dir}")
    print(f"发布 zip：{final_zip}（{final_zip.stat().st_size / (1024 * 1024):.1f} MiB）")
    print("提示：接收方解压后先运行 .\\scripts\\fetch_dataset.ps1 -Repo BlaCatFishLE/LastEpoch-AIBuildSimulator，再运行 .\\scripts\\le.ps1 doctor。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="package.py",
        description="按显式清单构建公开版发布包（含 package-manifest.json 与 zip）。",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出目录（默认 dist/<name>-<version>）",
    )
    parser.add_argument("--zip", default=None, help="zip 路径（默认与输出目录同名 .zip）")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖非空输出目录")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass
    args = build_parser().parse_args(argv)
    config = load_config()
    project = config.get("project") or {}
    version = str(project.get("version") or "0.0.0")
    default_out = ROOT / "dist" / f"{project.get('name') or 'LastEpoch-BuildSimulator'}-{version}"
    out_dir = (Path(args.out) if args.out else default_out)
    out_dir = out_dir.resolve() if out_dir.is_absolute() else (ROOT / out_dir).resolve()
    zip_path = None
    if args.zip:
        zip_path = (Path(args.zip) if Path(args.zip).is_absolute() else ROOT / args.zip).resolve()
    return build(out_dir, zip_path, overwrite=args.overwrite)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
