"""Player-readable item text generation (2026-11-03 review rework).

This layer turns definitions + restored rolls + catalog translations into the lines a
player expects to read on an item card.  It is generated at import time and stored in
the BuildSpec, so the offline HTML viewer never queries the database itself.

Evidence and rules:

* modifier semantics / templates: archived ``Ea``/``kb``/``Za``/``na``/``$a`` functions
  in the archived planner-runtime.js (``archive://planner-runtime.js``) (offsets recorded in
  ``le_build.rolls.EVIDENCE``) plus the ``item_db.item_card.property.display_template.*``
  translations in the catalog.
* unique composition: LE Tools ``items.extensions.tooltipEntries`` ordering convention
  (``< 128`` = mod index, ``>= 128`` = tooltip description index), verified on every
  unique of the fixed sample (see ``Catalog.unique_tooltip``).
* hidden mods (``hideInTooltip``) are never emitted as standalone lines; they are
  expressed through the combined tooltip description or retained in diagnostics.
* absent rolls never become 0: the line shows the declared range with
  ``value_status=range`` (or ``constant`` for constant ranges).
"""

from __future__ import annotations

import re

from . import rolls

READABLE_VERSION = "letools-readable-v1"
TEMPLATE_PREFIX = "item_db.item_card.property.display_template."

TEMPLATE_FALLBACK = {
    "base": "{value} {modDescription}",
    "of": "{value} of {modDescription}",
    "to": "{value} to {modDescription}",
    "increased": "{value} increased {modDescription}",
    "reduced": "{value} reduced {modDescription}",
    "more": "{value} more {modDescription}",
    "less": "{value} less {modDescription}",
}

CHANNEL_LABELS = {
    "affix_normal": "普通词缀",
    "affix_sealed": "封印词缀",
    "affix_primordial": "原初词缀",
    "affix_corrupted": "腐化词缀",
    "implicit": "固有",
    "unique_mod": "暗金词条",
    "unique_description": "暗金描述",
}

VALUE_STATUS_ACTUAL = "actual"
VALUE_STATUS_RANGE = "range"
VALUE_STATUS_CONSTANT = "constant"
VALUE_STATUS_NOT_PROVIDED = "not_provided"
VALUE_STATUS_UNRESOLVED = "unresolved"

_PLACEHOLDER_RE = re.compile(
    r"(\+?)\[(\d+(?:\.\d+)?),\s*(\d+(?:\.\d+)?),\s*(\d+)\](\s*%)?"
)
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _template(catalog, name: str) -> str:
    lookup = getattr(catalog, "translation", None)
    text = lookup(TEMPLATE_PREFIX + name) if lookup else None
    return text or TEMPLATE_FALLBACK.get(name, "{value} {modDescription}")


def _resolve_stat(catalog, *, stat_id=None, property_id=None, tags=0, special_tag=0) -> dict | None:
    if stat_id:
        stat = catalog.stat(stat_id)
        if stat:
            still_flags = catalog.stat_by_property(property_id, tags, special_tag) if property_id is not None else None
            if still_flags:
                merged = dict(stat)
                merged.update(
                    {
                        key: value
                        for key, value in still_flags.items()
                        if key.startswith(
                            (
                                "display_",
                                "round",
                                "dont_",
                                "name_includes",
                                "property_",
                                "alt_text",
                                "stat_extensions",
                                "hide_",
                                "special_",
                                "non_",
                            )
                        )
                    }
                )
                return merged
            return stat
    if property_id is None:
        return None
    try:
        return catalog.stat_by_property(property_id, tags=tags, special_tag=special_tag)
    except TypeError:  # pragma: no cover - legacy positional signature
        return catalog.stat_by_property(property_id, tags, special_tag)


