# -*- coding: utf-8 -*-
"""可重建的 SQLite 查询索引（``data/cache/knowledge.sqlite``）。

索引是**生成物**，不作为知识源：删除/损坏后由 ``rebuild`` 从 JSON 权威源恢复。
表结构：

* ``meta``：索引格式、内容指纹（``source_digest``）、计数、FTS 状态、告警；
* ``files``：参与指纹的内容文件与 sha256；
* ``entries``：归一化条目（含完整 ``document_json``，供 ``show`` 使用）；
* ``selectors``：精确匹配入口（``namespace:id``）；
* ``aliases``：维护的别名与中英文标题（精确名称查询）；
* ``search_terms``：关键词查询词表；
* ``mappings``：技能模板 → 机制条目的映射引用；
* ``terms_fts``（可选）：SQLite 支持 FTS5 时的全文索引；不支持时自动回退 LIKE。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .errors import KnowledgeIndexError
from .loader import sha256_file
from .paths import (
    INDEX_FORMAT,
    PACKAGE_VERSION,
    display_path,
)

_DDL = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE files (path TEXT PRIMARY KEY, kind TEXT NOT NULL, sha256 TEXT NOT NULL)",
    """
    CREATE TABLE entries (
        entry_id TEXT PRIMARY KEY,
        entry_kind TEXT NOT NULL,
        revision INTEGER,
        title_zh TEXT NOT NULL,
        title_en TEXT,
        evidence_state TEXT,
        implementation_state TEXT,
        components_json TEXT NOT NULL,
        tags_json TEXT NOT NULL,
        conditions_json TEXT NOT NULL,
        required_nodes_json TEXT NOT NULL,
        namespaces_json TEXT NOT NULL,
        rule_refs_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL,
        implementation_refs_json TEXT NOT NULL,
        unresolved_json TEXT NOT NULL,
        case_scope_json TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_sha256 TEXT NOT NULL,
        document_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE selectors (
        entry_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        selector TEXT NOT NULL,
        selector_norm TEXT NOT NULL,
        kind TEXT NOT NULL,
        namespace TEXT NOT NULL,
        PRIMARY KEY (entry_id, ordinal)
    )
    """,
    "CREATE INDEX idx_selectors_norm ON selectors (selector_norm)",
    """
    CREATE TABLE aliases (
        entry_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        alias TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (entry_id, ordinal)
    )
    """,
    "CREATE INDEX idx_aliases_norm ON aliases (alias_norm)",
    """
    CREATE TABLE search_terms (
        entry_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        term TEXT NOT NULL,
        term_norm TEXT NOT NULL,
        source TEXT NOT NULL,
        PRIMARY KEY (entry_id, ordinal)
    )
    """,
    "CREATE INDEX idx_terms_norm ON search_terms (term_norm)",
    """
    CREATE TABLE mappings (
        entry_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        mechanism_id TEXT NOT NULL,
        selector TEXT NOT NULL,
        PRIMARY KEY (entry_id, ordinal)
    )
    """,
    "CREATE INDEX idx_mappings_mechanism ON mappings (mechanism_id)",
)

_FTS_DDL = (
    "CREATE VIRTUAL TABLE terms_fts USING fts5("
    "term_norm, entry_id UNINDEXED, source UNINDEXED, "
    "tokenize='unicode61 remove_diacritics 2')"
)

_JSON_COLUMNS = {
    "components_json": "components",
    "tags_json": "tags",
    "conditions_json": "conditions",
    "required_nodes_json": "required_nodes",
    "namespaces_json": "namespaces",
    "rule_refs_json": "rule_refs",
    "evidence_refs_json": "evidence_refs",
    "implementation_refs_json": "implementation_refs",
    "unresolved_json": "unresolved",
    "case_scope_json": "case_scope",
}


# ---------------------------------------------------------------------------
# 查询词表
# ---------------------------------------------------------------------------


def _collect_terms(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """从条目派生关键词查询词表；返回去重后的 ``(term, source)`` 列表。"""
    ordered: dict[str, str] = {}

    def add(source: str, value: Any) -> None:
        if value is None or isinstance(value, (dict, list, tuple, set)):
            return
        text = str(value).strip()
        if not text:
            return
        norm = text.casefold()
        if norm not in ordered:
            ordered[norm] = source

    add("entry_id", entry["entry_id"])
    title = entry.get("title") or {}
    add("title_zh", title.get("zh"))
    add("title_en", title.get("en"))
    for alias in entry.get("aliases") or []:
        add("alias", alias)
    for selector in entry.get("selectors") or []:
        value = str(selector.get("selector") or "")
        add("selector", value)
        if ":" in value:
            body = value.split(":", 1)[1]
            add("selector_body", body)
            if ":" in body:
                add("selector_body_head", body.split(":", 1)[0])
    for source in ("components", "tags", "conditions", "required_nodes"):
        for value in entry.get(source) or []:
            add(source[:-1] if source.endswith("s") else source, value)
    for rule in entry.get("rule_refs") or []:
        for key in ("kind", "ref", "path", "locator"):
            add("rule_ref", rule.get(key))
    for reference in entry.get("evidence_refs") or []:
        for key in ("kind", "locator"):
            add("evidence_ref", reference.get(key))
    for implementation in entry.get("implementation_refs") or []:
        for key in ("module", "symbol", "block"):
            add("implementation_ref", implementation.get(key))
    for value in entry.get("unresolved") or []:
        add("unresolved", value)
    for value in entry.get("case_scope") or []:
        add("case_scope", value)
    add("evidence_state", entry.get("evidence_state"))
    add("implementation_state", entry.get("implementation_state"))
    add("entry_kind", entry.get("entry_kind"))

    document = entry.get("document") or {}
    add("model_id", document.get("model_id"))
    add("class", document.get("class"))
    add("mastery", document.get("mastery"))
    add("ability_id", document.get("ability_id"))
    add("rule_schema", document.get("schema"))
    for value in document.get("damage_tags") or []:
        add("damage_tag", value)
    for component in document.get("components") or []:
        if not isinstance(component, dict):
            continue
        for key in ("component_id", "damage_type", "kind", "notes"):
            add("component", component.get(key))
        for tag in component.get("tags") or []:
            add("component_tag", tag)
        for value in component.get("added_sources") or []:
            add("added_source", value)
    for block in document.get("calculation_blocks") or []:
        if not isinstance(block, dict):
            continue
        for key in ("block", "module", "symbol", "note"):
            add("calculation_block", block.get(key))
    for path in document.get("parameter_paths") or []:
        if not isinstance(path, dict):
            continue
        for key in ("parameter", "from", "unit"):
            add("parameter_path", path.get(key))
    schedule = document.get("schedule") or {}
    if isinstance(schedule, dict):
        add("schedule_driver", schedule.get("driver"))
        for value in schedule.get("windows_s") or []:
            add("schedule_window", value)
    for value in document.get("supported_interactions") or []:
        add("supported_interaction", value)
    for item in document.get("unsupported") or []:
        if isinstance(item, dict):
            add("unsupported", item.get("item"))
            add("unsupported", item.get("reason"))
    for assumption in document.get("assumptions") or []:
        if isinstance(assumption, dict):
            add("assumption", assumption.get("assumption_id"))
            add("assumption", assumption.get("status"))
    capability = document.get("capability_ref") or {}
    if isinstance(capability, dict):
        add("capability", capability.get("model_id"))
        add("capability", capability.get("path"))
        for key in ("pages", "target_fields", "buff_controls"):
            for value in capability.get(key) or []:
                add("capability", value)
    for binding in document.get("parameter_bindings") or []:
        if isinstance(binding, dict):
            add("parameter_binding", binding.get("parameter"))
            add("parameter_binding", binding.get("source"))
    aggregation = document.get("aggregation") or {}
    if isinstance(aggregation, dict):
        for key in ("operation", "more_group", "stacking_policy", "order_policy"):
            add("aggregation", aggregation.get(key))
    for requirement in document.get("interaction_requirements") or []:
        if isinstance(requirement, dict):
            add("interaction_requirement", requirement.get("with"))
            add("interaction_requirement", requirement.get("requirement"))
    notes = document.get("research_notes") or {}
    if isinstance(notes, dict):
        for key in ("checked_sources", "open_questions", "requery_when"):
            for value in notes.get(key) or []:
                add("research_note", value)
    compatibility = document.get("compatibility_baseline") or {}
    if isinstance(compatibility, dict):
        add("compatibility_baseline", compatibility.get("baseline_id"))
        add("compatibility_baseline", compatibility.get("game_version"))

    return [(term, source) for term, source in ordered.items()]


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------


def fts5_supported() -> bool:
    """检测当前 Python/SQLite 是否编译了 FTS5（不写任何文件）。"""
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        finally:
            connection.close()
        return True
    except sqlite3.Error:
        return False


def _fts_query(term: str) -> str:
    """把用户词转换为 FTS5 短语查询，避免特殊字符造成语法错误。"""
    escaped = str(term).replace('"', '""')
    return f'"{escaped}"'


def _write_meta(connection: sqlite3.Connection, meta: dict[str, Any]) -> None:
    rows = [(str(key), json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value)
            for key, value in meta.items()]
    connection.executemany("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", rows)


def build_database(
    corpus: dict[str, Any],
    index_path: str | Path,
    *,
    use_fts: bool | None = None,
    index_md_sha256: str | None = None,
) -> dict[str, Any]:
    """从 corpus 原子重建 SQLite 索引，返回 ``{"path", "fts", "sha256", "counts"}``。"""
    target = Path(index_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    if temporary.exists():
        temporary.unlink()
    fts_available = fts5_supported()
    use_fts = fts_available if use_fts is None else (bool(use_fts) and fts_available)

    counts = {
        "entries": 0,
        "selectors": 0,
        "aliases": 0,
        "terms": 0,
        "mappings": 0,
    }
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(str(temporary))
        for statement in _DDL:
            connection.execute(statement)
        fts_enabled = False
        if use_fts:
            try:
                connection.execute(_FTS_DDL)
                fts_enabled = True
            except sqlite3.OperationalError:
                fts_enabled = False

        for statement in corpus.get("files") or []:
            connection.execute(
                "INSERT OR REPLACE INTO files (path, kind, sha256) VALUES (?, ?, ?)",
                (statement["path"], statement["kind"], statement["sha256"]),
            )

        fts_rows: list[tuple[str, str, str]] = []
        for entry in corpus["entries"]:
            entry_id = entry["entry_id"]
            connection.execute(
                """
                INSERT INTO entries (
                    entry_id, entry_kind, revision, title_zh, title_en,
                    evidence_state, implementation_state,
                    components_json, tags_json, conditions_json, required_nodes_json,
                    namespaces_json, rule_refs_json, evidence_refs_json,
                    implementation_refs_json, unresolved_json, case_scope_json,
                    source_path, source_sha256, document_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry_id,
                    entry["entry_kind"],
                    entry.get("revision"),
                    (entry.get("title") or {}).get("zh") or "",
                    (entry.get("title") or {}).get("en"),
                    entry.get("evidence_state"),
                    entry.get("implementation_state"),
                    json.dumps(entry.get("components") or [], ensure_ascii=False),
                    json.dumps(entry.get("tags") or [], ensure_ascii=False),
                    json.dumps(entry.get("conditions") or [], ensure_ascii=False),
                    json.dumps(entry.get("required_nodes") or [], ensure_ascii=False),
                    json.dumps(entry.get("namespaces") or [], ensure_ascii=False),
                    json.dumps(entry.get("rule_refs") or [], ensure_ascii=False),
                    json.dumps(entry.get("evidence_refs") or [], ensure_ascii=False),
                    json.dumps(entry.get("implementation_refs") or [], ensure_ascii=False),
                    json.dumps(entry.get("unresolved") or [], ensure_ascii=False),
                    json.dumps(entry.get("case_scope") or [], ensure_ascii=False),
                    entry.get("source_path") or "",
                    entry.get("source_sha256") or "",
                    json.dumps(entry.get("document") or {}, ensure_ascii=False, sort_keys=False),
                ),
            )
            counts["entries"] += 1

            for ordinal, selector in enumerate(entry.get("selectors") or []):
                connection.execute(
                    """
                    INSERT INTO selectors (entry_id, ordinal, selector, selector_norm, kind, namespace)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        ordinal,
                        selector.get("selector") or "",
                        str(selector.get("selector") or "").casefold(),
                        selector.get("kind") or "",
                        selector.get("namespace") or "",
                    ),
                )
                counts["selectors"] += 1

            alias_rows: list[tuple[str, str]] = [
                (str(alias), "alias") for alias in entry.get("aliases") or []
            ]
            title = entry.get("title") or {}
            if title.get("zh"):
                alias_rows.append((str(title["zh"]), "title_zh"))
            if title.get("en"):
                alias_rows.append((str(title["en"]), "title_en"))
            for ordinal, (alias, source) in enumerate(alias_rows):
                connection.execute(
                    """
                    INSERT INTO aliases (entry_id, ordinal, alias, alias_norm, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entry_id, ordinal, alias, alias.casefold(), source),
                )
                counts["aliases"] += 1

            for ordinal, (term, source) in enumerate(_collect_terms(entry)):
                term_norm = term.casefold()
                connection.execute(
                    """
                    INSERT INTO search_terms (entry_id, ordinal, term, term_norm, source)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (entry_id, ordinal, term, term_norm, source),
                )
                counts["terms"] += 1
                if fts_enabled:
                    fts_rows.append((term_norm, entry_id, source))

            for ordinal, mapping in enumerate(entry.get("mappings") or []):
                connection.execute(
                    """
                    INSERT INTO mappings (entry_id, ordinal, mechanism_id, selector)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        ordinal,
                        mapping.get("mechanism_id") or "",
                        mapping.get("selector") or "",
                    ),
                )
                counts["mappings"] += 1

        if fts_enabled and fts_rows:
            connection.executemany(
                "INSERT INTO terms_fts (term_norm, entry_id, source) VALUES (?, ?, ?)", fts_rows
            )

        warnings_json = json.dumps(corpus.get("warnings") or [], ensure_ascii=False)
        meta = {
            "index_format": INDEX_FORMAT,
            "index_format_note": INDEX_FORMAT,
            "source_digest": corpus["digest"],
            "generated_by": f"le_knowledge/{PACKAGE_VERSION}",
            "fts": "1" if fts_enabled else "0",
            "warnings_json": warnings_json,
            "counts_json": json.dumps(counts, ensure_ascii=False),
            "index_md_sha256": index_md_sha256 or "",
        }
        for key in ("mechanisms", "skill_templates", "entries", "selectors"):
            value = (corpus.get("counts") or {}).get(key)
            if value is not None:
                meta[f"count_{key}"] = str(value)
        _write_meta(connection, meta)
        connection.commit()
        connection.close()
        connection = None
        os.replace(temporary, target)
    except Exception:
        if connection is not None:
            try:
                connection.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:  # pragma: no cover - best effort cleanup
                pass
        raise

    return {
        "path": target,
        "fts": fts_enabled,
        "sha256": sha256_file(target),
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------


def read_meta(index_path: str | Path) -> dict[str, str] | None:
    """读取索引 meta；索引缺失/损坏/无 meta 时返回 None（调用方据此重建）。"""
    path = Path(index_path)
    if not path.is_file():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = connection.execute("SELECT key, value FROM meta").fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return {str(key): str(value) for key, value in rows}


def _decode_json_columns(row: sqlite3.Row) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in row.keys():
        if key in _JSON_COLUMNS:
            try:
                result[_JSON_COLUMNS[key]] = json.loads(row[key] or "[]")
            except json.JSONDecodeError:  # pragma: no cover - 索引由本模块生成
                result[_JSON_COLUMNS[key]] = []
        else:
            result[key] = row[key]
    if "title_zh" in result or "title_en" in result:
        result["title"] = {"zh": result.get("title_zh") or "", "en": result.get("title_en")}
    return result


class IndexReader:
    """只读索引访问器（短生命周期；``with`` 语句自动关闭连接）。"""

    def __init__(self, index_path: str | Path, *, root: str | Path | None = None) -> None:
        self.path = Path(index_path)
        self.root = Path(root) if root is not None else self.path.parent
        if not self.path.is_file():
            raise KnowledgeIndexError(
                f"查询索引不存在：{display_path(self.path, self.root)}（请先运行 knowledge rebuild）"
            )
        try:
            self.connection = sqlite3.connect(f"file:{self.path.as_posix()}?mode=ro", uri=True)
            self.connection.row_factory = sqlite3.Row
            self.meta = {
                str(row["key"]): str(row["value"])
                for row in self.connection.execute("SELECT key, value FROM meta")
            }
        except sqlite3.Error as exc:
            raise KnowledgeIndexError(f"查询索引不可读（{display_path(self.path, self.root)}）：{exc}") from exc
        if self.meta.get("index_format") != INDEX_FORMAT:
            raise KnowledgeIndexError(
                "查询索引格式不兼容："
                f"{self.meta.get('index_format') or '未知'}（需要 {INDEX_FORMAT}，请运行 knowledge rebuild）"
            )

    # -- 元信息 ---------------------------------------------------------
    @property
    def source_digest(self) -> str:
        return self.meta.get("source_digest", "")

    @property
    def fts_enabled(self) -> bool:
        return self.meta.get("fts") == "1"

    @property
    def warnings(self) -> list[dict[str, Any]]:
        try:
            value = json.loads(self.meta.get("warnings_json") or "[]")
        except json.JSONDecodeError:  # pragma: no cover - 索引由本模块生成
            return []
        return value if isinstance(value, list) else []

    @property
    def counts(self) -> dict[str, Any]:
        try:
            value = json.loads(self.meta.get("counts_json") or "{}")
        except json.JSONDecodeError:  # pragma: no cover - 索引由本模块生成
            return {}
        return value if isinstance(value, dict) else {}

    # -- 条目 -----------------------------------------------------------
    def entries(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM entries ORDER BY entry_kind, entry_id"
        ).fetchall()
        return [_decode_json_columns(row) for row in rows]

    def selectors(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT entry_id, ordinal, selector, selector_norm, kind, namespace "
            "FROM selectors ORDER BY entry_id, ordinal"
        ).fetchall()
        return [dict(row) for row in rows]

    def aliases(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT entry_id, ordinal, alias, alias_norm, source FROM aliases "
            "ORDER BY entry_id, ordinal"
        ).fetchall()
        return [dict(row) for row in rows]

    def mappings(self) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT entry_id, ordinal, mechanism_id, selector FROM mappings "
            "ORDER BY entry_id, ordinal"
        ).fetchall()
        return [dict(row) for row in rows]

    def entry_document(self, entry_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT document_json FROM entries WHERE entry_id = ?", (entry_id,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["document_json"])
        except json.JSONDecodeError:  # pragma: no cover - 索引由本模块生成
            return None

    def find_entry_id(self, entry_id: str) -> str | None:
        """大小写不敏感地解析条目 ID（返回索引中保存的原始写法）。"""
        row = self.connection.execute(
            "SELECT entry_id FROM entries WHERE lower(entry_id) = lower(?) LIMIT 1", (entry_id,)
        ).fetchone()
        return str(row["entry_id"]) if row is not None else None

    # -- 关键词查询 ------------------------------------------------------
    def terms_exact(self, term_norm: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT DISTINCT entry_id, term, source FROM search_terms "
            "WHERE term_norm = ? ORDER BY entry_id",
            (term_norm,),
        ).fetchall()
        return [dict(row) for row in rows]

    def terms_like(self, term_norm: str) -> list[dict[str, Any]]:
        escaped = term_norm.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self.connection.execute(
            "SELECT DISTINCT entry_id, term, source FROM search_terms "
            "WHERE term_norm LIKE ? ESCAPE '\\' ORDER BY entry_id",
            (f"%{escaped}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def terms_fts(self, term: str) -> list[dict[str, Any]]:
        """FTS5 查询；表缺失/不可用时抛 :class:`sqlite3.OperationalError` 由调用方回退。"""
        rows = self.connection.execute(
            "SELECT DISTINCT entry_id, term_norm FROM terms_fts WHERE terms_fts MATCH ? ORDER BY entry_id",
            (_fts_query(term),),
        ).fetchall()
        return [dict(row) for row in rows]

    def mappings_for_mechanism(self, mechanism_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT entry_id, ordinal, mechanism_id, selector FROM mappings "
            "WHERE mechanism_id = ? ORDER BY entry_id, ordinal",
            (mechanism_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def __enter__(self) -> "IndexReader":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def close(self) -> None:
        try:
            self.connection.close()
        except sqlite3.Error:  # pragma: no cover - best effort cleanup
            pass
