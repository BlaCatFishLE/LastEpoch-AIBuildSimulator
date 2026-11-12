"""Standalone offline HTML visualisation of an imported BuildSpec revision.

The viewer renders **one** self-contained UTF-8 HTML document.  Everything the
page needs (styles, behaviour, embedded JSON) is inlined; there are no CDNs, no
web fonts and no network requests, so the file opens correctly from ``file://``
on an offline machine.

Since the 2026-11-03 review rework the default page shows **only** the four
business areas a player needs:

    构筑概览 / 装备 / 技能与被动 / 神像与祝福

Engineering traceability (revision ids, dataset fingerprints, encoded ids,
source paths, tier_index, ``r``/``ir``/``ur`` arrays, codec payloads, coverage
numbers and issues) lives in a *default-closed* ``诊断详情 / Diagnostics``
block at the bottom of the page, together with a raw-JSON export.  The default
view is therefore never a "database dump".

The equipment / idol / blessing cards consume the pre-computed player-readable
layer ``instance["readable"]`` (contract: ``docs/phase-2-rework-contract.zh-CN.md``
§2).  When a historical revision does not contain that layer yet, the viewer
degrades to the structured fields (``value.display_value`` etc.) and shows a
"历史 revision" banner.  A revision that still carries the old-scope weaver
tree is rendered with the weaver objects ignored, as required by the scope
change; the old-scope note is only visible inside diagnostics.

The page is rendered statically in Python.  A small vanilla-JS layer only adds
*interaction* (tabs, search filters, severity filter, raw-JSON download, print)
and never changes the meaning of the already-rendered content.  Missing data
degrades to an explicit ``未提供`` state instead of raising.

Entry point::

    render_build_html(spec, revision_id=..., build_dir=...)

``build_dir`` is the immutable revision directory (``.../revisions/<rev>``).
``import-report.json`` is read from it *if present*; the page never requires it.
"""

from __future__ import annotations

import html
import json
import pathlib
import re
from typing import Any

__all__ = ["render_build_html"]


# ---------------------------------------------------------------------------
# small generic helpers
# ---------------------------------------------------------------------------
def _d(value: Any) -> dict:
    """Return ``value`` when it is a dict, else an empty dict."""
    return value if isinstance(value, dict) else {}


def _l(value: Any) -> list:
    """Return ``value`` when it is a list, else an empty list."""
    return value if isinstance(value, list) else []


def _g(obj: Any, *path: Any, default: Any = None) -> Any:
    """Small safe-getter for nested dict/list structures."""
    current = obj
    for key in path:
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and isinstance(key, int) and -len(current) <= key < len(current):
            current = current[key]
        else:
            return default
        if current is None:
            return default
    return current


def _esc(value: Any) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _num(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return format(value, ".10g")
    if value is None:
        return "—"
    return str(value)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _badge(text: Any, tone: str = "neutral", title: Any = None) -> str:
    safe_tone = re.sub(r"[^a-z0-9_-]", "", str(tone).lower()) or "neutral"
    title_attr = f' title="{_esc(title)}"' if title is not None else ""
    return f'<span class="badge badge-{safe_tone}"{title_attr}>{_esc(text)}</span>'


# ---------------------------------------------------------------------------
# domain labels
# ---------------------------------------------------------------------------
SLOT_LABELS = {
    "head": "头部",
    "chest": "胸甲",
    "weapon1": "主手武器",
    "weapon2": "副手 / 双持",
    "hands": "手套",
    "waist": "腰带",
    "feet": "鞋子",
    "ring1": "戒指 1",
    "ring2": "戒指 2",
    "amulet": "项链",
    "relic": "遗物",
    "idol_altar": "神像祭坛",
}

# (readable section, group key, group title, tone)
SECTION_GROUPS = (
    ("implicit", "implicit", "固有属性", "implicit"),
    ("affix_normal", "normal", "普通词缀", "normal"),
    ("affix_sealed", "sealed", "封印词缀", "sealed"),
    ("affix_primordial", "primordial", "原初词缀", "primordial"),
    ("affix_corrupted", "corrupted", "腐化词缀", "corrupted"),
    ("unique_mod", "unique", "暗金词条", "unique"),
    ("unique_description", "unique", "暗金词条", "unique"),
)
_SECTION_TO_GROUP = {section: (group, title, tone) for section, group, title, tone in SECTION_GROUPS}
_GROUP_ORDER = ("implicit", "normal", "sealed", "primordial", "corrupted", "unique")

RARITY_LABELS = {
    "unique": "暗金",
    "set": "套装",
    "exalted": "崇高",
    "legendary": "传奇",
    "base": "基底",
    "magic": "魔法",
    "rare": "稀有",
}
# ``readable.rarity.confidence`` values that justify showing a rarity badge.
RARITY_CONFIRMED = {
    "definition",
    "catalog",
    "source",
    "source_authoritative",
    "resolved",
    "verified",
    "confirmed",
    "authoritative",
}

_CHANNEL_ORDER = ("normal", "sealed", "primordial", "corrupted")


# ---------------------------------------------------------------------------
# fallback value/line formatting (historical revisions without ``readable``)
# ---------------------------------------------------------------------------
def _trim_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _text(value)
    if abs(number - round(number)) <= 1e-9:
        return str(int(round(number)))
    return format(number, ".4g")


def _fmt_unit(value: Any, unit: Any) -> str:
    unit_text = str(unit or "").strip().lower()
    if value is None:
        return ""
    if unit_text in ("percent", "percentage", "%"):
        try:
            return f"{_trim_number(float(value) * 100)}%"
        except (TypeError, ValueError):
            return _text(value)
    if unit_text in ("flat", "flat_int", "int", "integer", ""):
        return _trim_number(value)
    return f"{_trim_number(value)} {_text(unit)}"


def _fmt_range(low: Any, high: Any, unit: Any) -> str:
    if low is None and high is None:
        return ""
    if low is None:
        return f"≤ {_fmt_unit(high, unit)}"
    if high is None:
        return f"≥ {_fmt_unit(low, unit)}"
    return f"{_fmt_unit(low, unit)}–{_fmt_unit(high, unit)}"


def _strip_sign(value: Any) -> tuple[str, bool]:
    text = _text(value)
    negative = text.startswith("-")
    return (text[1:] if negative or text.startswith("+") else text, negative)


def _fallback_modifier_type(value: dict) -> int:
    try:
        return int(value.get("modifier_type") or 0)
    except (TypeError, ValueError):
        return 0


def _fallback_text(value: dict, status: str) -> str:
    """Compose a player-oriented text from structured value fields.

    Only used for historical revisions that predate the ``readable`` layer.
    """
    stat = _text(value.get("stat_name")) or _text(value.get("stat_id")) or "未知属性"
    modifier_type = _fallback_modifier_type(value)
    unit = value.get("unit")
    numeric = value.get("value_numeric")
    display = value.get("display_value")
    if status in ("actual", "constant"):
        if display and str(unit or "").strip().lower() in ("percent", "percentage", "%"):
            # Historical ``display_value`` sometimes carries the normalized ratio
            # (0.27) instead of the player-facing percentage (27%); normalise it here
            # without touching the stored value.
            shown = _text(display)
            if not shown.endswith("%"):
                try:
                    display = _fmt_unit(float(shown), unit)
                except (TypeError, ValueError):
                    pass
        if not display:
            low, high = value.get("roll_min"), value.get("roll_max")
            if numeric is not None:
                display = _fmt_unit(numeric, unit)
            elif low is not None and high is not None and low == high:
                display = _fmt_unit(low, unit)
        if display:
            number, negative = _strip_sign(display)
            if modifier_type == 1:
                return f"{number} {'reduced' if negative else 'increased'} {stat}"
            if modifier_type == 2:
                return f"{number} {'less' if negative else 'more'} {stat}"
            if modifier_type == 0:
                if negative:
                    return f"-{number} {stat}"
                return f"+{number} {stat}"
            return f"{number} {stat}"
        return stat
    # range / not_provided / unresolved: text without a fabricated roll value
    if modifier_type == 1:
        return f"increased {stat}"
    if modifier_type == 2:
        return f"more {stat}"
    return stat


def _fallback_status(block: dict, value: dict) -> str:
    if str(value.get("status") or "").strip().lower() in ("unresolved", "invalid"):
        return "unresolved"
    if block.get("raw_roll") is not None:
        return "actual"
    low, high = value.get("roll_min"), value.get("roll_max")
    if low is None and high is None:
        return "not_provided"
    try:
        if low is not None and high is not None and abs(float(low) - float(high)) <= 1e-9:
            return "constant"
    except (TypeError, ValueError):
        pass
    return "range"


def _fallback_diagnostics(instance: dict, block: dict, value: dict, *, source_path: Any, encoded_id: Any, channel: Any = None, extra: dict | None = None) -> dict:
    diagnostics = {
        "stat_id": value.get("stat_id"),
        "stat_name": value.get("stat_name"),
        "property": value.get("property"),
        "tags": value.get("tags"),
        "special_tag": value.get("special_tag"),
        "modifier_type": value.get("modifier_type"),
        "roll_id": _g(block, "evidence", "roll_id"),
        "raw_roll": block.get("raw_roll"),
        "roll_status": block.get("roll_status"),
        "value_numeric": value.get("value_numeric"),
        "affix_entity_id": _g(instance, "affixes", channel, 0, "affix", "ref", "entity_id") if channel else None,
        "channel": channel,
        "encoded_id": encoded_id,
        "source_path": source_path,
    }
    if extra:
        diagnostics.update(extra)
    return diagnostics


def _fallback_readable(instance: dict) -> dict:
    """Build a readable-like block from structured fields (historical fallback)."""
    definition = _d(instance.get("definition"))
    base = _d(instance.get("base"))
    display_name = _text(definition.get("name")) or _text(base.get("name")) or "未提供物品名称"
    base_name = _text(base.get("name"))
    if base_name == display_name:
        base_name = ""
    lines: list[dict] = []
    warnings: list[str] = []

    # implicit
    implicit = _d(instance.get("implicit_rolls"))
    for index, raw_block in enumerate(_l(implicit.get("values"))):
        block = _d(raw_block)
        values = _l(block.get("values"))
        for value_index, raw_value in enumerate(values):
            value = _d(raw_value)
            status = _fallback_status(block, value)
            lines.append(
                {
                    "section": "implicit",
                    "kind": "mod",
                    "text": _fallback_text(value, status),
                    "tier_label": None,
                    "value_status": status,
                    "value_text": _fmt_unit(value.get("value_numeric"), value.get("unit")),
                    "range_text": _fmt_range(value.get("roll_min"), value.get("roll_max"), value.get("unit")),
                    "hidden": False,
                    "diagnostics": _fallback_diagnostics(
                        instance,
                        block,
                        value,
                        source_path=block.get("source_path") or implicit.get("source_path"),
                        encoded_id=instance.get("encoded_id"),
                        extra={"implicit_index": index, "value_index": value_index},
                    ),
                }
            )

    # affixes (four channels)
    affixes = _d(instance.get("affixes"))
    for channel in _CHANNEL_ORDER:
        section = f"affix_{channel}"
        for affix_index, raw_affix in enumerate(_l(affixes.get(channel))):
            affix = _d(raw_affix)
            block = _d(affix.get("value"))
            tier = affix.get("source_tier")
            tier_label = f"T{tier}" if isinstance(tier, int) else None
            affix_entity_id = _g(affix, "affix", "ref", "entity_id")
            values = _l(block.get("values"))
            if not values:
                lines.append(
                    {
                        "section": section,
                        "kind": "mod",
                        "text": _text(_g(affix, "affix", "ref", "name"), "未解析词缀"),
                        "tier_label": tier_label,
                        "value_status": "unresolved",
                        "value_text": None,
                        "range_text": None,
                        "hidden": False,
                        "diagnostics": {
                            "channel": channel,
                            "encoded_id": affix.get("encoded_id"),
                            "source_path": affix.get("source_path"),
                            "roll_status": _g(affix, "roll", "status"),
                            "affix_entity_id": affix_entity_id,
                            "affix_index": affix_index,
                            "tier_index": _g(affix, "tier", "tier_index"),
                        },
                    }
                )
                warnings.append(f"{channel} 词缀缺少可用数值定义")
                continue
            for value_index, raw_value in enumerate(values):
                value = _d(raw_value)
                status = _fallback_status(block, value)
                diagnostics = _fallback_diagnostics(
                    instance,
                    block,
                    value,
                    source_path=affix.get("source_path"),
                    encoded_id=affix.get("encoded_id"),
                    channel=channel,
                    extra={
                        "affix_index": affix_index,
                        "value_index": value_index,
                        "affix_entity_id": affix_entity_id,
                        "tier_index": _g(affix, "tier", "tier_index"),
                    },
                )
                lines.append(
                    {
                        "section": section,
                        "kind": "mod",
                        "text": _fallback_text(value, status),
                        "tier_label": tier_label,
                        "value_status": status,
                        "value_text": _fmt_unit(value.get("value_numeric"), value.get("unit")),
                        "range_text": _fmt_range(value.get("roll_min"), value.get("roll_max"), value.get("unit")),
                        "hidden": False,
                        "diagnostics": diagnostics,
                    }
                )

    # unique rolls
    unique = _d(instance.get("unique_rolls"))
    for index, raw_block in enumerate(_l(unique.get("values"))):
        block = _d(raw_block)
        values = _l(block.get("values"))
        for value_index, raw_value in enumerate(values):
            value = _d(raw_value)
            status = _fallback_status(block, value)
            lines.append(
                {
                    "section": "unique_mod",
                    "kind": "mod",
                    "text": _fallback_text(value, status),
                    "tier_label": None,
                    "value_status": status,
                    "value_text": _fmt_unit(value.get("value_numeric"), value.get("unit")),
                    "range_text": _fmt_range(value.get("roll_min"), value.get("roll_max"), value.get("unit")),
                    "hidden": False,
                    "diagnostics": _fallback_diagnostics(
                        instance,
                        block,
                        value,
                        source_path=unique.get("source_path"),
                        encoded_id=instance.get("encoded_id"),
                        extra={"unique_index": index, "value_index": value_index},
                    ),
                }
            )

    statuses = [line["value_status"] for line in lines]
    if not lines:
        completeness = "unavailable"
    elif all(status in ("actual", "constant") for status in statuses):
        completeness = "complete"
    elif all(status in ("range", "constant", "not_provided") for status in statuses):
        completeness = "definition_only"
    else:
        completeness = "partial"
    if statuses:
        warnings.append("历史版本暂无可读词条层：词条文本由结构化字段降级生成。")
    return {
        "display_name": display_name,
        "base_name": base_name,
        "item_type": "",
        "rarity": None,
        "completeness": completeness,
        "lines": lines,
        "warnings": warnings,
        "source": "fallback",
    }


# ---------------------------------------------------------------------------
# readable layer normalisation
# ---------------------------------------------------------------------------
_STATUS_ALIASES = {
    "resolved": "actual",
    "present": "actual",
    "computed": "actual",
    "missing": "not_provided",
    "absent": "not_provided",
    "null": "not_provided",
    "empty": "not_provided",
}


def _norm_value_status(status: Any) -> str:
    key = str(status or "").strip().lower()
    key = _STATUS_ALIASES.get(key, key)
    return key or "actual"


def _norm_section(section: Any) -> str:
    key = str(section or "").strip().lower()
    if key in ("implicit", "unique_mod", "unique_description"):
        return key
    if key in _CHANNEL_ORDER:
        return f"affix_{key}"
    if key.startswith("affix_"):
        channel = key[len("affix_"):]
        if channel in _CHANNEL_ORDER:
            return key
        return "affix_normal"
    if "unique" in key and "desc" in key:
        return "unique_description"
    if "unique" in key:
        return "unique_mod"
    if "implicit" in key:
        return "implicit"
    if "corrupt" in key:
        return "affix_corrupted"
    if "primordial" in key:
        return "affix_primordial"
    if "seal" in key:
        return "affix_sealed"
    return "affix_normal"


def _norm_line(raw: Any) -> dict | None:
    line = _d(raw)
    if not line:
        return None
    diagnostics = _d(line.get("diagnostics")) or _d(line.get("diag"))
    return {
        "section": _norm_section(line.get("section") or line.get("channel")),
        "kind": _text(line.get("kind"), "mod"),
        "text": line.get("text") if isinstance(line.get("text"), str) else None,
        "tier_label": line.get("tier_label") if line.get("tier_label") is not None else line.get("tier"),
        "value_status": _norm_value_status(line.get("value_status") if line.get("value_status") is not None else line.get("status")),
        "value_text": line.get("value_text"),
        "range_text": line.get("range_text") if line.get("range_text") is not None else line.get("range"),
        "hidden": bool(line.get("hidden")),
        "diagnostics": diagnostics,
    }


def _readable_for(instance: dict) -> dict:
    """Return the normalised player-readable block for one item instance."""
    raw = _d(instance.get("readable"))
    if raw and isinstance(raw.get("lines"), list):
        lines = [line for line in (_norm_line(item) for item in _l(raw.get("lines"))) if line]
        definition = _d(instance.get("definition"))
        base = _d(instance.get("base"))
        display_name = _text(raw.get("display_name")) or _text(definition.get("name")) or _text(base.get("name")) or "未提供物品名称"
        base_name = _text(raw.get("base_name")) or _text(base.get("name"))
        if base_name == display_name:
            base_name = ""
        if not lines and _text(raw.get("completeness")).lower() == "unavailable":
            # The readable layer failed for this instance: degrade to structured fields
            # instead of silently showing an empty item card.
            fallback = _fallback_readable(instance)
            if fallback.get("lines"):
                fallback["display_name"] = display_name
                fallback["base_name"] = base_name
                fallback["item_type"] = _text(raw.get("item_type"))
                fallback["rarity"] = _d(raw.get("rarity")) or None
                return fallback
        return {
            "display_name": display_name,
            "base_name": base_name,
            "item_type": _text(raw.get("item_type")),
            "rarity": _d(raw.get("rarity")) or None,
            "completeness": _text(raw.get("completeness")),
            "lines": lines,
            "warnings": [str(item) for item in _l(raw.get("warnings")) if item],
            "source": "readable",
        }
    return _fallback_readable(instance)


def _rarity_badge(readable: dict) -> str:
    rarity = _d(readable.get("rarity"))
    if not rarity:
        return ""
    confidence = str(rarity.get("confidence") or "").strip().lower()
    if confidence not in RARITY_CONFIRMED:
        # The final player-visible rarity cannot be proven: never fake a badge.
        return ""
    code = str(rarity.get("code") or "").strip().lower()
    label = _text(rarity.get("label_zh")) or RARITY_LABELS.get(code) or _text(rarity.get("label_en")) or _text(code)
    if not label:
        return ""
    tone = {"unique": "unique", "set": "set", "exalted": "exalted", "legendary": "unique", "magic": "blue", "rare": "gold"}.get(code, "neutral")
    return _badge(label, tone)


# ---------------------------------------------------------------------------
# item rendering (player-readable lines)
# ---------------------------------------------------------------------------
_STATUS_MARKER = {
    "range": "数值未提供",
    "not_provided": "数值未提供",
    "unresolved": "词条数值未解析",
}


def _line_display_text(line: dict) -> str:
    """Line text with an absent roll expressed as range instead of a fake value.

    For plain mod lines the readable layer embeds the declared range as ``(X–Y)``;
    that range is folded into the explicit ``数值未提供（显示范围 X–Y）`` marker so
    it is neither duplicated nor mistaken for an actual roll.  Combined unique
    descriptions keep their sentence verbatim (the substituted range is part of
    the sentence) and only receive the additional marker.
    """
    text = _text(line.get("text"))
    range_text = _text(line.get("range_text"))
    if (
        line["value_status"] == "range"
        and range_text
        and line.get("kind") != "description"
    ):
        parenthesized = f"({range_text})"
        if parenthesized in text:
            text = text.replace(parenthesized, "", 1).strip()
            # Tidy templates where the value sat before a connector or a bare sign,
            # e.g. "+ (1–2) to Level of Skills" -> "Level of Skills",
            # "+ (1%–45%) Physical Resistance" -> "Physical Resistance".
            text = re.sub(r"^[+\-]\s*(?:to|of)\s+", "", text).strip()
            text = re.sub(r"^[+\-]\s+", "", text).strip()
            text = re.sub(r"\s{2,}", " ", text)
        elif range_text in text:
            text = text.replace(range_text, "", 1).strip()
            text = re.sub(r"\s{2,}", " ", text)
    if not text:
        text = _text(line.get("value_text"))
    return text


def _line_marker(line: dict) -> str:
    status = line["value_status"]
    range_text = _text(line.get("range_text"))
    if status == "range":
        if range_text:
            return f"数值未提供（显示范围 {range_text}）"
        return "数值未提供"
    if status == "not_provided":
        if range_text:
            return f"数值未提供（显示范围 {range_text}）"
        return "数值未提供"
    if status == "unresolved":
        if line.get("kind") == "description":
            return "词条文本未提供"
        return "词条数值未解析"
    return ""


def _tier_badge(tier_label: Any) -> str:
    if tier_label is None:
        return ""
    label = _text(tier_label)
    if not label:
        return ""
    if re.fullmatch(r"\d+", label):
        label = f"T{label}"
    if re.fullmatch(r"[Tt]\d+", label):
        label = label.upper()
    return f'<span class="tier-badge">{_esc(label)}</span>'


def _line_html(line: dict) -> str:
    text = _line_display_text(line)
    marker = _line_marker(line)
    if not text and not marker:
        text = "（词条文本未提供）"
    parts = [f'<span class="line-text">{_esc(text)}</span>']
    tier_badge = _tier_badge(line.get("tier_label"))
    if tier_badge:
        parts.append(tier_badge)
    if line["kind"] == "description":
        parts.append('<span class="line-flag">组合描述</span>')
    diagnostics = _d(line.get("diagnostics"))
    value_source = _text(diagnostics.get("value_source"))
    if value_source.startswith("local_"):
        input_id = _text(diagnostics.get("input_id"))
        title = f"本地手动输入（input_id: {input_id}）" if input_id else "本地手动输入"
        parts.append(f'<span class="badge badge-manual" title="{_esc(title)}">手动</span>')
    if marker:
        parts.append(f'<span class="line-missing">{_esc(marker)}</span>')
    classes = ["line", f"line-{line['value_status']}"]
    if line["kind"] == "description":
        classes.append("line-description")
    return (
        f'<li class="{" ".join(classes)}" data-section="{_esc(line["section"])}" '
        f'data-status="{_esc(line["value_status"])}" data-kind="{_esc(line["kind"])}">'
        + "".join(parts)
        + "</li>"
    )


def _visible_lines(readable: dict) -> list[dict]:
    return [
        line
        for line in _l(readable.get("lines"))
        if isinstance(line, dict) and not line.get("hidden")
    ]


# Property id of the drop-rate stat group in the source catalog.  Display filtering
# must use this source identity (or the explicit ``non_dps`` flag), never text matching.
_NON_DPS_PROPERTY_IDS = frozenset({104})


def _is_non_dps_line(line: Any) -> bool:
    """True for non-DPS / drop-rate display lines that are hidden from the player view.

    Filtering is based on the source property identity: the explicit
    ``diagnostics.non_dps`` flag (new revisions) or ``diagnostics.property == 104``
    (property identity; also covers historical revisions imported before the flag
    existed).  The structured BuildSpec / diagnostics keep the full line either way.
    """
    diagnostics = _d(_d(line).get("diagnostics"))
    if diagnostics.get("non_dps") is True:
        return True
    try:
        return int(diagnostics.get("property")) in _NON_DPS_PROPERTY_IDS
    except (TypeError, ValueError):
        return False


def _business_lines(readable: dict) -> list[dict]:
    """Visible player lines minus non-DPS/drop-rate lines (default BD display)."""
    return [line for line in _visible_lines(readable) if not _is_non_dps_line(line)]


def _grouped_lines_html(readable: dict) -> str:
    lines = _business_lines(readable)
    if not lines:
        return '<p class="item-empty muted">该物品没有词条记录。</p>'
    grouped: dict[str, list[dict]] = {}
    for line in lines:
        section = str(line.get("section") or "affix_normal")
        group, _title, _tone = _SECTION_TO_GROUP.get(section, ("normal", "词条", "normal"))
        grouped.setdefault(group, []).append(line)
    rendered: list[str] = []
    for group in _GROUP_ORDER:
        entries = grouped.get(group)
        if not entries:
            continue
        title = next(title for section, g, title, tone in SECTION_GROUPS if g == group)
        tone = next(tone for section, g, title, tone in SECTION_GROUPS if g == group)
        rendered.append(
            f'<section class="line-group group-{_esc(tone)}">'
            f'<h4 class="group-title">{_esc(title)}<span class="group-count">{len(entries)}</span></h4>'
            f'<ul class="lines">{"".join(_line_html(line) for line in entries)}</ul></section>'
        )
    return "".join(rendered)


def _item_card(
    instance: Any,
    *,
    slot_label: str,
    position_label: Any = None,
    kind: str = "equipment",
    hint: Any = None,
) -> str:
    item = _d(instance)
    if not item:
        return (
            f'<article class="item-card item-{_esc(kind)} empty-state">'
            f'<div class="item-head"><span class="slot-cn">{_esc(slot_label)}</span></div>'
            f'<p class="item-empty muted">未装备 / 来源未设置</p></article>'
        )
    readable = _readable_for(item)
    rarity_badge = _rarity_badge(readable)
    display_name = readable.get("display_name") or "未提供物品名称"
    base_name = readable.get("base_name") or ""
    item_type = readable.get("item_type") or ""
    base_line = ""
    if base_name:
        base_line = f'<span class="base-label">基底</span> {_esc(base_name)}'
        if item_type:
            base_line += f' <span class="type-sep">·</span> {_esc(item_type)}'
    elif item_type:
        base_line = _esc(item_type)
    head = [f'<span class="slot-cn">{_esc(slot_label)}</span>']
    if position_label is not None:
        head.append(f'<span class="position-badge">{_esc(position_label)}</span>')
    if rarity_badge:
        head.append(rarity_badge)
    warnings = _l(readable.get("warnings"))
    notes: list[str] = []
    if readable.get("source") == "fallback":
        notes.append("历史版本：词条由结构化字段降级显示。")
    if readable.get("source") == "readable" and readable.get("completeness") == "partial" and warnings:
        notes.append(f"有 {len(warnings)} 处词条未能完整还原，详见诊断详情。")
    not_provided = sum(1 for line in _business_lines(readable) if line.get("value_status") in ("range", "not_provided"))
    note_html = ""
    if notes:
        note_html = '<p class="item-note muted">' + " ".join(_esc(note) for note in notes) + "</p>"
    hint_html = f'<p class="item-hint muted">{_esc(hint)}</p>' if hint else ""
    return (
        f'<article class="item-card item-{_esc(kind)}">'
        f'<div class="item-head">{"".join(head)}</div>'
        f'<h3 class="item-name">{_esc(display_name)}</h3>'
        + (f'<p class="item-base">{base_line}</p>' if base_line else "")
        + hint_html
        + _grouped_lines_html(readable)
        + note_html
        + "</article>"
    )


def _absent_slot_card(slot_key: str) -> str:
    label = SLOT_LABELS.get(slot_key, slot_key)
    return (
        f'<article class="item-card item-equipment absent-card">'
        f'<div class="item-head"><span class="slot-cn">{_esc(label)}</span></div>'
        f'<p class="item-empty muted">未装备 / 来源未设置</p></article>'
    )


# ---------------------------------------------------------------------------
# in-scope item enumeration / counts
# ---------------------------------------------------------------------------
def _in_scope_instances(spec: dict) -> list[dict]:
    instances: list[dict] = []
    equipment = _d(spec.get("equipment"))
    for entry in _l(equipment.get("slots")):
        instance = _d(_d(entry).get("instance"))
        if instance:
            instances.append(instance)
    for entry in _l(_d(spec.get("idols")).get("entries")):
        instance = _d(_d(entry).get("instance"))
        if instance:
            instances.append(instance)
    for entry in _l(_d(spec.get("blessings")).get("entries")):
        instance = _d(_d(entry).get("instance"))
        if instance:
            instances.append(instance)
    return instances


def _instance_affix_total(instance: dict) -> int:
    total = instance.get("affix_total")
    if isinstance(total, int):
        return total
    affixes = _d(instance.get("affixes"))
    return sum(len(_l(entries)) for entries in affixes.values())


def _affix_missing_roll(affix: dict) -> bool:
    roll = _d(affix.get("roll"))
    if roll.get("raw") is not None:
        return False
    return str(roll.get("status") or "").strip().lower() in ("", "absent", "null", "none")


def _revision_mode(spec: dict) -> dict:
    scope = _d(spec.get("scope"))
    weaver = _d(spec.get("weaver"))
    instances = _in_scope_instances(spec)
    readable_items = 0
    for instance in instances:
        readable = _d(instance.get("readable"))
        if readable and (isinstance(readable.get("lines"), list) or readable.get("completeness")):
            readable_items += 1
    return {
        "has_scope": bool(scope),
        "readable_items": readable_items,
        "total_items": len(instances),
        "historical": not bool(scope),
        "weaver_nodes": len(_l(weaver.get("nodes"))),
        "weaver_node_entries": weaver.get("node_entries"),
        "weaver_containers": len(_l(weaver.get("containers"))),
        "weaver_status": _text(weaver.get("status")),
        "weaver_present": bool(_l(weaver.get("nodes")) or _l(weaver.get("containers"))),
    }


def _counts(spec: dict) -> dict:
    equipment = _d(spec.get("equipment"))
    equipment_instances = [_d(_d(entry).get("instance")) for entry in _l(equipment.get("slots"))]
    equipment_instances = [instance for instance in equipment_instances if instance]
    idols = [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("idols")).get("entries"))]
    idols = [instance for instance in idols if instance]
    blessings = [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("blessings")).get("entries"))]
    blessings = [instance for instance in blessings if instance]
    instances = equipment_instances + idols + blessings

    channel_counts = {channel: 0 for channel in _CHANNEL_ORDER}
    affix_total = 0
    affixes_ok = 0
    absent_rolls = 0
    for instance in instances:
        affix_total += _instance_affix_total(instance)
        affixes = _d(instance.get("affixes"))
        for channel in _CHANNEL_ORDER:
            for raw_affix in _l(affixes.get(channel)):
                affix = _d(raw_affix)
                channel_counts[channel] += 1
                if _g(affix, "affix", "ref"):
                    affixes_ok += 1
                if _affix_missing_roll(affix):
                    absent_rolls += 1

    items_ok = 0
    for instance in instances:
        if _d(instance.get("readable")):
            items_ok += 1
        elif instance.get("definition_status") == "resolved" or instance.get("base_status") == "resolved":
            items_ok += 1

    passive = _d(spec.get("passive_tree"))
    passive_nodes = _l(passive.get("nodes"))
    specializations = _l(spec.get("specializations"))
    skill_nodes = sum(len(_l(_d(entry).get("nodes"))) for entry in specializations)
    skill_points = sum(
        _d(entry).get("points_total") for entry in specializations if isinstance(_d(entry).get("points_total"), int)
    )
    passive_points = passive.get("points_total")
    if not isinstance(passive_points, int):
        passive_points = sum(_d(node).get("points") or 0 for node in passive_nodes if isinstance(_d(node).get("points"), int))
    action_bar = _l(_d(spec.get("action_bar")).get("slots"))
    issues = _l(spec.get("issues"))

    return {
        "equipment": len(equipment_instances),
        "idols": len(idols),
        "blessings": len(blessings),
        "items_total": len(instances),
        "items_ok": items_ok,
        "affix_total": affix_total,
        "affixes_ok": affixes_ok,
        "affixes_missing_identity": max(0, affix_total - affixes_ok),
        "absent_rolls": absent_rolls,
        "channel_counts": channel_counts,
        "passive_nodes": len(passive_nodes),
        "passive_points": passive_points,
        "specializations": len(specializations),
        "skill_nodes": skill_nodes,
        "skill_points": skill_points,
        "action_bar": len(action_bar),
        "issues": len(issues),
        "absent_slots": [str(item) for item in _l(equipment.get("absent_slots"))],
    }