def _description(catalog, stat, modifier_type, tags, special_tag) -> str:
    text = None
    if stat is not None:
        describe = getattr(catalog, "stat_description", None)
        if describe:
            try:
                text = describe(stat, modifier_type, tags, special_tag)
            except TypeError:  # pragma: no cover - defensive
                text = None
        if not text:
            text = stat.get("name")
    if not text:
        text = "Unknown effect"
    tag_names = []
    tag_renderer = getattr(catalog, "tag_names", None)
    if tag_renderer:
        try:
            tag_names = tag_renderer(tags)
        except TypeError:  # pragma: no cover - defensive
            tag_names = []
    if tag_names and stat is not None:
        name = (stat.get("name") or "").lower()
        damage_types = [
            tag for tag in tag_names
            if tag in ("Physical", "Lightning", "Cold", "Fire", "Void", "Necrotic", "Poison")
        ]
        qualifier = " and ".join(damage_types) if damage_types else " and ".join(tag_names)
        # Qualify generic category properties with their tag names, e.g. Level of Skills
        # + tags=4 -> "Level of Cold Skills"; Damage + tags=128 -> "Elemental Damage";
        # Penetration + tags=8 -> "Fire Penetration"; All Resistances + tags=8192 ->
        # "Minion All Resistances" (multi-affixes such as 852/848/1070 otherwise collapse
        # into two identical player lines, which the 2026-11-03 review flagged).
        if name in ("level of skills", "level of skill"):
            text = f"Level of {qualifier} Skills"
        elif name == "damage":
            text = f"{qualifier} Damage"
        elif "penetration" in name:
            text = f"{qualifier} Penetration"
        if "Minion" in tag_names and not text.lower().startswith("minion"):
            text = f"Minion {text}"
    return text


def _value_line_text(
    catalog,
    *,
    stat: dict | None,
    modifier_type: int,
    value,
    max_value,
    description: str,
    has_actual: bool = True,
) -> tuple[str | None, str, str | None, str | None]:
    """Compose one ``{value} <template> {description}`` line.

    ``has_actual`` distinguishes a restored instance value from a declared range
    (absent roll): ranges are wrapped in parentheses and tagged ``range``/``constant``
    instead of pretending to be an actual roll.

    Returns ``(text, value_status, value_text, range_text)``.
    """
    semantic = rolls.display_semantic(modifier_type, value, max_value, stat)
    signed_value = semantic.get("value")
    signed_max = semantic.get("max_value")
    range_text = rolls.display_range(signed_value, signed_max, stat, modifier_type)
    value_text = None
    value_status = VALUE_STATUS_UNRESOLVED
    if has_actual:
        actual = rolls.display_value(signed_value, stat, modifier_type)
        if actual is not None:
            value_text = actual
            value_status = VALUE_STATUS_ACTUAL
    else:
        if range_text is not None:
            value_text = range_text
            if signed_max is None or _numbers_equal(signed_value, signed_max):
                value_status = VALUE_STATUS_CONSTANT
            else:
                value_status = VALUE_STATUS_RANGE
    if value_text is None:
        return None, VALUE_STATUS_UNRESOLVED, None, range_text
    if stat and int(stat.get("hide_modifier_value") or 0):
        # Archived runtime ``Ea()``: when ``t.hideModifierValue`` is set the description is
        # returned verbatim (no value, no plus, no template).  E.g. "You have Crescendoing
        # Rage" instead of "+1 You have Crescendoing Rage" (2026-11-04 re-review R2).
        hidden_text = " ".join(str(description).split())
        return hidden_text, value_status, value_text, range_text
    shown = f"({value_text})" if value_status == VALUE_STATUS_RANGE else value_text
    if semantic.get("plus"):
        shown = f"+{shown}"
    template = _template(catalog, semantic.get("template") or "base")
    text = template.replace("{value}", shown).replace("{modDescription}", description)
    return " ".join(text.split()), value_status, value_text, range_text


def _non_dps_diagnostics(stat: dict | None) -> dict:
    """Mark a readable line as a non-DPS effect (source property identity, no text match)."""
    if stat and stat.get("non_dps"):
        return {"non_dps": True}
    return {}


def _numbers_equal(left, right) -> bool:
    try:
        return abs(float(left) - float(right)) <= 1e-9
    except (TypeError, ValueError):
        return False


def _input_provenance(value_source, input_id) -> dict:
    """Only add provenance keys when a local input actually supplied the value.

    Source-only builds must stay byte-identical to earlier revisions, so the keys are
    omitted entirely for ``source`` values (no local inputs file present).
    """
    source = value_source or "source"
    if source == "source" and not input_id:
        return {}
    payload = {"value_source": source}
    if input_id:
        payload["input_id"] = input_id
    return payload


