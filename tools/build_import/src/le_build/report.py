"""Import report, acceptance checklist and calculation-gap report (JSON + Chinese Markdown)."""

from __future__ import annotations

import datetime as dt

from .validate import coverage as compute_coverage
from .validate import validate_spec

SAMPLE_SHARE_ID = "o3ZRm9my"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _count(spec: dict) -> dict:
    items = spec.get("equipment", {}).get("slots", [])
    idols = spec.get("idols", {}).get("entries", [])
    blessings = spec.get("blessings", {}).get("entries", [])
    affix_channels: dict = {}
    for slot in items:
        for channel, entries in ((slot.get("instance") or {}).get("affixes") or {}).items():
            affix_channels[channel] = affix_channels.get(channel, 0) + len(entries or [])
    for entry in idols:
        for channel, entries in ((entry.get("instance") or {}).get("affixes") or {}).items():
            affix_channels[channel] = affix_channels.get(channel, 0) + len(entries or [])
    for entry in blessings:
        for channel, entries in ((entry.get("instance") or {}).get("affixes") or {}).items():
            affix_channels[channel] = affix_channels.get(channel, 0) + len(entries or [])
    readable = (spec.get("readable_summary") or {})
    return {
        "equipment_slots": len(items),
        "idols": len(idols),
        "blessings": len(blessings),
        "item_instances": len(items) + len(idols) + len(blessings),
        "specializations": len(spec.get("specializations", [])),
        "action_bar": len(spec.get("action_bar", {}).get("slots", [])),
        "passive_nodes": len(spec.get("passive_tree", {}).get("nodes", [])),
        "skill_tree_nodes": sum(len(entry.get("nodes", [])) for entry in spec.get("specializations", [])),
        "node_entries": len(spec.get("passive_tree", {}).get("nodes", []))
        + sum(len(entry.get("nodes", [])) for entry in spec.get("specializations", [])),
        "passive_points": spec.get("passive_tree", {}).get("points_total"),
        "skill_points": sum(entry.get("points_total", 0) for entry in spec.get("specializations", [])),
        "affix_instances": sum(affix_channels.values()),
        "affix_channels": affix_channels,
        "completed_quests": len(spec.get("progression", {}).get("completed_quests", [])),
        "readable_items": readable.get("items"),
        "readable_by_completeness": readable.get("by_completeness"),
    }


def _slot_map(spec: dict) -> dict:
    return {
        (slot.get("slot") or ""): slot.get("instance") or {}
        for slot in spec.get("equipment", {}).get("slots", [])
    }


def _all_item_instances(spec: dict) -> list[dict]:
    instances: list[dict] = []
    for slot in spec.get("equipment", {}).get("slots", []):
        instances.append(slot.get("instance") or {})
    for entry in spec.get("idols", {}).get("entries", []):
        instances.append(entry.get("instance") or {})
    for entry in spec.get("blessings", {}).get("entries", []):
        instances.append(entry.get("instance") or {})
    return instances


def _all_affix_entries(spec: dict):
    sources = []
    for slot in spec.get("equipment", {}).get("slots", []):
        sources.append((slot.get("slot") or "", slot.get("instance") or {}))
    for entry in spec.get("idols", {}).get("entries", []):
        sources.append((f"idols.{entry.get('index')}", entry.get("instance") or {}))
    for entry in spec.get("blessings", {}).get("entries", []):
        sources.append((f"blessings.{entry.get('timeline')}", entry.get("instance") or {}))
    for name, instance in sources:
        for channel, entries in (instance.get("affixes") or {}).items():
            for entry in entries or []:
                yield name, channel, entry


def _multi_affix_lines_distinguishable(spec: dict) -> bool:
    """Review P1-2: generic property names must carry their tag/ailment qualifier."""
    checks = 0
    for path, instance in _iter_instances_for_acceptance(spec):
        by_affix: dict = {}
        for line in (instance.get("readable") or {}).get("lines") or []:
            diagnostics = line.get("diagnostics") or {}
            affix_id = diagnostics.get("affix_entity_id")
            if not affix_id:
                continue
            by_affix.setdefault(affix_id, []).append(line.get("text") or "")
        for affix_id, texts in by_affix.items():
            if len(texts) > 1 and len(set(texts)) != len(texts):
                return False
            if len(texts) > 1:
                checks += 1
    return checks >= 1


def _multi_affix_observed(spec: dict) -> dict:
    samples = []
    for path, instance in _iter_instances_for_acceptance(spec):
        for line in (instance.get("readable") or {}).get("lines") or []:
            diagnostics = line.get("diagnostics") or {}
            if diagnostics.get("affix_entity_id", "").endswith(
                (":167", ":852", ":848", ":1070")
            ):
                samples.append({"path": path, "text": line.get("text")})
    deduped = []
    seen = set()
    for sample in samples:
        key = (sample["path"], sample["text"])
        if key not in seen:
            seen.add(key)
            deduped.append(sample)
    return deduped[:8]


def _iter_instances_for_acceptance(spec: dict):
    for slot in (spec.get("equipment") or {}).get("slots", []):
        yield f"equipment.{slot.get('slot')}", slot.get("instance") or {}
    for entry in (spec.get("idols") or {}).get("entries", []):
        yield f"idols.{entry.get('index')}", entry.get("instance") or {}
    for entry in (spec.get("blessings") or {}).get("entries", []):
        yield f"blessings.{entry.get('timeline')}", entry.get("instance") or {}


def _acceptance_check(check_id: str, label: str, ok: bool, observed, expected) -> dict:
    return {
        "check": check_id,
        "label": label,
        "status": "pass" if ok else "fail",
        "observed": observed,
        "expected": expected,
    }