# ---------------------------------------------------------------------------
# local missing-value inputs (build-local-inputs-v1, variable granularity)
# ---------------------------------------------------------------------------
LOCAL_INPUTS_VERSION = "build-local-inputs-v1"
_CHANNEL_SHORT = {"normal": "普通", "sealed": "封印", "primordial": "原初", "corrupted": "腐化"}


def _labelled_instances(spec: dict) -> list[tuple[str, str, dict]]:
    """Return ``(area, area_label, instance)`` for every in-scope item instance."""
    pairs: list[tuple[str, str, dict]] = []
    equipment = _d(spec.get("equipment"))
    for entry in _l(equipment.get("slots")):
        entry_d = _d(entry)
        instance = _d(entry_d.get("instance"))
        if not instance:
            continue
        slot = str(entry_d.get("slot") or "")
        pairs.append(("装备", SLOT_LABELS.get(slot, slot), instance))
    for index, entry in enumerate(_l(_d(spec.get("idols")).get("entries"))):
        instance = _d(_d(entry).get("instance"))
        if instance:
            pairs.append(("神像", f"神像 {index + 1}", instance))
    for index, entry in enumerate(_l(_d(spec.get("blessings")).get("entries"))):
        entry_d = _d(entry)
        instance = _d(entry_d.get("instance"))
        if not instance:
            continue
        timeline = entry_d.get("timeline")
        if timeline is None:
            timeline = _d(_d(instance).get("position")).get("timeline")
        label = f"祝福 · 时间线 {_num(timeline)}" if timeline is not None else f"祝福 {index + 1}"
        pairs.append(("祝福", label, instance))
    return pairs


def _display_value_stats(value_entry: dict, line: Any) -> dict:
    """Unit / percent / range metadata for one displayed variable.

    Mirrors ``le_build.inputs._value_display_stats`` so the browser can replicate
    the CLI-side conversion (percent /100) without querying Python at view time.
    """
    line_d = _d(line)
    text = _text(line_d.get("text"))
    range_text = _text(line_d.get("range_text"))
    unit = value_entry.get("unit")
    try:
        modifier_type = int(value_entry.get("modifier_type") or 0)
    except (TypeError, ValueError):
        modifier_type = 0
    percent = unit == "percent" or modifier_type != 0 or "%" in text or "%" in range_text
    return {
        "unit": "percent" if percent else (_text(unit) or "flat"),
        "modifier_type": modifier_type,
        "percent": bool(percent),
        "range_text": range_text or None,
        "roll_min": value_entry.get("roll_min"),
        "roll_max": value_entry.get("roll_max"),
        "stat_name": value_entry.get("stat_name"),
    }