def _line(
    *,
    section: str,
    kind: str,
    text: str | None,
    value_status: str,
    tier_label: str | None = None,
    value_text: str | None = None,
    range_text: str | None = None,
    hidden: bool = False,
    diagnostics: dict | None = None,
) -> dict:
    return {
        "section": section,
        "kind": kind,
        "text": text,
        "tier_label": tier_label,
        "value_status": value_status,
        "value_text": value_text,
        "range_text": range_text,
        "hidden": hidden,
        "diagnostics": diagnostics or {},
    }


def _implicit_lines(instance: dict, catalog, item_definition: dict) -> tuple[list[dict], list[str]]:
    lines: list[dict] = []
    warnings: list[str] = []
    block = instance.get("implicit_rolls") or {}
    results = block.get("values") or []
    definitions = item_definition.get("implicits") or []
    used = block.get("used_ir_values")
    provenance = item_definition.get("implicits_provenance") or {}
    for index, definition in enumerate(definitions):
        if not isinstance(definition, dict):
            continue
        result = results[index] if index < len(results) else None
        entry = (result or {}).get("values") or []
        entry = entry[0] if entry else None
        property_id = definition.get("property")
        tags = definition.get("tags") or 0
        special_tag = definition.get("specialTag") or 0
        modifier_type = int(definition.get("type") or 0)
        stat = _resolve_stat(
            catalog, stat_id=(entry or {}).get("stat_id"), property_id=property_id,
            tags=tags, special_tag=special_tag,
        )
        description = _description(catalog, stat, modifier_type, tags, special_tag)
        value = definition.get("implicitValue")
        max_value = definition.get("implicitMaxValue")
        actual_value = None
        roll_status = None
        raw_roll = None
        if entry is not None:
            actual_value = entry.get("value_numeric")
            roll_status = (result or {}).get("roll_status")
            raw_roll = (result or {}).get("raw_roll")
        if actual_value is not None:
            text, value_status, value_text, range_text = _value_line_text(
                catalog, stat=stat, modifier_type=modifier_type,
                value=actual_value, max_value=actual_value, description=description,
                has_actual=True,
            )
        else:
            text, value_status, value_text, range_text = _value_line_text(
                catalog, stat=stat, modifier_type=modifier_type,
                value=value, max_value=max_value if max_value is not None else value,
                description=description,
                has_actual=False,
            )
        if text is None:
            warnings.append(f"固有词条 #{index} 无法生成文本（属性未解析）")
        lines.append(
            _line(
                section="implicit",
                kind="mod",
                text=text,
                value_status=value_status,
                value_text=value_text,
                range_text=range_text,
                diagnostics={
                    "stat_id": (stat or {}).get("stat_id"),
                    "property": property_id,
                    "tags": tags,
                    "special_tag": special_tag,
                    "modifier_type": modifier_type,
                    "raw_value": value,
                    "max_value": max_value,
                    "value_numeric": actual_value,
                    "roll_status": roll_status,
                    "raw_roll": raw_roll,
                    "implicit_index": index,
                    **_non_dps_diagnostics(stat),
                    **_input_provenance(
                        (entry or {}).get("value_source") or (result or {}).get("value_source"),
                        (entry or {}).get("input_id") or (result or {}).get("input_id"),
                    ),
                    "definition_provenance": provenance.get("source"),
                    "ir_width": block.get("ir_width"),
                    "unused_ir_values": block.get("unused_ir_values"),
                },
            )
        )
    if used is None and definitions:
        warnings.append("该物品没有 ir 数据，固有词条仅显示定义范围")
    return lines, warnings