def acceptance_checks(spec: dict) -> list[dict]:
    """Acceptance checklist for the reworked 2026-11-03 scope and display contract.

    The fixed regression sample checks: character, 29 in-scope item instances
    (11 equipment + 8 idols + 10 blessings), 80 node entries (19+61), 47 affixes,
    weaver exclusion, player-readable display fixes (percent semantics, unique
    composition, ur=[] definitions, blessing implicits, per-mod roll ids) and the
    source-traceability facts that must not regress.
    """
    counts = _count(spec)
    instances = _all_item_instances(spec)
    by_role: dict = {}
    for instance in instances:
        by_role[instance.get("role")] = by_role.get(instance.get("role"), 0) + 1
    readable_instances = [instance for instance in instances if (instance.get("readable") or {}).get("lines")]
    readable_lines = [
        line for instance in instances for line in (instance.get("readable") or {}).get("lines") or []
    ]

    def instance_by_slot(slot: str) -> dict:
        for entry in (spec.get("equipment") or {}).get("slots", []):
            if entry.get("slot") == slot:
                return entry.get("instance") or {}
        return {}

    chest = instance_by_slot("chest")
    head = instance_by_slot("head")
    hands = instance_by_slot("hands")
    ring2 = instance_by_slot("ring2")
    amulet = instance_by_slot("amulet")
    spine = instance_by_slot("weapon1")
    def any_line(instance, *needles, section=None):
        for line in (instance.get("readable") or {}).get("lines") or []:
            text = line.get("text") or ""
            if section and line.get("section") != section:
                continue
            if all(needle in text for needle in needles):
                return line
        return None

    chest_mana = any_line(chest, "11%", "increased Mana")
    chest_regen = any_line(chest, "22%", "increased Mana Regen")
    head_regen = any_line(head, "27%", "increased Mana Regen")
    percent_ok = bool(chest_mana and chest_regen and head_regen)
    damage_line = next(
        (
            line
            for line in (chest.get("readable") or {}).get("lines") or []
            if line.get("section") == "unique_mod"
            and "68" in (line.get("text") or "")
            and "increased" in (line.get("text") or "")
            and "Damage" in (line.get("text") or "")
        ),
        None,
    )
    damage_ok = bool(
        damage_line
        and any(
            tag in (damage_line.get("text") or "")
            for tag in ("Elemental", "Fire", "Cold", "Lightning", "Physical", "Void", "Necrotic", "Poison")
        )
    )

    withstand_levels = [
        line.get("text")
        for line in (hands.get("readable") or {}).get("lines") or []
        if "Level of" in (line.get("text") or "") and "Skills" in (line.get("text") or "")
    ]
    withstand_distinct = len(set(withstand_levels)) >= 2 and any("Physical" in (text or "") for text in withstand_levels) and any("Cold" in (text or "") for text in withstand_levels)

    apex_lines = (head.get("readable") or {}).get("lines") or []
    apex_ailment_mod_lines = [
        line for line in apex_lines
        if line.get("section") == "unique_mod" and "Ailment Duration" in (line.get("text") or "")
    ]
    apex_descriptions = [line.get("text") or "" for line in apex_lines if line.get("section") == "unique_description"]
    apex_ok = (
        not apex_ailment_mod_lines
        and len(apex_descriptions) >= 2
        and any("Glyph of Dominion" in text for text in apex_descriptions)
        and any("Duration of Ignite" in text and "%" in text and "%%" not in text for text in apex_descriptions)
    )

    omnis_lines = (amulet.get("readable") or {}).get("lines") or []
    unsated_lines = (ring2.get("readable") or {}).get("lines") or []

    def _definition_value_ok(line: dict) -> bool:
        """Range from the definition, or an actual value explicitly supplied by local input."""
        status = line.get("value_status")
        source = str((line.get("diagnostics") or {}).get("value_source") or "source")
        if status == "range":
            return True
        return status == "actual" and source.startswith("local_")

    omnis_range_ok = (
        any(_definition_value_ok(line) and "Level of Skills" in (line.get("text") or "") for line in omnis_lines)
        and any("Resistance" in (line.get("text") or "") for line in omnis_lines)
    )
    unsated_range_ok = (
        any(_definition_value_ok(line) and "Endurance" in (line.get("text") or "") for line in unsated_lines)
        and any("Strength" in (line.get("text") or "") or "Vitality" in (line.get("text") or "") for line in unsated_lines)
        and any("Frenzy" in (line.get("text") or "") for line in unsated_lines)
    )
    unique_definition_ok = omnis_range_ok and unsated_range_ok

    blessing_implicit_ok = all(
        [
            any(
                line.get("section") == "implicit" and line.get("value_status") == "actual"
                for line in (entry.get("instance") or {}).get("readable", {}).get("lines") or []
            )
            for entry in (spec.get("blessings") or {}).get("entries", [])
        ]
    ) and not any(
        issue.get("code") == "implicit_definition_unavailable"
        and "blessing" in str((issue.get("detail") or {}).get("role", ""))
        for issue in spec.get("issues", [])
    )
    first_blessing_ok = any(
        "Drop Rate" in (line.get("text") or "") and line.get("value_status") == "actual"
        for line in (((spec.get("blessings") or {}).get("entries") or [{}])[0].get("instance") or {}).get("readable", {}).get("lines") or []
    )

    # 2026-11-04 re-review R1: the ward blessing must state its trigger event.
    blessing_instances = [
        entry.get("instance") or {} for entry in (spec.get("blessings") or {}).get("entries", [])
    ]
    ward_blessing = next(
        (
            instance
            for instance in blessing_instances
            if (instance.get("readable") or {}).get("display_name") == "Grand Greed of Darkness"
        ),
        {},
    )
    ward_lines = [
        line
        for line in (ward_blessing.get("readable") or {}).get("lines") or []
        if (line.get("diagnostics") or {}).get("stat_name") == "Ward Gain"
        or "Ward Gain" in (line.get("text") or "")
    ]
    ward_event_ok = any(
        "on Kill" in (line.get("text") or "") and "Ward Gain" in (line.get("text") or "")
        for line in ward_lines
    )

    # 2026-11-04 re-review R2: hideModifierValue must drop the numeric prefix/plus.
    ring2_sealed_line = next(
        (
            line
            for line in (ring2.get("readable") or {}).get("lines") or []
            if line.get("section") == "affix_sealed"
        ),
        None,
    )
    hidden_value_ok = bool(
        ring2_sealed_line
        and (ring2_sealed_line.get("text") or "") == "You have Crescendoing Rage"
        and not (ring2_sealed_line.get("text") or "").startswith("+")
        and ring2_sealed_line.get("value_text") == "1"
    )

    # R1 drop-rate removal: identified by source property identity (propertyList 104), and
    # flagged for the display/calculator layers without deleting the structured value.
    drop_rate_lines = [
        line
        for line in readable_lines
        if (line.get("diagnostics") or {}).get("property") == 104
    ]
    non_dps_ok = len(drop_rate_lines) == 5 and all(
        (line.get("diagnostics") or {}).get("non_dps") is True for line in drop_rate_lines
    )

    unique_mods = (head.get("unique_rolls") or {}).get("mods") or []
    roll_id_ok = bool(unique_mods) and unique_mods[0].get("roll_id") == 3 and unique_mods[0].get("raw_roll") == 234
    first_unique_line_roll_ids = {
        (line.get("diagnostics") or {}).get("roll_id")
        for line in apex_lines
        if line.get("section") == "unique_mod"
    }

    excluded_features = [entry.get("feature") for entry in (spec.get("scope") or {}).get("excluded_features") or []]
    weaver_issues = [issue for issue in spec.get("issues", []) if "weaver" in (issue.get("code") or "")]
    double_units = [line for line in readable_lines if "%%" in (line.get("text") or "")]
    no_text_lines = [line for line in readable_lines if not line.get("text")]

    checks: list[dict] = [
        _acceptance_check(
            "character",
            "角色 profile（Mage / Sorcerer / 100）",
            ((spec.get("character") or {}).get("class") or {}).get("ref", {}).get("name") == "Mage"
            and ((spec.get("character") or {}).get("mastery") or {}).get("ref", {}).get("name") == "Sorcerer"
            and (spec.get("character") or {}).get("level") == 100,
            {
                "class": ((spec.get("character") or {}).get("class") or {}).get("ref", {}).get("name"),
                "mastery": ((spec.get("character") or {}).get("mastery") or {}).get("ref", {}).get("name"),
                "level": (spec.get("character") or {}).get("level"),
            },
            {"class": "Mage", "mastery": "Sorcerer", "level": 100},
        ),
        _acceptance_check(
            "scope_excluded_weaver",
            "编织者天赋树按用户范围排除（原始响应仍归档）",
            "weaver_tree" in excluded_features
            and (spec.get("weaver") or {}).get("status") == "excluded_by_scope"
            and not weaver_issues,
            {
                "excluded_features": excluded_features,
                "weaver_status": (spec.get("weaver") or {}).get("status"),
                "weaver_issues": len(weaver_issues),
            },
            {"excluded_features": ["weaver_tree"], "weaver_status": "excluded_by_scope", "weaver_issues": 0},
        ),
        _acceptance_check(
            "item_instance_counts",
            "物品实例计数（11 装备 / 8 神像 / 10 祝福 = 29；不含编织容器）",
            counts["equipment_slots"] == 11
            and counts["idols"] == 8
            and counts["blessings"] == 10
            and counts["item_instances"] == 29,
            {
                "equipment": counts["equipment_slots"],
                "idols": counts["idols"],
                "blessings": counts["blessings"],
                "total": counts["item_instances"],
            },
            {"equipment": 11, "idols": 8, "blessings": 10, "total": 29},
        ),
        _acceptance_check(
            "item_identity",
            "全部 29 个物品实例基底/定义可关联",
            all((instance.get("base") or {}).get("ref") for instance in instances),
            {
                "with_base": sum(1 for instance in instances if (instance.get("base") or {}).get("ref")),
                "total": len(instances),
            },
            {"with_base": 29, "total": 29},
        ),
        _acceptance_check(
            "slots_preserved",
            "全部来源槽位与坐标保留，weapon2 不被自动补齐",
            (spec.get("equipment") or {}).get("slot_order")
            == ["head", "chest", "weapon1", "hands", "waist", "feet", "ring1", "ring2", "amulet", "relic", "idol_altar"]
            and (spec.get("equipment") or {}).get("absent_slots") == ["weapon2"]
            and all((entry.get("position") or {}).get("x") is not None for entry in (spec.get("idols") or {}).get("entries", [])),
            {
                "absent_slots": (spec.get("equipment") or {}).get("absent_slots"),
                "idol_positions_preserved": sum(
                    1 for entry in (spec.get("idols") or {}).get("entries", [])
                    if (entry.get("position") or {}).get("x") is not None
                ),
            },
            {"absent_slots": ["weapon2"], "idol_positions_preserved": 8},
        ),
        _acceptance_check(
            "specialization_and_hud",
            "5 专精与 5 HUD 项独立；Teleport 在 HUD、Flame Ward 在专精",
            counts["specializations"] == 5
            and counts["action_bar"] == 5
            and "te44" in ((spec.get("action_bar") or {}).get("source_ids") or [])
            and any((entry.get("tree") or {}).get("entity_id", "").endswith(":fw3d") for entry in spec.get("specializations", []))
            and not any((entry.get("owner_skill") or {}).get("name") == "Teleport" for entry in spec.get("specializations", [])),
            {
                "specializations": counts["specializations"],
                "action_bar": counts["action_bar"],
                "hud_ids": (spec.get("action_bar") or {}).get("source_ids"),
            },
            {"specializations": 5, "action_bar": 5, "hud_contains": "te44"},
        ),
        _acceptance_check(
            "node_entry_counts",
            "节点条目计数（19 被动 / 61 专精 = 80；编织树不计入）",
            counts["passive_nodes"] == 19 and counts["skill_tree_nodes"] == 61 and counts["node_entries"] == 80,
            {
                "passive": counts["passive_nodes"],
                "skill_tree": counts["skill_tree_nodes"],
                "total": counts["node_entries"],
            },
            {"passive": 19, "skill_tree": 61, "total": 80},
        ),
        _acceptance_check(
            "explicit_zero_nodes",
            "3 个显式 0 点专精节点不消失",
            sum(
                1 for entry in spec.get("specializations", []) for node in entry.get("nodes", [])
                if node.get("explicit_zero")
            ) == 3,
            {
                "explicit_zero": sum(
                    1 for entry in spec.get("specializations", []) for node in entry.get("nodes", [])
                    if node.get("explicit_zero")
                )
            },
            {"explicit_zero": 3},
        ),
        _acceptance_check(
            "affix_channels",
            "47 条词缀实例与 4 种通道（普通 37 / 封印 2 / 原初 2 / 腐化 6）",
            counts["affix_instances"] == 47
            and counts["affix_channels"] == {"normal": 37, "sealed": 2, "primordial": 2, "corrupted": 6},
            counts["affix_channels"],
            {"normal": 37, "sealed": 2, "primordial": 2, "corrupted": 6},
        ),
        _acceptance_check(
            "affix_tiers",
            "47/47 词缀 tier 关联到库内 tier（source tier-1），T8 不丢失",
            all(
                (entry.get("tier") or {}).get("status") == "resolved"
                for _, _, entry in _all_affix_entries(spec)
            )
            and any((entry.get("tier") or {}).get("source_tier") == 8 for _, _, entry in _all_affix_entries(spec)),
            {
                "resolved": sum(
                    1 for _, _, entry in _all_affix_entries(spec)
                    if (entry.get("tier") or {}).get("status") == "resolved"
                ),
                "source_tier_8_present": any(
                    (entry.get("tier") or {}).get("source_tier") == 8 for _, _, entry in _all_affix_entries(spec)
                ),
            },
            {"resolved": 47, "source_tier_8_present": True},
        ),
        _acceptance_check(
            "absent_rolls",
            "6 个来源缺失 roll 全部可定位（来源 absent；可被本地输入补齐并标记 value_source）",
            sum(
                1
                for _, _, entry in _all_affix_entries(spec)
                if ((entry.get("roll") or {}).get("source_status") or (entry.get("roll") or {}).get("status"))
                in ("absent", "null")
            )
            == 6,
            {
                "source_absent": [
                    {
                        "path": entry.get("source_path"),
                        "encoded_id": entry.get("encoded_id"),
                        "current_status": (entry.get("roll") or {}).get("status"),
                        "value_source": (entry.get("value") or {}).get("value_source"),
                    }
                    for _, _, entry in _all_affix_entries(spec)
                    if ((entry.get("roll") or {}).get("source_status") or (entry.get("roll") or {}).get("status"))
                    in ("absent", "null")
                ],
                "filled_from_local_inputs": sum(
                    1
                    for _, _, entry in _all_affix_entries(spec)
                    if (entry.get("roll") or {}).get("status") == "local_input"
                ),
            },
            {"count": 6},
        ),
        _acceptance_check(
            "new_definitions",
            "Withstand the Elements / Unsated Rage / 1147 / 1144 / 1124 不依赖旧静态映射即可解析",
            any(((instance.get("definition") or {}).get("ref", {}) or {}).get("name") == "Withstand the Elements" for instance in instances)
            and any(((instance.get("definition") or {}).get("ref", {}) or {}).get("name") == "Unsated Rage" for instance in instances)
            and all(
                any(
                    (entry.get("codec") or {}).get("affix_id") == affix_id
                    and (entry.get("affix") or {}).get("status") == "resolved"
                    for _, _, entry in _all_affix_entries(spec)
                )
                for affix_id in (1147, 1144, 1124)
            ),
            {
                "withstand": any(((instance.get("definition") or {}).get("ref", {}) or {}).get("name") == "Withstand the Elements" for instance in instances),
                "unsated_rage": any(((instance.get("definition") or {}).get("ref", {}) or {}).get("name") == "Unsated Rage" for instance in instances),
                "new_affix_ids": [1147, 1144, 1124],
            },
            "all resolved without legacy mapping tables",
        ),
        _acceptance_check(
            "context_preserved",
            "activeBuffs.skills / completedQuests / faction / newGrid / progression 全部保留",
            ((spec.get("context") or {}).get("active_buffs", {}).get("raw", {}) or {}).get("skills") == ["sb44eQ"]
            and counts["completed_quests"] == 20
            and (spec.get("idols") or {}).get("new_grid") is True
            and all(
                key in (spec.get("progression") or {})
                for key in ("char_tree_progression", "skill_trees_progression", "completed_quests")
            ),
            {
                "active_buff_skills": ((spec.get("context") or {}).get("active_buffs", {}).get("raw", {}) or {}).get("skills"),
                "completed_quests": counts["completed_quests"],
                "new_grid": (spec.get("idols") or {}).get("new_grid"),
            },
            {"active_buff_skills": ["sb44eQ"], "completed_quests": 20},
        ),
        _acceptance_check(
            "unknown_fields_preserved",
            "未知字段保留并可追溯（不静默丢失），排除的范围字段有声明",
            isinstance((spec.get("extensions") or {}).get("source_field_catalog"), dict)
            and isinstance((spec.get("extensions") or {}).get("raw_source"), dict)
            and isinstance((spec.get("extensions") or {}).get("excluded_by_scope_source_fields"), dict),
            {
                "unexpected_top_level": (spec.get("extensions") or {}).get("unexpected_top_level_fields"),
                "unexpected_data": (spec.get("extensions") or {}).get("unexpected_data_fields"),
                "excluded_source_fields": sorted(((spec.get("extensions") or {}).get("excluded_by_scope_source_fields") or {}).keys()),
            },
            "raw archive pointer + excluded scope declaration present",
        ),
        _acceptance_check(
            "readable_layer",
            "玩家可读词条层覆盖全部范围内物品且无缺文本/重复单位",
            len(readable_instances) == len(instances) and not double_units and not no_text_lines,
            {
                "items_with_readable": len(readable_instances),
                "items_total": len(instances),
                "lines": len(readable_lines),
                "duplicated_percent_units": len(double_units),
                "lines_without_text": len(no_text_lines),
            },
            {"items_with_readable": 29, "duplicated_percent_units": 0, "lines_without_text": 0},
        ),
        _acceptance_check(
            "percent_modifier_text",
            "百分比修饰文本（胸甲 Mana 11% / Mana Regen 22% / 头部 Mana Regen 27%；Damage 带标签）",
            percent_ok and damage_ok,
            {
                "chest_mana": (chest_mana or {}).get("text"),
                "chest_mana_regen": (chest_regen or {}).get("text"),
                "head_mana_regen": (head_regen or {}).get("text"),
                "damage": (damage_line or {}).get("text"),
            },
            {
                "chest_mana": "11% increased Mana",
                "chest_mana_regen": "22% increased Mana Regen",
                "head_mana_regen": "27% increased Mana Regen",
            },
        ),
        _acceptance_check(
            "generic_property_distinguishable",
            "泛化属性名带标签限定（多词缀/暗金不出现无法区分的同名行）",
            _multi_affix_lines_distinguishable(spec),
            _multi_affix_observed(spec),
            "Ignite/Spreading Flames Duration and Fire/Minion Fire Resistance etc. distinguishable",
        ),
        _acceptance_check(
            "unique_skill_level_distinguishable",
            "Withstand the Elements 两条技能等级可区分（Physical / Cold）",
            withstand_distinct,
            {"lines": withstand_levels},
            "two distinct Level of ... Skills lines",
        ),
        _acceptance_check(
            "unique_tooltip_composition",
            "Apex of Thought 不出现六条同名内部效果，两条组合描述保留并替换实值",
            apex_ok,
            {
                "ailment_mod_lines": [line.get("text") for line in apex_ailment_mod_lines],
                "descriptions": apex_descriptions,
            },
            "no duplicate hidden ailment mod lines; both description translations rendered",
        ),
        _acceptance_check(
            "unique_definition_without_roll",
            "Omnis / Unsated Rage 的无 roll 定义词条可见（范围与常量，不伪造实值）",
            unique_definition_ok,
            {
                "omnis_lines": [line.get("text") for line in omnis_lines],
                "unsated_lines": [line.get("text") for line in unsated_lines],
            },
            "definition lines present with value_status range/constant",
        ),
        _acceptance_check(
            "blessing_implicits",
            "10 个祝福 implicit 从归档 item_base 定义恢复（不再全部缺失）",
            blessing_implicit_ok and first_blessing_ok,
            {
                "blessings_with_actual_implicit": sum(
                    1
                    for entry in (spec.get("blessings") or {}).get("entries", [])
                    if any(
                        line.get("section") == "implicit" and line.get("value_status") == "actual"
                        for line in (entry.get("instance") or {}).get("readable", {}).get("lines") or []
                    )
                ),
                "first_blessing_lines": [
                    line.get("text")
                    for line in (((spec.get("blessings") or {}).get("entries") or [{}])[0].get("instance") or {}).get("readable", {}).get("lines") or []
                ],
            },
            {"blessings_with_actual_implicit": 10},
        ),
        _acceptance_check(
            "per_mod_roll_ids",
            "逐 mod rollId 与实例取值一致（Apex 首条 rollId=3 / raw_roll=234）",
            roll_id_ok and first_unique_line_roll_ids <= {3, 1, 2, 0},
            {
                "first_mod": unique_mods[0] if unique_mods else None,
                "unique_line_roll_ids": sorted(value for value in first_unique_line_roll_ids if value is not None),
            },
            {"roll_id": 3, "raw_roll": 234},
        ),
        _acceptance_check(
            "definitions_readable_other_uniques",
            "Unstable Core / Spine of Malatros 组合词条可读",
            bool((chest.get("readable") or {}).get("lines"))
            and any("Elemental Nova" in (line.get("text") or "") for line in (chest.get("readable") or {}).get("lines") or [])
            and bool((spine.get("readable") or {}).get("lines"))
            and any("Chthonic" in (line.get("text") or "") for line in (spine.get("readable") or {}).get("lines") or []),
            {
                "unstable_core_has_description": any(
                    "Elemental Nova" in (line.get("text") or "")
                    for line in (chest.get("readable") or {}).get("lines") or []
                ),
                "spine_has_description": any(
                    "Chthonic" in (line.get("text") or "")
                    for line in (spine.get("readable") or {}).get("lines") or []
                ),
            },
            "combined descriptions readable for both uniques",
        ),
        _acceptance_check(
            "ward_gain_event",
            "护盾祝福补全获得事件（Grand Greed of Darkness → 18 Ward Gain on Kill）",
            ward_event_ok,
            {
                "ward_lines": [line.get("text") for line in ward_lines],
                "trigger": "on Kill",
            },
            ["18 Ward Gain on Kill"],
        ),
        _acceptance_check(
            "hidden_modifier_value",
            "hideModifierValue 生效（Crescendoing Rage 不再显示 +1；结构化值保留）",
            hidden_value_ok,
            {
                "sealed_line": (ring2_sealed_line or {}).get("text"),
                "value_text": (ring2_sealed_line or {}).get("value_text"),
                "value_status": (ring2_sealed_line or {}).get("value_status"),
            },
            {"text": "You have Crescendoing Rage", "value_text": "1"},
        ),
        _acceptance_check(
            "drop_rate_non_dps",
            "掉落率效果按来源属性身份标记为非 DPS（默认展示/计算输入过滤，结构化数据保留）",
            non_dps_ok,
            {
                "drop_rate_lines": [
                    {"text": line.get("text"), "property": (line.get("diagnostics") or {}).get("property")}
                    for line in drop_rate_lines
                ],
            },
            {"count": 5, "property": 104, "non_dps": True},
        ),
    ]
    return checks


