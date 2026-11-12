# -*- coding: utf-8 -*-
"""知识权威源的扫描、最小 schema 校验与归一化。

权威源：``knowledge/mechanisms/*.json``（一条机制一个文件）与
``knowledge/skills/*.json``（一个技能模板一个文件），字段见
``knowledge/schemas/mechanism-v1.json`` / ``skill-template-v1.json``。

本模块只读 JSON；索引、Markdown 页与查询都由其它模块生成。归一化结果既用于
SQLite 构建，也用于 ``rebuild`` 报告与 Markdown 索引，保证三者一致。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from .errors import KnowledgeValidationError
from .paths import (
    INDEX_FORMAT,
    KNOWN_NAMESPACES,
    display_path,
    find_repo_root,
)
from .schema import SCHEMA_FILE_BY_ID, load_schema_file, validate_document

MECHANISM_SCHEMA = "le-knowledge-mechanism-v1"
SKILL_SCHEMA = "le-knowledge-skill-template-v1"

#: (entry_kind, 子目录)
CONTENT_DIRS: tuple[tuple[str, str], ...] = (
    ("mechanism", "mechanisms"),
    ("skill_template", "skills"),
)
SCHEMA_DIR = "schemas"

SCHEMA_ID_BY_FILE = {filename: schema_id for schema_id, filename in SCHEMA_FILE_BY_ID.items()}


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_content_json(path: Path) -> bool:
    return path.suffix.lower() == ".json" and not path.name.startswith(("_", "."))


def scan_source_files(knowledge_dir: str | Path) -> dict[str, Any]:
    """扫描参与索引的内容文件（机制/技能/记录 schema）并计算内容指纹。

    返回 ``{"files": [{"kind", "path", "rel", "sha256"}], "digest": "sha256:..."}``。
    指纹只依赖文件集合与字节内容，因此同一内容重复重建结果稳定。
    """
    directory = Path(knowledge_dir)
    files: list[dict[str, Any]] = []
    for kind, subdir in CONTENT_DIRS:
        folder = directory / subdir
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                if _is_content_json(path):
                    files.append({"kind": kind, "path": path, "rel": f"{subdir}/{path.name}"})
    schema_folder = directory / SCHEMA_DIR
    if schema_folder.is_dir():
        for path in sorted(schema_folder.glob("*.json")):
            if _is_content_json(path):
                files.append({"kind": "schema", "path": path, "rel": f"{SCHEMA_DIR}/{path.name}"})
    for item in files:
        item["sha256"] = sha256_file(item["path"])
    lines = [f"{item['kind']}\t{item['rel']}\t{item['sha256']}" for item in files]
    payload = INDEX_FORMAT + "\n" + "\n".join(lines) + ("\n" if lines else "")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return {"files": files, "digest": f"sha256:{digest}"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _entry_id(document: dict[str, Any], kind: str) -> str:
    key = "mechanism_id" if kind == "mechanism" else "skill_id"
    return str(document.get(key) or "")


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_selector(
    kind: str,
    namespace: str,
    raw: str,
    *,
    warnings: list[dict[str, str]],
    source: str,
) -> tuple[str, str]:
    """把 ``(kind, namespace, id)`` 组合为契约要求的完整选择器 ``kind:...``。

    ``knowledge/README.md`` 规则 3 要求 ``selectors`` 的 ID 带命名空间，但 schema 把
    语义命名空间放在 ``kind``、把来源子命名空间放在 ``namespace``。P1 内容实际登记
    形式包括（见 ``docs/mechanisms.md``）：

    * ``{kind: node, namespace: fr11mv, ids: [16]}``   → ``node:fr11mv:16``
    * ``{kind: node, namespace: node, ids: [fr11mv:16]}`` → ``node:fr11mv:16``
    * ``{kind: ability, namespace: ability, ids: [fr11mv]}`` → ``ability:fr11mv``
    * ``{kind: item, namespace: item, ids: [letools:uniqueList.uniques:443]}``
      → ``item:letools:uniqueList.uniques:443``

    组合规则：``kind:`` 前缀 + 正文；正文若已带来源子命名空间（或已带已知契约
    命名空间）则原样保留，否则用 ``namespace:`` 补齐。
    """
    selector_kind = str(kind or "").strip()
    ns = str(namespace or "").strip()
    value = str(raw or "").strip()
    if not value:
        return "", ns
    if ":" in value:
        first = value.split(":", 1)[0]
        if first in KNOWN_NAMESPACES:
            # 已经带契约命名空间（如 node:fr11mv:16 / stat:fire_damage）
            return value, first
        if selector_kind:
            return f"{selector_kind}:{value}", selector_kind
        return value, first
    if selector_kind:
        body = f"{ns}:{value}" if ns and ns != selector_kind else value
        return f"{selector_kind}:{body}", selector_kind
    canonical = f"{ns}:{value}" if ns else value
    if ns and ns not in KNOWN_NAMESPACES:
        warnings.append({"path": source, "message": f"selector 命名空间未在约定集合内：{canonical}"})
    return canonical, ns


def _compat_selectors(kind: str, namespace: str, raw: str) -> list[tuple[str, str]]:
    """契约示例形式的兼容选择器（``ability:bh2`` / ``stat:damage`` 等）。

    内容登记时 ``namespace`` 常填来源命名空间（``dataset``/``le_calc``/``slot``），
    与 workflow-interface §5.1 的示例（``ability:fr11mv``、``stat:fire_damage``）
    不完全一致。除 ``kind:namespace:id`` 主选择器外，再补一条去掉来源命名空间的
    ``kind:id`` 兼容选择器，保证按契约示例查询也能命中；不改变主选择器与告警。
    """
    kind = str(kind or "").strip()
    namespace = str(namespace or "").strip()
    value = str(raw or "").strip()
    if not kind or not value or ":" in value or namespace in ("", kind):
        return []
    if kind in ("ability", "stat", "item", "blessing", "idol", "unique", "condition", "mechanism"):
        return [(f"{kind}:{value}", kind)]
    return []


def _mechanism_selectors(
    document: dict[str, Any], *, warnings: list[dict[str, str]], source: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for selector in document.get("selectors") or []:
        if not isinstance(selector, dict):
            continue
        kind = str(selector.get("kind") or "")
        namespace = str(selector.get("namespace") or "")
        for raw in selector.get("ids") or []:
            canonical, selector_ns = _normalize_selector(
                kind, namespace, str(raw), warnings=warnings, source=source
            )
            if not canonical:
                continue
            rows.append({"selector": canonical, "kind": kind, "namespace": selector_ns or namespace})
            for compat, compat_ns in _compat_selectors(kind, namespace, str(raw)):
                rows.append({"selector": compat, "kind": kind, "namespace": compat_ns})
    return _dedupe_rows(rows)


def _parse_known_selector(
    raw: str, *, warnings: list[dict[str, str]], source: str
) -> tuple[str, str] | None:
    value = str(raw or "").strip()
    if not value:
        warnings.append({"path": source, "message": "mapping selector 为空，已跳过"})
        return None
    if ":" not in value:
        warnings.append(
            {"path": source, "message": f"mapping selector 缺少命名空间（应为 node:/item:/... 前缀）：{value}"}
        )
        return f"unknown:{value}", "unknown"
    namespace = value.split(":", 1)[0]
    if namespace not in KNOWN_NAMESPACES:
        warnings.append({"path": source, "message": f"mapping selector 命名空间未在约定集合内：{value}"})
    return value, namespace


def _skill_selectors(
    document: dict[str, Any], *, warnings: list[dict[str, str]], source: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    ability_id = str(document.get("ability_id") or "").strip()
    if ability_id:
        rows.append({"selector": f"ability:{ability_id}", "kind": "ability", "namespace": "ability"})
    for mapping in document.get("mappings") or []:
        if not isinstance(mapping, dict):
            continue
        parsed = _parse_known_selector(
            str(mapping.get("selector") or ""), warnings=warnings, source=source
        )
        if parsed is None:
            continue
        rows.append({"selector": parsed[0], "kind": "mapping", "namespace": parsed[1]})
    return _dedupe_rows(rows)


def _dedupe_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for row in rows:
        key = row.get("selector", "")
        if key in seen:
            continue
        seen.add(key)
        result.append(row)
    return result


def _normalize_entry(
    document: dict[str, Any],
    *,
    entry_kind: str,
    schema_id: str,
    source_path: str,
    source_sha256: str,
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    entry_id = _entry_id(document, entry_kind)
    title = document.get("title") or {}
    if entry_kind == "mechanism":
        selectors = _mechanism_selectors(document, warnings=warnings, source=source_path)
        applicability = document.get("applicability") or {}
        components = _dedupe(app for app in (applicability.get("components") or []) if isinstance(app, str))
        tags = _dedupe(tag for tag in (applicability.get("tags") or []) if isinstance(tag, str))
        conditions = _dedupe(
            condition
            for condition in (applicability.get("conditions") or [])
            if isinstance(condition, str) and condition != "none"
        )
        required_nodes: list[str] = []
        for raw in applicability.get("required_nodes") or []:
            canonical, _ = _normalize_selector(
                "node", "node", str(raw), warnings=warnings, source=source_path
            )
            if canonical and canonical not in required_nodes:
                required_nodes.append(canonical)
        rule_refs: list[dict[str, Any]] = []
        rule_ref = document.get("rule_ref")
        if isinstance(rule_ref, dict):
            rule_refs.append(dict(rule_ref))
        handler_ref = document.get("handler_ref")
        implementation_refs: list[dict[str, Any]] = [dict(handler_ref)] if isinstance(handler_ref, dict) else []
        evidence_refs: list[dict[str, Any]] = [
            dict(item) for item in (document.get("evidence_refs") or []) if isinstance(item, dict)
        ]
        evidence_refs += [
            {"kind": "validation", "locator": str(item)} for item in (document.get("validation_refs") or [])
        ]
        research_notes = document.get("research_notes") or {}
        unresolved = [str(item) for item in (research_notes.get("open_questions") or []) if str(item).strip()]
        mappings: list[dict[str, str]] = []
        aliases = [str(item) for item in (document.get("aliases") or []) if str(item).strip()]
    else:
        selectors = _skill_selectors(document, warnings=warnings, source=source_path)
        components = _dedupe(
            str(component.get("kind") or "")
            for component in (document.get("components") or [])
            if isinstance(component, dict) and component.get("kind")
        )
        component_tags: list[str] = []
        for component in document.get("components") or []:
            if isinstance(component, dict):
                component_tags.extend(str(tag) for tag in (component.get("tags") or []))
        tags = _dedupe([*[str(tag) for tag in (document.get("damage_tags") or [])], *component_tags])
        conditions = []
        required_nodes = []
        rule_refs = []
        implementation_refs = [
            dict(block)
            for block in (document.get("calculation_blocks") or [])
            if isinstance(block, dict)
        ]
        evidence_refs = [
            {"kind": "validation", "locator": str(item)} for item in (document.get("validation_refs") or [])
        ]
        for assumption in document.get("assumptions") or []:
            if isinstance(assumption, dict) and assumption.get("evidence_locator"):
                evidence_refs.append(
                    {"kind": "assumption", "locator": str(assumption["evidence_locator"])}
                )
        unresolved = [
            f"{item}: {reason}" if reason else str(item)
            for item, reason in (
                (str(unsupported.get("item") or ""), str(unsupported.get("reason") or ""))
                for unsupported in (document.get("unsupported") or [])
                if isinstance(unsupported, dict)
            )
            if item
        ]
        mappings = []
        for mapping in document.get("mappings") or []:
            if not isinstance(mapping, dict):
                continue
            parsed = _parse_known_selector(
                str(mapping.get("selector") or ""), warnings=warnings, source=source_path
            )
            if parsed is None:
                continue
            mappings.append(
                {
                    "selector": parsed[0],
                    "mechanism_id": str(mapping.get("mechanism_id") or ""),
                }
            )
        aliases = []

    namespaces = sorted({row["namespace"] for row in selectors if row.get("namespace")})
    # 条目自身 ID 的命名空间也计入，便于按 namespaces 过滤机制/技能条目。
    id_namespace = "mechanism" if entry_kind == "mechanism" else "skill"
    if id_namespace not in namespaces:
        namespaces.append(id_namespace)
    namespaces = sorted(set(namespaces))

    return {
        "entry_id": entry_id,
        "entry_kind": entry_kind,
        "schema": schema_id,
        "revision": document.get("revision"),
        "title": {
            "zh": str(title.get("zh") or ""),
            "en": str(title.get("en")) if title.get("en") else None,
        },
        "evidence_state": document.get("evidence_state"),
        "implementation_state": document.get("implementation_state"),
        "aliases": _dedupe(aliases),
        "selectors": selectors,
        "components": components,
        "tags": tags,
        "conditions": conditions,
        "required_nodes": required_nodes,
        "namespaces": namespaces,
        "rule_refs": rule_refs,
        "evidence_refs": evidence_refs,
        "implementation_refs": implementation_refs,
        "unresolved": _dedupe(unresolved),
        "case_scope": _dedupe(str(item) for item in (document.get("case_scope") or [])),
        "mappings": mappings,
        "source_path": source_path,
        "source_sha256": source_sha256,
        "document": document,
    }


def load_corpus(knowledge_dir: str | Path, *, root: str | Path | None = None) -> dict[str, Any]:
    """读取并校验全部知识条目，返回归一化 corpus。

    出错时抛 :class:`~le_knowledge.errors.KnowledgeValidationError`（错误含文件与
    字段路径）；非阻塞问题（文件名与 ID 不一致、mapping 引用缺失等）放在
    ``warnings`` 里，不阻止重建。
    """
    directory = Path(knowledge_dir)
    base_root = Path(root).resolve() if root is not None else (
        find_repo_root() or directory.resolve().parent
    )

    def shown(path: str | Path) -> str:
        return display_path(path, base_root) if not isinstance(path, str) else path

    scan = scan_source_files(directory)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    schemas: dict[str, dict[str, Any]] = {}
    for item in scan["files"]:
        if item["kind"] != "schema":
            continue
        path = item["path"]
        try:
            schema = load_schema_file(path)
        except Exception as exc:  # 缺失/损坏的 schema 由该文件错误报告
            errors.append({"path": shown(path), "pointer": "$", "message": str(exc)})
            continue
        schema_id = SCHEMA_ID_BY_FILE.get(Path(path).name)
        if schema_id:
            schemas[schema_id] = schema

    entries: list[dict[str, Any]] = []
    seen_ids: dict[str, str] = {}

    for item in scan["files"]:
        if item["kind"] == "schema":
            continue
        path = item["path"]
        source = shown(path)
        try:
            document = _read_json(path)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            errors.append({"path": source, "pointer": "$", "message": f"JSON 解析失败：{exc}"})
            continue
        if not isinstance(document, dict):
            errors.append({"path": source, "pointer": "$", "message": "条目必须是 JSON 对象"})
            continue
        schema_id = document.get("schema")
        if not isinstance(schema_id, str) or not schema_id:
            errors.append({"path": source, "pointer": "$.schema", "message": "缺少 schema 字段"})
            continue
        if schema_id not in SCHEMA_FILE_BY_ID:
            errors.append(
                {
                    "path": source,
                    "pointer": "$.schema",
                    "message": f"未知 schema：{schema_id!r}（可选：{', '.join(SCHEMA_FILE_BY_ID)}）",
                }
            )
            continue
        schema = schemas.get(schema_id)
        if schema is None:
            errors.append(
                {
                    "path": source,
                    "pointer": "$.schema",
                    "message": f"缺少 schema 定义文件 schemas/{SCHEMA_FILE_BY_ID[schema_id]}",
                }
            )
            continue
        item_errors = validate_document(document, schema)
        if item_errors:
            errors.extend({"path": source, **error} for error in item_errors)
            continue
        expected_kind = "mechanism" if schema_id == MECHANISM_SCHEMA else "skill_template"
        if item["kind"] != expected_kind:
            errors.append(
                {
                    "path": source,
                    "pointer": "$.schema",
                    "message": (
                        f"条目 schema 与目录不一致：{schema_id} 应放在 "
                        f"{'mechanisms/' if expected_kind == 'mechanism' else 'skills/'}"
                    ),
                }
            )
            continue
        entry = _normalize_entry(
            document,
            entry_kind=expected_kind,
            schema_id=schema_id,
            source_path=source,
            source_sha256=item["sha256"],
            warnings=warnings,
        )
        if not entry["entry_id"]:
            errors.append({"path": source, "pointer": "$", "message": "条目 ID 为空"})
            continue
        existing = seen_ids.get(entry["entry_id"])
        if existing is not None:
            errors.append(
                {
                    "path": source,
                    "pointer": "$",
                    "message": f"重复的条目 ID {entry['entry_id']!r}（另一个定义在 {existing}）",
                }
            )
            continue
        seen_ids[entry["entry_id"]] = source
        expected_filename = f"{entry['entry_id']}.json"
        if Path(path).name != expected_filename:
            warnings.append(
                {
                    "path": source,
                    "message": f"文件名与条目 ID 不一致（建议 {expected_filename}）",
                }
            )
        entries.append(entry)

    # 交叉引用：技能模板 mappings[].mechanism_id 指向的机制必须存在，否则登记为告警。
    known_ids = {entry["entry_id"] for entry in entries}
    for entry in entries:
        if entry["entry_kind"] != "skill_template":
            continue
        for mapping in entry.get("mappings") or []:
            mechanism_id = mapping.get("mechanism_id") or ""
            if mechanism_id and mechanism_id not in known_ids:
                warnings.append(
                    {
                        "path": entry["source_path"],
                        "message": (
                            f"mappings 引用了不存在的 mechanism_id：{mechanism_id}"
                            f"（selector {mapping.get('selector')}）"
                        ),
                    }
                )

    if errors:
        errors.sort(key=lambda item: (item.get("path", ""), item.get("pointer", ""), item.get("message", "")))
        raise KnowledgeValidationError(errors, context=shown(directory))

    entries.sort(key=lambda entry: (entry["entry_kind"], entry["entry_id"]))
    mechanisms = [entry for entry in entries if entry["entry_kind"] == "mechanism"]
    skills = [entry for entry in entries if entry["entry_kind"] == "skill_template"]
    counts = {
        "mechanisms": len(mechanisms),
        "skill_templates": len(skills),
        "entries": len(entries),
        "selectors": sum(len(entry["selectors"]) for entry in entries),
        "aliases": sum(len(entry["aliases"]) for entry in entries),
        "content_files": sum(1 for item in scan["files"] if item["kind"] != "schema"),
        "schema_files": sum(1 for item in scan["files"] if item["kind"] == "schema"),
        "files": len(scan["files"]),
    }
    warnings.sort(key=lambda item: (item.get("path", ""), item.get("message", "")))
    return {
        "entries": entries,
        "warnings": warnings,
        "files": [
            {"kind": item["kind"], "path": shown(item["path"]), "sha256": item["sha256"]}
            for item in scan["files"]
        ],
        "digest": scan["digest"],
        "counts": counts,
    }