def _affix_lines(instance: dict, catalog) -> tuple[list[dict], list[str]]:
    lines: list[dict] = []
    warnings: list[str] = []
    sections = {
        "normal": "affix_normal",
        "sealed": "affix_sealed",
        "primordial": "affix_primordial",
        "corrupted": "affix_corrupted",
    }
    for channel, section in sections.items():
        for affix in (instance.get("affixes") or {}).get(channel) or []:
            tier_label = None
            source_tier = affix.get("source_tier")
            if isinstance(source_tier, int):
                tier_label = f"T{source_tier}"
            affix_ref = (affix.get("affix") or {}).get("ref") or {}
            affix_name = affix_ref.get("name")
            value_block = affix.get("value") or {}
            values = value_block.get("values") or []
            roll_block = affix.get("roll") or {}
            if not values:
                lines.append(
                    _line(
                        section=section,
                        kind="mod",
                        text=affix_name,
                        value_status=VALUE_STATUS_UNRESOLVED,
                        tier_label=tier_label,
                        diagnostics={
                            "encoded_id": affix.get("encoded_id"),
                            "channel": channel,
                            "source_path": affix.get("source_path"),
                            "roll_status": roll_block.get("status"),
                        },
                    )
                )
                warnings.append(f"{channel} 词缀 {affix.get('encoded_id')!r} 无可用数值")
                continue
            for index, value in enumerate(values):
                property_id = value.get("property")
                tags = value.get("tags") or 0
                special_tag = value.get("special_tag") or 0
                modifier_type = int(value.get("modifier_type") or 0)
                stat = _resolve_stat(
                    catalog, stat_id=value.get("stat_id"), property_id=property_id,
                    tags=tags, special_tag=special_tag,
                )
                description = _description(catalog, stat, modifier_type, tags, special_tag)
                if affix_name and (value.get("stat_name") is None or (stat is None)):
                    description = affix_name
                entry_status = value.get("status")
                numeric = value.get("value_numeric")
                roll_min = value.get("roll_min")
                roll_max = value.get("roll_max")
                roll_status = roll_block.get("status")
                if numeric is not None:
                    text, value_status, value_text, range_text = _value_line_text(
                        catalog, stat=stat, modifier_type=modifier_type,
                        value=numeric, max_value=numeric, description=description,
                        has_actual=True,
                    )
                else:
                    text, value_status, value_text, range_text = _value_line_text(
                        catalog, stat=stat, modifier_type=modifier_type,
                        value=roll_min, max_value=roll_max, description=description,
                        has_actual=False,
                    )
                if entry_status == "unresolved" or text is None:
                    value_status = VALUE_STATUS_UNRESOLVED
                lines.append(
                    _line(
                        section=section,
                        kind="mod",
                        text=text,
                        value_status=value_status,
                        tier_label=tier_label,
                        value_text=value_text,
                        range_text=range_text,
                        diagnostics={
                            "stat_id": (stat or {}).get("stat_id"),
                            "stat_name": value.get("stat_name"),
                            "property": property_id,
                            "tags": tags,
                            "special_tag": special_tag,
                            "modifier_type": modifier_type,
                            "roll_id": None,
                            "raw_roll": roll_block.get("raw"),
                            "roll_status": roll_status,
                            "value_numeric": numeric,
                            "affix_index": index,
                            **_non_dps_diagnostics(stat),
                            **_input_provenance(value.get("value_source"), value.get("input_id")),
                            "encoded_id": affix.get("encoded_id"),
                            "affix_entity_id": affix_ref.get("entity_id"),
                            "channel": channel,
                            "source_path": value.get("source_path") or affix.get("source_path"),
                        },
                    )
                )
    return lines, warnings


def _stat_for_mod(catalog, mod: dict) -> dict | None:
    return _resolve_stat(
        catalog,
        property_id=mod.get("property"),
        tags=mod.get("tags") or 0,
        special_tag=mod.get("specialTag") or 0,
    )


def _unique_mod_display(
    catalog,
    mod: dict,
    entry: dict | None,
    *,
    roll_status,
    raw_roll,
) -> tuple[str | None, str, str | None, str | None]:
    property_id = mod.get("property")
    tags = mod.get("tags") or 0
    special_tag = mod.get("specialTag") or 0
    modifier_type = int(mod.get("type") or 0)
    stat = _stat_for_mod(catalog, mod)
    if stat is None:
        warnings.append(
            f"暗金 mod #{mod.get('_index', '?')} 的 property={property_id} tags={tags} "
            "在目录中无法解析，属性名不完整"
        )
    description = _description(catalog, stat, modifier_type, tags, special_tag)
    numeric = entry.get("value_numeric") if entry else None
    if numeric is not None:
        return _value_line_text(
            catalog, stat=stat, modifier_type=modifier_type,
            value=numeric, max_value=numeric, description=description,
            has_actual=True,
        )
    value = mod.get("value")
    max_value = mod.get("maxValue")
    return _value_line_text(
        catalog, stat=stat, modifier_type=modifier_type,
        value=value, max_value=max_value if max_value is not None else value,
        description=description,
        has_actual=False,
    )