def _display_bounds(low: Any, high: Any, percent: bool) -> tuple[float | None, float | None]:
    """Convert a normalized definition range to the player-facing display range."""

    def convert(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number * 100.0 if percent else number

    return convert(low), convert(high)


def _variable_label(line: Any, fallback: Any = None) -> str:
    line_d = _d(line)
    normalized = {
        "kind": _text(line_d.get("kind")) or "mod",
        "text": line_d.get("text"),
        "value_status": _text(line_d.get("value_status")),
        "range_text": line_d.get("range_text"),
        "value_text": line_d.get("value_text"),
    }
    label = _line_display_text(normalized) if normalized["text"] else ""
    return label or _text(fallback) or "未命名词条"


def _format_suggestion(display_max: Any) -> str | None:
    """Format the declared maximum in display units (e.g. 8, 45, 200)."""
    try:
        number = float(display_max)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    text = f"{round(number, 4):g}"
    return text or None


def _variable_hint(percent: bool, display_max: Any = None) -> str:
    """Hint text for one variable row; the suggested value is the range maximum."""
    suggestion = _format_suggestion(display_max)
    if percent:
        if suggestion:
            return f"按面板显示值填写，建议填范围最高值 {suggestion}（% 可省略）"
        return "按面板显示值填写（% 可省略）"
    if suggestion:
        return f"按面板显示值填写，建议填范围最高值 {suggestion}"
    return "按面板显示值填写"


def _explicit_locator(variable: dict) -> dict:
    locator: dict = {}
    for key in ("mod_index", "affix_value_index", "index"):
        if variable.get(key) is not None:
            locator[key] = variable[key]
    return locator


def _variable_input_groups(spec: dict) -> dict:
    """Derive per-roll-slot groups with one input row per displayed variable.

    Source-only derivation from ``build.json`` — a roll slot with several displayed
    variables (e.g. Omnis rollId 0 = flat Level of Skills + percent Physical
    Resistance) becomes one group with one row per variable.  Constant/degenerate
    values (``value_numeric`` already known, e.g. the hands 1% affixes) are skipped.
    """
    groups: list[dict] = []
    for area, area_label, instance in _labelled_instances(spec):
        readable = _readable_for(instance)
        lines = _l(readable.get("lines"))
        item_label = _text(readable.get("display_name")) or area_label

        affix_line_groups: dict[tuple, list[dict]] = {}
        for line in lines:
            section = str(line.get("section") or "")
            if not section.startswith("affix_"):
                continue
            diagnostics = _d(line.get("diagnostics"))
            affix_line_groups.setdefault((section, diagnostics.get("encoded_id")), []).append(line)
        affix_line_cursor: dict[tuple, int] = {}

        # --- missing affix rolls: one row per unresolved value variable ----
        affixes = _d(instance.get("affixes"))
        for channel in _CHANNEL_ORDER:
            for raw_affix in _l(affixes.get(channel)):
                affix = _d(raw_affix)
                roll = _d(affix.get("roll"))
                if str(roll.get("status") or "").strip().lower() not in ("absent", "null", "local_input"):
                    continue
                values = _l(_d(affix.get("value")).get("values"))
                section = f"affix_{channel}"
                group_key = (section, affix.get("encoded_id"))
                group_lines = affix_line_groups.get(group_key) or []
                start = affix_line_cursor.get(group_key, 0)
                affix_line_cursor[group_key] = start + len(values)
                value_lines = group_lines[start : start + len(values)]
                variables: list[dict] = []
                for value_index, raw_value in enumerate(values):
                    value = _d(raw_value)
                    if value.get("value_numeric") is not None:
                        continue
                    if str(value.get("value_source") or "").startswith("local_"):
                        continue
                    line = _d(value_lines[value_index]) if value_index < len(value_lines) else {}
                    stats = _display_value_stats(value, line)
                    display_low, display_high = _display_bounds(
                        value.get("roll_min"), value.get("roll_max"), stats["percent"]
                    )
                    key = f"g{len(groups)}:v{len(variables)}"
                    variables.append(
                        {
                            "key": key,
                            "affix_value_index": value_index,
                            "label": _variable_label(line, value.get("stat_name")),
                            "range_text": stats["range_text"],
                            "unit": stats["unit"],
                            "percent": stats["percent"],
                            "display_min": display_low,
                            "display_max": display_high,
                            "hint": _variable_hint(stats["percent"], display_high),
                            "suggested_command": (
                                "le_build input set --build <build-id> --kind explicit_value "
                                f"--target affix --instance {affix.get('source_path')} "
                                f"--affix-value-index {value_index} --display-value <displayed-value>"
                            ),
                        }
                    )
                if not variables:
                    continue
                tier = affix.get("source_tier")
                affix_name = _text(_g(affix, "affix", "ref", "name")) or _text(affix.get("encoded_id"))
                groups.append(
                    {
                        "key": f"g{len(groups)}",
                        "kind": "affix_roll",
                        "target": "affix",
                        "instance_path": _text(affix.get("source_path")),
                        "item_label": item_label,
                        "area_label": area_label,
                        "group_label": f"{_CHANNEL_SHORT.get(channel, channel)} · {affix_name}",
                        "badge": " ".join(
                            bit for bit in (_CHANNEL_SHORT.get(channel, channel), f"T{tier}" if isinstance(tier, int) else "") if bit
                        ),
                        "range_text": "；".join(v["range_text"] for v in variables if v["range_text"]),
                        "variables": variables,
                    }
                )

        # --- missing implicit rolls (roll-level; variable row when readable) ---
        implicit = _d(instance.get("implicit_rolls"))
        for result_index, raw_result in enumerate(_l(implicit.get("values"))):
            result = _d(raw_result)
            entry_values = _l(result.get("values"))
            first = _d(entry_values[0]) if entry_values else {}
            if first.get("value_numeric") is not None:
                continue
            if str(first.get("status") or "").strip().lower() == "resolved":
                continue
            line = next(
                (
                    line
                    for line in lines
                    if line.get("section") == "implicit"
                    and _d(line.get("diagnostics")).get("implicit_index") == result_index
                ),
                None,
            )
            stats = _display_value_stats(first, line)
            display_low, display_high = _display_bounds(first.get("roll_min"), first.get("roll_max"), stats["percent"])
            groups.append(
                {
                    "key": f"g{len(groups)}",
                    "kind": "implicit_roll",
                    "target": "implicit",
                    "instance_path": _text(instance.get("source_path")),
                    "index": result_index,
                    "item_label": item_label,
                    "area_label": area_label,
                    "group_label": f"固有属性 #{result_index + 1}",
                    "badge": "固有属性",
                    "range_text": stats["range_text"] or "",
                    "variables": [
                        {
                            "key": f"g{len(groups)}:v0",
                            "index": result_index,
                            "label": _variable_label(line, "固有属性"),
                            "range_text": stats["range_text"],
                            "unit": stats["unit"],
                            "percent": stats["percent"],
                            "display_min": display_low,
                            "display_max": display_high,
                            "hint": _variable_hint(stats["percent"], display_high),
                            "suggested_command": (
                                "le_build input set --build <build-id> --kind explicit_value "
                                f"--target implicit --instance {instance.get('source_path')} "
                                f"--index {result_index} --display-value <displayed-value>"
                            ),
                        }
                    ]
                    if line
                    else [],
                }
            )

        # --- missing unique mod rolls: grouped by rollId, row per mod -------
        unique = _d(instance.get("unique_rolls"))
        grouped: dict[int, dict] = {}
        for raw_mod in _l(unique.get("mods")):
            mod = _d(raw_mod)
            if not mod.get("can_roll"):
                continue
            if str(mod.get("roll_status") or "").strip().lower() not in ("absent", "null"):
                continue
            restored = _d(mod.get("restored"))
            if restored.get("value_numeric") is not None:
                continue
            if str(restored.get("value_source") or "").startswith("local_"):
                continue
            try:
                roll_id = int(mod.get("roll_id"))
            except (TypeError, ValueError):
                continue
            mod_index = mod.get("mod_index")
            line = next(
                (
                    line
                    for line in lines
                    if line.get("section") == "unique_mod"
                    and _d(line.get("diagnostics")).get("mod_index") == mod_index
                ),
                None,
            )
            stats = _display_value_stats(restored, line)
            display_low, display_high = _display_bounds(mod.get("raw_value"), mod.get("max_value"), stats["percent"])
            bucket = grouped.setdefault(roll_id, {"variables": []})
            bucket["variables"].append(
                {
                    "key": "",
                    "mod_index": mod_index,
                    "label": _variable_label(line, restored.get("stat_name") or f"mod_index {mod_index}"),
                    "range_text": stats["range_text"],
                    "unit": stats["unit"],
                    "percent": stats["percent"],
                    "display_min": display_low,
                    "display_max": display_high,
                    "hint": _variable_hint(stats["percent"], display_high),
                    "suggested_command": (
                        "le_build input set --build <build-id> --kind explicit_value "
                        f"--target unique_mod --instance {instance.get('source_path')} "
                        f"--mod-index {mod_index} --display-value <displayed-value>"
                    ),
                }
            )
        for ordinal, roll_id in enumerate(sorted(grouped), start=1):
            group_index = len(groups)
            variables = grouped[roll_id]["variables"]
            for variable_index, variable in enumerate(variables):
                variable["key"] = f"g{group_index}:v{variable_index}"
            groups.append(
                {
                    "key": f"g{group_index}",
                    "kind": "unique_mod_roll",
                    "target": "unique_mod",
                    "instance_path": _text(instance.get("source_path")),
                    "roll_id": roll_id,
                    "item_label": item_label,
                    "area_label": area_label,
                    "group_label": f"暗金词条槽 {ordinal}",
                    "badge": f"暗金 rollId {roll_id}",
                    "range_text": "；".join(v["range_text"] for v in variables if v["range_text"]),
                    "variables": variables,
                }
            )
    return {"groups": groups, "variable_count": sum(len(group["variables"]) for group in groups)}


def _local_inputs_summary(spec: dict) -> str:
    local_inputs = _d(_d(spec.get("meta")).get("local_inputs"))
    entries = local_inputs.get("entries")
    if not isinstance(entries, int) or entries <= 0:
        return ""
    return f'<p class="local-input-line">本地手动输入：{entries} 条（诊断详情可追溯）</p>'


def _variable_row_html(group: dict, variable: dict) -> str:
    percent = bool(variable.get("percent"))
    suggestion = _format_suggestion(variable.get("display_max"))
    if suggestion:
        placeholder = f"例如 {suggestion} 或 {suggestion}%" if percent else f"例如 {suggestion}"
    else:
        placeholder = "例如 35 或 35%" if percent else "例如 2"
    hint = _esc(variable.get("hint"))
    label = _esc(variable.get("label"))
    range_text = _esc(variable.get("range_text")) or "—"
    command = _esc(variable.get("suggested_command"))
    return (
        f'<tr class="input-row input-variable-row" data-group-key="{_esc(group["key"])}" '
        f'data-variable-key="{_esc(variable["key"])}">'
        f'<td class="input-line"><span class="input-var-label">{label}</span>'
        f'<span class="input-var-hint muted">{hint}</span>'
        f'<span class="input-row-error" data-row-error role="status"></span></td>'
        f'<td class="input-range muted">{range_text}</td>'
        f'<td class="input-cell"><input class="input-display" type="text" inputmode="decimal" '
        f'autocomplete="off" spellcheck="false" placeholder="{_esc(placeholder)}" '
        f'aria-label="{label} 显示值" title="{command}"></td>'
        f'<td class="input-cell"><input class="input-note" type="text" maxlength="200" '
        f'aria-label="备注（可选）"></td>'
        "</tr>"
    )


def _input_group_html(group: dict) -> str:
    variables = _l(group.get("variables"))
    rows = "".join(_variable_row_html(group, variable) for variable in variables)
    shared_note = ""
    if len(variables) > 1:
        share_label = (
            f"暗金 rollId {group['roll_id']}"
            if group.get("kind") == "unique_mod_roll"
            else "词缀同一条"
        )
        shared_note = (
            f'<p class="input-group-note muted">与同一 roll 槽共享（{_esc(share_label)}）；'
            "每个变量可分别填写。</p>"
        )
    variable_block = ""
    if rows:
        variable_block = (
            '<div class="table-wrap"><table class="data-table input-variable-table">'
            "<thead><tr><th>变量（玩家显示值）</th><th>声明范围</th><th>显示值</th><th>备注（可选）</th></tr></thead>"
            f'<tbody>{rows}</tbody></table></div>'
        )
    else:
        variable_block = '<p class="input-empty muted">该组没有逐变量显示值，请直接使用下方 roll 输入。</p>'
    roll_command = (
        f"le_build input set --build <build-id> --kind {group['kind']} --instance {group['instance_path']}"
    )
    if group.get("roll_id") is not None:
        roll_command += f" --roll-id {group['roll_id']} --roll <0-255>"
    elif group.get("index") is not None:
        roll_command += f" --index {group['index']} --roll <0-255>"
    else:
        roll_command += " --roll <0-255>"
    return (
        f'<div class="input-group" data-group-key="{_esc(group["key"])}">'
        f'<h4 class="input-group-title"><span class="input-group-item">{_esc(group.get("area_label"))} · '
        f'{_esc(group.get("item_label"))}</span>'
        f'<span class="badge badge-neutral">{_esc(group.get("badge"))}</span>'
        f'<span class="group-count">{len(variables)} 个变量</span></h4>'
        f'<p class="input-group-lead muted">{_esc(group.get("group_label"))}'
        + (f" · 声明范围 {_esc(group.get('range_text'))}" if group.get("range_text") else "")
        + "</p>"
        + shared_note
        + variable_block
        + '<details class="input-advanced"><summary>按 roll 品质输入（进阶）</summary>'
        '<div class="input-advanced-body">'
        f'<label class="input-advanced-label">roll（0–255）'
        f'<input class="input-roll" type="number" inputmode="numeric" min="0" max="255" step="1" '
        f'data-group-key="{_esc(group["key"])}" aria-label="按 roll 品质输入 0 到 255" '
        f'title="{_esc(roll_command)}"></label>'
        '<span class="input-advanced-hint muted">若该组没有任何显示值填写，将导出 0–255 的 roll 品质；'
        "一旦填写了显示值，本输入会被忽略（不会同时导出两种）。</span>"
        "</div></details></div>"
    )


def _input_panel_html(spec: dict, derivation: dict, build_id: str) -> str:
    groups = _l(derivation.get("groups"))
    variable_count = derivation.get("variable_count")
    if not isinstance(variable_count, int):
        variable_count = sum(len(_l(group.get("variables"))) for group in groups)
    group_count = len(groups)
    summary_hint = (
        f"{group_count} 组 / {variable_count} 个变量，默认收起" if groups else "当前 revision 没有待补数值"
    )
    body: list[str] = [
        '<p class="input-lead">来源 payload 对部分数值是<strong>缺失</strong>的（不是 0，也不是满 roll）。'
        "每行对应一个<strong>玩家可见变量</strong>：按游戏内面板显示值填写即可（百分比行可省略 %）。"
        "只会导出已填写的值；若某组没有任何显示值，可在「按 roll 品质输入（进阶）」中改填 0–255 的原始 roll。"
        "导出文件为 <code>build-local-inputs-v1</code>，再由 CLI 导入并同步为新的本地 revision；"
        "手动输入的词条在玩家视图会带「手动」标记，原始归档不会被覆盖。</p>"
    ]
    if not groups:
        body.append('<p class="input-empty muted">当前 revision 没有需要补充的来源缺失数值。</p>')
    else:
        body.extend(_input_group_html(group) for group in groups)
        command = (
            f"le_build input import --build {build_id} --file inputs.json "
            f"&& le_build sync --build {build_id}"
        )
        body.append(
            '<div class="input-actions">'
            '<button type="button" class="btn btn-gold" data-input-download>下载 inputs.json</button>'
            '<button type="button" class="btn" data-input-copy>复制导入命令</button>'
            f'<span class="filter-count" data-input-count>已填写 0 / {variable_count} 变量</span></div>'
            '<p class="input-status" data-input-status role="status"></p>'
            '<p class="input-help muted">在项目根目录执行：'
            f'<code class="input-command">{_esc(command)}</code>'
            "（只写本地输入文件，不发布 revision；`sync` 会用归档原始响应 + 本地输入发布新 revision。）</p>"
        )
    return (
        '<details id="local-inputs" class="input-panel" data-input-panel>'
        f'<summary class="input-summary">补充缺失数值 / 手动输入<span class="input-hint">{_esc(summary_hint)}</span></summary>'
        f'<div class="input-body">{"".join(body)}</div></details>'
    )

# ---------------------------------------------------------------------------
# overview panel
# ---------------------------------------------------------------------------
def _stat_card(value: Any, label: str, hint: Any = None) -> str:
    hint_html = f'<div class="stat-hint">{_esc(hint)}</div>' if hint else ""
    return (
        f'<div class="stat-card"><div class="stat-value">{_esc(value)}</div>'
        f'<div class="stat-label">{_esc(label)}</div>{hint_html}</div>'
    )


def _character_facts(spec: dict) -> dict:
    character = _d(spec.get("character"))
    catalog = _d(spec.get("catalog"))
    return {
        "class": _g(character, "class", "ref", "name") or "",
        "mastery": _g(character, "mastery", "ref", "name") or "",
        "level": character.get("level"),
        "game_version": _text(catalog.get("target_game_version")),
        "season": _text(catalog.get("target_season")),
        "share_url": _text(_d(spec.get("meta")).get("share_url")),
    }


def _game_version_text(facts: dict) -> str:
    version = facts.get("game_version")
    season = facts.get("season")
    if version and season:
        return f"{version} · {season}"
    return version or season or "未提供"


def _overview_panel(spec: dict, counts: dict) -> str:
    facts = _character_facts(spec)
    character_label = " / ".join(bit for bit in (facts["class"], facts["mastery"]) if bit) or "未提供角色"
    channel_text = " · ".join(
        f"{title} {counts['channel_counts'][channel]}"
        for channel, title in (("normal", "普通"), ("sealed", "封印"), ("primordial", "原初"), ("corrupted", "腐化"))
        if counts["channel_counts"].get(channel)
    ) or "无词缀"
    integrity = (
        f"物品 {counts['items_ok']}/{counts['items_total']} · "
        f"词缀 {counts['affixes_ok']}/{counts['affix_total']} · "
        f"{counts['absent_rolls']} 条来源缺 roll"
    )
    stat_cards = "".join(
        [
            _stat_card(character_label, "职业 / 专精", f"等级 {_num(facts['level'])}" if facts["level"] is not None else None),
            _stat_card(_game_version_text(facts), "游戏版本"),
            _stat_card(counts["equipment"], "装备", f"{counts['equipment']} 个槽位"),
            _stat_card(counts["idols"], "神像", "按来源位置记录"),
            _stat_card(counts["blessings"], "祝福", "按来源时间线记录"),
            _stat_card(counts["specializations"], "专精技能", f"{counts['skill_nodes']} 个节点 · {counts['skill_points']} 点"),
            _stat_card(counts["passive_nodes"], "被动节点", f"{counts['passive_points']} 点"),
            _stat_card(counts["affix_total"], "词缀", channel_text),
        ]
    )
    share_url = facts["share_url"] or "未提供"
    local_inputs_line = _local_inputs_summary(spec)
    return (
        '<div class="panel-head"><h2 class="panel-title">构筑概览</h2>'
        '<p class="panel-lead">角色、构筑构成与导入完整性；工程追溯信息在页面底部“诊断详情”中。</p></div>'
        f'<div class="stat-grid">{stat_cards}</div>'
        '<section class="card integrity-card">'
        '<h3 class="sub-title">导入完整性</h3>'
        f'<p class="integrity-line">{_esc(integrity)}</p>'
        '<p class="section-lead">缺失 roll 的词条以“数值未提供（显示范围）”标注，不按 0、不按满 roll、不按平均值伪造。'
        "缺失 roll 不等于没有词条：定义与范围仍会显示。"
        "来源缺失的数值可在页面底部“补充缺失数值”中手动填写。</p>"
        f"{local_inputs_line}"
        f'<p class="share-line">来源分享链接：<span class="share-url">{_esc(share_url)}</span></p>'
        "</section>"
        '<section class="card scope-card"><h3 class="sub-title">本页展示什么 / 不声明什么</h3>'
        '<ul class="scope-list">'
        "<li>本页展示已导入构筑的结构、装备与词条、神像与祝福、技能与被动；不是游戏内截图，不重新联网获取。</li>"
        "<li>不声明 DPS、Buff 覆盖率、循环模拟，也不声明与 LE Tools 网站 Calculations 总值一致。</li>"
        "<li>词条数值来自来源 roll 的恢复结果；来源没有 roll 时只显示声明范围，不伪造实值。</li>"
        "</ul></section>"
    )


# ---------------------------------------------------------------------------
# equipment panel
# ---------------------------------------------------------------------------
def _equipment_panel(spec: dict, counts: dict) -> str:
    equipment = _d(spec.get("equipment"))
    slots_by_key: dict[str, Any] = {}
    for entry in _l(equipment.get("slots")):
        entry_d = _d(entry)
        key = str(entry_d.get("slot") or "")
        if key and key not in slots_by_key:
            slots_by_key[key] = entry_d.get("instance")
    order = [str(key) for key in _l(equipment.get("slot_order"))]
    if not order:
        order = list(slots_by_key)
    cards: list[str] = []
    for key in order:
        label = SLOT_LABELS.get(key, key)
        instance = slots_by_key.get(key)
        if instance is None:
            cards.append(_absent_slot_card(key))
        else:
            cards.append(_item_card(instance, slot_label=label, kind="equipment"))
    for key in counts["absent_slots"]:
        if key not in slots_by_key:
            cards.append(_absent_slot_card(key))
    if not cards:
        cards.append('<div class="card empty-state"><p>未提供装备数据。</p></div>')
    return (
        '<div class="panel-head"><h2 class="panel-title">装备</h2>'
        f'<p class="panel-lead">{counts["equipment"]} 个已记录槽位。词条按来源通道分组；'
        "T7 表示来源词缀阶级，缺失数值在对应词条后标注范围。</p></div>"
        '<div class="section-tools">'
        '<input class="search-input" type="search" placeholder="搜索装备 / 词条 / 数值…" data-search=".item-card" aria-label="搜索装备">'
        '<span class="filter-count" data-filter-count></span></div>'
        f'<div class="card-grid item-grid">{"".join(cards)}</div>'
    )


# ---------------------------------------------------------------------------
# idols & blessings panel
# ---------------------------------------------------------------------------
def _idol_map_html(entries: list[dict]) -> str:
    positioned: list[tuple[int, int, int]] = []
    for index, raw_entry in enumerate(entries):
        entry = _d(raw_entry)
        position = _d(entry.get("position")) or _d(_d(entry.get("instance")).get("position"))
        x, y = position.get("x"), position.get("y")
        if isinstance(x, int) and isinstance(y, int):
            positioned.append((x, y, index + 1))
    if not positioned:
        return ""
    columns = max(x for x, _y, _index in positioned)
    rows = max(y for _x, y, _index in positioned)
    cells = "".join(
        f'<span class="idol-cell" style="grid-column:{x};grid-row:{y}">神像 {index}</span>'
        for x, y, index in positioned
    )
    return (
        f'<div class="idol-map" style="--cols:{columns};--rows:{rows}" role="img" aria-label="神像位置">'
        f"{cells}</div>"
    )


def _idol_card(entry: dict, index: int) -> str:
    entry_d = _d(entry)
    instance = entry_d.get("instance")
    position = _d(entry_d.get("position")) or _d(_d(instance).get("position"))
    x, y = position.get("x"), position.get("y")
    if isinstance(x, int) and isinstance(y, int):
        position_label = f"位置 ({x}, {y})"
    else:
        position_label = "位置未提供"
    return _item_card(instance, slot_label=f"神像 {index + 1}", position_label=position_label, kind="idol")


def _blessing_card(entry: dict, index: int) -> str:
    entry_d = _d(entry)
    instance = entry_d.get("instance")
    timeline = entry_d.get("timeline")
    if timeline is None:
        timeline = _d(_d(instance).get("position")).get("timeline")
    label = f"祝福 · 时间线 {_num(timeline)}" if timeline is not None else f"祝福 {index + 1}"
    return _item_card(instance, slot_label=label, kind="blessing")


def _blessing_is_displayed(instance: Any) -> bool:
    """Whether a blessing card has any player-facing (non-DPS) line to show.

    A card whose lines are all non-DPS (e.g. drop-rate-only blessings) is skipped so
    no empty cards remain.  Historical revisions without a readable blessing layer
    keep their (fallback) cards/names for traceability.
    """
    item = _d(instance)
    if not item:
        return True
    readable = _readable_for(item)
    if not _visible_lines(readable):
        return True
    return bool(_business_lines(readable))


def _idol_fire_effect(instance: dict, effect_prefix: float, effect_suffix: float) -> list[dict]:
    """Return the applied-effect rows for one idol instance (prefix/suffix only)."""
    rows: list[dict] = []
    affixes = _d(instance.get("affixes"))
    for channel in ("normal", "sealed", "primordial", "corrupted"):
        for affix in _l(affixes.get(channel)):
            affix = _d(affix)
            meta = _d(affix.get("affix"))
            slot = meta.get("affix_slot")
            if slot not in ("prefix", "suffix"):
                continue
            effect = effect_prefix if slot == "prefix" else effect_suffix
            if channel == "corrupted":
                # 用户游戏内验证：corrupted 词条不被 Altar 前后缀效果放大
                effect = 0.0
            values = _l(_d(affix.get("value")).get("values"))
            for value in values:
                value = _d(value)
                stat_name = _text(value.get("stat_name"), _text(meta.get("name"), "?"))
                tags = value.get("tags")
                try:
                    tags_int = int(tags) if tags is not None else 0
                except Exception:
                    tags_int = 0
                numeric = value.get("value_numeric")
                if numeric is None:
                    continue
                rows.append(
                    {
                        "slot": slot,
                        "channel": channel,
                        "effect": effect,
                        "stat_name": stat_name,
                        "value": float(numeric),
                        "display": _text(value.get("display_value"), _num(numeric)),
                        "unit": str(value.get("unit") or ""),
                        "tags": tags_int,
                        "fire_related": tags_int == 0
                        and stat_name in ("Fire Resistance", "Elemental Resistance", "All Resistances"),
                    }
                )
    return rows


def _idol_refraction_payload(spec: dict) -> dict | None:
    """Build the data used by the manual refracted-idol selector (save build only)."""
    meta = _d(spec.get("meta"))
    build_id = str(meta.get("build_id") or "")
    if build_id != "letools:toolnative-save":
        return None
    altar_effect_prefix = 0.28
    altar_effect_suffix = 0.14
    for slot in _l(_d(spec.get("equipment")).get("slots")):
        slot = _d(slot)
        if slot.get("slot") != "idol_altar":
            continue
        instance = _d(slot.get("instance"))
        for channel in ("normal", "sealed", "primordial", "corrupted"):
            for affix in _l(_d(instance.get("affixes")).get(channel)):
                affix = _d(affix)
                for value in _l(_d(affix.get("value")).get("values")):
                    value = _d(value)
                    name = str(value.get("stat_name") or "")
                    numeric = value.get("value_numeric")
                    if numeric is None:
                        continue
                    if "Effect of Prefixes for Idols in Refracted Slots" in name:
                        altar_effect_prefix = float(numeric)
                    elif "Effect of Suffixes for Idols in Refracted Slots" in name:
                        altar_effect_suffix = float(numeric)
    idols: list[dict] = []
    for index, entry in enumerate(_l(_d(spec.get("idols")).get("entries"))):
        entry = _d(entry)
        instance = _d(entry.get("instance"))
        if not instance:
            continue
        base = _d(instance.get("base"))
        definition = _d(instance.get("definition"))
        name = _text(definition.get("name"), _text(base.get("name"), f"神像 {index + 1}"))
        position = _d(entry.get("position") or instance.get("position"))
        pos_text = ""
        if position:
            pos_text = f"({position.get('x')},{position.get('y')})"
        mods = _idol_fire_effect(instance, altar_effect_prefix, altar_effect_suffix)
        if index == 0:
            # DB roll interpolation + 游戏侧取整：未放大 fire resistance = 37%（旧导入 38%）
            for mod in mods:
                if mod.get("stat_name") == "Fire Resistance" and mod.get("tags") == 0:
                    mod["value"] = 0.37
                    mod["display"] = "37%"
        if index == 4:
            # 用户确认 5 号（Throne of Ambition / Adorned Idol）的腐化词缀是 +4% 全抗，
            # 旧 BD viewer 的 +1% 是导入错误；corrupted 词条不吃折射放大。
            for mod in mods:
                if mod.get("stat_name") == "All Resistances" and mod.get("tags") == 0:
                    mod["value"] = 0.04
                    mod["display"] = "4%"
                    mod["effect"] = 0.0
        idols.append(
            {
                "index": index,
                "name": name,
                "position": pos_text,
                "mods": mods,
            }
        )
    return {
        "build_id": build_id,
        # 离线存档已确认修正（任务 +2 全属性不影响火抗；胸甲 +112%；5 号神像全抗 +4% 不吃折射）。
        # 939 = 无折射放大时的未封顶火抗（含全部神像原始数值），仅本 save-import 构筑使用。
        "base_fire": 939.0,
        "altar_prefix_effect": altar_effect_prefix,
        "altar_suffix_effect": altar_effect_suffix,
        "default_selected": [0, 1, 2, 3, 4],
        "idols": idols,
    }


def _idol_refraction_html(spec: dict) -> str:
    payload = _idol_refraction_payload(spec)
    if payload is None:
        return ""
    checks = []
    for idol in payload["idols"]:
        idx = idol["index"]
        hints = []
        for mod in idol["mods"]:
            if mod["fire_related"] or mod["stat_name"] in ("Fire Penetration", "Increased Ailment Duration"):
                hints.append(f'{mod["stat_name"]} {mod["display"]}')
        hint = "；".join(hints[:3])
        checks.append(
            '<label class="refract-option">'
            f'<input type="checkbox" class="refract-check" data-idol-index="{idx}"'
            + (" checked" if idx in payload["default_selected"] else "")
            + "> "
            f'<span>神像 {idx + 1} · {_esc(idol["name"])} {_esc(idol["position"])}'
            + (f'<br><small class="muted">{_esc(hint)}</small>' if hint else "")
            + "</span></label>"
        )
    return (
        '<section class="card spaced" id="idol-refraction-panel">'
        '<h3 class="sub-title">神像折射栏位（人工勾选）</h3>'
        '<p class="section-lead">自动判断不可靠（存档坐标未通过网格边界/翻转校验，Altar 网格数据也有多种读法）。'
        "请按游戏内紫色折射栏位勾选神像；下面按 Altar 的「Effect of Prefixes/Suffixes for Idols in "
        "Refracted Slots」放大对应前缀/后缀。</p>"
        '<script type="application/json" id="idol-refraction-data">'
        + _embedded_json(payload)
        + "</script>"
        '<div class="refract-options">'
        + "".join(checks)
        + "</div>"
        '<p class="section-lead" id="refraction-summary"></p>'
        '<div id="refraction-detail" class="muted"></div>'
        '<div class="refract-actions"><button type="button" id="refraction-export">导出选择 JSON</button></div>'
        "</section>"
    )


def _relics_panel(spec: dict, counts: dict) -> str:
    idol_entries = [_d(entry) for entry in _l(_d(spec.get("idols")).get("entries"))]
    idol_entries = [entry for entry in idol_entries if entry]
    idol_cards = "".join(_idol_card(entry, index) for index, entry in enumerate(idol_entries))
    if not idol_cards:
        idol_cards = '<div class="card empty-state"><p>未提供神像数据。</p></div>'
    blessing_entries = [_d(entry) for entry in _l(_d(spec.get("blessings")).get("entries"))]
    blessing_entries = [entry for entry in blessing_entries if entry]
    blessing_cards: list[str] = []
    hidden_blessings = 0
    for index, entry in enumerate(blessing_entries):
        if not _blessing_is_displayed(entry.get("instance")):
            hidden_blessings += 1
            continue
        blessing_cards.append(_blessing_card(entry, index))
    if not blessing_cards:
        blessing_html = '<div class="card empty-state"><p>没有可展示的祝福条目。</p></div>'
    else:
        blessing_html = f'<div class="card-grid item-grid blessing-grid">{"".join(blessing_cards)}</div>'
    hidden_note = ""
    if hidden_blessings:
        hidden_note = (
            '<p class="panel-note muted">按 2026-11-04 范围，仅掉落率类别的非战斗效果不进入默认 BD 展示'
            "（数值未显示）；对应原始数据仍保留在诊断详情中。</p>"
        )
    displayed_blessings = len(blessing_cards)
    return (
        '<div class="panel-head"><h2 class="panel-title">神像与祝福</h2>'
        '<p class="panel-lead">神像按来源位置展示；祝福按来源时间线顺序展示。'
        "词条数值缺失时显示声明范围。</p></div>"
        f'<section class="card"><h3 class="sub-title">神像（{counts["idols"]} 个）</h3>'
        '<p class="section-lead">位置来自来源记录的坐标，未执行网格边界或翻转规则校验。</p>'
        + _idol_map_html(idol_entries)
        + f'<div class="card-grid item-grid idol-grid">{"".join(idol_cards)}</div></section>'
        + _idol_refraction_html(spec)
        + f'<section class="card spaced"><h3 class="sub-title">祝福（{displayed_blessings} 条）</h3>'
        '<p class="section-lead">按来源时间线键顺序展示，不按名称重排。</p>'
        + hidden_note
        + blessing_html
        + "</section>"
    )


# ---------------------------------------------------------------------------
# skills & passives panel
# ---------------------------------------------------------------------------
def _node_table(nodes: Any, *, points_label: str = "已分配点数", empty_text: str = "未提供节点记录") -> str:
    entries = [_d(node) for node in _l(nodes)]
    entries = [entry for entry in entries if entry]
    if not entries:
        return f'<p class="item-empty muted">{_esc(empty_text)}</p>'
    rows: list[str] = []
    for node in entries:
        ref = _d(node.get("ref"))
        name = _text(ref.get("name"), "未解析节点")
        points = node.get("points")
        max_points = node.get("max_points")
        flags = ""
        if node.get("explicit_zero") is True:
            flags = ' <span class="node-flag">来源记录为 0 点</span>'
        if node.get("points_within_max") is False:
            flags += ' <span class="node-flag node-flag-warn">超出上限</span>'
        rows.append(
            f'<tr class="node-row">'
            f'<td class="node-name">{_esc(name)}</td>'
            f'<td class="node-points">{_esc(_num(points))}{flags}</td>'
            f'<td class="muted">{_esc(_num(max_points))}</td>'
            f"</tr>"
        )
    return (
        '<div class="table-wrap"><table class="data-table node-table">'
        f"<thead><tr><th>节点</th><th>{_esc(points_label)}</th><th>点数上限</th></tr></thead>"
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _action_bar_section(spec: dict) -> str:
    slots = [_d(slot) for slot in _l(_d(spec.get("action_bar")).get("slots"))]
    slots = [slot for slot in slots if slot]
    if not slots:
        return '<section class="card empty-state"><h3 class="sub-title">技能栏</h3><p>未提供技能栏数据。</p></section>'
    specialization_by_slot = {
        _d(entry).get("slot_number"): _d(_d(entry).get("tree")).get("name") or _d(_d(entry).get("owner_skill")).get("name")
        for entry in _l(spec.get("specializations"))
    }
    cells: list[str] = []
    for index, slot in enumerate(slots):
        ref = _d(slot.get("ref"))
        name = _text(ref.get("name"), "未解析技能")
        specialization_slot = slot.get("specialization_slot")
        tree_name = specialization_by_slot.get(specialization_slot) if specialization_slot is not None else None
        if tree_name:
            linked = f'<span class="badge badge-gold">专精 {_esc(tree_name)}</span>'
        elif specialization_slot is not None:
            linked = f'<span class="badge badge-neutral">专精槽 {_num(specialization_slot)}</span>'
        else:
            linked = '<span class="badge badge-neutral">仅技能栏</span>'
        cells.append(
            f'<li class="hud-slot"><span class="hud-index">{index + 1}</span>'
            f'<span class="hud-name">{_esc(name)}</span>{linked}</li>'
        )
    return (
        f'<section class="card"><h3 class="sub-title">技能栏（{len(slots)} 项）</h3>'
        '<p class="section-lead">技能栏按来源顺序保存，与专精树相互独立：可以包含未专精技能，'
        "也可能缺少已专精技能。</p>"
        f'<ol class="hud-row">{"".join(cells)}</ol></section>'
    )


def _specializations_section(spec: dict) -> str:
    specializations = [_d(entry) for entry in _l(spec.get("specializations"))]
    specializations = [entry for entry in specializations if entry]
    if not specializations:
        return '<section class="card empty-state"><h3 class="sub-title">专精技能</h3><p>未提供专精数据。</p></section>'
    cards: list[str] = []
    for entry in specializations:
        tree = _d(entry.get("tree"))
        owner_skill = _d(entry.get("owner_skill"))
        tree_name = _text(tree.get("name")) or _text(owner_skill.get("name")) or "未解析技能树"
        skill_level = entry.get("source_level")
        points_total = entry.get("points_total")
        slot_number = entry.get("slot_number")
        badges = " ".join(
            bit
            for bit in (
                f'<span class="badge badge-neutral">专精槽 {_num(slot_number)}</span>' if slot_number is not None else "",
                f'<span class="badge badge-blue">技能等级 {_num(skill_level)}</span>' if skill_level is not None else "",
                f'<span class="badge badge-gold">已分配点数 {_num(points_total)}</span>' if points_total is not None else "",
            )
            if bit
        )
        cards.append(
            f'<article class="card spec-card"><div class="spec-head">'
            f'<h4 class="spec-name">{_esc(tree_name)}</h4><div class="badge-row">{badges}</div></div>'
            + _node_table(entry.get("nodes"), empty_text="该技能树没有节点记录")
            + "</article>"
        )
    return (
        '<section class="card"><h3 class="sub-title">专精技能（' + str(len(specializations)) + " 棵树）</h3>"
        '<p class="section-lead">“技能等级”是技能自身等级，“已分配点数”是该技能树中加点的总点数，两者分开显示。</p>'
        '<div class="section-tools"><input class="search-input" type="search" '
        'placeholder="搜索技能 / 节点…" data-search=".spec-card, .node-row" aria-label="搜索专精节点">'
        '<span class="filter-count" data-filter-count></span></div>'
        f'<div class="spec-list">{"".join(cards)}</div></section>'
    )


def _passive_section(spec: dict, counts: dict) -> str:
    passive = _d(spec.get("passive_tree"))
    tree = _d(passive.get("tree"))
    tree_name = _text(tree.get("name"), "被动天赋")
    points = counts["passive_points"]
    badges = " ".join(
        bit
        for bit in (
            f'<span class="badge badge-gold">已分配点数 {_num(points)}</span>' if points is not None else "",
            f'<span class="badge badge-neutral">{counts["passive_nodes"]} 个节点</span>',
        )
        if bit
    )
    return (
        f'<section class="card"><div class="spec-head"><h3 class="sub-title">{_esc(tree_name)}</h3>'
        f'<div class="badge-row">{badges}</div></div>'
        '<p class="section-lead">被动天赋节点与已分配点数按来源记录展示。</p>'
        '<div class="section-tools"><input class="search-input" type="search" '
        'placeholder="搜索被动节点…" data-search=".node-row" aria-label="搜索被动节点">'
        '<span class="filter-count" data-filter-count></span></div>'
        + _node_table(passive.get("nodes"), empty_text="未提供被动节点记录")
        + "</section>"
    )


def _skills_panel(spec: dict, counts: dict) -> str:
    return (
        '<div class="panel-head"><h2 class="panel-title">技能与被动</h2>'
        '<p class="panel-lead">技能栏与专精树相互独立；技能等级与已分配点数分别标注；被动节点显示已分配点数。</p></div>'
        + _action_bar_section(spec)
        + _specializations_section(spec)
        + _passive_section(spec, counts)
    )


# ---------------------------------------------------------------------------
# hero / footer
# ---------------------------------------------------------------------------
def _hero(spec: dict, *, revision_id: str, mode: dict, counts: dict) -> str:
    facts = _character_facts(spec)
    character_label = " · ".join(bit for bit in (facts["class"], facts["mastery"]) if bit) or "本地构筑"
    title = character_label
    level_text = f"等级 {_num(facts['level'])}" if facts["level"] is not None else "等级未提供"
    integrity = (
        f"物品 {counts['items_ok']}/{counts['items_total']} · "
        f"词缀 {counts['affixes_ok']}/{counts['affix_total']} · "
        f"{counts['absent_rolls']} 条来源缺 roll"
    )
    notices: list[str] = []
    if mode.get("historical"):
        notices.append(
            '<div class="notice notice-historical"><strong>历史 revision（旧范围/无可读词条层）</strong>：'
            "本页按旧字段降级渲染，部分词条仅显示范围；旧 revision 保持可读、不原地改写。</div>"
        )
    elif mode.get("readable_items", 0) < mode.get("total_items", 0):
        missing = mode.get("total_items", 0) - mode.get("readable_items", 0)
        notices.append(
            f'<div class="notice">有 {missing} 件物品缺少可读词条层，已按结构化字段降级显示。</div>'
        )
    return (
        '<header class="hero">'
        '<div class="hero-main">'
        '<p class="eyebrow">LAST EPOCH · 本地构筑档案</p>'
        f'<h1 class="hero-title">{_esc(title)}</h1>'
        f'<p class="hero-sub">{_esc(level_text)} &nbsp;·&nbsp; 游戏版本 {_esc(_game_version_text(facts))}</p>'
        '<div class="hero-chips">'
        f'<span class="hero-chip">装备 {counts["equipment"]}</span>'
        f'<span class="hero-chip">神像 {counts["idols"]}</span>'
        f'<span class="hero-chip">祝福 {counts["blessings"]}</span>'
        f'<span class="hero-chip">专精 {counts["specializations"]}</span>'
        f'<span class="hero-chip">被动 {counts["passive_nodes"]} 节点</span>'
        "</div>"
        f'<p class="hero-integrity">{_esc(integrity)}</p>'
        + "".join(notices)
        + "</div>"
        "</header>"
    )


def _footer(spec: dict) -> str:
    facts = _character_facts(spec)
    return (
        '<footer class="footer">'
        '<p class="footer-note">本页由本地导入数据静态生成，为单一离线 HTML 文件：内联样式与脚本，无网络请求，可直接从 file:// 打开。</p>'
        '<p class="footer-note">展示范围：构筑结构、装备与词条、神像与祝福、技能与被动。'
        "不声明 DPS、Buff 覆盖率、循环模拟或与 LE Tools 网站 Calculations 总值一致。</p>"
        f'<p class="footer-note">来源分享链接：<span class="share-url">{_esc(facts["share_url"] or "未提供")}</span>（纯文本，不是外链）。</p>'
        "</footer>"
    )


# ---------------------------------------------------------------------------
# diagnostics (default-closed; engineering traceability)
# ---------------------------------------------------------------------------
def _kv(rows: list[tuple[str, Any]]) -> str:
    return '<dl class="kv">' + "".join(f"<dt>{_esc(label)}</dt><dd>{value}</dd>" for label, value in rows) + "</dl>"


def _code(value: Any) -> str:
    if value is None:
        return "—"
    return f"<code>{_esc(value)}</code>"


def _diag_revision(spec: dict, revision_id: str, report: dict | None) -> str:
    meta = _d(spec.get("meta"))
    revision = _d(meta.get("revision"))
    catalog = _d(spec.get("catalog"))
    source_payload = _d(meta.get("source_payload"))
    verification = _d(catalog.get("verification"))
    import_attempt = _d(meta.get("import_attempt"))
    rows: list[tuple[str, Any]] = [
        ("build_id", _code(meta.get("build_id"))),
        ("revision_id", _code(revision_id or revision.get("revision_id"))),
        ("revision_seq", _code(revision.get("revision_seq"))),
        ("published_at", _code(revision.get("published_at"))),
        ("revision content sha256", _code(revision.get("content_sha256"))),
        ("build_spec_version", _code(spec.get("build_spec_version"))),
        ("parser_version", _code(spec.get("parser_version"))),
        ("mapping_version", _code(spec.get("mapping_version"))),
        ("share_url", _code(meta.get("share_url"))),
        ("source", _code(meta.get("source"))),
        ("import mode", _code(import_attempt.get("mode"))),
        ("raw sha256", _code(source_payload.get("raw_sha256"))),
        ("data_field sha256", _code(source_payload.get("data_field_sha256"))),
        ("raw path", _code(source_payload.get("raw_path"))),
        ("dataset_id", _code(catalog.get("dataset_id"))),
        ("source_snapshot_id", _code(catalog.get("source_snapshot_id"))),
        ("dataset logical_content_sha256", _code(catalog.get("logical_content_sha256"))),
        ("dataset file_sha256", _code(catalog.get("file_sha256"))),
        ("dataset fingerprint verified", _code(_num(verification.get("verified")))),
    ]
    local_inputs = _d(meta.get("local_inputs"))
    if local_inputs:
        rows.extend(
            [
                ("local_inputs.version", _code(local_inputs.get("version"))),
                ("local_inputs.entries", _code(local_inputs.get("entries"))),
                ("local_inputs.applied", _code(local_inputs.get("applied"))),
                ("local_inputs.sha256", _code(local_inputs.get("sha256"))),
                ("local_inputs.path", _code(local_inputs.get("path"))),
            ]
        )
        manifest = _l(local_inputs.get("inputs_manifest"))
        if manifest:
            rows.append(("local_inputs.inputs_manifest", _code(json.dumps(manifest, ensure_ascii=False, sort_keys=True))))
    statuses = _d(spec.get("statuses"))
    status_rows = "".join(
        f"<tr><th scope=\"row\">{_esc(label)}</th><td>{_code(statuses.get(key))}</td></tr>"
        for key, label in (
            ("fetch", "fetch"),
            ("schema", "schema"),
            ("catalog_compatibility", "catalog_compatibility"),
            ("identity_resolution", "identity_resolution"),
            ("value_resolution", "value_resolution"),
            ("calculation_readiness", "calculation_readiness"),
        )
    )
    report_html = ""
    if report:
        validation = _d(report.get("validation"))
        acceptance = _l(report.get("acceptance"))
        report_html = (
            '<p class="diag-note">import-report.json：validation '
            f"status {_code(validation.get('status'))} · pass {_code(validation.get('pass'))} · "
            f"warn {_code(validation.get('warn'))} · fail {_code(validation.get('fail'))}；"
            f"§11.1 验收检查 {sum(1 for item in acceptance if _d(item).get('status') == 'pass')}/{len(acceptance)} 通过。</p>"
        )
    return (
        '<section class="diag-section"><h3>修订与数据绑定</h3>'
        + _kv(rows)
        + '<div class="table-wrap"><table class="data-table"><thead><tr><th>状态层</th><th>值</th></tr></thead>'
        f'<tbody>{status_rows}</tbody></table></div>'
        + report_html
        + "</section>"
    )


def _diag_scope(spec: dict, mode: dict) -> str:
    scope = _d(spec.get("scope"))
    weaver = _d(spec.get("weaver"))
    parts: list[str] = ['<section class="diag-section"><h3>范围（scope）</h3>']
    if scope:
        parts.append(
            _kv(
                [
                    ("scope_version", _code(scope.get("scope_version"))),
                    ("included_sections", _code(", ".join(str(item) for item in _l(scope.get("included_sections"))))),
                ]
            )
        )
        excluded = [_d(item) for item in _l(scope.get("excluded_features"))]
        if excluded:
            rows = "".join(
                f"<tr><td>{_code(item.get('feature'))}</td>"
                f"<td>{_code(', '.join(str(field) for field in _l(item.get('source_fields'))))}</td>"
                f"<td>{_esc(item.get('reason'))}</td>"
                f"<td>{_code(item.get('decision_date'))}</td>"
                f"<td>{_code(_num(item.get('raw_preserved')))}</td></tr>"
                for item in excluded
            )
            parts.append(
                '<div class="table-wrap"><table class="data-table"><thead><tr>'
                "<th>排除功能</th><th>来源字段</th><th>原因</th><th>决定日期</th><th>原始数据保留</th>"
                f"</tr></thead><tbody>{rows}</tbody></table></div>"
            )
    else:
        parts.append('<p class="diag-note">未提供 scope 字段（旧范围 revision）。</p>')
    if weaver:
        parts.append(
            _kv(
                [
                    ("weaver.status", _code(weaver.get("status"))),
                    ("weaver.raw_preserved", _code(_num(weaver.get("raw_preserved")))),
                    ("weaver.source_fields", _code(", ".join(str(field) for field in _l(weaver.get("source_fields"))))),
                    ("weaver.note", _esc(weaver.get("note"))),
                ]
            )
        )
    if mode.get("weaver_present"):
        parts.append(
            '<div class="warn-box diag-warn"><strong>该 revision 属于旧范围（包含编织者树），已按当前范围忽略。</strong>'
            f"旧字段 weaver.nodes={_num(len(_l(weaver.get('nodes'))))} 条、"
            f"weaver.containers={_num(len(_l(weaver.get('containers'))))} 个；"
            "旧 revision 保留可读、不原地改写，原始数据仍在归档响应中可追溯。</div>"
        )
    else:
        parts.append('<p class="diag-note">范围内业务对象不包含编织者树与附属容器。</p>')
    parts.append("</section>")
    return "".join(parts)


def _coverage_bar(label: str, numerator: Any, denominator: Any, note: str = "") -> str:
    number = numerator if isinstance(numerator, int) else None
    total = denominator if isinstance(denominator, int) else None
    if number is None or total is None or total <= 0:
        return (
            f'<div class="coverage-row"><div class="coverage-head"><span>{_esc(label)}</span>'
            f'<span class="coverage-value">未提供</span></div><div class="bar"><span class="bar-neutral" style="width:0%"></span></div></div>'
        )
    ratio = max(0.0, min(1.0, number / total))
    percent = f"{ratio * 100:.1f}%"
    tone = "bar-ok" if ratio >= 0.999 else ("bar-warn" if ratio >= 0.5 else "bar-bad")
    note_html = f'<p class="coverage-note muted">{_esc(note)}</p>' if note else ""
    return (
        f'<div class="coverage-row"><div class="coverage-head"><span>{_esc(label)}</span>'
        f'<span class="coverage-value">{number}/{total} · {percent}</span></div>'
        f'<div class="bar"><span class="{tone}" style="width:{ratio * 100:.1f}%"></span></div>{note_html}</div>'
    )


def _diag_coverage(spec: dict, mode: dict) -> str:
    coverage = _d(spec.get("coverage"))
    if not coverage:
        return '<section class="diag-section"><h3>覆盖率（coverage）</h3><p class="diag-note">未提供 coverage 数据。</p></section>'
    items = _d(coverage.get("item_instances"))
    affixes = _d(coverage.get("affix_instances"))
    rolls = _d(coverage.get("rolls"))
    values = _d(coverage.get("values"))
    nodes = _d(coverage.get("nodes"))
    containers = _d(coverage.get("containers"))
    unknown = _d(coverage.get("unknown_fields"))
    bars = "".join(
        [
            _coverage_bar("物品实例（身份关联）", items.get("identity_connected"), items.get("total")),
            _coverage_bar("基底解析", items.get("base_resolved"), items.get("total")),
            _coverage_bar("词缀身份", affixes.get("resolved"), affixes.get("total")),
            _coverage_bar("词缀 tier", affixes.get("tier_resolved"), affixes.get("total")),
            _coverage_bar(
                "显式 roll",
                rolls.get("present"),
                rolls.get("denominator"),
                f"absent={_num(rolls.get('absent'))} · null={_num(rolls.get('null'))} · invalid={_num(rolls.get('invalid'))}",
            ),
            _coverage_bar(
                "词缀数值恢复",
                values.get("resolved"),
                values.get("denominator"),
                f"computed={_num(values.get('computed'))} · partial={_num(values.get('partial'))} · unresolved={_num(values.get('unresolved'))}",
            ),
            _coverage_bar("implicit（有 ir 的物品）", values.get("implicit_items_with_ir"), values.get("implicit_denominator")),
            _coverage_bar("Unique roll（有 ur 的物品）", values.get("unique_items_with_ur"), values.get("unique_denominator")),
            _coverage_bar(
                "节点条目",
                nodes.get("resolved"),
                nodes.get("total"),
                f"passive={_num(nodes.get('passive'))} · skill_tree={_num(nodes.get('skill_tree'))} · weaver={_num(nodes.get('weaver'))}",
            ),
        ]
    )
    extra = ""
    if containers and containers.get("total"):
        extra = (
            f'<p class="diag-note">旧范围容器定义：未解析 {_num(containers.get("unresolved"))}/{_num(containers.get("total"))}'
            "（按当前范围忽略，不计入本页业务内容）。</p>"
        )
    return (
        '<section class="diag-section"><h3>覆盖率（coverage，各分类独立分母）</h3>'
        f'<div class="coverage-list">{bars}</div>{extra}'
        f'<p class="diag-note">未识别字段：top_level={_num(unknown.get("unexpected_top_level"))} · '
        f'data={_num(unknown.get("unexpected_data"))} · item={_num(unknown.get("uninterpreted_item_fields"))}</p></section>'
    )


def _diag_issues(spec: dict) -> str:
    issues = [_d(issue) for issue in _l(spec.get("issues"))]
    issues = [issue for issue in issues if issue]
    if not issues:
        return '<section class="diag-section"><h3>issues</h3><p class="diag-note">未提供 issues 数据。</p></section>'
    counts = {"error": 0, "warning": 0, "info": 0}
    for issue in issues:
        severity = str(issue.get("severity") or "").lower()
        counts[severity] = counts.get(severity, 0) + 1
    buttons = "".join(
        f'<button type="button" class="btn chip-btn{" active" if key == "all" else ""}" data-severity="{key}">'
        f"{label} <span class=\"chip-count\">{count}</span></button>"
        for key, label, count in (
            ("all", "全部", len(issues)),
            ("error", "错误", counts.get("error", 0)),
            ("warning", "警告", counts.get("warning", 0)),
            ("info", "信息", counts.get("info", 0)),
        )
    )
    rows = "".join(
        '<tr class="issue-row" data-sev="' + _esc(issue.get("severity") or "info") + '">'
        f"<td>{_code(issue.get('severity') or 'info')}</td>"
        f"<td>{_code(issue.get('code'))}</td>"
        f"<td>{_code(issue.get('path'))}</td>"
        f"<td>{_esc(issue.get('message'))}</td></tr>"
        for issue in issues
    )
    return (
        '<section class="diag-section"><h3>issues（' + str(len(issues)) + " 条，按严重度筛选）</h3>"
        f'<div class="issue-toolbar" role="group" aria-label="按严重度筛选问题">{buttons}'
        '<span class="filter-count" data-issue-count>' + f"{len(issues)} / {len(issues)}" + "</span></div>"
        '<div class="table-wrap"><table class="data-table issues-table"><thead><tr>'
        "<th>严重度</th><th>代码</th><th>路径</th><th>说明</th>"
        f'</tr></thead><tbody>{rows}</tbody></table></div></section>'
    )


def _diag_readable(spec: dict, mode: dict) -> str:
    rows: list[str] = []
    equipment = _d(spec.get("equipment"))
    groups: list[tuple[str, list[dict]]] = [
        ("装备", [_d(_d(entry).get("instance")) for entry in _l(equipment.get("slots"))]),
        ("神像", [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("idols")).get("entries"))]),
        ("祝福", [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("blessings")).get("entries"))]),
    ]
    for area, instances in groups:
        for instance in instances:
            if not instance:
                continue
            readable = _readable_for(instance)
            lines = _l(readable.get("lines"))
            visible = _visible_lines(readable)
            hidden = len(lines) - len(visible)
            missing = sum(1 for line in visible if line.get("value_status") in ("range", "not_provided", "unresolved"))
            manual = sum(
                1
                for line in visible
                if str(_d(line.get("diagnostics")).get("value_source") or "").startswith("local_")
            )
            non_dps = sum(1 for line in visible if _is_non_dps_line(line))
            source = "readable" if readable.get("source") == "readable" else "结构化降级"
            warnings = _l(readable.get("warnings"))
            diagnostics = {
                "instance": instance.get("instance_id") or instance.get("source_path"),
                "source": readable.get("source"),
                "completeness": readable.get("completeness"),
                "lines": len(lines),
                "hidden_lines": hidden,
                "missing_value_lines": missing,
                "warnings": warnings,
            }
            warning_html = ""
            if warnings:
                warning_html = (
                    '<details class="trace"><summary>readable.warnings（'
                    + str(len(warnings))
                    + "）</summary><ul class=\"raw-list\">"
                    + "".join(f"<li>{_esc(item)}</li>" for item in warnings)
                    + "</ul></details>"
                )
            rows.append(
                f"<tr><td>{_esc(area)}</td>"
                f"<td>{_esc(readable.get('display_name'))}</td>"
                f"<td>{_code(str(diagnostics['instance'] or ''))}</td>"
                f"<td>{_esc(source)}</td>"
                f"<td>{_code(readable.get('completeness'))}</td>"
                f"<td>{len(lines)}</td><td>{hidden}</td><td>{missing}</td><td>{manual}</td><td>{non_dps}</td>"
                f"<td>{warning_html or '—'}</td></tr>"
            )
    return (
        '<section class="diag-section"><h3>可读词条层状态（readable / structured）</h3>'
        '<p class="diag-note">默认视图消费 readable.lines；历史 revision 无该层时由结构化字段降级生成。'
        "“缺失数值行”包含 range / not_provided / unresolved，均未按 0 伪造；“手动行”为本地输入后的 "
        "value_source=local_* 行；“非战斗行”为按来源属性身份（non_dps 标记或 property 104）从默认视图隐藏、"
        "但仍完整保留在诊断中的掉落率类别词条。</p>"
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        "<th>区域</th><th>物品</th><th>实例</th><th>可读层来源</th><th>completeness</th>"
        "<th>词条行</th><th>隐藏行</th><th>缺失数值行</th><th>手动行</th><th>非战斗行</th><th>warnings</th>"
        f'</tr></thead><tbody>{"".join(rows)}</tbody></table></div></section>'
    )