def build_import_report(
    spec: dict,
    *,
    import_info: dict,
    validation: dict | None = None,
    coverage: dict | None = None,
    acceptance: list[dict] | None = None,
) -> dict:
    validation = validation or validate_spec(spec)
    coverage = coverage or compute_coverage(spec)
    acceptance = acceptance if acceptance is not None else acceptance_checks(spec)
    counts = _count(spec)
    issues = spec.get("issues", [])
    by_severity: dict = {}
    by_code: dict = {}
    for issue in issues:
        by_severity[issue.get("severity")] = by_severity.get(issue.get("severity"), 0) + 1
        by_code[issue.get("code")] = by_code.get(issue.get("code"), 0) + 1
    unresolved = [issue for issue in issues if issue.get("code", "").startswith("identity_")]
    absent_rolls = [
        {
            "path": entry.get("source_path"),
            "channel": channel,
            "encoded_id": entry.get("encoded_id"),
            "source_tier": entry.get("source_tier"),
            "affix": ((entry.get("affix") or {}).get("ref") or {}).get("name"),
            "roll_status": (entry.get("roll") or {}).get("status"),
        }
        for _, channel, entry in _all_affix_entries(spec)
        if (entry.get("roll") or {}).get("status") in ("absent", "null")
    ]
    return {
        "report_version": "import-report-v1",
        "generated_at": _now(),
        "build_id": (spec.get("meta") or {}).get("build_id"),
        "share_id": (spec.get("meta") or {}).get("share_id"),
        "share_url": (spec.get("meta") or {}).get("share_url"),
        "revision": (spec.get("meta") or {}).get("revision"),
        "import": {
            "attempt_id": (spec.get("meta") or {}).get("source_payload", {}).get("attempt_id"),
            "raw_sha256": (spec.get("meta") or {}).get("source_payload", {}).get("raw_sha256"),
            "fetched_at": (spec.get("meta") or {}).get("source_payload", {}).get("fetched_at"),
            "fetch_mode": (spec.get("meta") or {}).get("source_payload", {}).get("fetch_mode"),
            "requested_url": (spec.get("meta") or {}).get("share_url"),
            **{key: value for key, value in (import_info or {}).items() if value is not None},
        },
        "statuses": spec.get("statuses"),
        "catalog": {
            key: (spec.get("catalog") or {}).get(key)
            for key in (
                "dataset_id",
                "schema_version",
                "target_game_version",
                "target_season",
                "source_snapshot_id",
                "logical_content_sha256",
                "file_sha256",
                "compatibility",
                "verification",
            )
        },
        "protocol": (spec.get("meta") or {}).get("protocol"),
        "counts": counts,
        "coverage": coverage,
        "validation": validation,
        "acceptance": acceptance,
        "issues": {
            "by_severity": by_severity,
            "by_code": by_code,
            "items": issues,
            "identity_unresolved_or_ambiguous": unresolved,
        },
        "absent_rolls": absent_rolls,
        "scope": spec.get("scope"),
        "readable": {
            "summary": spec.get("readable_summary"),
            "items_not_complete": (spec.get("readable_summary") or {}).get("items_not_complete"),
        },
        "value_resolution": {
            "status": (spec.get("statuses") or {}).get("value_resolution"),
            "coverage": coverage.get("values"),
            "notes": spec.get("value_resolution_notes"),
        },
        "context": spec.get("context"),
        "commands": {
            "import_url": (
                f"python -m le_build import --url {spec.get('meta', {}).get('share_url')} "
                f"--dataset {(spec.get('catalog') or {}).get('dataset_id')}"
            ),
            "validate": f"python -m le_build validate --build {(spec.get('meta') or {}).get('build_id')} --offline",
            "explain": f"python -m le_build explain --build {(spec.get('meta') or {}).get('build_id')} --path equipment.hands",
        },
        "notes": _report_notes(spec),
    }