def _range_matches(values: tuple[float, float], expected: tuple[float, float]) -> bool:
    candidates = []
    for value in values:
        candidates.append(value)
        candidates.append(value * 100)
    for candidate in candidates:
        for other in candidates:
            if abs(candidate - expected[0]) <= 0.51 and abs(other - expected[1]) <= 0.51:
                return True
    return False


def _placeholder_replacement(
    catalog,
    placeholder,
    mods: list[dict],
    results: list[dict],
    ur_present: bool,
    warnings: list[str],
    trailing_percent: bool,
) -> tuple[str, list[int]]:
    plus, first, second, roll_id = placeholder
    expected = (float(first), float(second))
    roll_id = int(roll_id)
    candidates = [index for index, mod in enumerate(mods) if int(mod.get("rollId") if mod.get("rollId") is not None else -1) == roll_id]
    chosen = None
    covered: list[int] = []
    for index in candidates:
        mod = mods[index]
        value = mod.get("value")
        max_value = mod.get("maxValue")
        try:
            stat = _stat_for_mod(catalog, mod)
            range_text = rolls.display_range(value, max_value if max_value is not None else value, stat, int(mod.get("type") or 0))
        except (TypeError, ValueError):
            range_text = None
        if range_text is None:
            continue
        numbers = [float(token) for token in _NUMBER_RE.findall(range_text)]
        if len(numbers) >= 2 and _range_matches((numbers[0], numbers[1]), expected):
            covered.append(index)
            if chosen is None:
                chosen = index
    if chosen is None and candidates:
        chosen = candidates[0]
        covered.append(chosen)
        warnings.append(f"暗金描述占位符 [{first},{second},{roll_id}] 未找到数值匹配的 mod，使用首个同 rollId 词条")
    stat = None
    actual_text = None
    range_text = None
    if chosen is not None:
        mod = mods[chosen]
        stat = _stat_for_mod(catalog, mod)
        modifier_type = int(mod.get("type") or 0)
        result = results[chosen] if chosen < len(results) else None
        entry = ((result or {}).get("values") or [None])[0]
        if entry and entry.get("value_numeric") is not None:
            actual_text = rolls.display_value(entry["value_numeric"], stat, modifier_type)
        range_text = rolls.display_range(
            mod.get("value"), mod.get("maxValue") if mod.get("maxValue") is not None else mod.get("value"),
            stat, modifier_type,
        )
    if actual_text:
        replacement = actual_text
    elif range_text:
        replacement = range_text
    else:
        replacement = f"{first}–{second}"
        warnings.append(f"暗金描述占位符 [{first},{second},{roll_id}] 无法从定义解析，保留原始范围文本")
    if trailing_percent:
        if replacement.endswith("%"):
            pass
        else:
            replacement = f"{replacement}%"
    return f"{plus}{replacement}", covered


