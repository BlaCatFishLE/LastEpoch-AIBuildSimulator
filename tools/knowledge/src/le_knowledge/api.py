# -*- coding: utf-8 -*-
"""独立可调用的知识查询接口（绕过编排/案例/聊天上下文）。

公开接口：

* ``search(query, *, knowledge_dir=None, index_path=None, root=None) -> dict``
* ``show(entry_id, *, ...) -> dict``
* ``rebuild(*, ...) -> dict``

索引与资产根通过显式参数（或 ``LE_REPO_ROOT`` 推导的仓库根）传入；查询结果只依赖
JSON 权威源 + 查询字典，不依赖当前案例或全局状态。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import index_md, loader, store
from .errors import KnowledgeIndexError, KnowledgeNotFoundError
from .paths import (
    DEFAULT_INDEX_PATH,
    ENTRY_DETAIL_SCHEMA,
    INDEX_FORMAT,
    Paths,
    QUERY_SCHEMA,
    RESULT_SCHEMA,
    digest_label,
    display_path,
    resolve_paths,
)
from .query import evaluate_applicability, execute_query, normalize_query, related_entries


# ---------------------------------------------------------------------------
# 索引状态
# ---------------------------------------------------------------------------


def index_state(
    *,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """只读检查索引是否需要重建（不写文件）。"""
    paths = resolve_paths(root=root, knowledge_dir=knowledge_dir, index_path=index_path)
    scan = loader.scan_source_files(paths.knowledge_dir)
    meta = store.read_meta(paths.index_path)
    if meta is None:
        return {
            "status": "missing" if not paths.index_path.exists() else "corrupt",
            "stale": True,
            "compatible": False,
            "source_digest": scan["digest"],
            "index_digest": None,
            "index_format": None,
            "index_path": paths.display(paths.index_path),
            "knowledge_dir": paths.display(paths.knowledge_dir),
            "counts": {},
        }
    stale = meta.get("source_digest") != scan["digest"]
    compatible = meta.get("index_format") == INDEX_FORMAT
    counts: dict[str, Any] = {}
    if compatible and paths.index_path.is_file():
        with store.IndexReader(paths.index_path, root=paths.root) as reader:
            counts = reader.counts
    return {
        "status": "ok" if compatible and not stale else ("stale" if compatible else "incompatible"),
        "stale": stale,
        "compatible": compatible,
        "source_digest": scan["digest"],
        "index_digest": meta.get("source_digest"),
        "index_format": meta.get("index_format"),
        "index_path": paths.display(paths.index_path),
        "knowledge_dir": paths.display(paths.knowledge_dir),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 重建
# ---------------------------------------------------------------------------


def _perform_rebuild(paths: Paths, *, use_fts: bool | None = None) -> dict[str, Any]:
    """从 JSON 权威源重建 sqlite + INDEX.generated.md，返回可序列化报告。"""
    corpus = loader.load_corpus(paths.knowledge_dir, root=paths.root)
    md_text = index_md.render_index_md(corpus)
    md_sha = index_md.text_sha256(md_text)

    database = store.build_database(
        corpus,
        paths.index_path,
        use_fts=use_fts,
        index_md_sha256=md_sha,
    )

    md_path = paths.index_md_path
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_changed = True
    md_overwritten = False
    md_had_marker = True
    if md_path.is_file():
        existing = md_path.read_text(encoding="utf-8")
        if existing == md_text:
            md_changed = False
        else:
            md_overwritten = True
            md_had_marker = index_md.GENERATED_MARKER in existing
    if md_changed:
        temporary = md_path.with_name(md_path.name + ".tmp")
        temporary.write_text(md_text, encoding="utf-8", newline="\n")
        temporary.replace(md_path)

    warnings = list(corpus.get("warnings") or [])
    if md_overwritten:
        if md_had_marker:
            warnings.append(
                {
                    "path": paths.display(md_path),
                    "message": "已存在内容与本次生成结果不同（可能是手工修改或旧内容），已覆盖；"
                    "该文件禁止手改",
                }
            )
        else:
            warnings.append(
                {
                    "path": paths.display(md_path),
                    "message": "已存在文件缺少 GENERATED 头（疑似手工创建/修改），已按生成结果覆盖",
                }
            )

    counts = dict(corpus.get("counts") or {})
    return {
        "ok": True,
        "index_format": INDEX_FORMAT,
        "source_digest": corpus["digest"],
        "index_digest": corpus["digest"],
        "knowledge_dir": paths.display(paths.knowledge_dir),
        "index": {
            "path": paths.display(paths.index_path),
            "sha256": database["sha256"],
            "rebuilt": True,
            "fts": bool(database["fts"]),
        },
        "index_md": {
            "path": paths.display(md_path),
            "changed": md_changed,
            "overwritten": md_overwritten,
            "sha256": md_sha,
        },
        "counts": counts,
        "tables": dict(database["counts"]),
        "warnings": warnings,
        "files": list(corpus.get("files") or []),
    }


def _ensure_index(
    paths: Paths, *, use_fts: bool | None = None, ensure_fresh: bool = True
) -> dict[str, Any]:
    """索引缺失/损坏/内容指纹变化时重建；返回 ``{"rebuilt", "stale_before"}``。"""
    state: dict[str, Any] = {"rebuilt": False, "stale_before": False, "report": None}
    if not ensure_fresh:
        if not paths.index_path.is_file():
            raise KnowledgeIndexError(
                "查询索引不存在："
                f"{paths.display(paths.index_path)}（请先运行 knowledge rebuild）"
            )
        store.read_meta(paths.index_path)
        return state
    scan = loader.scan_source_files(paths.knowledge_dir)
    meta = store.read_meta(paths.index_path)
    if (
        meta is not None
        and meta.get("index_format") == INDEX_FORMAT
        and meta.get("source_digest") == scan["digest"]
    ):
        return state
    if meta is not None or paths.index_path.exists():
        state["stale_before"] = True
    report = _perform_rebuild(paths, use_fts=use_fts)
    state.update(rebuilt=True, report=report)
    return state


def rebuild(
    *,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    index_md_path: str | Path | None = None,
    root: str | Path | None = None,
    use_fts: bool | None = None,
) -> dict[str, Any]:
    """强制从 JSON 权威源重建 SQLite 索引与 Markdown 索引。"""
    paths = resolve_paths(
        root=root,
        knowledge_dir=knowledge_dir,
        index_path=index_path,
        index_md_path=index_md_path,
    )
    return _perform_rebuild(paths, use_fts=use_fts)


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------


def _apply_index_ref(
    query: dict[str, Any],
    *,
    knowledge_dir: str | Path | None,
    index_path: str | Path | None,
    root: str | Path | None,
) -> tuple[str | Path | None, str | Path | None, str | Path | None]:
    """``query.index_ref`` 支持显式索引文件或知识资产根（仓库相对路径）。"""
    index_ref = query.get("index_ref")
    if not index_ref or knowledge_dir is not None or index_path is not None:
        return knowledge_dir, index_path, root
    paths = resolve_paths(root=root)
    candidate = Path(index_ref).expanduser()
    if not candidate.is_absolute():
        candidate = paths.root / candidate
    resolved = candidate.resolve()
    if resolved.is_dir():
        return resolved, None, root
    return None, resolved, root


def search(
    query: dict[str, Any] | str,
    *,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    root: str | Path | None = None,
    ensure_fresh: bool = True,
    use_fts: bool | None = None,
) -> dict[str, Any]:
    """执行机制/技能查询，返回 ``le-mechanism-result-v1`` 形状的字典。

    ``query`` 可以是 :class:`dict`（``le-mechanism-query-v1``）或字符串简写。
    """
    normalized = normalize_query(query)
    knowledge_dir, index_path, root = _apply_index_ref(
        {"index_ref": normalized.get("index_ref")},
        knowledge_dir=knowledge_dir,
        index_path=index_path,
        root=root,
    )
    paths = resolve_paths(
        root=root, knowledge_dir=knowledge_dir, index_path=index_path
    )
    state = _ensure_index(paths, use_fts=use_fts, ensure_fresh=ensure_fresh)
    with store.IndexReader(paths.index_path, root=paths.root) as reader:
        result = execute_query(normalized, reader)
        warnings = list(reader.warnings)
        index_meta = {
            "rebuilt": state["rebuilt"],
            "stale_before_query": state["stale_before"],
            "index_path": paths.display(paths.index_path),
            "index_format": reader.meta.get("index_format"),
            "fts": reader.fts_enabled,
            "counts": reader.counts,
            "source_digest": reader.source_digest,
        }
    if state["rebuilt"] and state["report"] is not None:
        warnings.append(
            {
                "path": state["report"]["index"]["path"],
                "message": "索引缺失或内容指纹变化，查询前已自动重建",
            }
        )
    result["index_state"] = index_meta
    result["warnings"] = _dedupe_warnings(warnings)
    result["query_schema_version"] = QUERY_SCHEMA
    result["schema_version"] = RESULT_SCHEMA
    return result


def show(
    entry_id: str,
    *,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    root: str | Path | None = None,
    ensure_fresh: bool = True,
    use_fts: bool | None = None,
) -> dict[str, Any]:
    """按 ID 返回条目详情（含完整权威 JSON 文档）；不存在时抛异常。"""
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise KnowledgeNotFoundError("")
    requested = entry_id.strip()
    for prefix in ("mechanism:", "skill:"):
        if requested.casefold().startswith(prefix):
            requested = requested[len(prefix):]
            break
    paths = resolve_paths(root=root, knowledge_dir=knowledge_dir, index_path=index_path)
    state = _ensure_index(paths, use_fts=use_fts, ensure_fresh=ensure_fresh)
    with store.IndexReader(paths.index_path, root=paths.root) as reader:
        resolved = reader.find_entry_id(requested) or reader.find_entry_id(entry_id.strip())
        if resolved is None:
            raise KnowledgeNotFoundError(entry_id.strip(), index_digest=reader.source_digest)
        entry = next(row for row in reader.entries() if row["entry_id"] == resolved)
        entry["selector_rows"] = [
            row for row in reader.selectors() if row["entry_id"] == resolved
        ]
        entry_aliases = [
            row["alias"]
            for row in reader.aliases()
            if row["entry_id"] == resolved and row.get("source") == "alias"
        ]
        applicability = evaluate_applicability(entry, {})
        detail: dict[str, Any] = {
            "schema_version": ENTRY_DETAIL_SCHEMA,
            "entry_id": resolved,
            "entry_kind": entry["entry_kind"],
            "mechanism_id": resolved,
            "revision": entry.get("revision") if entry.get("revision") is not None else 0,
            "title": {
                key: value
                for key, value in (entry.get("title") or {}).items()
                if value
            },
            "source": {
                "path": entry.get("source_path") or "",
                "sha256": entry.get("source_sha256") or "",
            },
            "index_digest": reader.source_digest,
            "states": {
                "evidence_state": entry.get("evidence_state"),
                "implementation_state": entry.get("implementation_state"),
            },
            "selectors": [
                {
                    "selector": row["selector"],
                    "kind": row.get("kind"),
                    "namespace": row.get("namespace"),
                }
                for row in entry["selector_rows"]
            ],
            "aliases": entry_aliases,
            "components": entry.get("components") or [],
            "tags": entry.get("tags") or [],
            "conditions": entry.get("conditions") or [],
            "required_nodes": entry.get("required_nodes") or [],
            "applicability": applicability,
            "rule_refs": entry.get("rule_refs") or [],
            "evidence_refs": entry.get("evidence_refs") or [],
            "implementation_refs": entry.get("implementation_refs") or [],
            "unresolved": entry.get("unresolved") or [],
            "case_scope": entry.get("case_scope") or [],
        }
        if entry["entry_kind"] == "skill_template":
            detail["skill_id"] = resolved
        document = reader.entry_document(resolved)
        if document is not None:
            detail["document"] = document
            if entry["entry_kind"] == "skill_template" and document.get("ability_id"):
                detail["ability_id"] = document["ability_id"]
        detail.update(related_entries(reader, entry))
        warnings = list(reader.warnings)
        index_meta = {
            "rebuilt": state["rebuilt"],
            "stale_before_query": state["stale_before"],
            "index_path": paths.display(paths.index_path),
            "index_format": reader.meta.get("index_format"),
            "fts": reader.fts_enabled,
            "counts": reader.counts,
            "source_digest": reader.source_digest,
        }
    if state["rebuilt"] and state["report"] is not None:
        warnings.append(
            {
                "path": state["report"]["index"]["path"],
                "message": "索引缺失或内容指纹变化，查询前已自动重建",
            }
        )
    detail["warnings"] = _dedupe_warnings(warnings)
    detail["index_state"] = index_meta
    return detail


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _dedupe_warnings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = (str(item.get("path") or ""), str(item.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(item))
    return result


def list_entries(
    *,
    knowledge_dir: str | Path | None = None,
    index_path: str | Path | None = None,
    root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """列出索引中的条目（调试/验收用；短摘要）。"""
    paths = resolve_paths(root=root, knowledge_dir=knowledge_dir, index_path=index_path)
    with store.IndexReader(paths.index_path, root=paths.root) as reader:
        return [
            {
                "entry_id": row["entry_id"],
                "entry_kind": row["entry_kind"],
                "revision": row.get("revision") or 0,
                "title": row.get("title"),
                "evidence_state": row.get("evidence_state"),
                "implementation_state": row.get("implementation_state"),
            }
            for row in reader.entries()
        ]


__all__ = [
    "DEFAULT_INDEX_PATH",
    "digest_label",
    "display_path",
    "index_state",
    "list_entries",
    "rebuild",
    "search",
    "show",
]