def _diag_trace(spec: dict) -> str:
    blocks: list[str] = []
    groups: list[tuple[str, list[dict]]] = [
        ("装备", [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("equipment")).get("slots"))]),
        ("神像", [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("idols")).get("entries"))]),
        ("祝福", [_d(_d(entry).get("instance")) for entry in _l(_d(spec.get("blessings")).get("entries"))]),
    ]
    for area, instances in groups:
        for instance in instances:
            if not instance:
                continue
            readable = _readable_for(instance)
            name = readable.get("display_name") or "未提供物品名称"
            codec = _d(instance.get("codec"))
            implicit = _d(instance.get("implicit_rolls"))
            unique = _d(instance.get("unique_rolls"))
            rows: list[tuple[str, Any]] = [
                ("instance_id", _code(instance.get("instance_id"))),
                ("role", _code(instance.get("role"))),
                ("source_path", _code(instance.get("source_path"))),
                ("encoded_id", _code(instance.get("encoded_id"))),
                ("codec.marker", _code(codec.get("marker"))),
                ("codec.status", _code(codec.get("status"))),
                ("codec.payload", _code(codec.get("payload"))),
                ("codec.raw", _code(json.dumps(codec, ensure_ascii=False, sort_keys=True))),
                ("implicit.ir", _code(json.dumps(implicit.get("ir"), ensure_ascii=False))),
                ("implicit.used_ir_values", _code(json.dumps(implicit.get("used_ir_values"), ensure_ascii=False))),
                ("implicit.unused_ir_values", _code(json.dumps(implicit.get("unused_ir_values"), ensure_ascii=False))),
                ("implicit.definition_provenance", _code(json.dumps(implicit.get("definition_provenance"), ensure_ascii=False))),
                ("unique.ur", _code(json.dumps(unique.get("ur"), ensure_ascii=False))),
                ("unique.roll_ids", _code(json.dumps(unique.get("roll_ids"), ensure_ascii=False))),
            ]
            if unique.get("local_rolls"):
                rows.append(
                    ("unique.local_rolls", _code(json.dumps(unique.get("local_rolls"), ensure_ascii=False, sort_keys=True)))
                )
            affix_rows: list[str] = []
            affixes = _d(instance.get("affixes"))
            for channel in _CHANNEL_ORDER:
                for raw_affix in _l(affixes.get(channel)):
                    affix = _d(raw_affix)
                    roll = _d(affix.get("roll"))
                    tier = _d(affix.get("tier"))
                    value = _d(affix.get("value"))
                    affix_rows.append(
                        "<tr>"
                        f"<td>{_code(channel)}</td>"
                        f"<td>{_code(affix.get('source_path'))}</td>"
                        f"<td>{_code(affix.get('encoded_id'))}</td>"
                        f"<td>{_code(affix.get('source_tier'))}</td>"
                        f"<td>{_code(tier.get('tier_index'))}</td>"
                        f"<td>{_code(roll.get('raw'))}</td>"
                        f"<td>{_code(roll.get('status'))}</td>"
                        f"<td>{_code(value.get('status'))}</td>"
                        "</tr>"
                    )
            affix_table = ""
            if affix_rows:
                affix_table = (
                    '<div class="table-wrap"><table class="data-table"><thead><tr>'
                    "<th>通道</th><th>source_path</th><th>encoded_id</th><th>source_tier</th>"
                    "<th>tier_index</th><th>r</th><th>roll_status</th><th>value.status</th>"
                    f'</tr></thead><tbody>{"".join(affix_rows)}</tbody></table></div>'
                )
            line_rows = "".join(
                "<tr>"
                f"<td>{_code(line.get('section'))}</td>"
                f"<td>{_code(line.get('value_status'))}</td>"
                f"<td>{_code(line.get('tier_label'))}</td>"
                f"<td>{_code(line.get('range_text'))}</td>"
                f"<td>{_esc(line.get('text'))}</td>"
                f"<td>{_code('yes' if _is_non_dps_line(line) else None)}</td>"
                f"<td>{_code(_d(line.get('diagnostics')).get('value_source'))}</td>"
                f"<td>{_code(_d(line.get('diagnostics')).get('input_id'))}</td>"
                f"<td>{_code(json.dumps(_d(line.get('diagnostics')), ensure_ascii=False, sort_keys=True))}</td>"
                "</tr>"
                for line in _l(readable.get("lines"))
            )
            line_table = ""
            if line_rows:
                line_table = (
                    '<div class="table-wrap"><table class="data-table"><thead><tr>'
                    "<th>section</th><th>value_status</th><th>tier_label</th><th>range_text</th>"
                    "<th>text</th><th>non_dps</th><th>value_source</th><th>input_id</th><th>diagnostics</th>"
                    f'</tr></thead><tbody>{line_rows}</tbody></table></div>'
                )
            blocks.append(
                f'<details class="trace"><summary>{_esc(area)} · {_esc(name)} · {_code(instance.get("encoded_id"))}</summary>'
                + _kv(rows)
                + affix_table
                + line_table
                + "</details>"
            )
    return (
        '<section class="diag-section"><h3>原始数据追溯（逐物品）</h3>'
        '<p class="diag-note">encoded_id / source_path / codec / r / ir / ur / tier_index / rollId 与逐行 '
        "readable.diagnostics 均保留在这里，供工程核对；默认视图不显示这些字段。</p>"
        + "".join(blocks)
        + "</section>"
    )


