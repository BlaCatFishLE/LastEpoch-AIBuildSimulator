# -*- coding: utf-8 -*-
r"""Verify an unpacked public release directory produced by ``scripts/package.py``.

Checks performed (PASS/WARN/FAIL per item, non-zero exit on any FAIL):

1. ``package-manifest.json`` exists, parses and has the expected schema; it must
   not list itself (self-hash exclusion is stated in the manifest).
2. every manifest entry is present on disk with the recorded size and SHA-256.
3. required public files exist (README/LICENSE/config/scripts/tools/knowledge/
   data/affix-db/dataset manifest/case template) and required data is readable.
4. no author-local artifacts are present (cases, reports, builds, saves, caches,
   virtualenvs, scratch, bundled external tools, dataset sqlite).
5. no machine absolute paths or personal identity markers in the package; the
   portable surface (scripts/config/template/pipeline) is a hard failure, frozen
   provenance text elsewhere is a warning upgraded by ``--strict``.
6. no invalid JSON in shipped JSON files.

The offline dataset database is a GitHub Release asset and is intentionally
absent from the package; when it is present (local verification) its SHA-256 is
checked against ``data/current.json``.

Usage::

    & .\.venv\Scripts\python.exe scripts\check_package.py <unpacked-package-dir>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

MANIFEST_NAME = "package-manifest.json"
MANIFEST_SCHEMA = "le-package-manifest-v1"

#: Required files of the minimal public package (runtime + data pointers).
REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
    "AGENTS.md",
    ".gitignore",
    ".gitattributes",
    "config/project.json",
    "config/sources.example.json",
    "scripts/le.ps1",
    "scripts/le_commands.py",
    "scripts/fetch_dataset.ps1",
    "examples/_template/case.json",
    "knowledge/README.md",
    "knowledge/definitions/index.json",
    "knowledge/INDEX.generated.md",
    "data/current.json",
    "data/releases/phase1-dataset-003/manifest.json",
    "data/affix-db/v1/manifest.json",
    "data/affix-db/v1/normalized/affix-db.sqlite",
    "tools/phase3/src/le_calc/__init__.py",
    "tools/phase3/rules/ruleset-bh-stage1-v4.json",
    "tools/phase3/rules/ruleset-fr-hit-stage1-v2.json",
    "tools/build_import/src/le_build/__init__.py",
    "tools/save_import/src/le_save_import/__init__.py",
    "tools/knowledge/src/le_knowledge/__init__.py",
]

#: Paths that must never be shipped (author-local artifacts / machinery).
FORBIDDEN_PREFIXES = [
    "reports/",
    "data/builds/",
    "data/raw/",
    "data/cache/",
    "data/staging/",
    "data/normalized/",
    "data/reference/",
    "docs/history/",
    "docs/reviews/",
    "docs/tasks/",
    "docs/design/",
    "docs/evidence/screenshots/",
    "tools/node/",
    "tools/jdk-",
    "tools/ghidra_",
    ".git/",
    ".venv/",
    ".scratch/",
    ".pytest_cache/",
    "dist/",
]
FORBIDDEN_FILES = [
    "package-manifest.json.tmp",
    "data/releases/phase1-dataset-003/data.sqlite",
    "docs/inventory.json",
    "docs/source-manifest.json",
]
FORBIDDEN_EXAMPLE_ENTRIES = ("examples/_template",)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


#: Files that must stay portable: machine absolute paths here are a FAIL.
PORTABLE_PREFIXES = (
    "scripts/",
    "config/project.json",
    "examples/_template/",
    "tools/save_import/scripts/pipeline/",
)

TEXT_SUFFIXES = {
    ".py", ".ps1", ".json", ".jsonl", ".md", ".txt", ".toml", ".cfg", ".ini",
    ".yml", ".yaml", ".html", ".htm", ".js", ".css", ".csv", ".xml", ".sql",
}

MACHINE_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]"
    r"|(?<![\\A-Za-z0-9])\\\\[A-Za-z0-9._$-]+\\[A-Za-z0-9._$-]+"
)

#: Personal identity / author-local machine markers: never acceptable.
#: Placeholder user names used by synthetic test fixtures (e.g. C:\\Users\\x\\...)
#: are allowed; real profile names are not.
PERSONAL_MARKERS = (
    ("author-handle", re.compile("97" "393")),
    ("repo-absolute-path", re.compile("E:" + r"[\\/]+Codex[\\/]+Projects", re.IGNORECASE)),
    ("steam-library", re.compile("Steam" + "Library", re.IGNORECASE)),
    ("qq-email", re.compile("@" + "qq" + r"\.com", re.IGNORECASE)),
)
WIN_USER_RE = re.compile("C:" + r"[\\/]+Users[\\/]+([A-Za-z0-9._-]+)")
PLACEHOLDER_USERS = {"x", "user", "username", "test", "runner", "yourname", "name", "example"}


def personal_markers_in(text: str) -> list[str]:
    """Return the personal/local markers found in ``text`` (placeholders excluded)."""
    hits = [label for label, regex in PERSONAL_MARKERS if regex.search(text)]
    for match in WIN_USER_RE.finditer(text):
        if match.group(1).lower() not in PLACEHOLDER_USERS:
            hits.append("windows-user-path")
    return hits


def is_portable_surface(relative_posix: str) -> bool:
    return any(
        relative_posix == prefix or relative_posix.startswith(prefix)
        for prefix in PORTABLE_PREFIXES
    )


def iter_text_files(root: Path, files: Iterable[str]) -> Iterable[Path]:
    for relative_posix in files:
        path = root / relative_posix
        if path.suffix.lower() in TEXT_SUFFIXES and path.is_file():
            yield path


def main(argv: list[str] | None = None) -> int:
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass
    parser = argparse.ArgumentParser(
        prog="check_package.py",
        description="校验解压后的公开版发布包：清单、哈希、必需数据、禁止内容、机器绝对路径。",
    )
    parser.add_argument("path", help="解压后的发布包目录")
    parser.add_argument("--strict", action="store_true", help="把冻结/来源文本中的机器路径警告也视为失败")
    args = parser.parse_args(argv)

    package = Path(args.path).resolve()
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str = "", status: str | None = None) -> None:
        checks.append({"name": name, "status": status or ("PASS" if ok else "FAIL"), "detail": detail})

    if not package.is_dir():
        print(f"错误：发布包目录不存在：{package}", file=sys.stderr)
        return 1

    manifest_path = package / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"错误：缺少 {MANIFEST_NAME}：{manifest_path}", file=sys.stderr)
        return 1
    try:
        manifest = load_json(manifest_path)
    except json.JSONDecodeError as exc:
        print(f"错误：{MANIFEST_NAME} 不是合法 JSON：{exc}", file=sys.stderr)
        return 1

    add("清单 schema", manifest.get("schema") == MANIFEST_SCHEMA, str(manifest.get("schema")))
    exclusions = manifest.get("exclusions") or []
    add("清单排除规则", isinstance(exclusions, list) and bool(exclusions), f"{len(exclusions)} 条")
    declared = manifest.get("files") or []
    declared_paths = [str(item.get("path")) for item in declared if item.get("path")]
    add("清单文件条目", bool(declared_paths), f"{len(declared_paths)} 个文件")
    add(
        "清单不含自身哈希",
        MANIFEST_NAME not in declared_paths,
        "package-manifest.json 不在 files 列表中（避免自引用）",
    )

    # --- per-file size + hash -------------------------------------------
    bad_hash: list[str] = []
    bad_size: list[str] = []
    missing: list[str] = []
    for item in declared:
        relative_posix = str(item.get("path") or "")
        path = package / relative_posix
        if not path.is_file():
            missing.append(relative_posix)
            continue
        size = path.stat().st_size
        if size != item.get("size"):
            bad_size.append(f"{relative_posix}（{size} != {item.get('size')}）")
            continue
        actual = sha256_file(path)
        if actual != item.get("sha256"):
            bad_hash.append(relative_posix)
    add("文件完整性 · 存在", not missing, f"缺失 {len(missing)}" + (f"：{', '.join(missing[:8])}" if missing else ""))
    add("文件完整性 · 大小", not bad_size, "; ".join(bad_size[:8]))
    add("文件完整性 · SHA-256", not bad_hash, f"不匹配 {len(bad_hash)}" + (f"：{', '.join(bad_hash[:8])}" if bad_hash else ""))

    # --- unexpected extra files -----------------------------------------
    expected = set(declared_paths) | {MANIFEST_NAME}
    actual_files = {
        path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()
    }
    extra = sorted(actual_files - expected)
    add("无多余文件", not extra, f"额外 {len(extra)}" + (f"：{', '.join(extra[:8])}" if extra else ""), status="PASS" if not extra else "WARN")

    # --- required public files ------------------------------------------
    missing_required = [entry for entry in REQUIRED_FILES if not (package / entry).is_file()]
    add(
        "必需文件 · 存在",
        not missing_required,
        "齐全" if not missing_required else "缺失：" + ", ".join(missing_required[:10]),
    )
    for label, relative in (
        ("运行数据 · dataset 指针", "data/current.json"),
        ("运行数据 · dataset manifest", "data/releases/phase1-dataset-003/manifest.json"),
        ("运行数据 · affix DB manifest", "data/affix-db/v1/manifest.json"),
    ):
        path = package / relative
        ok = False
        detail = f"缺少 {relative}"
        if path.is_file():
            try:
                load_json(path)
                ok = True
                detail = relative
            except json.JSONDecodeError as exc:
                detail = f"JSON 解析失败 {relative}: {exc}"
        add(label, ok, detail)
    affix_db = package / "data/affix-db/v1/normalized/affix-db.sqlite"
    if affix_db.is_file():
        try:
            import sqlite3

            with sqlite3.connect(f"file:{affix_db}?mode=ro", uri=True) as con:
                count = con.execute("select count(*) from sqlite_master").fetchone()[0]
            add("运行数据 · affix DB 可读", count > 0, f"{count} 个 schema 对象")
        except sqlite3.Error as exc:
            add("运行数据 · affix DB 可读", False, str(exc))

    # --- forbidden content ----------------------------------------------
    forbidden_hits: list[str] = []
    for relative_posix in sorted(actual_files):
        if relative_posix in FORBIDDEN_FILES:
            forbidden_hits.append(relative_posix)
            continue
        if any(relative_posix.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            forbidden_hits.append(relative_posix)
            continue
        if relative_posix.startswith("examples/") and not any(
            relative_posix == entry or relative_posix.startswith(entry + "/")
            for entry in FORBIDDEN_EXAMPLE_ENTRIES
        ):
            forbidden_hits.append(relative_posix)
    add(
        "无案例/报告/构筑/缓存/外部工具",
        not forbidden_hits,
        "干净" if not forbidden_hits else f"{len(forbidden_hits)} 个：" + ", ".join(forbidden_hits[:8]),
    )

    # --- dataset database (optional, Release asset) ----------------------
    dataset_db = package / "data/releases/phase1-dataset-003/data.sqlite"
    if dataset_db.is_file():
        try:
            pointer = load_json(package / "data/current.json")
            expected_sha = str(pointer.get("file_sha256") or "")
            actual_sha = sha256_file(dataset_db)
            add(
                "dataset 数据库（本地存在时校验）",
                bool(expected_sha) and actual_sha == expected_sha,
                f"{actual_sha[:16]}… vs {expected_sha[:16]}…",
            )
        except (OSError, json.JSONDecodeError) as exc:
            add("dataset 数据库（本地存在时校验）", False, str(exc))
    else:
        add(
            "dataset 数据库（Release 附件）",
            True,
            "未随包分发（预期）；运行 scripts/fetch_dataset.ps1 获取",
            status="INFO",
        )

    # --- machine absolute paths / personal markers -----------------------
    portable_hits: list[str] = []
    provenance_hits: list[str] = []
    personal_hits: list[str] = []
    for path in iter_text_files(package, declared_paths):
        relative_posix = path.relative_to(package).as_posix()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for label in personal_markers_in(text):
            personal_hits.append(f"{relative_posix}（{label}）")
        matches = MACHINE_PATH_RE.findall(text)
        if not matches:
            continue
        sample = ", ".join(sorted(set(matches))[:3])
        if is_portable_surface(relative_posix):
            portable_hits.append(f"{relative_posix}（{sample}）")
        else:
            provenance_hits.append(f"{relative_posix}（{sample}）")
    add(
        "便携面 · 无机器绝对路径",
        not portable_hits,
        "干净" if not portable_hits else f"{len(portable_hits)} 个文件：" + "; ".join(portable_hits[:8]),
    )
    add(
        "无个人身份/本机路径标记",
        not personal_hits,
        "干净" if not personal_hits else f"{len(personal_hits)} 个文件：" + "; ".join(personal_hits[:8]),
    )
    if provenance_hits:
        status = "FAIL" if args.strict else "WARN"
        add(
            "冻结/来源文本 · 机器路径（只报告）",
            False,
            f"{len(provenance_hits)} 个文件保留来源路径；" + "; ".join(provenance_hits[:6]),
            status=status,
        )

    # --- JSON validity ----------------------------------------------------
    bad_json: list[str] = []
    for path in package.rglob("*.json"):
        try:
            load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            bad_json.append(f"{path.relative_to(package).as_posix()}: {exc}")
    add("JSON 合法", not bad_json, f"无效 {len(bad_json)}" + (f"：{', '.join(bad_json[:5])}" if bad_json else ""))

    # --- output ---------------------------------------------------------
    print("\n== 发布包校验 ==")
    print(f"目录：{package}")
    width = max(len(check["name"]) for check in checks)
    for check in checks:
        print(f"  {check['status']:<4}  {check['name']:<{width}}  {check['detail']}")
    failures = [check for check in checks if check["status"] == "FAIL"]
    warnings = [check for check in checks if check["status"] == "WARN"]
    print()
    print(f"文件数：{len(declared_paths)}；FAIL {len(failures)}；WARN {len(warnings)}")
    if failures:
        print("check_package：FAIL", file=sys.stderr)
        return 1
    if args.strict and warnings:
        print("check_package：FAIL（--strict：警告视为失败）", file=sys.stderr)
        return 1
    print("check_package：PASS（清单、哈希、必需数据、内容边界与便携路径检查通过）")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
