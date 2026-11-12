# -*- coding: utf-8 -*-
"""路径与契约常量。

所有默认路径都相对**仓库根**解析，不依赖当前工作目录，也不写入机器绝对路径；
索引路径与知识资产根都可以由调用方显式传入（``knowledge_dir`` / ``index_path`` /
``root``），因此查询接口可以绕过编排独立调用。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: 索引格式版本；重建逻辑变化时递增，旧索引会因指纹不匹配而被重建。
INDEX_FORMAT = "le-knowledge-index-v1"

#: 查询/结果契约版本（见 docs/contracts/workflow-interface.md §5.1）。
QUERY_SCHEMA = "le-mechanism-query-v1"
RESULT_SCHEMA = "le-mechanism-result-v1"
ENTRY_DETAIL_SCHEMA = "le-knowledge-entry-v1"

#: 包版本（写入索引 meta，仅用于追溯，不参与内容指纹）。
PACKAGE_VERSION = "0.1.0"

DEFAULT_KNOWLEDGE_DIR = "knowledge"
DEFAULT_INDEX_PATH = "data/cache/knowledge.sqlite"
DEFAULT_INDEX_MD = "INDEX.generated.md"

#: 条目选择器允许的命名空间（``knowledge/README.md`` 规则 3 + schema 的 selector kind）。
KNOWN_NAMESPACES = (
    "ability",
    "node",
    "item",
    "stat",
    "blessing",
    "idol",
    "unique",
    "condition",
    "mechanism",
    "skill",
)

#: 生成物/输出中替代机器绝对路径的占位符（沿用仓库可移植性约定）。
EXTERNAL_PATH_PLACEHOLDER = "<external-path>"


def find_repo_root(start: str | Path | None = None) -> Path | None:
    """从 ``LE_REPO_ROOT`` 或给定位置向上寻找仓库根；找不到返回 None。"""
    env = os.environ.get("LE_REPO_ROOT")
    if env:
        candidate = Path(env).expanduser().resolve()
        return candidate
    here = Path(start if start is not None else __file__).resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "config" / "project.json").is_file() and (
            candidate / "data" / "current.json"
        ).is_file():
            return candidate
    return None


def default_root() -> Path:
    """默认仓库根：优先环境变量/特征文件，其次由包位置推断。"""
    root = find_repo_root()
    if root is not None:
        return root
    # <root>/tools/knowledge/src/le_knowledge/paths.py -> parents[4] == <root>
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True)
class Paths:
    """一次查询/重建使用的显式路径集合（绝对路径只存在于进程内）。"""

    root: Path
    knowledge_dir: Path
    index_path: Path
    index_md_path: Path

    def display(self, path: str | Path) -> str:
        return display_path(path, self.root)


def _absolute(base: Path, value: str | Path | None, default: str | None) -> Path | None:
    if value is None:
        if default is None:
            return None
        value = default
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def resolve_paths(
    *,
    root: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    index_md_path: str | Path | None = None,
) -> Paths:
    """解析一次操作使用的四个路径；缺省值相对仓库根。"""
    base = _absolute(Path.cwd(), root, None) if root is not None else default_root()
    assert base is not None
    base = Path(base).resolve()
    resolved_knowledge = _absolute(base, knowledge_dir, DEFAULT_KNOWLEDGE_DIR)
    assert resolved_knowledge is not None
    resolved_index = _absolute(base, index_path, DEFAULT_INDEX_PATH)
    assert resolved_index is not None
    if index_md_path is not None:
        resolved_md = _absolute(base, index_md_path, None)
    else:
        # Markdown 索引默认与知识资产放在一起（临时 fixture 也能自包含）。
        resolved_md = resolved_knowledge / DEFAULT_INDEX_MD
    assert resolved_md is not None
    return Paths(
        root=base,
        knowledge_dir=resolved_knowledge,
        index_path=resolved_index,
        index_md_path=resolved_md,
    )


def display_path(path: str | Path, root: str | Path) -> str:
    """仓库根相对 POSIX 路径；根外路径用 ``<external-path>`` 占位（不泄漏绝对路径）。"""
    try:
        resolved = Path(path).resolve()
        relative = resolved.relative_to(Path(root).resolve())
        text = relative.as_posix()
        return text if text else "."
    except (ValueError, OSError):
        return EXTERNAL_PATH_PLACEHOLDER


def digest_label(hexdigest: str) -> str:
    """把裸 sha256 十六进制串格式化为 ``sha256:<hex>``。"""
    value = str(hexdigest or "")
    if value.startswith("sha256:"):
        return value
    return f"sha256:{value}"