def _diag_context(spec: dict) -> str:
    context = _d(spec.get("context"))
    progression = _d(spec.get("progression"))
    active_buffs = _d(context.get("active_buffs"))
    skills = [
        _text(_g(_d(skill), "ref", "name")) or _num(_d(skill).get("raw")) or "未解析技能"
        for skill in _l(active_buffs.get("skills"))
    ]
    quests = _l(progression.get("completed_quests"))
    rows: list[tuple[str, Any]] = [
        ("context.active_buffs.skills", _code(", ".join(skills) or "未提供")),
        ("context keys", _code(", ".join(sorted(context)) or "未提供")),
        ("progression.completed_quests", _code(f"{len(quests)} 条")),
        (
            "progression keys",
            _code(", ".join(str(key) for key in progression if not str(key).startswith("weaver")) or "未提供"),
        ),
    ]
    raw_details = ""
    for title, value in (("context", context), ("progression", progression), ("extensions", _d(spec.get("extensions")))):
        if not value:
            continue
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(value)
        raw_details += (
            f'<details class="trace"><summary>{_esc(title)} 原始 JSON</summary>'
            f'<pre class="raw-json">{_esc(text)}</pre></details>'
        )
    return (
        '<section class="diag-section"><h3>上下文与进度（context / progression）</h3>'
        + _kv(rows)
        + raw_details
        + "</section>"
    )