def _unique_lines(instance: dict, catalog, definition_entity_id: str) -> tuple[list[dict], list[str], dict]:
    lines: list[dict] = []
    warnings: list[str] = []
    tooltip = catalog.unique_tooltip(definition_entity_id)
    mods = tooltip.get("mods") or []
    entries = tooltip.get("tooltip_entries") or []
    descriptions = tooltip.get("tooltip_descriptions") or []
    unique_rolls = instance.get("unique_rolls") or {}
    results = unique_rolls.get("values") or []
    ur_list = unique_rolls.get("ur")
    ur_present = isinstance(ur_list, list) and len(ur_list) > 0
    stats = {"lines": 0, "mods": 0, "descriptions": 0, "hidden_mods": 0}
    emitted_mods: set[int] = set()
    described_mod_indices: set[int] = set()
    for item in entries:
        if item.get("kind") == "mod":
            mod_index = item.get("mod_index")
            if mod_index is None or mod_index >= len(mods):
                continue
            mod = mods[mod_index]
            hidden = bool(mod.get("hideInTooltip"))
            if hidden:
                stats["hidden_mods"] += 1
                continue
            emitted_mods.add(mod_index)
            result = results[mod_index] if mod_index < len(results) else None
            entry = ((result or {}).get("values") or [None])[0]
            roll_status = (result or {}).get("roll_status")
            raw_roll = (result or {}).get("raw_roll")
            text, value_status, value_text, range_text = _unique_mod_display(
                catalog, mod, entry, roll_status=roll_status, raw_roll=raw_roll
            )
            stats["lines"] += 1
            stats["mods"] += 1
            lines.append(
                _line(
                    section="unique_mod",
                    kind="mod",
                    text=text,
                    value_status=value_status,
                    value_text=value_text,
                    range_text=range_text,
                    diagnostics={
                        "mod_index": mod_index,
                        **_non_dps_diagnostics(_stat_for_mod(catalog, mod)),
                        "roll_id": mod.get("rollId"),
                        "can_roll": bool(mod.get("canRoll")),
                        "hidden": hidden,
                        "raw_roll": raw_roll,
                        "roll_status": roll_status,
                        "property": mod.get("property"),
                        "tags": mod.get("tags"),
                        "special_tag": mod.get("specialTag"),
                        "modifier_type": int(mod.get("type") or 0),
                        "raw_value": mod.get("value"),
                        "max_value": mod.get("maxValue"),
                        "value_numeric": (entry or {}).get("value_numeric"),
                        "stat_id": (entry or {}).get("stat_id"),
                        **_input_provenance(
                            (entry or {}).get("value_source") or (result or {}).get("value_source"),
                            (entry or {}).get("input_id") or (result or {}).get("input_id"),
                        ),
                    },
                )
            )
        elif item.get("kind") == "description":
            description_index = item.get("description_index")
            description = descriptions[description_index] if description_index < len(descriptions) else {}
            text = description.get("text")
            stats["descriptions"] += 1
            if not text:
                warnings.append(
                    f"暗金描述 #{description_index} 缺少翻译（key={description.get('key')!r}）；已保留结构化 mod 数据"
                )
                lines.append(
                    _line(
                        section="unique_description",
                        kind="description",
                        text=None,
                        value_status=VALUE_STATUS_UNRESOLVED,
                        diagnostics={"description_index": description_index, "translation_key": description.get("key")},
                    )
                )
                continue
            rendered, used_mods = _render_description(
                catalog, text, mods, results, ur_present, warnings
            )
            described_mod_indices.update(used_mods)
            lines.append(
                _line(
                    section="unique_description",
                    kind="description",
                    text=rendered,
                    value_status=VALUE_STATUS_ACTUAL if ur_present else VALUE_STATUS_RANGE,
                    diagnostics={"description_index": description_index, "translation_key": description.get("key")},
                )
            )
    # Hidden mods are covered when the tooltip descriptions reference them as value
    # sources, when they share (property, tags) with a visible mod, or when their name
    # appears in a description text.  Only genuinely uncovered hidden mods warn.
    described_text = " ".join(
        (description.get("text") or "") for description in descriptions
    ).lower()
    visible_mod_keys = {
        (mod.get("property"), mod.get("tags"))
        for index, mod in enumerate(mods)
        if index in emitted_mods
    }
    for mod_index, mod in enumerate(mods):
        if mod_index in emitted_mods:
            continue
        if not mod.get("hideInTooltip"):
            continue
        stats["hidden_mods"] += 1
        if mod_index in described_mod_indices:
            continue
        if (mod.get("property"), mod.get("tags")) in visible_mod_keys:
            continue
        stat = _stat_for_mod(catalog, mod)
        name = (catalog.stat_display_name(stat) or "").lower()
        if name and name in described_text:
            continue
        warnings.append(
            f"隐藏暗金 mod #{mod_index}（{catalog.stat_display_name(stat) or mod.get('property')}）"
            "没有被 tooltip 描述覆盖，保留在结构化数据中"
        )
    return lines, warnings, stats


def _render_description(catalog, text: str, mods, results, ur_present: bool, warnings: list[str]) -> tuple[str, set]:
    used_mods: set = set()

    def replace(match):
        replacement, used = _placeholder_replacement(
            catalog,
            (match.group(1), match.group(2), match.group(3), match.group(4)),
            mods,
            results,
            ur_present,
            warnings,
            trailing_percent=bool(match.group(5)),
        )
        used_mods.update(used)
        return replacement

    return _PLACEHOLDER_RE.sub(replace, text), used_mods


