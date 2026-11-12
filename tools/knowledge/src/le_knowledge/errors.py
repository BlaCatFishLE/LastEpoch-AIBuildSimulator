# -*- coding: utf-8 -*-
"""le_knowledge 的错误类型。

这些错误不依赖编排层（``scripts/le_commands.py``）、当前案例或聊天上下文，
独立调用方可以直接捕获：业务函数返回结构化 ``dict``，失败时抛出明确的异常。
"""

from __future__ import annotations

from typing import Any, Iterable


class KnowledgeError(RuntimeError):
    """知识库/索引/查询错误的基类。"""

    def to_dict(self) -> dict[str, Any]:
        return {"error": type(self).__name__, "message": str(self)}


class KnowledgeValidationError(KnowledgeError):
    """JSON 权威源不符合 ``knowledge/schemas/*.json``。"""

    def __init__(self, errors: Iterable[dict[str, Any]], *, context: str = "") -> None:
        self.errors: list[dict[str, Any]] = [dict(item) for item in errors]
        self.context = context
        super().__init__(self._format())

    def _format(self) -> str:
        header = "知识条目校验失败"
        if self.context:
            header += f"（{self.context}）"
        lines = [
            f"{item.get('path', '?')}: {item.get('pointer', '$')}: {item.get('message', '')}"
            for item in self.errors
        ]
        return header + ("\n" + "\n".join(lines) if lines else "")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "knowledge_validation_error",
            "context": self.context,
            "errors": list(self.errors),
        }


class KnowledgeQueryError(KnowledgeError):
    """``le-mechanism-query-v1`` 查询参数不合法。"""

    def __init__(self, message: str, *, pointer: str = "$") -> None:
        self.pointer = pointer
        super().__init__(f"{pointer}: {message}")

    def to_dict(self) -> dict[str, Any]:
        return {"error": "knowledge_query_error", "pointer": self.pointer, "message": str(self)}


class KnowledgeNotFoundError(KnowledgeError):
    """按 ID 查询条目时条目不存在。"""

    def __init__(self, entry_id: str, *, index_digest: str | None = None) -> None:
        self.entry_id = entry_id
        self.index_digest = index_digest
        suffix = f"（索引 {index_digest}）" if index_digest else ""
        super().__init__(f"知识条目不存在：{entry_id}{suffix}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "knowledge_not_found",
            "entry_id": self.entry_id,
            "index_digest": self.index_digest,
        }


class KnowledgeIndexError(KnowledgeError):
    """查询索引缺失/损坏/格式不兼容且无法自动重建。"""