def _report_notes(spec: dict) -> list[str]:
    notes = [
        "本报告只证明结构与身份/数值/可读文本关联的范围；不包含 DPS、Buff 覆盖率、循环模拟或与网站 Calculations 总值一致性的承诺。",
        "所有未解析、歧义、缺失与未识别字段都列入 issues，不以 READY 掩盖薄弱环节。",
        "覆盖率按各自类别的分母统计，未把实体数相加成单一支持率。",
        "数值恢复（value_resolution）与词条可读（text_readiness）分开报告，均不等于战斗机制已实现。",
    ]
    scope = spec.get("scope") or {}
    excluded = [entry.get("feature") for entry in scope.get("excluded_features") or []]
    if "weaver_tree" in excluded:
        notes.append("编织者天赋树按用户 2026-11-03 的范围要求排除；原始响应仍完整归档，历史 revision 只读保留。")
    readable = spec.get("readable_summary") or {}
    if readable.get("by_completeness"):
        notes.append(f"可读词条完整度：{readable.get('by_completeness')}（definition_only/partial 表示部分数值未提供，文本仍可见）。")
    if any((entry.get("roll") or {}).get("status") in ("absent", "null") for _, _, entry in _all_affix_entries(spec)):
        notes.append("来源未提供 roll 的词缀保存为 roll_status=absent，不伪造实际 roll、不按 0 处理。")
    return notes


