# -*- coding: utf-8 -*-
"""查询语义（workflow-interface §5.1 的实现）。

查询次序（实施方案 §5）：

1. 命名空间精确 ID（``mechanism:<id>`` / ``node:<skill>:<node>`` / …）；
2. 组件、标签、所选节点与条件过滤；
3. 别名 / stat_key / 计算块 / 关键词（FTS5 可用则用，否则回退 LIKE；中文走维护的别名）；
4. 返回规则引用、证据定位、实现入口与未解决项；
5. 无精确匹配时给 ``candidate``，不把近似匹配当成已支持。

逐条匹配字段路径：``entries[].matched_queries[]`` 的
``query_id`` / ``selector`` / ``match_quality`` / ``match_reason``；
适用性在 ``entries[].applicability.{status,missing_conditions,evaluated_on}``；
未命中查询在 ``unmatched_queries[]``；``summary.match_quality`` 只作摘要。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Iterable

from .errors import KnowledgeQueryError
from .paths import KNOWN_NAMESPACES, QUERY_SCHEMA
from .store import IndexReader

QUERY_KINDS = ("id", "alias", "keyword", "stat_key", "node_id", "ability_id", "item_id")

#: kind → 允许的 selector 命名空间
KIND_NAMESPACES: dict[str, tuple[str, ...]] = {
    "node_id": ("node",),
    "ability_id": ("ability",),
    "item_id": ("item", "unique"),
    "stat_key": ("stat",),
}

FILTER_KEYS = ("component", "tags", "node_ids", "conditions")

DEFAULT_LIMIT = 50
MAX_LIMIT = 500

_NAMESPACED_RE = re.compile(r"^(?P<namespace>[a-z]+):(?P<rest>.+)$")
#: 关键词子串匹配的最小长度（避免单字符噪声）。
_MIN_SUBSTRING = 2
#: selector 前缀候选的最小长度（含命名空间）。
_MIN_SELECTOR_PREFIX = 5


# ---------------------------------------------------------------------------
# 查询归一化
# ---------------------------------------------------------------------------


def _as_string_list(value: Any, *, pointer: str, allow_string: bool = True) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if not allow_string:
            raise KnowledgeQueryError("应为字符串数组", pointer=pointer)
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        raise KnowledgeQueryError("应为字符串数组", pointer=pointer)
    result: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise KnowledgeQueryError("数组项必须是非空字符串", pointer=pointer)
        result.append(item.strip())
    return result


def _infer_kind(term: str) -> str:
    """CLI 未显式指定 kind 时按词形推断（带命名空间 → 对应 kind，否则关键词）。"""
    match = _NAMESPACED_RE.match(term.strip())
    if not match:
        return "keyword"
    namespace = match.group("namespace")
    for kind, namespaces in KIND_NAMESPACES.items():
        if namespace in namespaces:
            return kind
    return "id"


def normalize_query(query: Any) -> dict[str, Any]:
    """把调用方输入归一化为 ``le-mechanism-query-v1`` 形状。"""
    if isinstance(query, str):
        query = {"queries": [{"term": query}]}
    if not isinstance(query, dict):
        raise KnowledgeQueryError("查询必须是 JSON 对象（或字符串简写）")

    schema_version = query.get("schema_version", QUERY_SCHEMA)
    if schema_version != QUERY_SCHEMA:
        raise KnowledgeQueryError(
            f"不支持的查询契约版本：{schema_version!r}（需要 {QUERY_SCHEMA}）",
            pointer="$.schema_version",
        )

    raw_queries = query.get("queries")
    if raw_queries is None and ("term" in query or "query" in query):
        single = {
            "term": query.get("term", query.get("query")),
            "kind": query.get("kind"),
            "namespace": query.get("namespace"),
            "query_id": query.get("query_id"),
        }
        raw_queries = [single]
    if raw_queries is None:
        raw_queries = []
    if not isinstance(raw_queries, list):
        raise KnowledgeQueryError("queries 必须是数组", pointer="$.queries")

    queries: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_queries):
        pointer = f"$.queries[{index}]"
        if isinstance(item, str):
            item = {"term": item}
        if not isinstance(item, dict):
            raise KnowledgeQueryError("查询项必须是 JSON 对象或字符串", pointer=pointer)
        term = item.get("term")
        if not isinstance(term, str) or not term.strip():
            raise KnowledgeQueryError('缺少非空字符串字段 "term"', pointer=f"{pointer}.term")
        kind = item.get("kind") or "keyword"
        if kind == "auto":
            kind = _infer_kind(term)
        if kind not in QUERY_KINDS:
            raise KnowledgeQueryError(
                f"未知 kind：{kind!r}（可选：{', '.join(QUERY_KINDS)}）",
                pointer=f"{pointer}.kind",
            )
        namespace = item.get("namespace")
        if namespace is not None:
            if not isinstance(namespace, str) or namespace not in KNOWN_NAMESPACES:
                raise KnowledgeQueryError(
                    f"未知 namespace：{namespace!r}（可选：{', '.join(KNOWN_NAMESPACES)}）",
                    pointer=f"{pointer}.namespace",
                )
        query_id = item.get("query_id")
        if query_id is None:
            query_id = f"q{index + 1}"
        if not isinstance(query_id, str) or not query_id.strip():
            raise KnowledgeQueryError("query_id 必须是非空字符串", pointer=f"{pointer}.query_id")
        if query_id in seen_ids:
            raise KnowledgeQueryError(f"query_id 重复：{query_id}", pointer=f"{pointer}.query_id")
        seen_ids.add(query_id)
        queries.append(
            {
                "query_id": query_id,
                "term": term.strip(),
                "kind": kind,
                "namespace": namespace,
            }
        )

    raw_filters = query.get("filters") or {}
    if not isinstance(raw_filters, dict):
        raise KnowledgeQueryError("filters 必须是 JSON 对象", pointer="$.filters")
    unknown_filters = sorted(set(raw_filters) - set(FILTER_KEYS))
    if unknown_filters:
        raise KnowledgeQueryError(
            f"未知过滤键：{', '.join(unknown_filters)}（可选：{', '.join(FILTER_KEYS)}）",
            pointer="$.filters",
        )
    filters: dict[str, list[str]] = {
        "component": _as_string_list(raw_filters.get("component"), pointer="$.filters.component"),
        "tags": _as_string_list(raw_filters.get("tags"), pointer="$.filters.tags"),
        "node_ids": _as_string_list(raw_filters.get("node_ids"), pointer="$.filters.node_ids"),
        "conditions": _as_string_list(
            raw_filters.get("conditions"), pointer="$.filters.conditions"
        ),
    }
    for index, node_id in enumerate(filters["node_ids"]):
        if ":" not in node_id:
            raise KnowledgeQueryError(
                "节点 ID 必须带命名空间（如 node:fr11mv:16）；无命名空间的裸数字不作为可绑定 ID",
                pointer=f"$.filters.node_ids[{index}]",
            )

    namespaces = _as_string_list(query.get("namespaces"), pointer="$.namespaces")
    for index, namespace in enumerate(namespaces):
        if namespace not in KNOWN_NAMESPACES:
            raise KnowledgeQueryError(
                f"未知 namespace：{namespace!r}", pointer=f"$.namespaces[{index}]"
            )

    context = query.get("context") or {}
    if not isinstance(context, dict):
        raise KnowledgeQueryError("context 必须是 JSON 对象", pointer="$.context")
    unknown_context = sorted(set(context) - {"conditions", "nodes", "exhaustive"})
    if unknown_context:
        raise KnowledgeQueryError(
            f"未知 context 键：{', '.join(unknown_context)}", pointer="$.context"
        )
    normalized_context = {
        "conditions": _as_string_list(context.get("conditions"), pointer="$.context.conditions"),
        "nodes": _as_string_list(context.get("nodes"), pointer="$.context.nodes"),
        "exhaustive": bool(context.get("exhaustive", False)),
        "provided": bool(context),
    }
    for index, node_id in enumerate(normalized_context["nodes"]):
        if ":" not in node_id:
            raise KnowledgeQueryError(
                "context.nodes 必须带命名空间（如 node:fr11mv:16）",
                pointer=f"$.context.nodes[{index}]",
            )

    limit = query.get("limit")
    if limit is None:
        limit = DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise KnowledgeQueryError("limit 必须是正整数", pointer="$.limit")
    limit = min(limit, MAX_LIMIT)

    index_ref = query.get("index_ref")
    if index_ref is not None and (not isinstance(index_ref, str) or not index_ref.strip()):
        raise KnowledgeQueryError("index_ref 必须是非空字符串路径/资产 ID", pointer="$.index_ref")

    if not queries and not any(filters.values()) and not namespaces:
        raise KnowledgeQueryError("查询至少需要一条 queries[] 或一个 filters 条件", pointer="$")

    return {
        "schema_version": QUERY_SCHEMA,
        "queries": queries,
        "filters": filters,
        "namespaces": namespaces,
        "context": normalized_context,
        "limit": limit,
        "index_ref": index_ref.strip() if isinstance(index_ref, str) else None,
        "filter_only": not queries,
    }


# ---------------------------------------------------------------------------
# 匹配
# ---------------------------------------------------------------------------


def _match_record(query: dict[str, Any], selector: str, quality: str, reason: str) -> dict[str, Any]:
    return {
        "query_id": query["query_id"],
        "term": query["term"],
        "kind": query["kind"],
        "selector": selector,
        "match_quality": quality,
        "match_reason": reason,
    }


def _namespace_of_term(term: str) -> str | None:
    match = _NAMESPACED_RE.match(term.strip())
    return match.group("namespace") if match else None


class _MatchContext:
    """一次查询使用的内存索引（条目数很小，全部加载比反复 SQL 更简单可靠）。"""

    def __init__(self, reader: IndexReader) -> None:
        self.reader = reader
        self.entries = {row["entry_id"]: row for row in reader.entries()}
        self.selectors_by_entry: dict[str, list[dict[str, Any]]] = {key: [] for key in self.entries}
        self.selector_index: dict[str, list[dict[str, Any]]] = {}
        for row in reader.selectors():
            if row["entry_id"] in self.selectors_by_entry:
                self.selectors_by_entry[row["entry_id"]].append(row)
            self.selector_index.setdefault(str(row["selector_norm"]), []).append(row)
        self.alias_index: dict[str, list[dict[str, Any]]] = {}
        for row in reader.aliases():
            self.alias_index.setdefault(str(row["alias_norm"]), []).append(row)
        self.entry_id_index = {entry_id.casefold(): entry_id for entry_id in self.entries}
        self.mappings = reader.mappings()
        self.keyword_backend = "not_used"

    # -- 通用 add -------------------------------------------------------
    def _add(
        self,
        record: dict[str, dict[str, Any]],
        entry_id: str,
        query: dict[str, Any],
        selector: str,
        quality: str,
        reason: str,
    ) -> None:
        if entry_id not in self.entries:
            return
        namespace = query.get("namespace")
        if namespace and namespace not in (self.entries[entry_id].get("namespaces") or []):
            return
        current = record.get(entry_id)
        if current is None or (current["match_quality"] != "exact" and quality == "exact"):
            record[entry_id] = _match_record(query, selector, quality, reason)

    # -- 各 kind --------------------------------------------------------
    def match(self, query: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
        """返回 ``(entry_id → match_record, 失效原因)``。"""
        kind = query["kind"]
        if kind == "id":
            return self._match_id(query), None
        if kind in KIND_NAMESPACES:
            return self._match_namespaced(query)
        if kind == "alias":
            return self._match_alias(query), None
        return self._match_keyword(query), None

    def _match_id(self, query: dict[str, Any]) -> dict[str, dict[str, Any]]:
        term = query["term"]
        term_norm = term.casefold()
        record: dict[str, dict[str, Any]] = {}

        target = self.entry_id_index.get(term_norm)
        if target is not None:
            prefix = "mechanism" if self.entries[target]["entry_kind"] == "mechanism" else "skill"
            self._add(record, target, query, f"{prefix}:{target}", "exact", "entry_id_exact")

        namespace = _namespace_of_term(term)
        if namespace in ("mechanism", "skill"):
            rest = term.split(":", 1)[1]
            target = self.entry_id_index.get(rest.casefold())
            if target is not None:
                expected = "mechanism" if namespace == "mechanism" else "skill_template"
                if self.entries[target]["entry_kind"] == expected:
                    self._add(
                        record, target, query, f"{namespace}:{target}", "exact", "entry_id_exact"
                    )

        for row in self.selector_index.get(term_norm, []):
            self._add(record, row["entry_id"], query, row["selector"], "exact", "selector_exact")

        if record:
            return record

        # 无精确匹配 → 候选（候选不参与自动绑定）
        if len(term_norm) >= 3:
            for entry_norm, entry_id in self.entry_id_index.items():
                if term_norm in entry_norm or entry_norm.startswith(term_norm):
                    prefix = (
                        "mechanism" if self.entries[entry_id]["entry_kind"] == "mechanism" else "skill"
                    )
                    self._add(
                        record, entry_id, query, f"{prefix}:{entry_id}", "candidate", "entry_id_substring"
                    )
        if not record and len(term_norm) >= _MIN_SELECTOR_PREFIX:
            for selector_norm, rows in self.selector_index.items():
                if term_norm in selector_norm:
                    for row in rows:
                        self._add(
                            record, row["entry_id"], query, row["selector"], "candidate", "selector_substring"
                        )
        if not record and len(term_norm) >= _MIN_SUBSTRING:
            for alias_norm, rows in self.alias_index.items():
                if term_norm in alias_norm:
                    for row in rows:
                        self._add(
                            record,
                            row["entry_id"],
                            query,
                            f"{row['source']}:{row['alias']}",
                            "candidate",
                            "name_substring",
                        )
        return record

    def _match_namespaced(
        self, query: dict[str, Any]
    ) -> tuple[dict[str, dict[str, Any]], str | None]:
        kind = query["kind"]
        term = query["term"]
        record: dict[str, dict[str, Any]] = {}
        if ":" not in term:
            return record, "missing_namespace"
        namespace = _namespace_of_term(term) or ""
        allowed = KIND_NAMESPACES[kind]
        if namespace not in allowed:
            return record, "namespace_mismatch"
        term_norm = term.casefold()
        for row in self.selector_index.get(term_norm, []):
            self._add(record, row["entry_id"], query, row["selector"], "exact", "selector_exact")
        if not record and len(term_norm) >= _MIN_SELECTOR_PREFIX:
            matched_prefix = False
            for selector_norm, rows in self.selector_index.items():
                if selector_norm.startswith(term_norm):
                    matched_prefix = True
                    for row in rows:
                        if row["namespace"] == namespace:
                            self._add(
                                record,
                                row["entry_id"],
                                query,
                                row["selector"],
                                "candidate",
                                "selector_prefix",
                            )
            if not matched_prefix and kind == "stat_key":
                # stat_key 允许选择器正文子串（如 stat:fire 命中 stat:fire_damage）
                for selector_norm, rows in self.selector_index.items():
                    if term_norm in selector_norm:
                        for row in rows:
                            if row["namespace"] == namespace:
                                self._add(
                                    record,
                                    row["entry_id"],
                                    query,
                                    row["selector"],
                                    "candidate",
                                    "selector_substring",
                                )
        return record, None

    def _match_alias(self, query: dict[str, Any]) -> dict[str, dict[str, Any]]:
        record: dict[str, dict[str, Any]] = {}
        term_norm = query["term"].casefold()
        for row in self.alias_index.get(term_norm, []):
            self._add(
                record,
                row["entry_id"],
                query,
                f"{row['source']}:{row['alias']}",
                "exact",
                f"{row['source']}_exact",
            )
        if not record and len(term_norm) >= _MIN_SUBSTRING:
            for alias_norm, rows in self.alias_index.items():
                if term_norm in alias_norm:
                    for row in rows:
                        self._add(
                            record,
                            row["entry_id"],
                            query,
                            f"{row['source']}:{row['alias']}",
                            "candidate",
                            "name_substring",
                        )
        return record

    def _match_keyword(self, query: dict[str, Any]) -> dict[str, dict[str, Any]]:
        record: dict[str, dict[str, Any]] = {}
        raw_term = query["term"]
        term_norm = raw_term.casefold()

        # 1) 维护的精确名称（条目 ID / 别名 / 中英文标题 / 完整选择器）
        target = self.entry_id_index.get(term_norm)
        if target is not None:
            prefix = "mechanism" if self.entries[target]["entry_kind"] == "mechanism" else "skill"
            self._add(record, target, query, f"{prefix}:{target}", "exact", "entry_id_exact")
        namespace = _namespace_of_term(raw_term)
        if namespace in ("mechanism", "skill"):
            rest = raw_term.split(":", 1)[1]
            target = self.entry_id_index.get(rest.casefold())
            if target is not None:
                expected = "mechanism" if namespace == "mechanism" else "skill_template"
                if self.entries[target]["entry_kind"] == expected:
                    self._add(
                        record, target, query, f"{namespace}:{target}", "exact", "entry_id_exact"
                    )
        for row in self.alias_index.get(term_norm, []):
            self._add(
                record,
                row["entry_id"],
                query,
                f"{row['source']}:{row['alias']}",
                "exact",
                f"{row['source']}_exact",
            )
        for row in self.selector_index.get(term_norm, []):
            self._add(record, row["entry_id"], query, row["selector"], "exact", "selector_exact")

        # 2) 名称子串（候选）
        if len(term_norm) >= _MIN_SUBSTRING:
            for alias_norm, rows in self.alias_index.items():
                if term_norm in alias_norm:
                    for row in rows:
                        self._add(
                            record,
                            row["entry_id"],
                            query,
                            f"{row['source']}:{row['alias']}",
                            "candidate",
                            "name_substring",
                        )
        if len(term_norm) >= _MIN_SELECTOR_PREFIX:
            for selector_norm, rows in self.selector_index.items():
                if term_norm in selector_norm:
                    for row in rows:
                        self._add(
                            record, row["entry_id"], query, row["selector"], "candidate", "selector_substring"
                        )

        # 3) 词表：FTS5 可用则用，否则（以及中文子串）回退 LIKE
        search_term = raw_term if " " not in raw_term.strip() else raw_term.strip()
        if len(term_norm) >= _MIN_SUBSTRING:
            if self.reader.fts_enabled:
                try:
                    hits = self.reader.terms_fts(search_term)
                    self.keyword_backend = "fts5"
                    for hit in hits:
                        self._add(
                            record,
                            hit["entry_id"],
                            query,
                            f"keyword:{raw_term}",
                            "candidate",
                            "keyword_fts",
                        )
                except sqlite3.OperationalError:  # FTS 表缺失/查询语法不兼容 → 回退 LIKE
                    self.keyword_backend = "like"
            else:
                self.keyword_backend = "like"
            for hit in self.reader.terms_like(term_norm):
                self._add(
                    record, hit["entry_id"], query, f"keyword:{raw_term}", "candidate", "keyword_like"
                )
            if self.keyword_backend == "not_used":
                self.keyword_backend = "like"
        return record


# ---------------------------------------------------------------------------
# 过滤与适用性
# ---------------------------------------------------------------------------


def _filter_node_set(values: Iterable[str]) -> set[str]:
    return {str(value).casefold() for value in values}


def passes_filters(
    entry: dict[str, Any],
    filters: dict[str, list[str]],
    selectors_by_entry: dict[str, list[dict[str, Any]]],
) -> bool:
    """组件/标签/所选节点/条件过滤（同一键内 OR，不同键之间 AND）。"""
    if not filters:
        return True
    components = filters.get("component") or []
    if components and not set(components).intersection(entry.get("components") or []):
        return False
    tags = filters.get("tags") or []
    if tags and not set(tags).intersection(entry.get("tags") or []):
        return False
    node_ids = filters.get("node_ids") or []
    if node_ids:
        associated = {
            str(selector["selector"]).casefold()
            for selector in selectors_by_entry.get(entry["entry_id"]) or []
            if selector.get("namespace") == "node"
        }
        associated |= _filter_node_set(entry.get("required_nodes") or [])
        if associated and not associated.intersection(_filter_node_set(node_ids)):
            return False
    conditions = filters.get("conditions") or []
    if conditions:
        required = set(entry.get("conditions") or [])
        if required and not required.intersection(set(conditions)):
            return False
    return True


def evaluate_applicability(entry: dict[str, Any], context: dict[str, Any] | None) -> dict[str, Any]:
    """按查询上下文评估必要条件；不足返回 ``unknown``，明确不满足才返回 ``not_applicable``。"""
    context = context or {}
    required_conditions = [item for item in entry.get("conditions") or [] if item != "none"]
    required_nodes = list(entry.get("required_nodes") or [])
    if not required_conditions and not required_nodes:
        return {"status": "satisfied", "evaluated_on": ["always"], "missing_conditions": []}

    provided_conditions = set(context.get("conditions") or [])
    provided_nodes = _filter_node_set(context.get("nodes") or [])
    exhaustive = bool(context.get("exhaustive"))

    missing: list[str] = []
    for condition in required_conditions:
        if condition not in provided_conditions:
            missing.append(condition)
    for node in required_nodes:
        if str(node).casefold() not in provided_nodes:
            missing.append(node if ":" in node else f"node:{node}")

    evaluated_on: list[str] = []
    if provided_conditions:
        evaluated_on.append("context.conditions")
    if provided_nodes:
        evaluated_on.append("context.nodes")
    if missing:
        return {
            "status": "not_applicable" if exhaustive else "unknown",
            "evaluated_on": evaluated_on,
            "missing_conditions": missing,
        }
    return {"status": "satisfied", "evaluated_on": evaluated_on, "missing_conditions": []}


# ---------------------------------------------------------------------------
# 结果组装
# ---------------------------------------------------------------------------


def _sort_key(entry: dict[str, Any], *, skills_first: bool = False) -> tuple[int, int, int, int, str]:
    records = entry.get("matched_queries") or []
    exact = sum(1 for record in records if record.get("match_quality") == "exact")
    candidate = len(records) - exact
    if exact:
        relevance = 0
    elif any(
        str(record.get("match_reason") or "").endswith("_exact")
        or record.get("match_reason") in ("name_substring",)
        for record in records
    ):
        relevance = 1
    elif any("keyword" in str(record.get("match_reason") or "") for record in records):
        relevance = 2
    else:
        relevance = 3
    is_mechanism = entry.get("entry_kind") == "mechanism"
    if skills_first and relevance > 0:
        kind_rank = 0 if not is_mechanism else 1
    else:
        kind_rank = 0 if is_mechanism else 1
    return (relevance, -exact, -candidate, kind_rank, str(entry.get("entry_id") or ""))


def _entry_payload(entry: dict[str, Any], match_records: list[dict[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mechanism_id": entry["entry_id"],
        "entry_id": entry["entry_id"],
        "entry_kind": entry["entry_kind"],
        "revision": entry.get("revision") if entry.get("revision") is not None else 0,
        "title": {
            key: value
            for key, value in (entry.get("title") or {}).items()
            if value
        },
        "matched_queries": match_records,
        "applicability": entry.get("applicability"),
        "evidence_state": entry.get("evidence_state"),
        "implementation_state": entry.get("implementation_state"),
        "rule_refs": entry.get("rule_refs") or [],
        "evidence_refs": entry.get("evidence_refs") or [],
        "implementation_refs": entry.get("implementation_refs") or [],
        "unresolved": entry.get("unresolved") or [],
        "components": entry.get("components") or [],
        "tags": entry.get("tags") or [],
        "conditions": entry.get("conditions") or [],
        "selectors": [row["selector"] for row in entry.get("selector_rows") or []],
        "case_scope": entry.get("case_scope") or [],
        "source": {
            "path": entry.get("source_path") or "",
            "sha256": entry.get("source_sha256") or "",
        },
    }
    if entry["entry_kind"] == "skill_template":
        ability_selectors = [
            row["selector"]
            for row in entry.get("selector_rows") or []
            if row.get("namespace") == "ability"
        ]
        if ability_selectors:
            payload["ability_id"] = ability_selectors[0].split(":", 1)[1]
        if entry.get("entry_id"):
            payload["skill_id"] = entry["entry_id"]
    return payload


def execute_query(query: dict[str, Any], reader: IndexReader) -> dict[str, Any]:
    """执行归一化查询，返回 ``le-mechanism-result-v1`` 结果字典。"""
    context = _MatchContext(reader)
    filters = query["filters"]
    top_namespaces = set(query.get("namespaces") or [])
    applicability_context = {
        "conditions": query["context"]["conditions"],
        "nodes": query["context"]["nodes"],
        "exhaustive": query["context"]["exhaustive"],
    }

    matches: dict[str, dict[str, dict[str, Any]]] = {}
    query_errors: dict[str, str] = {}
    for item in query["queries"]:
        record, invalid_reason = context.match(item)
        if invalid_reason:
            query_errors[item["query_id"]] = invalid_reason
        for entry_id, matched in record.items():
            matches.setdefault(entry_id, {})[item["query_id"]] = matched

    base_entry_ids: set[str] = set(matches)
    if query["filter_only"]:
        base_entry_ids = set(context.entries)

    def entry_namespaces(entry_id: str) -> set[str]:
        return set(context.entries[entry_id].get("namespaces") or [])

    pre_filter_hit_queries = {
        query_id
        for records in matches.values()
        for query_id in records
    }

    kept_ids: list[str] = []
    for entry_id in base_entry_ids:
        entry = context.entries[entry_id]
        if top_namespaces and not top_namespaces.intersection(entry_namespaces(entry_id)):
            continue
        if not passes_filters(entry, filters, context.selectors_by_entry):
            continue
        kept_ids.append(entry_id)

    entries: list[dict[str, Any]] = []
    order = {item["query_id"]: index for index, item in enumerate(query["queries"])}
    for entry_id in kept_ids:
        entry = dict(context.entries[entry_id])
        selector_rows = context.selectors_by_entry.get(entry_id) or []
        entry["selector_rows"] = selector_rows
        records = sorted(
            (matches.get(entry_id) or {}).values(),
            key=lambda record: order.get(record["query_id"], 10**6),
        )
        payload = _entry_payload(entry, records)
        document = reader.entry_document(entry_id) or {}
        payload["reuse_mode"] = document.get("reuse_mode")
        payload["required_abilities"] = document.get("required_abilities") or []
        payload["parameter_bindings"] = document.get("parameter_bindings") or []
        # Conditions describe runtime activation, not ownership of the knowledge.
        lookup_entry = entry
        if payload["reuse_mode"] == "shared":
            lookup_entry = dict(entry, conditions=[])
        payload["applicability"] = evaluate_applicability(lookup_entry, applicability_context)
        entries.append(payload)

    any_exact = any(
        any(record.get("match_quality") == "exact" for record in entry.get("matched_queries") or [])
        for entry in entries
    )
    entries.sort(key=lambda entry: _sort_key(entry, skills_first=not any_exact))
    total_before_limit = len(entries)
    limited = entries[: query["limit"]]
    # 未命中判定在“过滤后、截断前”的集合上做：limit 截断不代表查询未命中。
    post_filter_query_ids = {
        record["query_id"]
        for entry in entries
        for record in (entry.get("matched_queries") or [])
    }
    unmatched: list[dict[str, Any]] = []
    for item in query["queries"]:
        query_id = item["query_id"]
        if query_id in post_filter_query_ids:
            continue
        if query_id in query_errors:
            reason = query_errors[query_id]
        elif query_id in pre_filter_hit_queries:
            reason = "filtered_out"
        else:
            reason = "no_match"
        unmatched.append(
            {
                "query_id": query_id,
                "term": item["term"],
                "kind": item["kind"],
                "reason": reason,
            }
        )

    exact_entries = [
        entry
        for entry in limited
        if any(record.get("match_quality") == "exact" for record in entry["matched_queries"])
    ]
    if exact_entries:
        match_quality = "exact"
    elif limited:
        match_quality = "candidate"
    else:
        match_quality = "no_match"
    binding_eligible = [
        entry
        for entry in exact_entries
        if (entry.get("applicability") or {}).get("status") == "satisfied"
    ]

    summary = {
        "query_count": len(query["queries"]),
        "matched_query_count": len(query["queries"]) - len(unmatched),
        "unmatched_query_count": len(unmatched),
        "entry_count": len(limited),
        "entry_count_total": total_before_limit,
        "limit": query["limit"],
        "match_quality": match_quality,
        "binding_eligible_count": len(binding_eligible),
        "keyword_backend": context.keyword_backend,
        "filters_applied": {key: list(value) for key, value in filters.items() if value},
        "namespaces_applied": sorted(top_namespaces),
        "note": "summary.match_quality 仅作结果集摘要，不参与自动绑定；"
        "自动绑定只接受 entries[].matched_queries[] 至少一条 exact 且 applicability.status=satisfied 的条目。",
    }
    return {
        "schema_version": "le-mechanism-result-v1",
        "entries": limited,
        "unmatched_queries": unmatched,
        "summary": summary,
        "index_digest": reader.source_digest,
    }


def related_entries(reader: IndexReader, entry: dict[str, Any]) -> dict[str, Any]:
    """条目详情里的交叉引用（技能↔机制），供 ``show`` 使用。"""
    entry_id = entry["entry_id"]
    known_ids = {row["entry_id"] for row in reader.entries()}
    if entry["entry_kind"] == "mechanism":
        related_skills = [
            {
                "skill_id": row["entry_id"],
                "selector": row["selector"],
                "exists": row["entry_id"] in known_ids,
            }
            for row in reader.mappings_for_mechanism(entry_id)
        ]
        return {"related_skills": related_skills}
    related_mechanisms = [
        {
            "mechanism_id": row["mechanism_id"],
            "selector": row["selector"],
            "exists": row["mechanism_id"] in known_ids,
        }
        for row in reader.mappings()
        if row["entry_id"] == entry_id
    ]
    return {"related_mechanisms": related_mechanisms}