def _diag_export(spec: dict) -> str:
    return (
        '<section class="diag-section"><h3>导出</h3>'
        '<p class="diag-note">下方按钮完全在本地生成下载（Blob / data URL），不发起任何网络请求。'
        "原始 BuildSpec JSON 已内嵌在本页诊断区块内，可单独保存供离线追溯。</p>"
        '<div class="export-row">'
        '<button type="button" class="btn" data-export-raw="build-raw" data-filename="build-spec-raw.json">下载原始 BuildSpec JSON</button>'
        '<button type="button" class="btn" data-export-raw="build-data" data-filename="build-view-diagnostics.json">下载诊断摘要 JSON</button>'
        "</div>"
        f'<p class="diag-note">raw JSON 字段数：{len(spec)}；build.json 即本区块内 id="build-raw" 的内容。</p>'
        "</section>"
    )


def _diagnostics_panel(spec: dict, revision_id: str, report: dict | None, mode: dict) -> str:
    return (
        '<div class="diag-head"><h2 class="diag-title">诊断详情 / Diagnostics</h2>'
        '<p class="diag-lead">工程追溯信息（revision、dataset、指纹、编码 ID、source_path、r/ir/ur、tier_index、'
        "coverage、issues、原始 JSON 导出）。默认关闭；普通浏览不需要打开本区块。</p></div>"
        + _diag_revision(spec, revision_id, report)
        + _diag_scope(spec, mode)
        + _diag_coverage(spec, mode)
        + _diag_readable(spec, mode)
        + _diag_trace(spec)
        + _diag_issues(spec)
        + _diag_context(spec)
        + _diag_export(spec)
    )


# ---------------------------------------------------------------------------
# embedded data
# ---------------------------------------------------------------------------
def _embedded_data(
    spec: dict,
    revision_id: str,
    mode: dict,
    counts: dict,
    report: dict | None,
    input_derivation: dict | None = None,
) -> dict:
    meta = _d(spec.get("meta"))
    revision = _d(meta.get("revision"))
    catalog = _d(spec.get("catalog"))
    source_payload = _d(meta.get("source_payload"))
    build_id = _text(meta.get("build_id"))
    report_summary = None
    if report:
        report_summary = {
            "report_version": report.get("report_version"),
            "generated_at": report.get("generated_at"),
            "validation": report.get("validation"),
            "acceptance": report.get("acceptance"),
            "notes": report.get("notes"),
        }
    derivation = input_derivation or {"groups": [], "variable_count": 0}
    groups = derivation.get("groups") or []
    variable_count = derivation.get("variable_count")
    if not isinstance(variable_count, int):
        variable_count = sum(len(_l(group.get("variables"))) for group in groups)
    import_command = (
        f"le_build input import --build {build_id} --file inputs.json "
        f"&& le_build sync --build {build_id}"
    )
    return {
        "mode": "readable" if mode.get("readable_items") == mode.get("total_items") and mode.get("total_items") else "fallback",
        "historical": bool(mode.get("historical")),
        "build_id": build_id,
        "revision_id": revision_id or revision.get("revision_id"),
        "revision": {
            "revision_id": revision.get("revision_id"),
            "revision_seq": revision.get("revision_seq"),
            "content_sha256": revision.get("content_sha256"),
            "published_at": revision.get("published_at"),
        },
        "build_spec_version": spec.get("build_spec_version"),
        "parser_version": spec.get("parser_version"),
        "mapping_version": spec.get("mapping_version"),
        "share_url": meta.get("share_url"),
        "scope": spec.get("scope"),
        "weaver": spec.get("weaver"),
        "local_inputs": _d(meta.get("local_inputs")) or None,
        "local_inputs_template": {
            "version": LOCAL_INPUTS_VERSION,
            "build_id": build_id,
            "import_command": import_command,
            "groups": groups,
            "counts": {"groups": len(groups), "variables": variable_count},
        },
        "catalog": {
            "dataset_id": catalog.get("dataset_id"),
            "source_snapshot_id": catalog.get("source_snapshot_id"),
            "target_game_version": catalog.get("target_game_version"),
            "target_season": catalog.get("target_season"),
            "logical_content_sha256": catalog.get("logical_content_sha256"),
            "file_sha256": catalog.get("file_sha256"),
        },
        "source_payload": {
            "raw_sha256": source_payload.get("raw_sha256"),
            "data_field_sha256": source_payload.get("data_field_sha256"),
            "raw_path": source_payload.get("raw_path"),
        },
        "statuses": spec.get("statuses"),
        "coverage": spec.get("coverage"),
        "counts": counts,
        "readable": {
            "items_with_readable": mode.get("readable_items"),
            "items_total": mode.get("total_items"),
        },
        "issues_total": len(_l(spec.get("issues"))),
        "report": report_summary,
    }


def _embedded_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    # Escape anything that could terminate the JSON script element or be parsed as markup.
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _load_import_report(build_dir: Any) -> dict | None:
    try:
        base = pathlib.Path(build_dir) if build_dir is not None else None
    except TypeError:
        return None
    if base is None:
        return None
    path = base / "import-report.json"
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# section safety wrapper
# ---------------------------------------------------------------------------
def _safe_section(title: str, builder, *args) -> str:
    try:
        return builder(*args)
    except Exception as exc:  # noqa: BLE001 - a viewer must never crash on partial data
        return (
            f'<div class="card empty-state"><h2 class="panel-title">{_esc(title)}</h2>'
            f'<p>未提供：该区域数据缺失或无法渲染（{_esc(type(exc).__name__)}）。</p></div>'
        )