# ------------------------------------------------------------------- gap report
def build_gap_report(spec: dict, catalog) -> dict:
    """Calculation dependency inventory for phase 3 (no combat semantics are claimed)."""
    entries: list[dict] = []
    seen: set = set()

    def add_entry(
        category: str,
        ref: dict | None,
        *,
        source_path: str,
        extra: dict | None = None,
        identity_status: str | None = None,
    ) -> None:
        if not ref or not ref.get("entity_id"):
            return
        entity_id = ref["entity_id"]
        signature = (category, entity_id, source_path)
        if signature in seen:
            return
        seen.add(signature)
        entity = catalog.entity(entity_id) or {}
        effects = catalog.effects_for(entity_id)
        effect_ids = [effect["effect_id"] for effect in effects]
        values = catalog.effect_value_summary(effect_ids)
        buff = catalog.buff(entity_id) if entity.get("kind") == "buff" else None
        relationships = catalog.relationships_for(entity_id)
        child_relations = [
            {
                "type": row["relationship_type"],
                "other": row.get("to_entity_id") if row.get("from_entity_id") == entity_id else row.get("from_entity_id"),
                "resolution_status": row.get("resolution_status"),
            }
            for row in relationships
            if row["relationship_type"] in ("triggers_skill", "modifies_skill", "grants_ability", "applies_ailment", "grants_buff_candidate")
        ]
        parsed_effects = sum(1 for effect in effects if effect.get("parse_status") == "parsed")
        entries.append(
            {
                "category": category,
                "entity_id": entity_id,
                "name": ref.get("name") or catalog.name(entity_id),
                "entity_kind": entity.get("kind"),
                "source_path": source_path,
                "identity_status": identity_status or ref.get("status") or "resolved",
                "effects": {
                    "total": len(effects),
                    "parsed": parsed_effects,
                },
                "effect_values": values,
                "buff": {
                    "duration_ms": buff.get("duration_ms") if buff else None,
                    "max_stacks": buff.get("max_stacks") if buff else None,
                    "duration_known": bool(buff and buff.get("duration_ms") is not None),
                }
                if buff
                else None,
                "referenced_children": child_relations[:50],
                "duration_known": bool(buff and buff.get("duration_ms") is not None),
                "conditions_known": False,
                "calc_status": "not_implemented",
                "notes": (extra or {}).get("notes"),
            }
        )

    # selected skills (specializations, action bar, saved active-buff configuration)
    for index, specialization in enumerate(spec.get("specializations", [])):
        owner = specialization.get("owner_skill") or {}
        add_entry("selected_skill", owner, source_path=f"specializations.{index}.owner_skill")
        for node in specialization.get("nodes", []):
            if node.get("points", 0) and node.get("points", 0) > 0:
                add_entry(
                    "skill_tree_node",
                    node.get("ref"),
                    source_path=node.get("source_path"),
                    identity_status=node.get("status"),
                    extra={"notes": f"points={node.get('points')}"},
                )
    for slot in spec.get("action_bar", {}).get("slots", []):
        add_entry("action_bar_skill", slot.get("ref"), source_path=slot.get("source_path"))
    for skill in spec.get("context", {}).get("active_buffs", {}).get("skills", []):
        add_entry("active_buff_skill", skill.get("ref"), source_path=skill.get("source_path"))
    for index, node in enumerate(spec.get("passive_tree", {}).get("nodes", [])):
        if node.get("points", 0) and node.get("points", 0) > 0:
            add_entry(
                "passive_node",
                node.get("ref"),
                source_path=node.get("source_path"),
                identity_status=node.get("status"),
                extra={"notes": f"points={node.get('points')}"},
            )

    def add_item_refs(instance: dict, source_path: str) -> None:
        definition = (instance.get("definition") or {}).get("ref")
        base = (instance.get("base") or {}).get("ref")
        if definition:
            add_entry("item_definition", definition, source_path=f"{source_path}.definition")
        if base:
            add_entry("item_base", base, source_path=f"{source_path}.base")
        for channel, affixes in (instance.get("affixes") or {}).items():
            for entry in affixes or []:
                add_entry(
                    "affix_instance",
                    (entry.get("affix") or {}).get("ref"),
                    source_path=entry.get("source_path"),
                    identity_status=(entry.get("affix") or {}).get("status"),
                    extra={
                        "notes": f"channel={channel}; tier={entry.get('source_tier')}; "
                        f"roll={((entry.get('roll') or {}).get('raw'))}; "
                        f"roll_status={((entry.get('roll') or {}).get('status'))}"
                    },
                )

    for slot in spec.get("equipment", {}).get("slots", []):
        add_item_refs(slot.get("instance") or {}, slot.get("instance", {}).get("source_path") or slot.get("slot"))
    for entry in spec.get("idols", {}).get("entries", []):
        add_item_refs(entry.get("instance") or {}, (entry.get("instance") or {}).get("source_path") or "")
    for entry in spec.get("blessings", {}).get("entries", []):
        add_item_refs(entry.get("instance") or {}, (entry.get("instance") or {}).get("source_path") or "")

    categories: dict = {}
    for entry in entries:
        category = entry["category"]
        stats = categories.setdefault(
            category,
            {
                "entries": 0,
                "identity_resolved": 0,
                "with_effects": 0,
                "with_values": 0,
                "values_linked": 0,
                "documented_but_not_normalized": 0,
                "source_unknown": 0,
                "calc_implemented": 0,
            },
        )
        stats["entries"] += 1
        if entry["identity_status"] == "resolved":
            stats["identity_resolved"] += 1
        if entry["effects"]["total"]:
            stats["with_effects"] += 1
        if entry["effect_values"]["values"]:
            stats["with_values"] += 1
        if entry["effect_values"].get("values_stat_linked"):
            stats["values_linked"] += 1
        linked = entry["effect_values"].get("values_stat_linked") or 0
        total_values = entry["effect_values"].get("values") or 0
        if total_values > linked:
            stats["documented_but_not_normalized"] += 1
        if not entry["effects"]["total"] and not total_values:
            stats["source_unknown"] += 1
    report = {
        "report_version": "calculation-gap-report-v1",
        "generated_at": _now(),
        "build_id": (spec.get("meta") or {}).get("build_id"),
        "revision_id": ((spec.get("meta") or {}).get("revision") or {}).get("revision_id"),
        "scope_note": (
            "按本 BD 实际引用的内容导出；计算状态一律 not_implemented，"
            "duration/层数/条件仅在目录确有证据时标记 known。第二阶段不实现战斗计算。"
        ),
        "categories": categories,
        "entries": entries,
        "summary": {
            "total_entries": len(entries),
            "identity_resolved": sum(1 for entry in entries if entry["identity_status"] == "resolved"),
            "calc_implemented": 0,
            "documented_but_not_normalized": sum(
                1
                for entry in entries
                if (entry["effect_values"].get("values") or 0)
                > (entry["effect_values"].get("values_stat_linked") or 0)
            ),
            "source_unknown": sum(
                1
                for entry in entries
                if not entry["effects"]["total"] and not entry["effect_values"].get("values")
            ),
        },
        "phase3_candidates": _phase3_candidates(spec),
        "limitations": [
            "effect_values.stat_id 尚未全库补齐：已链接属性可追溯，未链接的属性语义仍缺",
            "Buff 关系/参数规范化不足：已有 duration/stack 元数据不等于战斗计算可用",
            "编织者天赋树与附属容器按用户 2026-11-03 范围要求排除，不进入计算依赖清单",
            "网站 context/conditions 当前为空，不能据此推断 Buff 满层",
        ],
    }
    return report


