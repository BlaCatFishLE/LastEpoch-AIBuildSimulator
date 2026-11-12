"""Player-readable source catalogue for the offline damage report.

The engine result already carries a complete ``source_ledger`` - one row per
consumed modifier, node effect, blessing, base value or derived attribute.  That
ledger is written for auditing (``contribution_id``, ``stat_key``, tags,
evidence locators), not for reading.  This module turns every row into a stable
display object so all pages can show the same name, slot, effect and value::

    {
      "id": "mod:equipment.weapon1#affix#normal#0.0",
      "category": "equipment", "category_label": "装备",
      "slot": "weapon1", "slot_label": "武器",
      "name": "Spine of Malatros",
      "display_name": "武器 · Spine of Malatros",
      "channel": "normal", "channel_label": "普通词缀",
      "raw_text": "+22% Fire Penetration",
      "imported_display": "40%",
      "stat_label": "火焰穿透",
      "effect_label": "火焰穿透 +40%",
      "kind": "fraction", "value": 0.4,
      "included": true, "status": "confirmed", "status_label": "已确认",
      "condition_status": "always", "condition_label": "恒定生效",
      "reason": null, "notes": null, "points": null,
      "evidence": [{"kind": ..., "detail": ..., "locator": ...}],
      "advanced": {...raw ids...},
      "applies": [],          # filled in by present.py from real consumption sites
    }

Nothing here recomputes a damage number: values are copied from the ledger.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from . import labels, units
from .player_text import clean_engineering_text, player_raw_text, player_reason

__all__ = [
    "build_sources",
    "collect_skill_node_names",
    "skill_node_index",
    "synthetic_source",
    "source_order_key",
    "source_identity",
    "conditional_category",
    "conditional_category_label",
    "conditional_source_name",
    "conditional_source_detail",
    "baseline_contribution_ids",
    "group_conditional_effects",
    "effect_source_identity",
    "find_source_group",
    "player_effect_name_parts",
    "player_effect_owner",
    "player_effect_condition",
    "player_effect_attributes",
    "player_source_attributes",
    "player_source_detail",
    "player_source_title",
    "player_attribute_name",
    "is_reference_value_line",
    "BRANCH_LABELS",
    "CONDITIONAL_CATEGORY_LABELS",
]

_ITEM_RE = re.compile(r"^(?P<name>.+?)\s*\[(?P<container>[A-Za-z0-9_.]+)\]$")
_PASSIVE_RE = re.compile(
    r"Mage passive '(?P<name>[^']*)' \((?P<tree>[A-Za-z0-9-]+) node (?P<node>\d+), (?P<points>\d+) pts?\)"
)
_SKILL_RE = re.compile(r"^Black Hole skill node (?P<node>\d+)$")
_NODE_GROUP_RE = re.compile(r"node:bh2:(?P<node>\d+)")
_SKILL_CID_RE = re.compile(r"^skill:bh2:(?P<node>\d+):(?P<suffix>[A-Za-z0-9_.:-]+)")
_DISPLAY_VALUE_RE = re.compile(r"display_value='([^']*)'")
_POINTS_RE = re.compile(r"(?P<points>\d+)\s*pts?")

_FRACTION_STATS = {
    "damage",
    "penetration",
    "penetration_with_ignite",
    "ailment_duration",
    "skill_duration",
    "ailment_chance",
    "cast_speed",
    "cooldown_recovery_speed",
    "cooldown_reduction",
    "resistance",
    "mana_regen",
}

_ATTRIBUTE_NAMES = {
    "intelligence": "智力",
    "strength": "力量",
    "vitality": "体力",
    "dexterity": "敏捷",
    "attunement": "协调",
    "baseIntelligence": "智力",
    "baseStrength": "力量",
    "baseVitality": "体力",
    "baseDexterity": "敏捷",
    "baseAttunement": "协调",
}

_RESISTANCE_NAMES = {
    "fire": "火焰抗性",
    "cold": "冰霜抗性",
    "lightning": "闪电抗性",
    "necrotic": "腐化抗性",
    "physical": "物理抗性",
    "poison": "毒素抗性",
    "void": "虚空抗性",
    "all": "全抗性",
    "elemental": "元素抗性",
}

_POINTS_LABELS = {
    "charges": "额外充能",
    "stack": "充能",
}


# --------------------------------------------------------------------------- #
# naming helpers
# --------------------------------------------------------------------------- #


def _text(value) -> str:
    return "" if value is None else str(value)


def _d(value) -> dict:
    return value if isinstance(value, dict) else {}


def _float(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # NaN -> None


def _item_label(raw_label: str) -> tuple[str, str]:
    """``"Spine of Malatros [equipment.weapon1]"`` -> (name, container)."""

    label = raw_label.strip()
    match = _ITEM_RE.match(label)
    if match:
        return match.group("name").strip(), match.group("container")
    return label, ""


def _node_from_group(value: str) -> str | None:
    match = _NODE_GROUP_RE.search(value or "")
    if match:
        return match.group("node")
    match = _SKILL_CID_RE.match(value or "")
    if match:
        return match.group("node")
    return None


def collect_skill_node_names(result: dict) -> dict[str, str]:
    """Recover real skill-node names (Umbral Core, ...) from more groups."""

    names: dict[str, str] = {}
    single = result.get("single_cast") or {}
    groups: list[dict] = []
    body = single.get("body") or {}
    for component in body.get("components") or []:
        groups.extend(component.get("more_groups") or [])
    ignite = single.get("ignite") or {}
    groups.extend(ignite.get("more_groups") or [])
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = _text(group.get("group"))
        node = _node_from_group(group_id)
        for row in group.get("rows") or []:
            if not isinstance(row, dict):
                continue
            label = _text(row.get("label")).strip()
            if not label or label.lower() in {"damage", "more", "more damage"}:
                continue
            node = node or _node_from_group(_text(row.get("contribution_id")))
            if node:
                names.setdefault(node, label)
    return names


def skill_node_index(result: dict) -> dict[str, dict[str, str]]:
    """Map skill-tree node id -> {stat_key: contribution_id} from the ledger."""

    index: dict[str, dict[str, str]] = {}
    for row in result.get("source_ledger") or []:
        if not isinstance(row, dict):
            continue
        if row.get("source_kind") != "skill_node":
            continue
        contribution_id = _text(row.get("contribution_id"))
        match = _SKILL_CID_RE.match(contribution_id)
        if not match:
            continue
        node = match.group("node")
        stat_key = _text(row.get("stat_key"))
        index.setdefault(node, {})[stat_key] = contribution_id
    return index


# --------------------------------------------------------------------------- #
# stat naming
# --------------------------------------------------------------------------- #


def _tag_set(row: dict) -> set[str]:
    return {str(tag) for tag in (row.get("damage_tags") or [])}


def _stat_label(row: dict, category: str, node_names: dict[str, str]) -> str:
    stat_key = _text(row.get("stat_key"))
    stat_name = _text(row.get("stat_name"))
    tags = _tag_set(row)
    operation = _text(row.get("operation"))
    target = _text(row.get("target"))

    if stat_key == "unsupported":
        return f"未建模：{stat_name or '未知属性'}"
    if stat_key == "damage":
        qualifiers: list[str] = []
        if "DoT" in tags:
            qualifiers.append("持续")
        if "Elemental" in tags:
            qualifiers.append("元素")
        elif "Fire" in tags:
            qualifiers.append("火焰")
        elif "Cold" in tags:
            qualifiers.append("冰霜")
        elif "Lightning" in tags:
            qualifiers.append("闪电")
        if "Spell" in tags:
            qualifiers.append("法术")
        if "Melee" in tags:
            qualifiers.append("近战")
        name = "".join(qualifiers) + "伤害增伤"
        if operation == "more" or "more" in stat_name.lower() or "More" in stat_name:
            # player label: "独立" conveys the separate multiplier without the
            # engine's "more group" wording
            return f"{name}（独立）"
        return name
    if stat_key == "added_damage":
        return "附加点伤"
    if stat_key == "penetration":
        if "Fire" in tags:
            return "火焰穿透"
        return "穿透"
    if stat_key == "penetration_with_ignite":
        return "火焰穿透（仅点燃）"
    if stat_key == "ailment_duration":
        affects_ignite = "Ignite" in stat_name or "Ignite" in tags or target.lower() == "ignite"
        return "点燃持续时间" if affects_ignite else "异常状态持续时间"
    if stat_key == "skill_duration":
        return "黑洞持续时间"
    if stat_key == "ailment_chance":
        if "Ignite" in stat_name:
            return "点燃概率"
        return "异常状态概率"
    if stat_key == "resistance":
        return _RESISTANCE_NAMES.get(target, f"{stat_name or '抗性'}")
    if stat_key == "attribute":
        attribute = _ATTRIBUTE_NAMES.get(stat_name) or _ATTRIBUTE_NAMES.get(target)
        if attribute:
            return f"{attribute}属性"
        return stat_name or "属性"
    if stat_key == "all_attributes":
        return "全属性"
    if stat_key == "cast_speed":
        return "施法速度"
    if stat_key == "cooldown_recovery_speed":
        return "冷却恢复速度"
    if stat_key == "cooldown_reduction":
        return "冷却缩减"
    if stat_key == "cooldown_flat":
        return "冷却时间固定调整"
    if stat_key == "charges":
        return "额外充能"
    if stat_key == "mana":
        return "法力"
    if stat_key == "mana_regen":
        return "法力回复"
    if node_names and stat_key == "unsupported":
        return stat_name or "技能效果"
    return stat_name or f"属性（{stat_key}）"


def _value_kind(row: dict) -> str:
    stat_key = _text(row.get("stat_key"))
    unit = _text(row.get("unit"))
    if stat_key in _FRACTION_STATS:
        return "fraction"
    if stat_key in ("attribute", "all_attributes", "charges"):
        return "points"
    if stat_key in ("added_damage", "mana"):
        return "number"
    if stat_key == "cooldown_flat" or unit == "seconds":
        return "seconds"
    if unit == "percent":
        return "fraction"
    if unit in ("points", "stack", "charges"):
        return "points"
    if unit == "flag":
        return "integer"
    if unit in ("unconfirmed", "unknown"):
        return "unknown"
    return "number"


def _effect_text(row: dict, kind: str, stat_label: str) -> str:
    value = _float(row.get("value"))
    if kind == "unknown" or value is None:
        return stat_label
    if kind == "fraction":
        return f"{stat_label} {units.signed_pct(value)}"
    if kind == "points":
        return f"{stat_label} +{units.fmt_compact(value, 'integer')} 点"
    if kind == "seconds":
        sign = "+" if value > 0 else ""
        return f"{stat_label} {sign}{units.fmt_compact(value, 'seconds')} 秒"
    if kind == "integer":
        return f"{stat_label} {units.fmt_compact(value, 'integer')}"
    return f"{stat_label} +{units.fmt_compact(value, 'number')}"


def _evidence(row: dict) -> list[dict]:
    entries: list[dict] = []
    for item in row.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        entries.append(
            {
                "kind": _text(item.get("kind")),
                "kind_label": labels.EVIDENCE_LABELS.get(_text(item.get("kind")), _text(item.get("kind"))),
                "detail": _text(item.get("detail"))[:600],
                "locator": _text(item.get("locator"))[:400],
                "snippet": _text(item.get("snippet"))[:300],
            }
        )
    return entries


def _raw_snippet(evidence: Iterable[dict]) -> str:
    for item in evidence:
        if item.get("snippet"):
            return _text(item["snippet"]).strip()
    return ""


def _imported_display(value: Any, evidence: list[dict]) -> str:
    """Extract the calculation-input ``display_value`` (post-import value)."""

    for source in (evidence,):
        for item in source:
            match = _DISPLAY_VALUE_RE.search(_text(item.get("detail")))
            if match:
                return match.group(1)
    return ""


def _points_for(row: dict, raw_label: str) -> int | None:
    raw = row.get("raw")
    if isinstance(raw, dict):
        for key in ("points", "allocated_points", "level"):
            value = _float(raw.get(key))
            if value is not None:
                return int(value)
    match = _POINTS_RE.search(raw_label or "")
    if match and "node" in (raw_label or "").lower():
        return int(match.group("points"))
    return None


def _channel(contribution_id: str) -> str:
    if "#implicit#" in contribution_id:
        return "implicit"
    if "#unique_mod#" in contribution_id:
        return "unique"
    if "#unique_description#" in contribution_id:
        return "unique_description"
    match = re.search(r"#affix#(normal|sealed|corrupted|exalted)#", contribution_id)
    if match:
        return match.group(1)
    return "affix"


def _category(row: dict) -> str:
    contribution_id = _text(row.get("contribution_id"))
    kind = _text(row.get("source_kind"))
    if contribution_id.startswith("mod:quest.reward"):
        return "quest"
    if contribution_id.startswith("mod:equipment.idol_altar"):
        return "idol_altar"
    if contribution_id.startswith("mod:equipment."):
        return "equipment"
    if contribution_id.startswith("mod:idols."):
        return "idol"
    if contribution_id.startswith("mod:blessings."):
        return "blessing"
    if kind in ("equipment", "idol", "blessing", "idol_altar", "passive_node", "skill_node", "base_character", "derived"):
        return kind
    return "unknown"


def _slot(contribution_id: str) -> str | None:
    match = re.match(r"mod:equipment\.([a-z0-9_]+)#", contribution_id)
    return match.group(1) if match else None


def _idol_layout(result: dict) -> dict[str, dict]:
    derived = (result.get("snapshot") or {}).get("derived") or {}
    layout: dict[str, dict] = {}
    for entry in derived.get("idol_layout") or []:
        if isinstance(entry, dict) and entry.get("instance_id"):
            layout[_text(entry["instance_id"])] = entry
    return layout


# --------------------------------------------------------------------------- #
# main builder
# --------------------------------------------------------------------------- #


def build_sources(result: dict) -> tuple[list[dict], dict[str, dict]]:
    """Return ``(ordered_source_list, by_id)`` for the whole source ledger."""

    node_names = collect_skill_node_names(result)
    idol_layout = _idol_layout(result)
    ordered: list[dict] = []
    by_id: dict[str, dict] = {}
    for index, row in enumerate(result.get("source_ledger") or []):
        if not isinstance(row, dict):
            continue
        contribution_id = _text(row.get("contribution_id"))
        if not contribution_id:
            continue
        category = _category(row)
        raw_label = _text(row.get("source_label")).strip()
        stat_label = _stat_label(row, category, node_names)
        kind = _value_kind(row)
        value = _float(row.get("value"))
        evidence = _evidence(row)
        raw_text = _raw_snippet(evidence)
        imported = _imported_display(value, evidence)

        name = raw_label or contribution_id
        slot: str | None = None
        slot_label: str | None = None
        detail_line = ""
        node_label = ""
        item_name = ""
        idol_index: int | None = None
        idol_position: dict | None = None

        if category == "quest":
            name = "任务奖励"
            display_name = "任务奖励 · +2 全属性"
            detail_line = "任务奖励（导入时临时挂在戒指记录下，不是戒指词缀）"
        elif category == "equipment":
            item_name, _container = _item_label(raw_label)
            name = item_name or raw_label or "装备"
            slot = _slot(contribution_id)
            slot_label = labels.SLOT_LABELS.get(slot or "", slot or "装备")
            channel = _channel(contribution_id)
            display_name = f"{slot_label} · {name}"
            detail_line = labels.CHANNEL_LABELS.get(channel, "词缀")
        elif category == "idol":
            item_name, _container = _item_label(raw_label)
            match = re.search(r"idols\.(\d+)", contribution_id)
            idol_index = int(match.group(1)) + 1 if match else None
            name = item_name or raw_label or "神像"
            layout = idol_layout.get(f"idols.{(idol_index or 1) - 1}", {})
            position = None
            if isinstance(layout, dict) and layout.get("x") is not None and layout.get("y") is not None:
                position = {"x": layout.get("x"), "y": layout.get("y")}
                idol_position = position
            display_name = f"神像 {idol_index} · {name}" if idol_index else f"神像 · {name}"
            channel = _channel(contribution_id)
            detail_line = labels.CHANNEL_LABELS.get(channel, "词缀")
            if position:
                detail_line += f" · 位置 {position['x']},{position['y']}"
        elif category == "blessing":
            item_name, _container = _item_label(raw_label)
            name = item_name or raw_label or "祝福"
            display_name = f"祝福 · {name}"
            detail_line = "祝福"
        elif category == "idol_altar":
            item_name, _container = _item_label(raw_label)
            name = item_name or raw_label or "神像祭坛"
            display_name = f"神像祭坛 · {name}"
            detail_line = "神像祭坛"
        elif category == "passive_node":
            match = _PASSIVE_RE.search(raw_label)
            if match:
                name = match.group("name")
                node_label = f"{match.group('points')} 点"
            display_name = f"职业被动 · {name}"
            detail_line = node_label or raw_label
        elif category == "skill_node":
            match = _SKILL_RE.match(raw_label)
            node = match.group("node") if match else _node_from_group(contribution_id)
            points = _points_for(row, raw_label)
            node_name = node_names.get(node or "", "") or "技能节点"
            name = node_name
            display_name = f"Black Hole 技能树 · {node_name}"
            detail_line = f"{points} 点" if points is not None else ""
        elif category == "base_character":
            name = raw_label or "角色基础"
            display_name = f"角色基础 · {name}"
            detail_line = ""
        elif category == "derived":
            name = raw_label or "派生属性"
            display_name = f"派生属性 · {name}"
            detail_line = ""
        else:
            display_name = name

        status = _text(row.get("status"))
        condition_status = _text(row.get("condition_status"))
        channel = _channel(contribution_id)
        points = _points_for(row, raw_label) if category in ("passive_node", "skill_node") else None
        source = {
            "id": contribution_id,
            "category": category,
            "category_label": labels.CATEGORY_LABELS.get(category, "来源"),
            "slot": slot,
            "slot_label": slot_label,
            "name": clean_engineering_text(name),
            "display_name": clean_engineering_text(display_name),
            "detail_line": clean_engineering_text(detail_line),
            "channel": channel,
            "channel_label": labels.CHANNEL_LABELS.get(channel, "词缀"),
            "raw_text": player_raw_text(raw_text),
            "imported_display": clean_engineering_text(imported),
            "stat_label": clean_engineering_text(stat_label),
            "effect_label": clean_engineering_text(_effect_text(row, kind, stat_label)),
            "kind": kind,
            "value": value,
            "included": bool(row.get("included")),
            "status": status,
            "status_label": labels.RULE_STATUS_LABELS.get(status, status or "未知"),
            "condition_status": condition_status,
            "condition_label": labels.CONDITION_LABELS.get(condition_status, condition_status or "未知"),
            "reason": player_reason(_text(row.get("exclusion_reason"))) or None,
            "notes": player_reason(_text(row.get("notes"))) or None,
            "points": points,
            "group": _text(row.get("group")) or None,
            "tags": [str(tag) for tag in (row.get("damage_tags") or [])],
            "evidence": evidence,
            "advanced": {
                "contribution_id": contribution_id,
                "stat_key": _text(row.get("stat_key")),
                "stat_name": _text(row.get("stat_name")),
                "operation": _text(row.get("operation")),
                "unit": _text(row.get("unit")),
                "target": _text(row.get("target")) or None,
                "source_kind": _text(row.get("source_kind")),
                "source_label": raw_label,
                "source_path": _text(row.get("source_path")),
                "value_source": _text(row.get("value_source")),
                "group": _text(row.get("group")) or None,
                "tags": [str(tag) for tag in (row.get("damage_tags") or [])],
            },
            "idol_index": idol_index,
            "idol_position": idol_position,
            "applies": [],
            "order": index,
            "synthetic": False,
        }
        ordered.append(source)
        by_id[contribution_id] = source
    return ordered, by_id


def synthetic_source(
    source_id: str,
    *,
    category: str,
    name: str,
    display_name: str,
    effect_label: str,
    kind: str,
    value,
    included: bool = True,
    status: str = "derived",
    condition_status: str = "always",
    reason: str | None = None,
    detail_line: str = "",
    notes: str | None = None,
    order: int = 10_000,
) -> dict:
    """Build a display-only source (attribute scaling, quest reward, ...)."""

    return {
        "id": source_id,
        "category": category,
        "category_label": labels.CATEGORY_LABELS.get(category, "来源"),
        "slot": None,
        "slot_label": None,
        "name": name,
        "display_name": display_name,
        "detail_line": detail_line,
        "channel": None,
        "channel_label": None,
        "raw_text": "",
        "imported_display": "",
        "stat_label": name,
        "effect_label": effect_label,
        "kind": kind,
        "value": _float(value),
        "included": included,
        "status": status,
        "status_label": labels.RULE_STATUS_LABELS.get(status, status),
        "condition_status": condition_status,
        "condition_label": labels.CONDITION_LABELS.get(condition_status, condition_status),
        "reason": reason,
        "notes": notes,
        "points": None,
        "group": None,
        "tags": [],
        "evidence": [],
        "advanced": {"contribution_id": source_id, "synthetic": True},
        "idol_index": None,
        "idol_position": None,
        "applies": [],
        "order": order,
        "synthetic": True,
    }


def source_order_key(source: dict) -> tuple[int, int, int]:
    category = source.get("category") or "unknown"
    rank = labels.CATEGORY_ORDER.index(category) if category in labels.CATEGORY_ORDER else 99
    return (rank, int(source.get("order") or 0), 0)


# --------------------------------------------------------------------------- #
# source identity (grouping a source's several contributions into one entry)
# --------------------------------------------------------------------------- #

_ITEM_NAME_RE = re.compile(r"^(?P<name>.+?)\s*\[(?P<container>[A-Za-z0-9_.]+)\]$")
_PASSIVE_LABEL_RE = re.compile(
    r"Mage passive '(?P<name>[^']*)' \((?P<tree>[A-Za-z0-9-]+) node (?P<node>\d+)"
)
_SKILL_LABEL_RE = re.compile(r"^Black Hole skill node (?P<node>\d+)$")


def _identity_name(source: dict) -> str:
    name = _text(source.get("name"))
    if not name:
        return _text(source.get("display_name")) or _text(source.get("id"))
    return name


def source_identity(source: dict) -> tuple:
    """Stable identity shared by every contribution row of one real source.

    One item can own several affixes (and one affix its ``:doubled`` variant),
    one passive node can grant several stats.  The browser groups by this key so
    a buff is not counted once per attribute line:

    * equipment item -> slot + item name (``Spine of Malatros`` on weapon1)
    * idol / blessing / altar -> category + item name
    * passive node -> tree + node number
    * Black Hole skill node -> node number
    * derived / base / quest -> the contribution id
    """

    category = _text(source.get("category")) or "unknown"
    contribution_id = _text(source.get("id"))
    raw_label = _text((source.get("advanced") or {}).get("source_label")) or ""

    if category == "equipment":
        slot = _text(source.get("slot"))
        match = _ITEM_NAME_RE.match(raw_label)
        item = match.group("name").strip() if match else _identity_name(source)
        return ("equipment", slot, item)
    if category == "idol":
        match = _ITEM_NAME_RE.match(raw_label)
        item = match.group("name").strip() if match else _identity_name(source)
        instance = source.get("idol_index")
        if instance is None:
            instance_match = re.search(r"idols\.(\d+)", contribution_id)
            instance = int(instance_match.group(1)) + 1 if instance_match else None
        return ("idol", instance, item)
    if category in ("blessing", "idol_altar", "quest"):
        match = _ITEM_NAME_RE.match(raw_label)
        item = match.group("name").strip() if match else _identity_name(source)
        return (category, item)
    if category == "passive_node":
        match = _PASSIVE_LABEL_RE.search(raw_label)
        if match:
            return ("passive_node", match.group("tree"), match.group("node"))
        return ("passive_node", _identity_name(source))
    if category == "skill_node":
        match = _SKILL_LABEL_RE.match(raw_label)
        node = match.group("node") if match else ""
        if not node:
            match = re.search(r"skill:bh2:(\d+)", contribution_id)
            node = match.group(1) if match else ""
        return ("skill_node", node or _identity_name(source))
    if category in ("derived", "base_character"):
        return (category, contribution_id)
    return (category, contribution_id or _identity_name(source))


# --------------------------------------------------------------------------- #
# conditional-effect sources (cross-skill Buffs and main-skill conditions)
# --------------------------------------------------------------------------- #

#: ledger categories for the conditional-effect registry categories
CONDITIONAL_CATEGORY_MAP = {
    "equipment": "equipment",
    "idol": "idol",
    "main_skill": "skill_node",
    "passive": "passive_node",
    "other_skill": "other_skill",
    "action": "action",
}

CONDITIONAL_CATEGORY_LABELS = {
    "other_skill": "其他技能",
    "action": "动作",
}

#: effect branches -> player-readable application scope labels
BRANCH_LABELS = {
    "body.fire": "火焰本体",
    "body.cold": "冰霜本体",
    "ignite": "点燃",
    "ignite.application": "点燃施加",
    "duration": "持续时间",
    "cast": "施法时间",
    "cooldown": "冷却与充能",
    "loops": "CD 循环",
    "penetration.body": "本体穿透",
    "penetration.ignite": "点燃穿透",
    "target.state": "目标状态",
}

_BASELINE_CONTRIBUTION_RE = re.compile(
    r"(?:mod|skill|derived|base|passive):[A-Za-z0-9_.:#-]+"
)


def conditional_category(effect: dict) -> str:
    category = _text(effect.get("category"))
    return CONDITIONAL_CATEGORY_MAP.get(category, category or "unknown")


def conditional_category_label(effect: dict) -> str:
    category = _text(effect.get("category"))
    return CONDITIONAL_CATEGORY_LABELS.get(category) or labels.CATEGORY_LABELS.get(
        CONDITIONAL_CATEGORY_MAP.get(category, category), "条件来源"
    )


def conditional_source_name(effect: dict) -> str:
    """Player-readable name of one conditional effect (never a database field)."""

    name = _text(effect.get("name"))
    if name:
        return name
    source_label = _text(effect.get("source_label"))
    return source_label.split("·")[0].strip() or _text(effect.get("effect_id"))


def conditional_source_detail(effect: dict) -> str:
    """``Meteor · Craterborn`` style second line for the source browser."""

    parts = []
    for key in ("source_label",):
        text = _text(effect.get(key)).strip()
        if text:
            parts.append(text)
    if not parts:
        category = _text(effect.get("category_label"))
        if category:
            parts.append(category)
    return " · ".join(parts)


def baseline_contribution_ids(effect: dict) -> list[str]:
    """Ledger contribution ids an ``already_in_baseline`` effect was counted as.

    The conditional view's ``baseline_note`` names the actual snapshot rows
    (e.g. ``已计入基础来源账（1 条生效贡献：skill:bh2:23:more-boss）``); the
    source browser joins on those ids so the baseline ledger row and the buff
    row are one source instead of two.

    The pattern is anchored on ledger-id prefixes; it is a real join, not a
    substring guess on labels.
    """

    note = _text(effect.get("baseline_note"))
    if not note:
        return []
    ids: list[str] = []
    for match in _BASELINE_CONTRIBUTION_RE.finditer(note):
        candidate = match.group(0).rstrip(":：,，;；)）")
        if candidate and candidate not in ids:
            ids.append(candidate)
    return ids


def conditional_source_identity(effect_group_id: str) -> tuple:
    return ("conditional", effect_group_id)


def effect_source_identity(effect: dict) -> tuple | None:
    """Physical source identity of a conditional effect when it names one.

    Used to join a Buff onto the item/node row that already exists in the
    ledger (Apex of Thought, Unsated Rage / Crescendoing Rage, Throne of
    Ambition, Frozen Steel, Pyramidal Altar), so a source is not shown twice as
    "contribution row + buff row".

    Returns ``None`` when the effect does not resolve to an identifiable
    ledger source (e.g. another skill tree's node).
    """

    detail = _d(effect.get("source_detail"))
    kind = _text(detail.get("kind"))
    item = _text(detail.get("item")).strip()
    slot = _text(detail.get("slot")).strip()
    node_id = detail.get("node_id")
    if item:
        if kind == "equipment" and slot:
            return ("equipment", slot, item)
        if kind == "idol" and slot.startswith("idols."):
            try:
                index = int(slot.split(".", 1)[1]) + 1
            except (IndexError, ValueError):
                index = None
            return ("idol", index, item)
        if kind in ("idol_altar", "blessing", "quest"):
            return (kind, item)
    if kind == "passive_node" and node_id is not None:
        # the conditional registry uses the catalog node id; the ledger identity
        # uses the passive-tree label, so the tree component is matched as *
        return ("passive_node", "*", str(node_id))
    return None


def find_source_group(identity: tuple | None, groups: dict) -> dict | None:
    """Resolve an effect identity against existing source-browser groups."""

    if not identity:
        return None
    if identity in groups:
        return groups[identity]
    prefix = identity[0]
    for key, group in groups.items():
        if not isinstance(key, tuple) or not key or key[0] != prefix:
            continue
        if prefix == "passive_node" and len(key) >= 3 and str(key[-1]) == str(identity[-1]):
            return group
        if prefix == "idol" and len(key) >= 3:
            if key[1] == identity[1] and str(key[2]).strip() == str(identity[2]).strip():
                return group
    return None


def group_conditional_effects(effects: Iterable[dict], groups: Iterable[dict]) -> list[dict]:
    """Group conditional effects by their registry source group.

    ``view["conditions"]["groups"]`` already links the effect ids that share one
    source (e.g. Eternal Flames passive + active); effects missing from that
    table become their own group.  Returns deterministic entries in the order
    the groups/effects appear.
    """

    effect_by_id = {
        _text(effect.get("effect_id")): effect
        for effect in effects
        if isinstance(effect, dict) and _text(effect.get("effect_id"))
    }
    grouped: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            continue
        group_id = _text(group.get("id"))
        if not group_id:
            continue
        members = [
            effect_by_id[_text(effect_id)]
            for effect_id in group.get("effect_ids") or []
            if _text(effect_id) in effect_by_id
        ]
        if not members:
            continue
        seen.update(_text(effect.get("effect_id")) for effect in members)
        first = members[0]
        grouped.append(
            {
                "identity": conditional_source_identity(group_id),
                "group_id": group_id,
                "label": _text(group.get("label")) or conditional_source_name(first),
                "name": conditional_source_name(first),
                "detail": conditional_source_detail(first),
                "category": conditional_category(first),
                "category_label": conditional_category_label(first),
                "effects": members,
            }
        )
    for effect in effects:
        if not isinstance(effect, dict):
            continue
        effect_id = _text(effect.get("effect_id"))
        if not effect_id or effect_id in seen:
            continue
        seen.add(effect_id)
        grouped.append(
            {
                "identity": conditional_source_identity(effect_id),
                "group_id": effect_id,
                "label": conditional_source_name(effect),
                "name": conditional_source_name(effect),
                "detail": conditional_source_detail(effect),
                "category": conditional_category(effect),
                "category_label": conditional_category_label(effect),
                "effects": [effect],
            }
        )
    return grouped


# --------------------------------------------------------------------------- #
# player-facing names, attributes and values (增伤来源 page)
# --------------------------------------------------------------------------- #

#: parenthetical suffix in a conditional-effect display name
_NAME_QUALIFIER_RE = re.compile(r"[（(]([^）)]*)[）)]\s*$")
_OWNER_SPLIT_RE = re.compile(r"\s*[·・]\s*")
_SENTENCE_SPLIT_RE = re.compile(r"[；;。\n]")


def player_effect_name_parts(effect: dict) -> dict:
    """Split a conditional-effect name into player title / source / qualifier.

    ``Eternal Flames（激活）`` -> title ``Eternal Flames``, qualifier
    ``激活额外``; ``Frozen Steel（4 点门槛）`` -> title ``Frozen Steel``,
    qualifier ``需要 4 点``.  The raw effect id is never used as a title.
    """

    raw_name = _text(effect.get("name")) or conditional_source_name(effect)
    title = raw_name
    qualifier = ""
    match = _NAME_QUALIFIER_RE.search(raw_name)
    if match:
        inner = match.group(1).strip()
        title = raw_name[: match.start()].strip()
        qualifier = labels.PLAYER_EFFECT_QUALIFIERS.get(inner, inner)
        if inner not in labels.PLAYER_EFFECT_QUALIFIERS:
            # unknown parenthetical: keep it as the condition hint, not the title
            qualifier = ""
    if not title:
        title = raw_name
    # a registry family label like "Grand Arcane Omen Idol · 火伤翻倍" keeps the
    # first segment as the source and the remainder as the effect
    owner = player_effect_owner(effect)
    if " · " in title:
        head, _sep, tail = title.partition(" · ")
        head_clean = _NAME_QUALIFIER_RE.sub("", head).strip()
        if head_clean and tail.strip():
            title = tail.strip()
            if not owner:
                owner = head_clean
    return {
        "title": title,
        "qualifier": qualifier,
        "owner": owner,
        "condition": player_effect_condition(effect),
    }


def player_effect_owner(effect: dict) -> str:
    """Readable source skill/tree for an effect (``Enchant Weapon`` etc.)."""

    source_label = _text(effect.get("source_label"))
    if not source_label:
        detail = _d(effect.get("source_detail"))
        return _text(detail.get("tree")) or _text(effect.get("category_label"))
    first = _OWNER_SPLIT_RE.split(source_label, maxsplit=1)[0].strip()
    first = re.sub(r"[（(][^）)]*[）)]\s*$", "", first).strip()
    if first.startswith("Mage "):
        first = "Mage 被动"
    return first or _text(effect.get("category_label"))


#: parenthetical/clause cleanup for player condition lines
_PLAYER_CALC_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_PLAYER_OPEN_PAREN_RE = re.compile(r"[（(][^）)]*$")
_PLAYER_CALC_PAREN_MARKERS = (
    "假设",
    "人工声明",
    "无法自动判定",
    "不模拟",
    "当前构筑",
    "由当前输入",
    "固定条件",
    "循环计数",
)
_PLAYER_CALC_NOISE = (
    "，需人工声明该条件",
    "，需人工声明",
    "当前构筑不满足",
    "由当前输入自动判定",
    "无法自动判定",
    "循环计数不在本条件内",
)


def _drop_calc_parenthetical(match: re.Match) -> str:
    inner = match.group(0)
    if any(marker in inner for marker in _PLAYER_CALC_PAREN_MARKERS):
        return ""
    return inner


def _short_condition(text: str) -> str:
    condition = _text(text).strip()
    if not condition:
        return ""
    for always in labels.PLAYER_ALWAYS_CONDITIONS:
        if always in condition:
            return ""
    condition = _SENTENCE_SPLIT_RE.split(condition, maxsplit=1)[0].strip()
    condition = _PLAYER_CALC_PAREN_RE.sub(_drop_calc_parenthetical, condition)
    condition = _PLAYER_OPEN_PAREN_RE.sub("", condition)
    for noise in _PLAYER_CALC_NOISE:
        condition = condition.replace(noise, "")
    condition = condition.replace("（）", "").replace("()", "")
    return condition.strip(" ，,；;。:：·")


def player_effect_condition(effect: dict) -> str:
    """One short, player-relevant requirement line (or empty)."""

    name = _text(effect.get("name"))
    match = _NAME_QUALIFIER_RE.search(name)
    inner = match.group(1).strip() if match else ""
    condition = _short_condition(_text(effect.get("condition_text")))
    if inner == "4 点门槛":
        return "需要 4 点" + (f" · {condition}" if condition else "")
    if condition:
        return condition
    if inner == "下一次元素法术":
        return "下一次元素法术"
    return ""


def is_reference_value_line(label: str) -> bool:
    """True for summary/"reference" value lines that would duplicate a total."""

    text = _text(label)
    return any(marker in text for marker in labels.PLAYER_REFERENCE_MARKERS)


def player_attribute_name(label: str) -> str:
    """Readable attribute name for conditional-effect value lines.

    Target-aware naming is applied in ``_stat_label`` for ledger rows; for
    conditional value lines a small explicit alias table fixes known
    awkward/duplicated labels without global regex edits.
    """

    text = _text(label).strip()
    if not text:
        return ""
    if text in labels.PLAYER_ATTRIBUTE_ALIASES:
        return labels.PLAYER_ATTRIBUTE_ALIASES[text]
    # position wording from registry value lines
    text = text.replace("每层 more ", "每层 more ")
    return text


def player_effect_attributes(effect: dict, qualifier: str = "") -> list[dict]:
    """Structured attribute rows for one conditional effect.

    Each row keeps the attribute name and exactly one value; reference/total
    duplicates are skipped.  ``qualifier`` (常驻 / 激活额外) distinguishes the
    passive and active parts of the same Buff without deleting either.
    """

    attributes: list[dict] = []
    for line in effect.get("value_lines") or []:
        if not isinstance(line, dict):
            continue
        raw_label = _text(line.get("label"))
        if is_reference_value_line(raw_label):
            continue
        name = player_attribute_name(raw_label)
        value_text = _text(line.get("value_text"))
        if not name or not value_text:
            continue
        attributes.append(
            {
                "name": name,
                "value": None,
                "kind": "text",
                "value_text": value_text,
                "unit": "",
                "qualifier": qualifier,
                "detail": _text(line.get("detail")),
            }
        )
    return attributes


def player_source_attributes(source: dict) -> list[dict]:
    """Structured attribute rows for one ledger contribution row."""

    name = _text(source.get("stat_label"))
    if not name:
        return []
    value = source.get("value")
    kind = _text(source.get("kind")) or "number"
    if value is None:
        return []
    if kind == "fraction":
        value_text = units.signed_pct(value)
        unit = ""
    elif kind == "seconds":
        value_text = units.fmt_compact(value, "seconds")
        unit = "秒"
    elif kind == "points":
        value_text = units.fmt_compact(value, "points")
        unit = "点"
    elif kind == "factor":
        value_text = units.fmt_compact(value, "factor")
        unit = ""
    else:
        value_text = units.fmt_compact(value, kind)
        unit = ""
    return [
        {
            "name": name,
            "value": value,
            "kind": kind,
            "value_text": value_text,
            "unit": unit,
            "qualifier": "",
            "detail": "",
        }
    ]


def player_source_detail(source: dict) -> str:
    """Second line under a ledger source title (slot / tree / points)."""

    category = _text(source.get("category"))
    parts: list[str] = []
    points = source.get("points")
    points_text = f"{int(points)} 点" if isinstance(points, (int, float)) and points else ""
    if category == "equipment":
        parts.append(_text(source.get("slot_label")) or "装备")
        channel_label = _text(source.get("channel_label"))
        if channel_label and channel_label not in ("词缀", ""):
            parts.append(channel_label)
    elif category == "skill_node":
        parts.append("Black Hole 技能树")
        if points_text:
            parts.append(points_text)
    elif category == "passive_node":
        parts.append("Mage 被动")
        if points_text:
            parts.append(points_text)
    elif category == "idol":
        parts.append("神像")
    elif category == "blessing":
        parts.append("祝福")
    elif category == "idol_altar":
        parts.append("神像祭坛")
    elif category == "quest":
        parts.append("任务奖励")
    elif category == "base_character":
        parts.append("角色基础")
    elif category == "derived":
        parts.append("属性派生")
    else:
        parts.append(_text(source.get("category_label")) or "来源")
    return " · ".join(part for part in parts if part)


def player_source_title(source: dict) -> str:
    """Player-readable title for one ledger source row."""

    category = _text(source.get("category"))
    if category == "idol":
        return _text(source.get("display_name")) or _text(source.get("name")) or "神像"
    name = _text(source.get("name")) or _text(source.get("display_name"))
    if category == "skill_node" and name.startswith("节点"):
        return f"Black Hole · {name}"
    if category == "base_character":
        return "角色基础属性"
    if category == "derived":
        return name or "属性派生"
    return name
