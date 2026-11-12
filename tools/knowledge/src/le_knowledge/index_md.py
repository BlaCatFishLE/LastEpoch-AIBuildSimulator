# -*- coding: utf-8 -*-
"""生成 ``knowledge/INDEX.generated.md``（生成物，禁止手改）。"""

from __future__ import annotations

import hashlib
from typing import Any

from .paths import INDEX_FORMAT

GENERATED_MARKER = "<!-- GENERATED: 本文件由脚本生成，请勿手工修改"
GENERATE_COMMAND = ".\\scripts\\le.ps1 knowledge rebuild"
MAX_CELL = 160


def _cell(value: Any, *, limit: int = MAX_CELL) -> str:
    text = "" if value is None else str(value)
    text = text.replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _dedupe(values: Any) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        text = str(value).strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _join(values: Any, *, limit: int = 4, separator: str = ", ") -> str:
    items = _dedupe(values)
    if not items:
        return "—"
    shown = items[:limit]
    suffix = f" +{len(items) - limit}" if len(items) > limit else ""
    return separator.join(shown) + suffix


def _rule_refs(entry: dict[str, Any]) -> str:
    refs = []
    for reference in entry.get("rule_refs") or []:
        kind = str(reference.get("kind") or "").strip()
        ref = str(reference.get("ref") or "").strip()
        if kind or ref:
            refs.append(f"{kind}:{ref}" if kind else ref)
    return _join(refs)


def render_index_md(corpus: dict[str, Any]) -> str:
    """渲染 Markdown 索引文本（同一内容指纹/条目集合 → 同一文本）。"""
    counts = corpus.get("counts") or {}
    digest = corpus.get("digest") or ""
    entries = corpus.get("entries") or []
    mechanisms = [entry for entry in entries if entry["entry_kind"] == "mechanism"]
    skills = [entry for entry in entries if entry["entry_kind"] == "skill_template"]

    lines: list[str] = []
    lines.append(f"{GENERATED_MARKER}；权威源是 JSON，重建命令见下表。 -->")
    lines.append("")
    lines.append("# knowledge 索引（生成物）")
    lines.append("")
    lines.append(
        "> **GENERATED** — 本文件由 `le.ps1 knowledge rebuild` 从 `knowledge/mechanisms/*.json`、"
        "`knowledge/skills/*.json` 生成，禁止手改；手工修改会在下次重建时被覆盖并告警。"
        "查询入口：`knowledge search` / `knowledge show`（详见 `knowledge/README.md`）。"
    )
    lines.append("")
    lines.append("| 项 | 值 |")
    lines.append("|---|---|")
    lines.append(f"| index_format | `{INDEX_FORMAT}` |")
    lines.append(f"| source_digest | `{digest}` |")
    lines.append(f"| 生成命令 | `{GENERATE_COMMAND}` |")
    lines.append(
        "| 内容文件 | "
        f"{int(counts.get('content_files') or 0)}（mechanisms {int(counts.get('mechanisms') or 0)} / "
        f"skills {int(counts.get('skill_templates') or 0)}）+ schema {int(counts.get('schema_files') or 0)} |"
    )
    lines.append(
        "| 条目 | "
        f"mechanisms {int(counts.get('mechanisms') or 0)} / skill_templates {int(counts.get('skill_templates') or 0)} |"
    )
    lines.append(
        "| 索引明细 | "
        f"selectors {int(counts.get('selectors') or 0)} / aliases {int(counts.get('aliases') or 0)} |"
    )
    lines.append("")

    lines.append("## 机制条目（knowledge/mechanisms）")
    lines.append("")
    if not mechanisms:
        lines.append("（尚无机制条目：`knowledge/mechanisms/*.json` 未发现内容）")
    else:
        lines.append(
            "| ID | 标题 | rev | evidence_state | implementation_state | 适用组件 | 命中来源（选择器） | 规则引用 | 来源文件 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for entry in mechanisms:
            title = entry.get("title") or {}
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(entry.get("entry_id")),
                        _cell(title.get("zh") or title.get("en")),
                        _cell(entry.get("revision")),
                        _cell(entry.get("evidence_state")),
                        _cell(entry.get("implementation_state")),
                        _cell(_join(entry.get("components"))),
                        _cell(_join([row["selector"] for row in entry.get("selectors") or []], limit=6)),
                        _cell(_rule_refs(entry)),
                        _cell(entry.get("source_path")),
                    ]
                )
                + " |"
            )
    lines.append("")

    lines.append("## 技能模板（knowledge/skills）")
    lines.append("")
    if not skills:
        lines.append("（尚无技能模板：`knowledge/skills/*.json` 未发现内容）")
    else:
        lines.append(
            "| ID | 标题 | ability_id | 组件 | 所需计算块 | 调度 driver | 关联机制 | 来源文件 |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for entry in skills:
            title = entry.get("title") or {}
            document = entry.get("document") or {}
            blocks = [str(block.get("block") or "") for block in document.get("calculation_blocks") or []]
            schedule = document.get("schedule") or {}
            component_kinds = [
                str(component.get("kind") or "") for component in document.get("components") or []
            ]
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(entry.get("entry_id")),
                        _cell(title.get("zh") or title.get("en")),
                        _cell(document.get("ability_id")),
                        _cell(_join(component_kinds)),
                        _cell(_join(blocks)),
                        _cell(schedule.get("driver")),
                        _cell(
                            _join(
                                [
                                    f"{mapping.get('mechanism_id')}"
                                    for mapping in document.get("mappings") or []
                                ]
                            )
                        ),
                        _cell(entry.get("source_path")),
                    ]
                )
                + " |"
            )
    lines.append("")

    unresolved_entries = [entry for entry in entries if entry.get("unresolved")]
    lines.append("## 未解决项（摘要）")
    lines.append("")
    if not unresolved_entries:
        lines.append("（当前条目未登记未解决项）")
    else:
        lines.append("| ID | evidence_state | 未解决项数 |")
        lines.append("|---|---|---|")
        for entry in unresolved_entries:
            lines.append(
                "| "
                + " | ".join(
                    [
                        _cell(entry.get("entry_id")),
                        _cell(entry.get("evidence_state")),
                        _cell(len(entry.get("unresolved") or [])),
                    ]
                )
                + " |"
            )
    lines.append("")

    warnings = corpus.get("warnings") or []
    lines.append("## 登记告警（重建时输出）")
    lines.append("")
    if not warnings:
        lines.append("（无）")
    else:
        for warning in warnings:
            lines.append(f"- `{_cell(warning.get('path'))}`：{_cell(warning.get('message'), limit=240)}")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(
        "本文件由脚本生成；详细条目内容用 `knowledge show --id <ID>` 获取，"
        "不要在此复制完整 JSON。"
    )
    lines.append("")
    return "\n".join(lines)


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