def _phase3_candidates(spec: dict) -> list[dict]:
    candidates = []
    skill_names = set()
    for specialization in spec.get("specializations", []):
        owner = specialization.get("owner_skill") or {}
        if owner.get("name"):
            skill_names.add(owner["name"])
    for name in sorted(skill_names):
        candidates.append(
            {
                "topic": f"skill:{name}",
                "why": "selected specialization skill; begin with one skill/trigger chain, not the whole build",
            }
        )
    for slot in spec.get("equipment", {}).get("slots", []):
        instance = slot.get("instance") or {}
        definition_name = (((instance.get("definition") or {}).get("ref")) or {}).get("name")
        if definition_name in ("Withstand the Elements", "Unsated Rage"):
            candidates.append(
                {
                    "topic": f"unique:{definition_name}",
                    "why": "special affix/mechanic relationship worth investigating first",
                }
            )
    channels = _count(spec)["affix_channels"]
    if channels.get("sealed") or channels.get("primordial") or channels.get("corrupted"):
        candidates.append(
            {
                "topic": "special-affix-channels",
                "why": "sealed/primordial/corrupted affix mechanics are preserved but not implemented",
            }
        )
    return candidates


# ---------------------------------------------------------------------- markdown
def _fmt_pct(part: int, total: int) -> str:
    if not total:
        return "-"
    return f"{part}/{total} ({part * 100 // total}%)"