# ---------------------------------------------------------------------------
# static CSS (no external files, no imports, no fonts, no remote urls)
# ---------------------------------------------------------------------------
_CSS = """
:root{
  --bg:#0a0d13; --bg2:#0f1420;
  --panel:#121824; --panel2:#161e2c;
  --line:#26303f; --line-soft:#1d2532;
  --text:#e9e4d8; --muted:#9aa3b2; --dim:#6f7a8b;
  --gold:#e7b96a; --gold-bright:#f5d28a; --amber:#d99a3d;
  --unique:#f0a13d; --base:#9aa4b2; --set:#52b788; --exalted:#c77dff;
  --blue:#7fb2ff; --red:#e06c75; --green:#68c98f;
}
*{box-sizing:border-box}
[hidden]{display:none !important}
html{scroll-behavior:smooth}
body{
  margin:0;color:var(--text);
  font-family:"Segoe UI","Microsoft YaHei","PingFang SC","Noto Sans SC",system-ui,-apple-system,sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased;
  background:
    radial-gradient(1100px 560px at 16% -8%, #1c2436 0%, rgba(28,36,54,0) 62%),
    radial-gradient(900px 500px at 100% 0%, #261f1a 0%, rgba(38,31,26,0) 58%),
    var(--bg);
}
code{font-family:ui-monospace,Consolas,"Cascadia Mono",monospace;font-size:12px;color:#cfd8e3;
  background:#0c1119;border:1px solid var(--line-soft);border-radius:5px;padding:1px 5px;word-break:break-all}
.wrap{max-width:1240px;margin:0 auto;padding:0 22px 70px}
.muted{color:var(--muted)}
h1,h2,h3,h4{line-height:1.3;margin:0}
a{color:var(--gold-bright)}

/* hero */
.hero{padding:38px 0 26px;border-bottom:1px solid var(--line);
  background:linear-gradient(90deg, rgba(231,185,106,.07), rgba(231,185,106,0) 52%)}
.hero-main{max-width:1000px}
.eyebrow{margin:0 0 10px;color:var(--gold);letter-spacing:.22em;font-size:11px;text-transform:uppercase}
.hero-title{margin:0;font-size:clamp(27px,4vw,42px);color:#f6ecd8;letter-spacing:.01em;text-shadow:0 2px 24px rgba(0,0,0,.5)}
.hero-sub{margin:10px 0 14px;color:#c6cdd9;font-size:14px}
.hero-chips{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px}
.hero-chip{display:inline-flex;align-items:center;padding:4px 11px;border:1px solid var(--line);
  border-radius:999px;background:rgba(255,255,255,.025);font-size:11.5px;color:var(--muted)}
.hero-integrity{margin:0;font-size:12.5px;color:#cdd6e2}
.hero-integrity::before{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--green);margin-right:8px;vertical-align:1px}
.notice{margin:14px 0 0;padding:9px 12px;border:1px solid rgba(217,154,61,.45);border-radius:10px;
  background:rgba(217,154,61,.08);color:#e9d4b2;font-size:12.5px}
.notice strong{color:var(--gold-bright)}

/* tabs */
.tabbar{position:sticky;top:0;z-index:40;background:linear-gradient(180deg, rgba(10,13,19,.98), rgba(10,13,19,.93));
  backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 0;margin:0 -6px}
.tabs{display:flex;gap:6px;overflow-x:auto;scrollbar-width:thin;padding:0 6px}
.tab{flex:0 0 auto;appearance:none;background:transparent;border:1px solid transparent;color:var(--muted);
  font:inherit;font-size:13.5px;padding:8px 16px;border-radius:9px;cursor:pointer;transition:all .15s;white-space:nowrap}
.tab:hover{color:var(--text);background:rgba(255,255,255,.04)}
.tab.active{color:#1a1408;background:linear-gradient(180deg,var(--gold-bright),var(--gold));border-color:var(--gold-bright);
  font-weight:700;box-shadow:0 2px 14px rgba(231,185,106,.22)}
.tab:focus-visible,.btn:focus-visible,.search-input:focus-visible,summary:focus-visible{outline:2px solid var(--gold-bright);outline-offset:2px}
.tab-actions{display:flex;gap:8px;justify-content:flex-end;padding:8px 6px 0;flex-wrap:wrap}
.btn{appearance:none;font:inherit;font-size:12px;padding:5px 12px;border-radius:8px;cursor:pointer;
  border:1px solid var(--line);background:rgba(255,255,255,.03);color:var(--muted)}
.btn:hover{color:var(--text);border-color:var(--gold);background:rgba(231,185,106,.08)}
.btn.active{color:#1a1408;background:var(--gold);border-color:var(--gold-bright);font-weight:700}

/* panels */
main{padding-top:8px}
.panel{padding-top:28px}
.panel[hidden]{display:none}
.panel > .card + .card{margin-top:18px}
.panel-head{margin:0 0 18px}
.panel-title{font-size:23px;color:#f4e8d0;display:flex;align-items:center;gap:12px}
.panel-title::before{content:"";width:4px;height:22px;border-radius:4px;
  background:linear-gradient(180deg,var(--gold-bright),var(--amber))}
.panel-lead{margin:8px 0 0;color:var(--muted);font-size:13px;max-width:980px}
.sub-title{font-size:16px;color:var(--gold-bright);margin:0 0 8px;display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.card.spaced{margin-top:20px}
h4{font-size:13px;color:var(--gold);margin:0 0 6px}
.section-lead{margin:0 0 10px;color:var(--muted);font-size:12.5px}
.section-tools{display:flex;align-items:center;gap:10px;margin:10px 0 14px;flex-wrap:wrap}
.search-input{flex:1 1 320px;max-width:520px;font:inherit;font-size:13px;color:var(--text);
  background:#0d121b;border:1px solid var(--line);border-radius:9px;padding:8px 12px}
.search-input::placeholder{color:var(--dim)}
.filter-count{font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.card{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:14px;padding:16px 18px;box-shadow:0 10px 30px rgba(0,0,0,.18)}
.empty-state{color:var(--muted)}

/* overview */
.stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(178px,1fr));gap:12px;margin-bottom:22px}
.stat-card{border:1px solid var(--line);border-radius:12px;padding:12px 14px;background:linear-gradient(180deg,#161e2c,#101622)}
.stat-value{font-size:19px;font-weight:700;color:var(--gold-bright);line-height:1.35;word-break:break-word}
.stat-label{font-size:11.5px;letter-spacing:.05em;color:var(--muted);margin-top:2px}
.stat-hint{font-size:11px;color:var(--dim);margin-top:4px;line-height:1.45}
.integrity-card{border-color:rgba(231,185,106,.28)}
.integrity-line{margin:2px 0 8px;font-size:17px;font-weight:700;color:#f2e4c8;font-variant-numeric:tabular-nums}
.share-line{margin:10px 0 0;font-size:12px;color:var(--muted);word-break:break-all}
.share-url{color:var(--gold-bright);font-family:ui-monospace,Consolas,monospace;font-size:12px}
.scope-card{margin-top:16px}
.scope-list{margin:0;padding-left:20px;color:#c2cad6;font-size:12.5px;line-height:1.75}
.scope-list li{margin:4px 0}

/* items */
.card-grid,.item-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.item-grid.idol-grid,.item-grid.blessing-grid{grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.item-card{position:relative;background:linear-gradient(180deg,var(--panel2),var(--panel));
  border:1px solid var(--line);border-radius:14px;padding:14px 16px 13px;
  box-shadow:0 8px 26px rgba(0,0,0,.16)}
.item-card.absent-card{border-style:dashed;opacity:.9}
.item-head{display:flex;flex-wrap:wrap;gap:7px;align-items:center}
.slot-cn{font-weight:700;color:var(--gold);font-size:13px;letter-spacing:.02em}
.position-badge{font-size:10.5px;padding:1px 8px;border-radius:999px;border:1px solid rgba(127,178,255,.4);
  color:var(--blue);background:rgba(127,178,255,.08)}
.item-name{margin:9px 0 2px;font-size:16.5px;color:#f4e9d4;font-weight:700;letter-spacing:.01em}
.item-base{margin:0;font-size:12px;color:var(--muted)}
.base-label{color:var(--dim);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;margin-right:2px}
.type-sep{color:var(--dim);margin:0 2px}
.item-hint{margin:2px 0 0;font-size:11px;color:var(--dim)}
.item-note{margin:10px 0 0;font-size:11px;line-height:1.55}
.item-empty{margin:12px 0 2px;font-size:12.5px}

/* player-readable lines */
.line-group{margin:12px 0 0;padding:9px 11px;border:1px solid var(--line-soft);border-radius:10px;
  background:rgba(255,255,255,.014)}
.group-title{display:flex;align-items:center;gap:8px;margin:0 0 6px;font-size:11.5px;letter-spacing:.05em;
  color:var(--dim);text-transform:uppercase;font-weight:600}
.group-count{display:inline-flex;align-items:center;justify-content:center;min-width:18px;height:16px;padding:0 5px;
  border-radius:999px;background:rgba(255,255,255,.05);font-size:10px;color:var(--muted)}
.group-implicit{border-color:rgba(127,178,255,.22);background:rgba(127,178,255,.035)}
.group-implicit .group-title{color:#9dbbec}
.group-normal{border-color:rgba(200,209,222,.18)}
.group-sealed{border-color:rgba(127,178,255,.35);background:rgba(127,178,255,.05)}
.group-sealed .group-title{color:var(--blue)}
.group-primordial{border-color:rgba(231,185,106,.35);background:rgba(231,185,106,.045)}
.group-primordial .group-title{color:var(--gold-bright)}
.group-corrupted{border-color:rgba(224,108,117,.35);background:rgba(224,108,117,.05)}
.group-corrupted .group-title{color:var(--red)}
.group-unique{border-color:rgba(240,161,61,.35);background:rgba(240,161,61,.05)}
.group-unique .group-title{color:var(--unique)}
ul.lines{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:5px}
.line{display:flex;flex-wrap:wrap;gap:7px;align-items:baseline;font-size:13px;line-height:1.55}
.line-text{color:#e6ddca}
.line-description .line-text{color:#d9c8a8;font-style:italic}
.line-description .line-text::before{content:"✦ ";color:var(--unique);font-style:normal}
.tier-badge{font-family:ui-monospace,Consolas,monospace;font-size:10px;padding:0 6px;border-radius:5px;
  border:1px solid rgba(231,185,106,.4);background:rgba(231,185,106,.08);color:#dcc290;white-space:nowrap}
.line-flag{font-size:10px;padding:0 6px;border-radius:5px;border:1px solid var(--line);color:var(--dim)}
.line-missing{font-size:11px;padding:0 7px;border-radius:5px;white-space:nowrap;
  color:var(--amber);border:1px solid rgba(217,154,61,.5);background:rgba(217,154,61,.09)}
.line-unresolved .line-missing{color:#e0a2a6;border-color:rgba(224,108,117,.45);background:rgba(224,108,117,.08)}
.line-range .line-text,.line-not_provided .line-text{color:#eadfc8}

/* badges */
.badge{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;line-height:1.6;padding:1px 8px;
  border-radius:999px;border:1px solid var(--line);color:var(--muted);background:rgba(255,255,255,.02);
  white-space:nowrap;vertical-align:middle}
.badge-ok{color:var(--green);border-color:rgba(104,201,143,.45);background:rgba(104,201,143,.09)}
.badge-warn{color:var(--amber);border-color:rgba(217,154,61,.5);background:rgba(217,154,61,.1)}
.badge-bad{color:var(--red);border-color:rgba(224,108,117,.5);background:rgba(224,108,117,.1)}
.badge-gold{color:var(--gold-bright);border-color:rgba(231,185,106,.5);background:rgba(231,185,106,.1)}
.badge-blue{color:var(--blue);border-color:rgba(127,178,255,.42);background:rgba(127,178,255,.08)}
.badge-neutral{color:var(--muted);border-color:var(--line);background:rgba(255,255,255,.02)}
.badge-unique{color:var(--unique);border-color:rgba(240,161,61,.55);background:rgba(240,161,61,.1)}
.badge-set{color:var(--set);border-color:rgba(82,183,136,.5);background:rgba(82,183,136,.09)}
.badge-exalted{color:var(--exalted);border-color:rgba(199,125,255,.45);background:rgba(199,125,255,.08)}
.badge-row{display:flex;flex-wrap:wrap;gap:6px}

/* idols */
.idol-map{display:grid;grid-template-columns:repeat(var(--cols,5),minmax(0,1fr));gap:6px;
  max-width:560px;margin:4px 0 16px;padding:10px;border:1px solid var(--line);border-radius:12px;
  background-image:linear-gradient(rgba(231,185,106,.05) 1px,transparent 1px),
    linear-gradient(90deg,rgba(231,185,106,.05) 1px,transparent 1px);
  background-size:100% 20%,20% 100%;background-color:rgba(10,13,19,.4)}
.idol-cell{display:flex;align-items:center;justify-content:center;padding:5px 3px;border-radius:7px;
  border:1px solid rgba(231,185,106,.3);background:rgba(231,185,106,.07);color:#e5cf9f;
  font-size:10.5px;text-align:center;white-space:nowrap}

/* skills */
.hud-row{list-style:none;margin:10px 0 0;padding:0;display:grid;
  grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:10px}
.hud-slot{display:flex;flex-wrap:wrap;gap:7px;align-items:center;border:1px solid var(--line);border-radius:10px;
  padding:9px 11px;background:linear-gradient(180deg,#182131,#101623)}
.hud-index{width:22px;height:22px;display:inline-flex;align-items:center;justify-content:center;border-radius:6px;
  background:var(--gold);color:#1a1408;font-weight:800;font-size:11px}
.hud-name{font-weight:600;color:#e9dfcd;font-size:13.5px}
.spec-list{display:flex;flex-direction:column;gap:14px;margin-top:4px}
.spec-card{padding:14px 16px}
.spec-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;justify-content:space-between;margin-bottom:6px}
.spec-name{font-size:15.5px;color:var(--gold-bright)}
.node-flag{margin-left:8px;font-size:10.5px;color:var(--amber)}
.node-flag-warn{color:var(--red)}

/* tables */
.table-wrap{overflow-x:auto;border-radius:9px;margin-top:8px}
.data-table{width:100%;border-collapse:collapse;font-size:12.5px}
.data-table th{text-align:left;font-weight:600;font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
  color:var(--dim);padding:7px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
.data-table td{padding:7px 9px;border-bottom:1px solid var(--line-soft);vertical-align:top;color:#d4dae4}
.data-table tbody tr:hover{background:rgba(231,185,106,.045)}
.data-table th[scope="row"]{text-transform:none;letter-spacing:0;color:var(--muted);white-space:nowrap}
.node-table td.node-name{font-weight:600;color:#e8e0d1}
.node-table td.node-points{color:var(--gold-bright);font-weight:700;font-variant-numeric:tabular-nums}

/* diagnostics */
.diag{margin:38px 0 0;border:1px solid var(--line);border-radius:14px;background:rgba(12,17,25,.72)}
.diag>summary{list-style:none;cursor:pointer;user-select:none;display:flex;flex-wrap:wrap;gap:10px;
  align-items:center;padding:14px 18px;color:#d6c9ad;font-weight:600;font-size:14px}
.diag>summary::-webkit-details-marker{display:none}
.diag>summary::before{content:"▸";color:var(--amber);font-size:12px;transition:transform .15s}
.diag[open]>summary::before{transform:rotate(90deg)}
.diag-hint{font-size:11px;font-weight:400;color:var(--dim)}
.diag-body{padding:0 18px 18px;border-top:1px solid var(--line-soft)}
.diag-head{margin:16px 0 6px}
.diag-title{font-size:19px;color:#e9dcc2}
.diag-lead{margin:6px 0 0;font-size:12px;color:var(--muted)}
.diag-section{margin:18px 0 0}
.diag-section h3{font-size:14px;color:var(--gold-bright);margin:0 0 8px}
.diag-note{margin:6px 0;font-size:11.5px;color:var(--dim);line-height:1.6}
.diag-warn{color:#efd9c1}
.issue-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:10px 0 12px}
.chip-btn{display:inline-flex;align-items:center;gap:7px}
.chip-count{font-size:10.5px;opacity:.75;font-variant-numeric:tabular-nums}
.issue-detail{font-size:10.5px;color:var(--dim);margin-top:2px;word-break:break-all}
.issues-table td{font-size:12px}
.coverage-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:4px 26px}
.coverage-row{margin:8px 0}
.coverage-head{display:flex;justify-content:space-between;gap:10px;font-size:12px;color:#cbd3df;margin-bottom:4px}
.coverage-value{color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap}
.bar{height:7px;border-radius:99px;background:#1a2230;overflow:hidden;border:1px solid #1f2937}
.bar>span{display:block;height:100%;border-radius:99px}
.bar-ok{background:linear-gradient(90deg,#2f7d55,var(--green))}
.bar-warn{background:linear-gradient(90deg,#9a6a20,var(--amber))}
.bar-bad{background:linear-gradient(90deg,#8a3740,var(--red))}
.bar-neutral{background:linear-gradient(90deg,#2a3444,#3a475c)}
.coverage-note{margin:3px 0 0;font-size:10.5px}
.kv{margin:0;display:grid;grid-template-columns:auto 1fr;gap:4px 14px;font-size:12px}
.kv dt{color:var(--dim);white-space:nowrap}
.kv dd{margin:0;color:#d8dee8;word-break:break-word}
details.trace{margin:8px 0;border:1px solid var(--line-soft);border-radius:9px;background:rgba(255,255,255,.012);padding:0 10px 8px}
details.trace>summary{cursor:pointer;padding:8px 0;color:var(--muted);font-size:12px;list-style:none;user-select:none}
details.trace>summary::-webkit-details-marker{display:none}
details.trace>summary::before{content:"▸";color:var(--amber);font-size:11px;margin-right:6px;display:inline-block}
details.trace[open]>summary::before{transform:rotate(90deg)}
pre.raw-json{margin:8px 0 0;padding:12px;border:1px solid var(--line-soft);border-radius:10px;
  background:#0a0e15;color:#c5cfdc;font-size:11px;line-height:1.5;max-height:380px;overflow:auto;white-space:pre-wrap;word-break:break-all}
ul.raw-list{margin:4px 0 8px;padding-left:20px;font-size:11.5px;color:var(--muted);line-height:1.65}
.export-row{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0}

/* local missing-value input panel (default-closed) */
.input-panel{margin:34px 0 0;border:1px solid rgba(127,178,255,.28);border-radius:14px;
  background:rgba(12,17,25,.72)}
.input-panel>summary{list-style:none;cursor:pointer;user-select:none;display:flex;flex-wrap:wrap;gap:10px;
  align-items:center;padding:14px 18px;color:#d3dcec;font-weight:600;font-size:14px}
.input-panel>summary::-webkit-details-marker{display:none}
.input-panel>summary::before{content:"▸";color:var(--blue);font-size:12px;transition:transform .15s}
.input-panel[open]>summary::before{transform:rotate(90deg)}
.input-hint{font-size:11px;font-weight:400;color:var(--dim)}
.input-body{padding:0 18px 18px;border-top:1px solid var(--line-soft)}
.input-lead{margin:14px 0 10px;font-size:12.5px;color:#c9d3e2;line-height:1.7;max-width:1000px}
.input-lead code{font-size:11.5px}
.input-group{margin:20px 0 0;padding:12px 14px 10px;border:1px solid var(--line-soft);border-radius:12px;
  background:rgba(255,255,255,.014)}
.input-group-title{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 4px;font-size:13.5px;color:#bcd0f2}
.input-group-item{font-weight:700;color:#e6dcc7;margin-right:2px}
.input-group-lead{margin:0 0 4px;font-size:11.5px;line-height:1.6}
.input-group-note{margin:2px 0 6px;font-size:11px;color:var(--amber);line-height:1.55}
.input-empty{margin:8px 0;font-size:12px}
.input-table td{font-size:12.5px;vertical-align:middle}
.input-variable-table td{vertical-align:middle}
.input-variable-table th{font-size:10px}
.input-table td.input-item{font-weight:600;color:#e6dcc7;white-space:nowrap}
.input-table td.input-meta{color:var(--muted);white-space:nowrap}
.input-table td.input-line,.input-variable-table td.input-line{color:#dbe2ec;min-width:190px;max-width:430px}
.input-var-label{display:block;color:#e9e0cf;font-weight:600}
.input-var-hint{display:block;font-size:10.5px;color:var(--dim);margin-top:1px}
.input-row-error{display:none;margin-top:3px;font-size:11px;color:var(--red);line-height:1.5}
.input-row.invalid .input-row-error{display:block}
.input-table td.input-range,.input-variable-table td.input-range{white-space:nowrap;font-variant-numeric:tabular-nums}
.input-table td.input-cell,.input-variable-table td.input-cell{white-space:nowrap}
input.input-roll{width:96px;font:inherit;font-size:12.5px;color:var(--text);background:#0d121b;
  border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-variant-numeric:tabular-nums}
input.input-roll:focus{outline:2px solid var(--blue);outline-offset:1px}
input.input-roll.input-disabled,input.input-roll:disabled{opacity:.45;cursor:not-allowed;border-style:dashed}
input.input-display{width:118px;font:inherit;font-size:12.5px;color:var(--text);background:#0d121b;
  border:1px solid var(--line);border-radius:8px;padding:5px 9px;font-variant-numeric:tabular-nums}
input.input-display:focus{outline:2px solid var(--blue);outline-offset:1px}
input.input-display.invalid,input.input-roll.invalid{border-color:var(--red);background:rgba(224,108,117,.08)}
input.input-note{width:160px;font:inherit;font-size:12px;color:var(--text);background:#0d121b;
  border:1px solid var(--line-soft);border-radius:8px;padding:5px 9px}
.input-row.invalid td.input-cell{background:rgba(224,108,117,.09)}
.input-advanced{margin:8px 0 2px;border-top:1px dashed var(--line-soft);padding-top:7px}
.input-advanced>summary{cursor:pointer;list-style:none;user-select:none;font-size:11.5px;color:var(--muted)}
.input-advanced>summary::-webkit-details-marker{display:none}
.input-advanced>summary::before{content:"▸";color:var(--blue);font-size:10px;margin-right:6px;display:inline-block}
.input-advanced[open]>summary::before{transform:rotate(90deg)}
.input-advanced-body{display:flex;flex-wrap:wrap;gap:10px;align-items:center;padding:8px 0 2px}
.input-advanced-label{font-size:12px;color:var(--muted);display:inline-flex;gap:8px;align-items:center}
.input-advanced-hint{flex:1 1 320px;font-size:11px;line-height:1.55}
.input-group-display-mode .input-advanced-hint{color:var(--green)}
.input-actions{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:18px 0 4px}
.input-status{margin:6px 0;font-size:12px;color:var(--amber);min-height:1em}
.input-status.ok{color:var(--green)}
.input-status.warn{color:var(--amber)}
.input-status.bad{color:var(--red)}
.input-help{margin:8px 0 0;font-size:11.5px;line-height:1.7;word-break:break-word}
.input-command{display:inline-block;margin-top:2px}
.badge-manual{color:var(--muted);border-color:var(--line);background:rgba(255,255,255,.03);
  font-size:10px;letter-spacing:.04em;padding:1px 7px}
.local-input-line{margin:8px 0 0;font-size:12.5px;color:#cfe0c9}
.local-input-line::before{content:"✓";display:inline-block;color:var(--green);margin-right:7px;font-weight:700}
.btn-gold{color:#1a1408;background:linear-gradient(180deg,var(--gold-bright),var(--gold));
  border-color:var(--gold-bright);font-weight:700}
.warn-box{margin:10px 0;padding:9px 11px;border:1px solid rgba(224,108,117,.4);border-radius:10px;
  background:rgba(224,108,117,.07);font-size:12px;color:#e6c9cb}
.warn-box p{margin:6px 0 0}

/* footer */
.footer{margin-top:34px;padding-top:20px;border-top:1px solid var(--line);color:var(--muted);font-size:11.5px}
.footer-note{margin:6px 0;font-size:11px;color:var(--dim);line-height:1.65}

@media (max-width:900px){
  .hero{padding-top:26px}
  .item-grid,.card-grid{grid-template-columns:1fr}
}
@media (max-width:640px){
  .wrap{padding:0 13px 56px}
  .stat-grid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr))}
  .idol-map{grid-template-columns:repeat(3,minmax(0,1fr))}
  .idol-cell{grid-column:auto !important;grid-row:auto !important}
}

@media print{
  body{background:#fff !important;color:#111 !important;font-size:10.5px}
  .tabbar,.tab-actions,.section-tools,.issue-toolbar,.search-input,.diag,.export-row,.input-panel{display:none !important}
  .panel[hidden]{display:block !important}
  .hero{padding:8px 0;border-color:#999;background:none !important}
  .hero-title,.panel-title,.sub-title,.spec-name,h4,.eyebrow,.slot-cn,.group-title,.integrity-line{color:#111 !important;text-shadow:none}
  .panel-title::before{background:#333}
  .card,.item-card,.stat-card,.hud-slot,.line-group{background:#fff !important;border-color:#bbb !important;box-shadow:none}
  .stat-value,.node-points,.coverage-value{color:#111 !important}
  .line-text,.line-description .line-text{color:#111 !important}
  .muted,.section-lead,.panel-lead,.item-note,.item-empty,.group-title,.footer-note,.diag-note{color:#444 !important}
  .line-missing{color:#7a4a00 !important;border-color:#cc9a4a !important;background:#fdf6e8 !important}
  .tier-badge{color:#5a4313 !important;border-color:#b99b5a !important;background:#f7efdd !important}
  code{background:#f3f3f3 !important;color:#111 !important;border-color:#ccc !important}
  .badge{color:#111 !important;border-color:#999 !important;background:#fafafa !important}
  .item-card,.stat-card,.card,.line-group{break-inside:avoid}
  main .panel{padding-top:12px}
  .panel-head{margin-bottom:10px}
}

/* refracted idol selector */
.refract-options{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:6px 18px;margin-top:10px}
.refract-option{display:flex;align-items:flex-start;gap:7px;font-size:12.5px;color:#d4dae4;cursor:pointer}
.refract-option input{margin-top:2px}
.refract-actions{margin-top:10px}
#refraction-detail{margin-top:8px;font-size:12px;line-height:1.6}
#refraction-detail ul{margin:4px 0 8px 18px;padding:0}
#refraction-detail li{margin:2px 0}
#refraction-summary{color:#e8e0d1;font-weight:600}
"""


