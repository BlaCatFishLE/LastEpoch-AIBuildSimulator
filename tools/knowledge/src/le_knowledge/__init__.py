# -*- coding: utf-8 -*-
"""le_knowledge — 机制/技能知识的可重建索引与独立查询接口。

P1 验收口径（见 ``docs/contracts/workflow-interface.md`` §5.1 / §9）：

* JSON 是唯一权威源（``knowledge/mechanisms/*.json``、``knowledge/skills/*.json``）；
* SQLite 索引 ``data/cache/knowledge.sqlite`` 可随时删除重建，不作为知识源；
* ``search()`` / ``show()`` 可绕过编排独立调用，只依赖显式路径参数与查询字典；
* 查询结果逐条回报 ``entries[].matched_queries[]``（query_id/selector/
  match_quality/match_reason）、``entries[].applicability``、``unmatched_queries[]``，
  ``summary.match_quality`` 只是摘要，不参与自动绑定。

快速用法::

    from le_knowledge import search, show, rebuild

    result = search({"queries": [{"query_id": "q1", "term": "node:fr11mv:16", "kind": "node_id"}]})
    detail = show("fr-rhythm-of-fire")
    report = rebuild()
"""

from __future__ import annotations

from .api import (
    index_state,
    list_entries,
    rebuild,
    search,
    show,
)
from .errors import (
    KnowledgeError,
    KnowledgeIndexError,
    KnowledgeNotFoundError,
    KnowledgeQueryError,
    KnowledgeValidationError,
)
from .loader import load_corpus, scan_source_files
from .paths import (
    DEFAULT_INDEX_PATH,
    DEFAULT_KNOWLEDGE_DIR,
    ENTRY_DETAIL_SCHEMA,
    INDEX_FORMAT,
    PACKAGE_VERSION,
    QUERY_SCHEMA,
    RESULT_SCHEMA,
    Paths,
    resolve_paths,
)

__version__ = PACKAGE_VERSION

__all__ = [
    "DEFAULT_INDEX_PATH",
    "DEFAULT_KNOWLEDGE_DIR",
    "ENTRY_DETAIL_SCHEMA",
    "INDEX_FORMAT",
    "KnowledgeError",
    "KnowledgeIndexError",
    "KnowledgeNotFoundError",
    "KnowledgeQueryError",
    "KnowledgeValidationError",
    "PACKAGE_VERSION",
    "Paths",
    "QUERY_SCHEMA",
    "RESULT_SCHEMA",
    "index_state",
    "list_entries",
    "load_corpus",
    "rebuild",
    "resolve_paths",
    "scan_source_files",
    "search",
    "show",
]