def _table(headers: list[str], rows: list[list]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join("" if value is None else str(value) for value in row) + " |")
    return "\n".join(lines)


def render_import_report_markdown(report: dict, spec: dict, gap_report: dict | None = None) -> str:
    counts = report.get("counts") or {}
    statuses = report.get("statuses") or {}
    coverage = report.get("coverage") or {}
    catalog = report.get("catalog") or {}
    lines: list[str] = []
    lines.append(f"# BD 导入报告：{(spec.get('meta') or {}).get('build_id')}")
    lines.append("")
    lines.append(f"- 生成时间：{report.get('generated_at')}")
    lines.append(f"- 分享链接：{(spec.get('meta') or {}).get('share_url')}")
    lines.append(f"- revision：`{((spec.get('meta') or {}).get('revision') or {}).get('revision_id')}`"
                 f"（seq {((spec.get('meta') or {}).get('revision') or {}).get('revision_seq')}）")
    lines.append(f"- 原始响应 sha256：`{((spec.get('meta') or {}).get('source_payload') or {}).get('raw_sha256')}`")
    lines.append(f"- 原始响应存档：`{((spec.get('meta') or {}).get('source_payload') or {}).get('raw_path')}`")
    lines.append("")
    lines.append("## 1. 分层状态")
    lines.append("")
    lines.append(_table(
        ["层", "状态"],
        [
            ["fetch", statuses.get("fetch")],
            ["schema", statuses.get("schema")],
            ["catalog_compatibility", statuses.get("catalog_compatibility")],
            ["identity_resolution", statuses.get("identity_resolution")],
            ["value_resolution", statuses.get("value_resolution")],
            ["text_readiness", statuses.get("text_readiness")],
            ["calculation_readiness", statuses.get("calculation_readiness")],
        ],
    ))
    lines.append("")
    lines.append("> 分层状态只表示各自范围；不允许用单一 READY 掩盖数值或机制未支持。")
    lines.append("")
    lines.append("## 2. 目录数据集绑定")
    lines.append("")
    lines.append(_table(
        ["字段", "值"],
        [
            ["dataset_id", catalog.get("dataset_id")],
            ["schema_version", catalog.get("schema_version")],
            ["target_game_version", catalog.get("target_game_version")],
            ["target_season", catalog.get("target_season")],
            ["source_snapshot_id", catalog.get("source_snapshot_id")],
            ["logical_content_sha256", catalog.get("logical_content_sha256")],
            ["file_sha256", catalog.get("file_sha256")],
            ["指纹校验", (catalog.get("verification") or {}).get("verified")],
        ],
    ))
    compatibility = catalog.get("compatibility") or {}
    lines.append("")
    lines.append(
        f"版本兼容：`{compatibility.get('status')}`；来源字段 "
        f"`{json_inline((compatibility.get('raw') or {}))}` → 规范化 "
        f"`{json_inline((compatibility.get('normalized') or {}))}`。"
    )
    if compatibility.get("warnings"):
        lines.append("")
        for warning in compatibility["warnings"]:
            lines.append(f"- 警告：{warning}")
    lines.append("")
    character = spec.get("character") or {}
    lines.append("## 3. 构筑摘要")
    lines.append("")
    lines.append(
        f"- 角色：{(character.get('class') or {}).get('ref', {}).get('name')} / "
        f"{(character.get('mastery') or {}).get('ref', {}).get('name')} / 等级 {character.get('level')}"
    )
    lines.append(
        f"- 物品实例：{counts.get('equipment_slots')} 装备 + {counts.get('idols')} 神像 + "
        f"{counts.get('blessings')} 祝福 = {counts.get('item_instances')}（编织者天赋树按范围排除）"
    )
    lines.append(
        f"- 专精树：{counts.get('specializations')} 棵，共 {counts.get('skill_tree_nodes')} 节点条目 / "
        f"{counts.get('skill_points')} 点；技能栏：{counts.get('action_bar')} 项（与专精列表独立）"
    )
    lines.append(
        f"- 被动：{counts.get('passive_nodes')} 条目 / {counts.get('passive_points')} 点；"
        f"节点条目合计：{counts.get('node_entries')}（19 被动 + 61 专精）"
    )
    lines.append(
        f"- 词缀实例：{counts.get('affix_instances')} 条，通道 "
        f"`{json_inline(counts.get('affix_channels'))}`"
    )
    lines.append(f"- 已完成任务：{counts.get('completed_quests')} 个（原始数值保留）")
    readable_counts = counts.get("readable_by_completeness") or {}
    lines.append(
        f"- 玩家可读词条：{counts.get('readable_items')} 个范围内物品，完整度分布 `{json_inline(readable_counts)}`"
        "（definition_only/partial 表示部分数值未提供，文本仍可见）"
    )
    lines.append("")
    lines.append("## 4. 覆盖率（分类分母）")
    lines.append("")
    cov_rows = []
    items_cov = coverage.get("item_instances") or {}
    cov_rows.append(["物品实例（结构关联）", _fmt_pct(items_cov.get("resolved", 0), items_cov.get("total", 0)),
                     f"identity_connected={items_cov.get('identity_connected')}"])
    affix_cov = coverage.get("affix_instances") or {}
    cov_rows.append(["词缀身份", _fmt_pct(affix_cov.get("resolved", 0), affix_cov.get("total", 0)), "affix → 定义"])
    cov_rows.append(["词缀 tier", _fmt_pct(affix_cov.get("tier_resolved", 0), affix_cov.get("total", 0)),
                     "source tier-1 → affix_tiers.tier_index"])
    rolls_cov = coverage.get("rolls") or {}
    cov_rows.append(["显式 roll", _fmt_pct(rolls_cov.get("present", 0), rolls_cov.get("denominator", 0)),
                     f"absent={rolls_cov.get('absent')} null={rolls_cov.get('null')}"])
    values_cov = coverage.get("values") or {}
    cov_rows.append(["数值恢复（词缀）", _fmt_pct(values_cov.get("resolved", 0), values_cov.get("denominator", 0)),
                     f"partial={values_cov.get('partial')} unresolved={values_cov.get('unresolved')}"])
    cov_rows.append(["implicit（有 ir 的物品）",
                     _fmt_pct(values_cov.get("implicit_items_with_ir", 0), values_cov.get("implicit_denominator", 0)),
                     "ir 为固定宽度数组；仅前 N 个对应基底 implicit，尾部保留但不使用"])
    cov_rows.append(["Unique roll（有 ur 的物品）",
                     _fmt_pct(values_cov.get("unique_items_with_ur", 0), values_cov.get("unique_denominator", 0)),
                     "ur 按 unique mod 的 rollId 索引（不是数组位置）"])
    nodes_cov = coverage.get("nodes") or {}
    cov_rows.append(["节点条目", _fmt_pct(nodes_cov.get("resolved", 0), nodes_cov.get("total", 0)),
                     f"passive={nodes_cov.get('passive')} skill_tree={nodes_cov.get('skill_tree')}"])
    readable_cov = coverage.get("readable") or {}
    readable_by_completeness = readable_cov.get("by_completeness") or {}
    readable_usable = sum(
        count for status, count in readable_by_completeness.items() if status in ("complete", "definition_only")
    )
    cov_rows.append(["玩家可读词条", _fmt_pct(readable_usable, readable_cov.get("items", 0)),
                     f"completeness={json_inline(readable_by_completeness)} "
                     f"lines_without_text={readable_cov.get('lines_without_text')}"])
    scope_cov = coverage.get("scope") or {}
    cov_rows.append(["导入范围", f"排除 {len(scope_cov.get('excluded_features') or [])} 项",
                     f"excluded={json_inline(scope_cov.get('excluded_features') or [])}"])
    lines.append(_table(["类别", "覆盖", "说明"], cov_rows))
    lines.append("")
    lines.append("## 5. 验收清单（2026-11-03 返工后范围与显示契约）")
    lines.append("")
    acceptance_rows = []
    for check in report.get("acceptance") or []:
        mark = "✅ pass" if check["status"] == "pass" else "❌ fail"
        acceptance_rows.append([check["label"], mark, json_inline(check.get("observed"))])
    lines.append(_table(["检查项", "结果", "观测"], acceptance_rows))
    failed = [check for check in report.get("acceptance") or [] if check["status"] != "pass"]
    lines.append("")
    lines.append(f"验收结论：{'全部通过' if not failed else f'{len(failed)} 项未通过'}。")
    lines.append("")
    lines.append("## 6. 缺失 roll（逐条可定位）")
    lines.append("")
    if report.get("absent_rolls"):
        lines.append(_table(
            ["来源路径", "通道", "编码 ID", "来源 tier", "词缀"],
            [
                [row["path"], row["channel"], row["encoded_id"], row["source_tier"], row["affix"]]
                for row in report["absent_rolls"]
            ],
        ))
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("## 7. 数值恢复与缺口")
    lines.append("")
    value_status = report.get("value_resolution") or {}
    lines.append(f"- value_resolution：`{value_status.get('status')}`")
    lines.append(f"- text_readiness：`{statuses.get('text_readiness')}`（文本可读 ≠ 战斗机制已实现）")
    if gap_report:
        summary = gap_report.get("summary") or {}
        lines.append(
            f"- 计算依赖条目：{summary.get('total_entries')}；身份已解析 {summary.get('identity_resolved')}；"
            f"计算已实现 {summary.get('calc_implemented')}"
        )
        lines.append(
            f"- 来源有资料但尚未规范化：{summary.get('documented_but_not_normalized')}；"
            f"来源资料本身未知：{summary.get('source_unknown')}"
        )
        lines.append("- 计算缺口详见 `calculation-gap-report.json`；第三阶段建议先做单个技能/触发链。")
    lines.append("")
    lines.append("## 8. 未解析 / 歧义 / 问题统计")
    lines.append("")
    issue_summary = report.get("issues") or {}
    lines.append(f"- 按严重度：`{json_inline(issue_summary.get('by_severity') or {})}`")
    lines.append(f"- 按代码：`{json_inline(issue_summary.get('by_code') or {})}`")
    lines.append("")
    severe = [
        issue for issue in (issue_summary.get("items") or [])
        if issue.get("severity") in ("error", "warning")
    ]
    if severe:
        lines.append(_table(
            ["严重度", "代码", "路径", "说明"],
            [[issue.get("severity"), issue.get("code"), issue.get("path"), issue.get("message")] for issue in severe[:40]],
        ))
        if len(severe) > 40:
            lines.append("")
            lines.append(f"（仅显示前 40 条，共 {len(severe)} 条；完整清单见 import-report.json）")
    else:
        lines.append("（无 error/warning 级问题）")
    lines.append("")
    lines.append("## 9. 导入范围与可读词条")
    lines.append("")
    scope = report.get("scope") or {}
    excluded = [entry.get("feature") for entry in scope.get("excluded_features") or []]
    lines.append(f"- 导入范围：`{scope.get('scope_version')}`；排除功能：{json_inline(excluded)}")
    for entry in scope.get("excluded_features") or []:
        lines.append(
            f"  - `{entry.get('feature')}`：{entry.get('reason')}；来源字段 "
            f"{json_inline(entry.get('source_fields'))}，原始响应仍归档（raw_preserved={entry.get('raw_preserved')}）"
        )
    readable = report.get("readable") or {}
    summary = readable.get("summary") or {}
    lines.append(
        f"- 可读词条：{summary.get('items')} 个物品，完整度 `{json_inline(summary.get('by_completeness') or {})}`，"
        f"行状态 `{json_inline(summary.get('line_value_status') or {})}`，缺文本行 {summary.get('lines_without_text')}"
    )
    incomplete = summary.get("items_not_complete") or []
    if incomplete:
        lines.append("")
        lines.append(_table(
            ["物品实例", "名称", "完整度", "说明"],
            [
                [row.get("instance_id"), row.get("display_name"), row.get("completeness"),
                 "；".join(row.get("warnings") or []) or "部分数值未提供，文本仍可见"]
                for row in incomplete
            ],
        ))
    lines.append("")
    lines.append("## 10. 复现命令")
    lines.append("")
    lines.append("```powershell")
    for label, command in (report.get("commands") or {}).items():
        lines.append(f"# {label}")
        lines.append(command)
    lines.append("```")
    lines.append("")
    lines.append("## 11. 边界声明")
    lines.append("")
    for note in report.get("notes") or []:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def json_inline(value) -> str:
    import json

    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except TypeError:  # pragma: no cover - defensive
        return str(value)