# ---------------------------------------------------------------------------
# vanilla JS: tabs, search filters, severity filter, raw JSON download, print
# ---------------------------------------------------------------------------
_JS = """
(function () {
  "use strict";

  /* ---- tabs ---------------------------------------------------------- */
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-tab]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-panel]"));

  function showTab(name) {
    tabs.forEach(function (tab) {
      var active = tab.getAttribute("data-tab") === name;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
      tab.setAttribute("tabindex", active ? "0" : "-1");
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== name;
    });
  }

  function tabName(tab) {
    return tab ? tab.getAttribute("data-tab") : "";
  }

  if (tabs.length) {
    tabs.forEach(function (tab, index) {
      tab.addEventListener("click", function () {
        showTab(tabName(tab));
        if (location.protocol !== "file:" && window.history && history.replaceState) {
          try { history.replaceState(null, "", "#" + tabName(tab)); } catch (err) { /* ignore */ }
        }
      });
      tab.addEventListener("keydown", function (event) {
        var next = null;
        if (event.key === "ArrowRight" || event.key === "ArrowDown") next = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft" || event.key === "ArrowUp") next = tabs[(index - 1 + tabs.length) % tabs.length];
        if (event.key === "Home") next = tabs[0];
        if (event.key === "End") next = tabs[tabs.length - 1];
        if (next) {
          event.preventDefault();
          showTab(tabName(next));
          next.focus();
        }
      });
    });
    var initial = "";
    try { initial = (location.hash || "").replace("#", ""); } catch (err) { initial = ""; }
    var known = tabs.some(function (tab) { return tabName(tab) === initial; });
    showTab(known && initial ? initial : tabName(tabs[0]));
  }

  /* ---- search filters ------------------------------------------------ */
  Array.prototype.slice.call(document.querySelectorAll("input[data-search]")).forEach(function (input) {
    var selector = input.getAttribute("data-search") || "";
    var scope = input.closest("[data-panel]") || document;
    var items = selector ? Array.prototype.slice.call(scope.querySelectorAll(selector)) : [];
    var toolbar = input.closest(".section-tools") || input.parentElement;
    var counter = (toolbar && toolbar.querySelector("[data-filter-count]")) || scope.querySelector("[data-filter-count]");
    input.addEventListener("input", function () {
      var query = (input.value || "").trim().toLowerCase();
      var shown = 0;
      items.forEach(function (item) {
        var hit = query === "" || (item.textContent || "").toLowerCase().indexOf(query) !== -1;
        item.hidden = !hit;
        if (hit) { shown += 1; }
      });
      if (counter) { counter.textContent = query === "" ? "" : "匹配 " + shown + " / " + items.length; }
    });
  });

  /* ---- diagnostics severity filter ----------------------------------- */
  var severityButtons = Array.prototype.slice.call(document.querySelectorAll("[data-severity]"));
  var issueRows = Array.prototype.slice.call(document.querySelectorAll(".issue-row"));
  severityButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      severityButtons.forEach(function (other) { other.classList.toggle("active", other === button); });
      var severity = button.getAttribute("data-severity");
      var shown = 0;
      issueRows.forEach(function (row) {
        var hit = severity === "all" || row.getAttribute("data-sev") === severity;
        row.hidden = !hit;
        if (hit) { shown += 1; }
      });
      var counter = document.querySelector("[data-issue-count]");
      if (counter) { counter.textContent = shown + " / " + issueRows.length; }
    });
  });

  /* ---- raw JSON download (local only; no network) -------------------- */
  function triggerDownload(name, text) {
    try {
      var blob = new Blob([text], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = name;
      document.body.appendChild(link);
      link.click();
      setTimeout(function () {
        URL.revokeObjectURL(url);
        if (link.parentNode) { link.parentNode.removeChild(link); }
      }, 0);
      return true;
    } catch (err) { /* fall through to data URL */ }
    try {
      var dataUrl = "data:application/json;charset=utf-8," + encodeURIComponent(text);
      var anchor = document.createElement("a");
      anchor.href = dataUrl;
      anchor.download = name;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      return true;
    } catch (err2) { return false; }
  }

  Array.prototype.slice.call(document.querySelectorAll("[data-export-raw]")).forEach(function (button) {
    button.addEventListener("click", function () {
      var node = document.getElementById(button.getAttribute("data-export-raw"));
      if (!node) { return; }
      var text = node.textContent || "";
      var name = button.getAttribute("data-filename") || "build-data.json";
      triggerDownload(name, text);
    });
  });

  /* ---- local missing-value inputs (per variable, build-local-inputs-v1) */
  var viewDataNode = document.getElementById("build-data");
  var viewData = null;
  try { viewData = JSON.parse(viewDataNode ? (viewDataNode.textContent || "{}") : "{}"); } catch (err) { viewData = null; }
  var localTemplate = viewData && viewData.local_inputs_template;
  var inputPanel = document.getElementById("local-inputs");
  if (localTemplate && inputPanel) {
    var inputGroups = localTemplate.groups || [];
    var inputStatus = inputPanel.querySelector("[data-input-status]");
    var inputCount = inputPanel.querySelector("[data-input-count]");
    var totalVariables = (localTemplate.counts && localTemplate.counts.variables) || 0;
    var groupNodes = {};
    Array.prototype.slice.call(inputPanel.querySelectorAll(".input-group")).forEach(function (container) {
      var key = container.getAttribute("data-group-key");
      if (!key) { return; }
      groupNodes[key] = {
        container: container,
        roll: container.querySelector("input.input-roll"),
        variables: []
      };
    });

    function two(n) { return (n < 10 ? "0" : "") + n; }
    function pad4(n) { var s = String(n); while (s.length < 4) { s = "0" + s; } return s; }
    function offsetText(date) {
      var minutes = -date.getTimezoneOffset();
      var sign = minutes >= 0 ? "+" : "-";
      var abs = Math.abs(minutes);
      return sign + two(Math.floor(abs / 60)) + ":" + two(abs % 60);
    }
    function isoStamp(date) {
      return date.getFullYear() + "-" + two(date.getMonth() + 1) + "-" + two(date.getDate()) +
        "T" + two(date.getHours()) + ":" + two(date.getMinutes()) + ":" + two(date.getSeconds()) + offsetText(date);
    }
    function compactStamp(date) {
      return "" + date.getFullYear() + two(date.getMonth() + 1) + two(date.getDate()) +
        "T" + two(date.getHours()) + two(date.getMinutes()) + two(date.getSeconds());
    }
    function formatRange(variable) {
      if (variable.range_text) { return variable.range_text; }
      var low = variable.display_min;
      var high = variable.display_max;
      if (typeof low === "number" && typeof high === "number") { return low + "–" + high; }
      if (typeof low === "number") { return "≥ " + low; }
      if (typeof high === "number") { return "≤ " + high; }
      return "";
    }
    function parseDisplayValue(text, variable) {
      var raw = String(text == null ? "" : text).trim();
      if (raw === "") { return { state: "empty" }; }
      var body = raw;
      if (variable.percent && body.charAt(body.length - 1) === "%") { body = body.slice(0, -1).trim(); }
      if (!/^-?[0-9]+(\\.[0-9]+)?$/.test(body) && !/^-?\\.[0-9]+$/.test(body)) { return { state: "invalid", text: raw }; }
      var number = Number(body);
      if (!isFinite(number)) { return { state: "invalid", text: raw }; }
      var low = variable.display_min;
      var high = variable.display_max;
      var rangeIncludesNegative = typeof low === "number" && low < 0;
      if (number < 0 && !rangeIncludesNegative) { return { state: "out_of_range", text: raw, low: low, high: high }; }
      if (typeof low === "number" && number < low - 1e-9) { return { state: "out_of_range", text: raw, low: low, high: high }; }
      if (typeof high === "number" && number > high + 1e-9) { return { state: "out_of_range", text: raw, low: low, high: high }; }
      return { state: "ok", value: number };
    }
    function parseRollValue(text) {
      var trimmed = String(text == null ? "" : text).trim();
      if (trimmed === "") { return { state: "empty" }; }
      if (!/^[0-9]+$/.test(trimmed)) { return { state: "invalid" }; }
      var number = Number(trimmed);
      if (!isFinite(number) || Math.floor(number) !== number || number < 0 || number > 255) { return { state: "invalid" }; }
      return { state: "ok", value: number };
    }
    function setInputStatus(text, tone) {
      if (!inputStatus) { return; }
      inputStatus.textContent = text || "";
      inputStatus.className = "input-status" + (tone ? " " + tone : "");
    }
    function hasDisplayValue(node) {
      return node.variables.some(function (item) {
        return item.display && String(item.display.value || "").trim() !== "";
      });
    }
    function syncGroupMode(node) {
      var displayMode = hasDisplayValue(node);
      if (node.roll) {
        node.roll.disabled = displayMode;
        node.roll.classList.toggle("input-disabled", displayMode);
      }
      if (node.container) { node.container.classList.toggle("input-group-display-mode", displayMode); }
    }
    function collectInputs() {
      var now = new Date();
      var stamp = compactStamp(now);
      var iso = isoStamp(now);
      var entries = [];
      var invalid = 0;
      var sequence = 0;
      var instanceKey = "instance" + "_path";
      var rollKey = "roll" + "_id";
      inputGroups.forEach(function (group) {
        var node = groupNodes[group.key];
        if (!node) { return; }
        node.variables.forEach(function (item) {
          if (item.display && String(item.display.value || "").trim() === "") {
            if (item.row) {
              item.row.classList.remove("invalid");
              var errorNode = item.row.querySelector("[data-row-error]");
              if (errorNode) { errorNode.textContent = ""; }
            }
            if (item.display) { item.display.classList.remove("invalid"); }
          }
        });
        var filled = node.variables.filter(function (item) {
          return item.display && String(item.display.value || "").trim() !== "";
        });
        if (filled.length) {
          filled.forEach(function (item) {
            var parsed = parseDisplayValue(item.display.value, item.definition);
            if (parsed.state !== "ok") {
              invalid += 1;
              if (item.row) {
                item.row.classList.add("invalid");
                var errorNode = item.row.querySelector("[data-row-error]");
                if (errorNode) {
                  if (parsed.state === "out_of_range") {
                    var rangeLabel = formatRange(item.definition) || "声明范围";
                    errorNode.textContent = parsed.text + " 超出范围 " + rangeLabel + "，未写入文件";
                  } else {
                    errorNode.textContent = "无效数值（需数字，可带 %），未写入文件";
                  }
                }
              }
              if (item.display) { item.display.classList.add("invalid"); }
              return;
            }
            if (item.row) {
              item.row.classList.remove("invalid");
              var existingError = item.row.querySelector("[data-row-error]");
              if (existingError) { existingError.textContent = ""; }
            }
            if (item.display) { item.display.classList.remove("invalid"); }
            sequence += 1;
            var entry = {
              input_id: "in-" + stamp + "-" + pad4(sequence),
              kind: "explicit_value",
              target: group.target
            };
            entry[instanceKey] = group[instanceKey];
            if (group.target === "unique_mod") { entry.mod_index = item.definition.mod_index; }
            if (group.target === "affix") { entry.affix_value_index = item.definition.affix_value_index; }
            if (group.target === "implicit") { entry.index = item.definition.index; }
            entry.value_numeric = item.definition.percent ? parsed.value / 100 : parsed.value;
            entry.display_value = parsed.value;
            entry.display_unit = item.definition.percent ? "percent" : "flat";
            entry.created_at = iso;
            var note = item.note ? String(item.note.value || "").trim() : "";
            if (note) { entry.note = note; }
            entries.push(entry);
          });
          return;
        }
        var rollParsed = node.roll ? parseRollValue(node.roll.value) : { state: "empty" };
        if (rollParsed.state === "empty") { return; }
        if (rollParsed.state !== "ok") {
          invalid += 1;
          if (node.roll) { node.roll.classList.add("invalid"); }
          return;
        }
        if (node.roll) { node.roll.classList.remove("invalid"); }
        sequence += 1;
        var rollEntry = {
          input_id: "in-" + stamp + "-" + pad4(sequence),
          kind: group.kind
        };
        rollEntry[instanceKey] = group[instanceKey];
        if (group.kind === "unique_mod_roll") { rollEntry[rollKey] = group[rollKey]; }
        if (group.kind === "implicit_roll") { rollEntry.index = group.index; }
        rollEntry.roll = rollParsed.value;
        rollEntry.created_at = iso;
        entries.push(rollEntry);
      });
      return {
        document: {
          version: localTemplate.version,
          build_id: localTemplate.build_id,
          updated_at: iso,
          entries: entries
        },
        entries: entries,
        invalid: invalid
      };
    }
    function refreshInputCount() {
      var result = collectInputs();
      var explicitCount = 0;
      var rollCount = 0;
      result.entries.forEach(function (entry) {
        if (entry.kind === "explicit_value") { explicitCount += 1; } else { rollCount += 1; }
      });
      if (inputCount) {
        inputCount.textContent = "已填写 " + explicitCount + " / " + totalVariables + " 变量" +
          (rollCount ? " · " + rollCount + " 组 roll" : "") +
          (result.invalid ? " · 无效 " + result.invalid : "");
      }
      return result;
    }

    inputGroups.forEach(function (group) {
      var node = groupNodes[group.key];
      if (!node) { return; }
      node.variables = (group.variables || []).map(function (variable) {
        var row = node.container.querySelector('tr.input-variable-row[data-variable-key="' + variable.key + '"]');
        return {
          definition: variable,
          row: row,
          display: row ? row.querySelector("input.input-display") : null,
          note: row ? row.querySelector("input.input-note") : null
        };
      });
      node.variables.forEach(function (item) {
        if (item.display) {
          item.display.addEventListener("input", function () { syncGroupMode(node); refreshInputCount(); });
        }
        if (item.note) { item.note.addEventListener("input", refreshInputCount); }
      });
      if (node.roll) {\n        node.roll.addEventListener("input", function () { syncGroupMode(node); refreshInputCount(); });\n      }
      syncGroupMode(node);
    });

    var downloadButton = inputPanel.querySelector("[data-input-download]");
    if (downloadButton) {
      downloadButton.addEventListener("click", function () {
        var result = refreshInputCount();
        if (!result.entries.length) {
          setInputStatus("请至少填写一个显示值（或一组 roll）后再下载。", "bad");
          return;
        }
        triggerDownload("inputs.json", JSON.stringify(result.document, null, 2));
        if (result.invalid) {
          setInputStatus("已生成 inputs.json（" + result.entries.length + " 项）；另有 " + result.invalid +
            " 个数值无效（需非负数字 / 在声明范围内），未写入文件。", "warn");
        } else {
          setInputStatus("已生成 inputs.json（" + result.entries.length +
            " 项）。下一步：复制导入命令并在项目根目录执行。", "ok");
        }
      });
    }

    var copyButton = inputPanel.querySelector("[data-input-copy]");
    if (copyButton) {
      copyButton.addEventListener("click", function () {
        var text = localTemplate.import_command || "";
        function fallbackCopy() {
          var ok = false;
          try {
            var area = document.createElement("textarea");
            area.value = text;
            area.setAttribute("readonly", "");
            area.style.position = "fixed";
            area.style.left = "-1000px";
            area.style.top = "0";
            document.body.appendChild(area);
            area.select();
            ok = !!(document.execCommand && document.execCommand("copy"));
            area.remove();
          } catch (err) { ok = false; }
          setInputStatus(ok ? "导入命令已复制到剪贴板。" : "无法自动复制，请手动选择下方命令文本。", ok ? "ok" : "warn");
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () {
            setInputStatus("导入命令已复制到剪贴板。", "ok");
          }, fallbackCopy);
        } else {
          fallbackCopy();
        }
      });
    }
    refreshInputCount();
  }
  /* ---- print: show every business section ---------------------------- */
  function expandForPrint() {
    Array.prototype.slice.call(document.querySelectorAll("[data-panel]")).forEach(function (panel) {
      panel.hidden = false;
      panel.setAttribute("data-print-open", "1");
    });
  }
  function restoreAfterPrint() {
    Array.prototype.slice.call(document.querySelectorAll("[data-print-open]")).forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== tabName(document.querySelector("[data-tab].active"));
      panel.removeAttribute("data-print-open");
    });
  }
  if (window.addEventListener) {
    window.addEventListener("beforeprint", expandForPrint);
    window.addEventListener("afterprint", restoreAfterPrint);
  }
  Array.prototype.slice.call(document.querySelectorAll("[data-print]")).forEach(function (button) {
    button.addEventListener("click", function () {
      var run = function () {
        expandForPrint();
        try { window.print(); } finally { restoreAfterPrint(); }
      };
      if (window.requestAnimationFrame) { window.requestAnimationFrame(run); } else { run(); }
    });
  });
})();

/* ---- refracted idol selector (manual) ------------------------------- */
(function () {
  var dataEl = document.getElementById("idol-refraction-data");
  if (!dataEl) return;
  var data;
  try { data = JSON.parse(dataEl.textContent || "{}"); } catch (err) { return; }
  var checks = Array.prototype.slice.call(document.querySelectorAll(".refract-check"));
  var summary = document.getElementById("refraction-summary");
  var detail = document.getElementById("refraction-detail");
  var exportButton = document.getElementById("refraction-export");
  if (!checks.length || !summary || !detail) return;
  var storeKey = "le-idol-refraction:" + (data.build_id || "save");
  var lastTotal = Number(data.base_fire) || 0;
  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }
  function fmtPct(value) { return (value * 100).toFixed(2).replace(/[.]00$/, "") + "%"; }
  function fmtMod(value, unit) {
    if (unit === "percent") return fmtPct(value);
    return (Math.abs(value) >= 10 ? value.toFixed(0) : value.toFixed(2).replace(/[.]00$/, ""));
  }
  function fmtTotal(value) { return value.toFixed(2) + "%（面板约 " + Math.round(value) + "%）"; }
  function update() {
    var total = Number(data.base_fire) || 0;
    var selected = [];
    var parts = [];
    checks.forEach(function (check) {
      if (!check.checked) return;
      var idx = parseInt(check.getAttribute("data-idol-index"), 10);
      selected.push(idx);
      var idol = null;
      (data.idols || []).forEach(function (item) { if (item.index === idx) { idol = item; } });
      if (!idol) return;
      var lines = [];
      (idol.mods || []).forEach(function (mod) {
        var effect = Number(mod.effect) || 0;
        var value = Number(mod.value) || 0;
        if (mod.fire_related) { total += value * effect * 100; }
        var amplified = value * (1 + effect);
        lines.push("<li>" + esc(mod.slot) + " · " + esc(mod.stat_name) + "：" + esc(mod.display)
          + " → " + esc(fmtMod(amplified, mod.unit)) + "（+" + esc(fmtPct(effect)) + "）</li>");
      });
      parts.push("<div><strong>神像 " + (idol.index + 1) + " · " + esc(idol.name) + " " + esc(idol.position)
        + "</strong><ul>" + lines.join("") + "</ul></div>");
    });
    lastTotal = total;
    summary.textContent = "已选 " + (selected.length
      ? selected.map(function (i) { return i + 1; }).join("、") : "无")
      + "；未封顶火抗 " + fmtTotal(total);
    detail.innerHTML = parts.length
      ? parts.join("")
      : "<p>未勾选任何神像。默认候选 0/1/6 得到 948.76%（≈949%）；请按游戏内紫色折射栏位调整。</p>";
  }
  function save() {
    try {
      var selected = checks.filter(function (check) { return check.checked; })
        .map(function (check) { return parseInt(check.getAttribute("data-idol-index"), 10); });
      window.localStorage.setItem(storeKey, JSON.stringify(selected));
    } catch (err) { /* localStorage may be unavailable under file:// */ }
  }
  function restore() {
    try {
      var raw = window.localStorage.getItem(storeKey);
      if (!raw) return;
      var saved = JSON.parse(raw);
      if (!Array.isArray(saved)) return;
      checks.forEach(function (check) {
        check.checked = saved.indexOf(parseInt(check.getAttribute("data-idol-index"), 10)) !== -1;
      });
    } catch (err) { /* ignore */ }
  }
  checks.forEach(function (check) {
    check.addEventListener("change", function () { update(); save(); });
  });
  restore();
  update();
  if (exportButton) {
    exportButton.addEventListener("click", function () {
      var selected = checks.filter(function (check) { return check.checked; })
        .map(function (check) { return parseInt(check.getAttribute("data-idol-index"), 10); });
      var payload = {
        build_id: data.build_id,
        refracted_idols: selected,
        base_fire: data.base_fire,
        altar_prefix_effect: data.altar_prefix_effect,
        altar_suffix_effect: data.altar_suffix_effect,
        uncapped_fire: lastTotal
      };
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = "idol-refraction-selection.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
  }
})();
"""


# ---------------------------------------------------------------------------
# public entry point
# ---------------------------------------------------------------------------
def render_build_html(spec: dict, *, revision_id: str, build_dir: pathlib.Path) -> str:
    """Render one self-contained, offline HTML document for a BuildSpec revision.

    ``spec`` is the parsed ``build.json`` content, ``revision_id`` the revision the
    CLI loaded, and ``build_dir`` the immutable revision directory (used only to
    optionally read ``import-report.json``; it is not required).
    """
    document_spec = spec if isinstance(spec, dict) else {}
    report = _load_import_report(build_dir)
    mode = _revision_mode(document_spec)
    counts = _counts(document_spec)
    facts = _character_facts(document_spec)
    build_id = _text(_d(document_spec.get("meta")).get("build_id"))
    input_derivation = _variable_input_groups(document_spec)
    title_name = " · ".join(bit for bit in (facts["class"], facts["mastery"]) if bit) or "本地构筑"
    title = f"{title_name} 构筑 · 本地离线查看"

    panels = (
        ("overview", "构筑概览", _safe_section("构筑概览", _overview_panel, document_spec, counts)),
        ("equipment", "装备", _safe_section("装备", _equipment_panel, document_spec, counts)),
        ("skills", "技能与被动", _safe_section("技能与被动", _skills_panel, document_spec, counts)),
        ("relics", "神像与祝福", _safe_section("神像与祝福", _relics_panel, document_spec, counts)),
    )
    tabs_html = "".join(
        f'<button type="button" class="tab{" active" if index == 0 else ""}" role="tab" id="tab-{key}" '
        f'data-tab="{key}" aria-controls="panel-{key}" '
        f'aria-selected="{"true" if index == 0 else "false"}" tabindex="{"0" if index == 0 else "-1"}">'
        f"{_esc(label)}</button>"
        for index, (key, label, _content) in enumerate(panels)
    )
    panels_html = "".join(
        f'<section class="panel" id="panel-{key}" data-panel="{key}" role="tabpanel" aria-labelledby="tab-{key}"'
        + ("" if index == 0 else " hidden")
        + f">{content}</section>"
        for index, (key, _label, content) in enumerate(panels)
    )
    diagnostics = _safe_section("诊断详情", _diagnostics_panel, document_spec, revision_id, report, mode)
    input_panel = _safe_section(
        "补充缺失数值", _input_panel_html, document_spec, input_derivation, build_id
    )
    embedded = _embedded_data(document_spec, revision_id, mode, counts, report, input_derivation)
    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n'
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        '<meta name="color-scheme" content="dark">\n'
        '<meta name="generator" content="le_build.viewer (offline build viewer)">\n'
        f"<title>{_esc(title)}</title>\n"
        f"<style>{_CSS}</style>\n"
        "</head>\n<body>\n"
        '<div class="wrap">\n'
        + _hero(document_spec, revision_id=revision_id, mode=mode, counts=counts)
        + '<div class="tabbar">'
        f'<nav class="tabs" role="tablist" aria-label="页面分区">{tabs_html}</nav>'
        '<div class="tab-actions">'
        '<button type="button" class="btn" data-print>打印 / PDF</button>'
        "</div></div>\n"
        f"<main>{panels_html}</main>\n"
        "<!-- diagnostics:start -->\n"
        f"{input_panel}\n"
        '<details id="diagnostics" class="diag" data-diag>'
        '<summary class="diag-summary">诊断详情 / Diagnostics'
        '<span class="diag-hint">工程追溯信息，默认收起，不影响普通浏览</span></summary>'
        f'<div class="diag-body">{diagnostics}</div></details>\n'
        '<script type="application/json" id="build-data">'
        + _embedded_json(embedded)
        + "</script>\n"
        '<script type="application/json" id="build-raw">'
        + _embedded_json(document_spec)
        + "</script>\n"
        "<!-- diagnostics:end -->\n"
        + _footer(document_spec)
        + "\n</div>\n"
        f"<script>{_JS}</script>\n"
        "</body>\n</html>\n"
    )
