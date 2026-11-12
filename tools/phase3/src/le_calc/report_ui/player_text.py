"""D4: shared player-text sanitizer for source drawer values."""

from __future__ import annotations

import re

_ENGINEERING_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bnode:[A-Za-z0-9_:]+"), "技能节点"),
    (re.compile(r"\bproperty\s*[=:]\s*[-+]?\d+(?:\.\d+)?" , re.I), "属性"),
    (re.compile(r"\bproperty\s+[-+]?\d+(?:\.\d+)?" , re.I), "属性"),
    (re.compile(r"\bconfirmed\b", re.I), "已计入"),
    (re.compile(r"\bevidence(?:_refs|_id)?\b", re.I), ""),
    (re.compile(r"\b(?:stat_key|source_path|contribution_id|raw_text|source_label|value_source|included_in_baseline)\b\s*[:=]?", re.I), ""),
    (re.compile(r"\bmod:[A-Za-z0-9_.#:-]+"), "词条"),
    (re.compile(r"\b(?:data|dataset|snapshot)\.[A-Za-z0-9_.]+"), "数据集"),
    (re.compile(r"\+\s*-(\d)"), r"±\1"),
    (re.compile(r"[ \t]{2,}"), " "),
)
_ENGLISH_SENTENCE = re.compile(r"[A-Za-z]{4,}")
_JSON_LEAK = re.compile(r'[{}"]|=\s*[-+]?\d|:\s*(?:true|false|null)\b')
_COMMON_EXCLUSION = (
    (re.compile(r"utility/behaviour stat", re.I), "辅助/行为类属性，不直接参与本次伤害"),
    (re.compile(r"does not affect .* damage", re.I), "不影响本次伤害"),
    (re.compile(r"not simulated", re.I), "未模拟"),
    (re.compile(r"infinite mana", re.I), "按无限法力假设"),
)


def clean_engineering_text(value: object, *, max_length: int = 240) -> str:
    """Remove internal IDs / field dumps / raw engine phrases from player text."""

    if value is None:
        return ""
    text = str(value)
    for pattern, replacement in _COMMON_EXCLUSION:
        text = pattern.sub(replacement, text)
    for pattern, replacement in _ENGINEERING_SUBS:
        text = pattern.sub(replacement, text)
    text = text.strip(" \t，,;；:：")
    if _JSON_LEAK.search(text):
        return ""
    if len(text) > max_length:
        text = text[:max_length].rstrip() + "…"
    return text


def player_raw_text(value: object) -> str:
    """In-game affix/tooltip text is shown; raw evidence dumps are dropped."""

    text = clean_engineering_text(value)
    if not text:
        return ""
    if _JSON_LEAK.search(text):
        return ""
    # Long English-only engineering explanations are not player copy.
    if len(text) > 90 and _ENGLISH_SENTENCE.search(text) and not re.search(r"[\u4e00-\u9fff]", text):
        return ""
    return text


def player_reason(value: object) -> str:
    """Readable, localized reason for ``excluded`` source rows."""

    text = clean_engineering_text(value)
    if not text:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", text) and _ENGLISH_SENTENCE.search(text):
        return "不参与本次伤害模型"
    return text
