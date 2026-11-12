# -*- coding: utf-8 -*-
"""最小 JSON Schema 子集校验器（不引入第三方依赖）。

只解释 ``knowledge/schemas/*.json`` 实际用到的关键字：
``type`` / ``required`` / ``properties`` / ``additionalProperties`` / ``items`` /
``enum`` / ``const`` / ``pattern`` / ``minLength`` / ``minimum`` / ``maximum`` / ``minItems``。

错误信息统一为 ``{"pointer": "$.selectors[0].kind", "message": "..."}``，
由 loader 补上文件路径后抛 :class:`~le_knowledge.errors.KnowledgeValidationError`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .errors import KnowledgeError

#: schema 文件名 → schema 标识（knowledge/schemas 下的契约）
SCHEMA_FILE_BY_ID = {
    "le-knowledge-mechanism-v1": "mechanism-v1.json",
    "le-knowledge-skill-template-v1": "skill-template-v1.json",
}

_TYPE_CHECKS = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
}

_TYPE_LABELS = {
    "object": "对象",
    "array": "数组",
    "string": "字符串",
    "integer": "整数",
    "number": "数值",
    "boolean": "布尔",
    "null": "null",
}


def _actual_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _type_matches(value: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_type_matches(value, item) for item in expected)
    check = _TYPE_CHECKS.get(str(expected))
    return bool(check and check(value))


def _type_label(expected: Any) -> str:
    if isinstance(expected, list):
        return " 或 ".join(_TYPE_LABELS.get(str(item), str(item)) for item in expected)
    return _TYPE_LABELS.get(str(expected), str(expected))


def _format_value(value: Any, limit: int = 80) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def validate_document(document: Any, schema: dict[str, Any], *, pointer: str = "$") -> list[dict[str, str]]:
    """按最小 schema 子集校验 ``document``，返回错误列表（空列表 = 通过）。"""
    errors: list[dict[str, str]] = []
    _validate(document, schema, pointer, errors)
    return errors


def _validate(value: Any, schema: Any, pointer: str, errors: list[dict[str, str]]) -> None:
    if not isinstance(schema, dict):
        return

    if "const" in schema and value != schema["const"]:
        errors.append(
            {
                "pointer": pointer,
                "message": f"常量应为 {_format_value(schema['const'])}，实际为 {_format_value(value)}",
            }
        )
        return

    if "enum" in schema:
        allowed = list(schema["enum"])
        if value not in allowed:
            options = " / ".join(_format_value(item, 40) for item in allowed)
            errors.append(
                {"pointer": pointer, "message": f"值 {_format_value(value)} 不在允许枚举中（{options}）"}
            )

    if "type" in schema and not _type_matches(value, schema["type"]):
        errors.append(
            {
                "pointer": pointer,
                "message": f"类型应为 {_type_label(schema['type'])}，实际为 {_actual_type(value)}",
            }
        )
        return

    if isinstance(value, str):
        pattern = schema.get("pattern")
        if pattern and not re.search(str(pattern), value):
            errors.append(
                {"pointer": pointer, "message": f"字符串 {_format_value(value)} 不匹配模式 {pattern!r}"}
            )
        min_length = schema.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            errors.append({"pointer": pointer, "message": f"字符串长度不足 {min_length}"})

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append({"pointer": pointer, "message": f"数值小于最小值 {minimum}"})
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append({"pointer": pointer, "message": f"数值大于最大值 {maximum}"})

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append({"pointer": pointer, "message": f"数组至少需要 {min_items} 项"})
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate(item, item_schema, f"{pointer}[{index}]", errors)

    if isinstance(value, dict):
        properties = schema.get("properties")
        properties = properties if isinstance(properties, dict) else {}
        required = schema.get("required")
        if isinstance(required, list):
            for name in required:
                if name not in value:
                    errors.append({"pointer": pointer, "message": f'缺少必填字段 "{name}"'})
        for name, item_schema in properties.items():
            if name in value:
                _validate(value[name], item_schema, f"{pointer}.{name}", errors)
        additional = schema.get("additionalProperties", True)
        if additional is False:
            for name in value:
                if name not in properties:
                    errors.append({"pointer": pointer, "message": f'不允许的附加字段 "{name}"'})
        elif isinstance(additional, dict):
            for name in value:
                if name not in properties:
                    _validate(value[name], additional, f"{pointer}.{name}", errors)


def load_schema_file(path: str | Path) -> dict[str, Any]:
    """读取一份 schema 定义；文件缺失/损坏时抛 :class:`KnowledgeError`。"""
    path = Path(path)
    if not path.is_file():
        raise KnowledgeError(f"缺少 schema 定义文件：{path.name}")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # pragma: no cover - 仓库契约文件应当合法
        raise KnowledgeError(f"schema 定义不是合法 JSON：{path.name}：{exc}") from exc
    if not isinstance(document, dict):
        raise KnowledgeError(f"schema 定义必须是 JSON 对象：{path.name}")
    return document