def render_gap_report_markdown(gap_report: dict) -> str:
    lines = [f"# 计算缺口报告：{gap_report.get('build_id')}", ""]
    lines.append(gap_report.get("scope_note") or "")
    lines.append("")
    summary = gap_report.get("summary") or {}
    lines.append(_table(
        ["指标", "值"],
        [
            ["计算依赖条目", summary.get("total_entries")],
            ["身份已解析", summary.get("identity_resolved")],
            ["计算已实现", summary.get("calc_implemented")],
            ["来源有资料但尚未规范化", summary.get("documented_but_not_normalized")],
            ["来源资料本身未知", summary.get("source_unknown")],
        ],
    ))
    lines.append("")
    lines.append("## 分类统计")
    lines.append("")
    categories = gap_report.get("categories") or {}
    rows = []
    for category, stats in sorted(categories.items()):
        rows.append([
            category,
            stats.get("entries"),
            stats.get("identity_resolved"),
            stats.get("with_effects"),
            stats.get("values_linked"),
            stats.get("documented_but_not_normalized"),
            stats.get("source_unknown"),
        ])
    lines.append(_table(
        ["类别", "条目", "身份已解析", "有 effect", "有已链接属性值", "资料待规范化", "资料未知"],
        rows,
    ))
    lines.append("")
    lines.append("## 第三阶段候选调查点")
    lines.append("")
    for candidate in gap_report.get("phase3_candidates") or []:
        lines.append(f"- `{candidate.get('topic')}`：{candidate.get('why')}")
    lines.append("")
    lines.append("## 限制")
    lines.append("")
    for limitation in gap_report.get("limitations") or []:
        lines.append(f"- {limitation}")
    lines.append("")
    return "\n".join(lines)