def _rarity(instance: dict) -> dict | None:
    marker = (instance.get("codec") or {}).get("marker")
    definition_status = instance.get("definition_status")
    if marker == "U" and definition_status == "resolved":
        return {"code": "unique", "label_zh": "暗金", "label_en": "Unique", "confidence": "definition"}
    if marker == "I":
        # A base-codec item's final rarity (normal/magic/rare) cannot be proven from the
        # source payload; do not show a rarity badge.
        return None
    if instance.get("definition") and definition_status == "resolved":
        return {"code": "unique", "label_zh": "暗金", "label_en": "Unique", "confidence": "definition"}
    return None


def _completeness(lines: list[dict]) -> str:
    if not lines:
        return "unavailable"
    statuses = [line.get("value_status") for line in lines]
    if all(status in (VALUE_STATUS_ACTUAL, VALUE_STATUS_CONSTANT) for status in statuses):
        return "complete"
    if all(status in (VALUE_STATUS_RANGE, VALUE_STATUS_CONSTANT, VALUE_STATUS_NOT_PROVIDED) for status in statuses):
        return "definition_only"
    if any(status == VALUE_STATUS_UNRESOLVED for status in statuses):
        return "partial"
    return "partial"


def build_item_readable(instance: dict, catalog) -> dict:
    """Build the ``readable`` block for one item/idol/blessing instance."""
    role = instance.get("role")
    definition = (instance.get("definition") or {}).get("ref") or {}
    base = (instance.get("base") or {}).get("ref") or {}
    definition_entity_id = definition.get("entity_id")
    base_entity_id = base.get("entity_id")
    display_name = definition.get("name") or base.get("name") or instance.get("encoded_id")
    base_name = base.get("name")
    warnings: list[str] = []
    lines: list[dict] = []

    implicit_entity = base_entity_id or definition_entity_id
    item_definition = catalog.item_definition(implicit_entity) if implicit_entity else {}
    implicit, implicit_warnings = _implicit_lines(instance, catalog, item_definition)
    warnings.extend(implicit_warnings)
    lines.extend(implicit)

    affix, affix_warnings = _affix_lines(instance, catalog)
    warnings.extend(affix_warnings)
    lines.extend(affix)

    unique_stats = {"lines": 0, "mods": 0, "descriptions": 0, "hidden_mods": 0}
    marker = (instance.get("codec") or {}).get("marker")
    definition_status = instance.get("definition_status")
    if marker == "U" and definition_entity_id and definition_status == "resolved":
        unique_from_instance = catalog.unique_tooltip(definition_entity_id)
        if not unique_from_instance.get("mods"):
            warnings.append("暗金定义没有 mods 数据，无法生成暗金词条")
        unique_lines, unique_warnings, unique_stats = _unique_lines(instance, catalog, definition_entity_id)
        warnings.extend(unique_warnings)
        lines.extend(unique_lines)
    elif instance.get("definition") and definition_entity_id:
        unique_lines, unique_warnings, unique_stats = _unique_lines(instance, catalog, definition_entity_id)
        warnings.extend(unique_warnings)
        lines.extend(unique_lines)

    block = {
        "version": READABLE_VERSION,
        "display_name": display_name,
        "base_name": base_name,
        "item_type": item_definition.get("item_type"),
        "gear_category": item_definition.get("gear_category"),
        "rarity": _rarity(instance),
        "role": role,
        "lines": lines,
        "warnings": warnings,
        "completeness": _completeness(lines),
        "unique_composition": dict(unique_stats),
    }
    if role in ("blessing", "idol"):
        block["item_type"] = block["item_type"] or ("Blessing" if role == "blessing" else "Idol")
    return block


__all__ = [
    "READABLE_VERSION",
    "CHANNEL_LABELS",
    "VALUE_STATUS_ACTUAL",
    "VALUE_STATUS_RANGE",
    "VALUE_STATUS_CONSTANT",
    "VALUE_STATUS_UNRESOLVED",
    "build_item_readable",
]
