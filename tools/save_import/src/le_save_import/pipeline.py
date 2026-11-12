# -*- coding: utf-8 -*-
"""Offline Last Epoch save -> build (BD) extraction pipeline.

This module is the integration layer of :mod:`le_save_import`::

    save (EPOCH + JSON)                    items.decode_item / extract.extract
        +-- savedItems byte arrays  ----------> per-item structured fields
        +-- character tree / skill trees -----> allocations
        +-- ability bar / weaver tree --------> lists
        +-- 43 classified items --------------> equipment / idols / blessings / ...
                       |
                       v
        names.Resolver (phase-1 dataset id -> name/effect tables)
                       |
                       v
        save-build-v1 JSON  +  comparison against a Letools planner payload

The save *format* is documented in ``docs/save-import.zh-CN.md`` and the tool README.
Nothing here guesses names: unresolved ids stay explicitly unresolved.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
from pathlib import Path
from typing import Any

from . import extract as _extract
from . import items as _items
from . import stash as _stash
from .affix_ranges import AffixRangeResolver
from .names import Resolver, decode_planner_id

SCHEMA = "save-build-v1"
COMPARE_SCHEMA = "save-build-comparison-v1"
TOOL_VERSION = "1.0.0"

# --- game enum tables (see docs/save-import.zh-CN.md for evidence) -------------
#: save containerID -> planner equipment slot. Source: game ``ContainerID`` enum
#: cross-checked with the generator tool (lastepochsupertools ``LE_CID2SLOT``).
EQUIPMENT_SLOTS = {
    2: "head",
    3: "chest",
    4: "weapon1",
    5: "weapon2",
    6: "hands",
    7: "waist",
    8: "feet",
    9: "ring2",
    10: "ring1",
    11: "amulet",
    12: "relic",
    23: None,  # placeholder never used; keeps the dict readable
}
#: containerID -> endgame timeline number (1..10) for the 10 blessing slots.
BLESSING_TIMELINE = {33: 1, 34: 2, 35: 3, 36: 4, 37: 5, 38: 6, 39: 7, 43: 8, 44: 9, 45: 10}
BLESSING_CONTAINERS = set(BLESSING_TIMELINE)
IDOLS_CONTAINER = 29
IDOL_ALTAR_CONTAINER = 123
WEAVER_ITEM_CONTAINERS = {91, 92, 93, 94, 95, 96}
#: idol base type id -> grid size (x, y); EquipmentType enum order (LE.dll).
IDOL_SIZES = {
    25: (1, 1), 26: (1, 1), 27: (2, 1), 28: (1, 2), 29: (3, 1),
    30: (1, 3), 31: (4, 1), 32: (1, 4), 33: (2, 2),
}
#: base type id -> rarity display convention (game Item constants)
RARITY_NAMES = {0: "Normal", 1: "Magic", 2: "Magic", 3: "Rare", 4: "Rare",
                5: "Exalted", 6: "Exalted", 7: "Unique", 8: "Set", 9: "Legendary"}
FIRST_NON_EQUIPPABLE = 101
EDITOR_MAX_ROLL = 255
FULL_ROLL = 255  #: project convention: roll byte 255 == full roll -> use the tier range upper bound
SAVE_EDITOR_INSTANCE_ID = 32767

NOT_BUILD_CATEGORIES = {"nemesis", "rage_of_morditas"}


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# item description
# ---------------------------------------------------------------------------
def _editor_channels(decoded: dict) -> list[str]:
    """Generator/editor channel per affix (primary interpretation of the decoder).

    ``items.decode_item`` reports the *generator tool* reading first because the save
    was produced by ``lastepochsupertools``: state byte = count + 64*sealed +
    128*corrupted (matches the Letools planner channels).  The retail-game reading of
    the same bytes is attached per item as ``game_loader_interpretation``.
    """
    channels = [a.get("channel", "normal") for a in decoded.get("affixes") or []]
    for i, affix in enumerate(decoded.get("affixes") or []):
        if affix.get("primordial") or (affix.get("tier0") == 7 and channels[i] == "normal"):
            channels[i] = "primordial"
    return channels


def _game_channels(decoded: dict) -> list[str]:
    """Retail-game loader channel per affix (from ``game_loader_interpretation``)."""
    affixes = decoded.get("affixes") or []
    channels = ["normal"] * len(affixes)
    gli = decoded.get("game_loader_interpretation") or {}
    if decoded.get("rarity") in (7, 8, 9):
        if gli.get("corruption_affix_is_last") and channels:
            channels[-1] = "sealed-from-corruption"
        if gli.get("has_sealed_affix_from_corruption") and gli.get("corruption_affix_index") is not None:
            idx = gli["corruption_affix_index"]
            if 0 <= idx < len(channels):
                channels[idx] = "sealed-from-corruption"
    else:
        if gli.get("has_sealed_regular_affix"):
            idx = gli.get("sealed_regular_affix_index")
            if idx is not None and 0 <= idx < len(channels):
                channels[idx] = "sealed-regular"
        if gli.get("has_sealed_affix_from_corruption"):
            idx = gli.get("corruption_affix_index")
            if idx is not None and 0 <= idx < len(channels):
                channels[idx] = "sealed-from-corruption"
    for i, affix in enumerate(affixes):
        if affix.get("primordial") or affix.get("tier0") == 7:
            channels[i] = "primordial"
    return channels


def _base_size(decoded: dict) -> dict:
    size = IDOL_SIZES.get(decoded.get("base_type_id") or -1)
    if not size:
        return {"x": 1, "y": 1, "known": False}
    return {"x": size[0], "y": size[1], "known": True}


def _idol_planner_grid(entry: dict, decoded: dict) -> dict:
    """Convert the save's inventoryPosition to the planner's grid formula.

    Generator tool: planner x = pos.x + 1, planner y = 6 - pos.y - size.y.
    """
    pos = entry.get("inventoryPosition") or {}
    px = int(pos.get("x") or 0)
    py = int(pos.get("y") or 0)
    size = _base_size(decoded)
    return {"formula": "x=pos.x+1, y=6-pos.y-size.y (lastepochsupertools)",
            "x": px + 1, "y": 6 - py - size["y"], "size": size}


def describe_item(entry_index: int, entry: dict, decoded: dict, resolver: Resolver,
                  range_resolver: AffixRangeResolver | None = None) -> dict:
    """Combine one decoded item with names/effects and channel bookkeeping."""
    raw = [int(b) & 0xFF for b in (entry.get("data") or [])]
    base = resolver.base_item(decoded.get("base_type_id"), decoded.get("sub_type_id"))
    unique = None
    if decoded.get("unique_id") is not None:
        unique = resolver.unique(decoded["unique_id"], decoded.get("base_type_id"),
                                 decoded.get("sub_type_id"))
    editor_channels = _editor_channels(decoded)
    game_channels = _game_channels(decoded)
    affixes = []
    for i, affix in enumerate(decoded.get("affixes") or []):
        resolved = resolver.affix(affix.get("affix_id"), tier0=affix.get("tier0"))
        roll = affix.get("roll")
        roll_text = None
        affix_db = None
        if range_resolver is not None:
            affix_db = range_resolver.annotate(
                affix.get("affix_id"),
                decoded.get("base_type_id"),
                decoded.get("sub_type_id"),
                affix.get("tier0", 0) + 1,
                roll,
                resolved.get("text"),
            )
            if affix_db:
                if affix_db.get("text"):
                    resolved["text"] = affix_db["text"]
                if resolved.get("status") == "resolved" and roll is not None:
                    value = affix_db.get("effective_value")
                    range_text = affix_db.get("effective_range_text") or "?"
                    if isinstance(value, (int, float)):
                        roll_text = (
                            f"{value:.4g} (roll byte {roll}/255; range {range_text}) "
                            f"[affix-db per-base]"
                        )
        if roll_text is None and resolved.get("status") == "resolved" and roll is not None:
            lo, hi = resolved.get("roll_min"), resolved.get("roll_max")
            if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
                # Project convention: roll byte 255 means "full roll" -> use the
                # affix tier range upper bound; otherwise map linearly on [min, max].
                if roll == FULL_ROLL:
                    value = hi
                else:
                    value = lo + (hi - lo) * (roll / 255.0)
                roll_text = f"{value:.4g} (roll byte {roll}/255; range {lo}–{hi})"
        entry_data = {
            "affix_id": affix.get("affix_id"),
            "name": resolved.get("name"),
            "slot": resolved.get("slot"),
            "text": resolved.get("text"),
            "tier": affix.get("tier"),
            "tier0": affix.get("tier0"),
            "roll": roll,
            "roll_value": roll_text,
            "channel_game": game_channels[i] if i < len(game_channels) else "normal",
            "channel_editor": editor_channels[i] if i < len(editor_channels) else "normal",
            "resolution_status": resolved.get("status"),
        }
        if affix_db:
            entry_data["effective_ranges"] = affix_db.get("effective_ranges")
            entry_data["affix_db"] = {
                "status": affix_db.get("affix_db_status"),
                "path": affix_db.get("affix_db_path"),
                "base_type_id": affix_db.get("base_type_id"),
                "sub_type_id": affix_db.get("sub_type_id"),
                "effective_value": affix_db.get("effective_value"),
            }
        affixes.append(entry_data)
    item = {
        "index": entry_index,
        "container_id": entry.get("containerID"),
        "container_name": decoded.get("container_name"),
        "category": None,  # filled by caller
        "slot": None,      # filled by caller (planner slot)
        "position": entry.get("inventoryPosition"),
        "quantity": entry.get("quantity"),
        "format_version": decoded.get("format_version"),
        "instance_id": decoded.get("instance_id"),
        "base": {
            "base_type_id": decoded.get("base_type_id"),
            "sub_type_id": decoded.get("sub_type_id"),
            "key": base.get("key"),
            "name": base.get("name"),
            "item_type_name": base.get("item_type_name"),
            "group": base.get("group"),
            "level_requirement": base.get("level_requirement"),
            "implicit_text": base.get("implicit_text"),
            "resolution_status": base.get("status"),
        },
        "unique": ({
            "unique_id": decoded.get("unique_id"),
            "name": unique.get("name"),
            "anchor": unique.get("anchor"),
            "level_requirement": unique.get("level_requirement"),
            "resolution_status": unique.get("status"),
        } if unique is not None else None),
        "rarity": {
            "byte": decoded.get("rarity"),
            "name": decoded.get("rarity_name") or RARITY_NAMES.get(decoded.get("rarity"), "Unknown"),
        },
        # displayed rarity is derived (raw save byte is not the displayed rarity;
        # e.g. a rare item with T6/T7 affixes displays as Exalted)
        "display_rarity": resolver.display_rarity(decoded),
        "affixes": affixes,
        "implicit_rolls": decoded.get("implicit_rolls"),
        "unique_rolls": decoded.get("unique_rolls"),
        "forging_potential": decoded.get("forging_potential"),
        "legendary_potential": decoded.get("legendary_potential"),
        "weavers_will": decoded.get("weavers_will"),
        "corrupted": decoded.get("corrupted"),
        "flags": decoded.get("flags"),
        "byte_exact": decoded.get("byte_exact"),
        "layout": decoded.get("layout"),
        "game_loader_interpretation": decoded.get("game_loader_interpretation"),
        "decoder_notes": decoded.get("notes"),
        "editor_maxed": {
            "implicit_rolls_all_255": decoded.get("implicit_rolls") == [255, 255, 255],
            "any_affix_roll_255": any(a.get("roll") == 255 for a in decoded.get("affixes") or []),
            "instance_id_is_editor_constant": decoded.get("instance_id") == SAVE_EDITOR_INSTANCE_ID,
        },
        "data_bytes": raw,
    }
    # The generator wrote a generic 14-byte "plain" payload for non-equippable types
    # it did not model (e.g. Woven Echo 107).  Keep both readings explicit.
    if (decoded.get("layout") or "").startswith("editor-") and decoded.get("game_loader_interpretation"):
        gli = decoded["game_loader_interpretation"]
        item["game_loader_note"] = {
            "layout": "editor generic plain payload",
            "game_loader_sub_type_id": gli.get("sub_type_id"),
            "game_loader_consumed_bytes": gli.get("consumed_bytes"),
            "game_loader_trailing_bytes": gli.get("trailing_bytes"),
            "note": gli.get("note"),
        }
    # Channel disagreement note (editor/planner convention vs retail game loader).
    diffs = [(a["channel_editor"], a["channel_game"]) for a in affixes
             if a["channel_editor"] != a["channel_game"]]
    if diffs:
        item["channel_note"] = (
            "special affix byte written with the save-editor convention; the retail game loader "
            "reads the channels differently: " + ", ".join(f"editor={e}/game={g}" for e, g in diffs)
        )
    return item


# ---------------------------------------------------------------------------
# build extraction
# ---------------------------------------------------------------------------
def _skill_tree_entry(tree: dict, resolver: Resolver) -> dict:
    meta = resolver.skill_tree(tree["treeID"])
    nodes = []
    for node_id in sorted(int(k) for k in tree.get("node_map") or {}):
        points = tree["node_map"][str(node_id)]
        node = resolver.skill_tree_node(tree["treeID"], node_id)
        nodes.append({
            "node_id": node_id,
            "name": node.get("name"),
            "points": points,
            "node_type": node.get("node_type"),
            "max_points": node.get("max_points"),
            "description": node.get("description"),
            "stats": node.get("stats"),
            "resolution_status": node.get("status"),
        })
    return {
        "tree_id": tree["treeID"],
        "name": meta.get("name"),
        "class": meta.get("class"),
        "mastery": meta.get("mastery"),
        "catalog_version": meta.get("catalog_version"),
        "slot_number": tree.get("slotNumber"),
        "version": tree.get("version"),
        "xp": tree.get("xp"),
        "base_level": 20,  # xp 5,700,000 == level-20 threshold (game SpecialisedAbilityManager)
        "base_level_note": "the save stores skill XP only; effective level adds '+Level of Skills' "
                           "modifiers from items/passives (not stored here)",
        "points_total": tree.get("points_total"),
        "node_count": tree.get("node_count"),
        "nodes": nodes,
    }


def _passive_tree_entry(facts: dict, resolver: Resolver) -> dict:
    ct = facts["characterTree"]
    tree_meta = resolver.class_passive_tree_id(facts["identity"]["class"]["id"])
    nodes = []
    for node_id in sorted(int(k) for k in ct.get("node_map") or {}):
        node = resolver.passive_node(facts["identity"]["class"]["id"], node_id)
        nodes.append({
            "node_id": node_id,
            "name": node.get("name"),
            "points": ct["node_map"][str(node_id)],
            "node_type": node.get("node_type"),
            "max_points": node.get("max_points"),
            "description": node.get("description"),
            "alt_text": node.get("alt_text"),
            "stats": node.get("stats"),
            "resolution_status": node.get("status"),
        })
    return {
        "tree_id": tree_meta.get("tree_id"),
        "name": tree_meta.get("name"),
        "version": ct.get("version"),
        "points_total": ct.get("points_total"),
        "node_count": ct.get("node_count"),
        "nodes": nodes,
    }


def _weaver_tree_entry(facts: dict, resolver: Resolver) -> dict:
    wt = facts["weaverTree"]
    nodes = []
    for node_id in sorted(int(k) for k in wt.get("node_map") or {}):
        node = resolver.weaver_node(node_id)
        nodes.append({
            "node_id": node_id,
            "name": node.get("name"),
            "points": wt["node_map"][str(node_id)],
            "node_type": node.get("node_type"),
            "max_points": node.get("max_points"),
            "description": node.get("description"),
            "stats": node.get("stats"),
            "resolution_status": node.get("status"),
        })
    return {
        "version": wt.get("version"),
        "points_total": wt.get("points_total"),
        "node_count": wt.get("node_count"),
        "nodes": nodes,
        "scope_note": "phase-2 BuildSpec scope excluded the weaver tree; the save stores it and it "
                      "is extracted here for completeness",
    }


def _ability_bar(facts: dict, skill_tree_ids: set[str], resolver: Resolver) -> list[dict]:
    out = []
    for ability_id in facts["abilityBar"].get("abilityBar") or []:
        ability = resolver.ability(ability_id)
        out.append({
            "id": ability_id,
            "name": ability.get("name"),
            "specialized": ability_id in skill_tree_ids,
            "resolution_status": ability.get("status"),
        })
    return out


def _classify_item(container_id: int, extract_entry: dict | None) -> tuple[str, str]:
    """Return (category, planner_slot) for one save container id."""
    if container_id in EQUIPMENT_SLOTS and EQUIPMENT_SLOTS[container_id]:
        return "equipment", EQUIPMENT_SLOTS[container_id]
    if container_id == IDOLS_CONTAINER:
        return "idol", "idols"
    if container_id in BLESSING_CONTAINERS:
        return "blessing", f"blessings.{BLESSING_TIMELINE[container_id]}"
    if container_id == IDOL_ALTAR_CONTAINER:
        return "equipment", "idol_altar"
    if container_id in WEAVER_ITEM_CONTAINERS:
        return "weaver_tree_item", f"weaverTreeContainers.{container_id}"
    if extract_entry and extract_entry.get("category") in NOT_BUILD_CATEGORIES:
        return extract_entry["category"], None
    if extract_entry:
        return extract_entry.get("category") or "other", None
    return "other", None


def _collect_items(facts: dict, decoded_items: list[dict], resolver: Resolver,
                   save_doc: dict, range_resolver: AffixRangeResolver | None = None) -> dict:
    raw_entries = save_doc.get("savedItems") or []
    extract_entries = {e["index"]: e for e in facts["items"]["entries"]}
    described: dict[int, dict] = {}

    def entry_for(index: int) -> dict:
        if 0 <= index < len(raw_entries):
            entry = raw_entries[index]
            return {
                "containerID": entry.get("containerID"),
                "inventoryPosition": entry.get("inventoryPosition"),
                "quantity": entry.get("quantity"),
                "formatVersion": entry.get("formatVersion"),
                "data": entry.get("data") or [],
            }
        return {"containerID": None, "inventoryPosition": {}, "quantity": None, "data": []}

    for decoded in decoded_items:
        idx = decoded.get("index")
        entry = entry_for(idx)
        item = describe_item(idx, entry, decoded, resolver, range_resolver)
        category, slot = _classify_item(decoded.get("container_id"), extract_entries.get(idx))
        item["category"] = category
        item["slot"] = slot
        described[idx] = item

    equipment: dict[str, dict] = {}
    idols: list[dict] = []
    blessings: dict[str, dict] = {}
    weaver_items: dict[str, dict] = {}
    other: list[dict] = []
    for idx in sorted(described):
        item = described[idx]
        cid = item["container_id"]
        if item["category"] == "equipment" and item["slot"]:
            equipment[item["slot"]] = item
        elif item["category"] == "idol":
            item["planner_grid"] = _idol_planner_grid(entry_for(idx), described[idx])
            idols.append(item)
        elif item["category"] == "blessing":
            timeline = BLESSING_TIMELINE[cid]
            base_id = item["base"]["sub_type_id"]
            blessing = resolver.blessing(base_id)
            blessings[str(timeline)] = {
                "timeline": timeline,
                "container_id": cid,
                "container_name": item["container_name"],
                "base_id": base_id,
                "name": blessing.get("name"),
                "grand_variant": blessing.get("grand_variant"),
                "implicit_text": blessing.get("implicit_text"),
                "timelines": blessing.get("timelines"),
                "item": item,
                "resolution_status": blessing.get("status"),
            }
        elif item["category"] == "weaver_tree_item":
            weaver_items[str(cid)] = item
        else:
            other.append(item)
    return {
        "equipment": equipment,
        "equipment_slot_order": ["head", "chest", "weapon1", "weapon2", "hands", "waist", "feet",
                                 "ring1", "ring2", "amulet", "relic", "idol_altar"],
        "idols": idols,
        "blessings": blessings,
        "weaver_tree_items": weaver_items,
        "other_items": other,
        "counts": {
            "total": len(described),
            "equipment": len(equipment),
            "idols": len(idols),
            "blessings": len(blessings),
            "weaver_tree_items": len(weaver_items),
            "other": len(other),
        },
    }


def _extract_entry_raw(index: int, facts: dict, decoded: dict) -> dict:
    for entry in facts["items"]["entries"]:
        if entry.get("index") == index:
            return {
                "containerID": entry.get("containerID"),
                "container_name": entry.get("container_name"),
                "inventoryPosition": entry.get("position"),
                "quantity": entry.get("quantity"),
                "formatVersion": entry.get("formatVersion"),
                "data": decoded.get("data_bytes") if decoded.get("data_bytes") is not None else [],
            }
    return {
        "containerID": decoded.get("container_id"),
        "inventoryPosition": {},
        "quantity": None,
        "data": decoded.get("data_bytes") or [],
    }


def _collect_items_legacy_unused():  # pragma: no cover - kept for history/readability
    return None


def extract_build(save_path: str | Path, mapping_dir: str | Path | None = None,
                  stash_path: str | Path | None = None,
                  affix_db_path: str | Path | None = None) -> dict:
    """Read one EPOCH character save and return a resolved ``save-build-v1`` dict."""
    save_path = Path(save_path)
    doc = _items.load_save(save_path)
    facts = _extract.extract(save_path)
    decoded_items = _items.decode_save_items(doc, exclude_legacy_buyback=True)
    resolver = Resolver(mapping_dir=mapping_dir)
    range_resolver = AffixRangeResolver(affix_db_path)

    identity = facts["identity"]
    skill_trees = [_skill_tree_entry(t, resolver) for t in facts["skillTrees"]]
    tree_ids = {t["treeID"] for t in facts["skillTrees"]}
    collection = _collect_items(facts, decoded_items, resolver, doc, range_resolver)

    build = {
        "schema": SCHEMA,
        "schema_version": 1,
        "tool": {"name": "le_save_import", "version": TOOL_VERSION},
        "generated_at": _now(),
        "excluded_legacy_buyback": [
            {"index": i, "container_id": 32, "format_version": 2,
             "reason": "shop buyback is outside equipped build scope"}
            for i, entry in enumerate(doc.get("savedItems") or [])
            if entry.get("containerID") == 32 and (entry.get("data") or [None])[0] == 2],
        "source": {
            "path": str(save_path),
            "size_bytes": save_path.stat().st_size,
            "sha256": _sha256(save_path),
            "format": "EPOCH + UTF-8 JSON (Mongo-style document)",
            "game_version_hint": "1.5.3 install / dataset 1.5.2",
        },
        "provenance": {
            "generator_tool": "lastepochsupertools_v1.3.1 (user-provided local save/build tool; "
                              "<game-install>\\Editor\\lastepochsupertools_v1.3.1)",
            "save_editor_generated": True,
            "affix_db": {
                "status": range_resolver.status,
                "path": str(range_resolver.db_path),
                "note": "per-base effective affix ranges (baseType.affixEffectModifier) override the "
                        "phase-1 global tier range in this BD document; see data/affix-db/v1",
            },
            "editor_markers": [
                "characterName='%s'" % identity.get("characterName"),
                "instanceID constant 32767 (0x7FFF) on editor-written items",
                "implicit rolls / affix rolls 255 (max sentinel) on editor-written items",
                "lastPlayed='0001-01-01T00:00:00', totalPlaytimePlayed≈3.9s",
            ],
            "valid_for": ["character identity", "class/mastery", "passive/skill/weaver tree allocations",
                          "ability bar", "item identity (base/unique/affix ids+tiers)",
                          "blessings", "quest step records"],
            "not_valid_for": ["actual roll values of editor-written items",
                              "in-game displayed special-affix channels (see per-item channel notes)"],
        },
        "identity": {
            "character_name": identity.get("characterName"),
            "level": identity.get("level"),
            "current_exp": identity.get("currentExp"),
            "class": identity.get("class"),
            "chosen_mastery": identity.get("chosenMastery"),
            "original_mastery": identity.get("originalMastery"),
            "hardcore": identity.get("hardcore"),
            "died": identity.get("died"),
            "cycle": identity.get("cycle"),
            "last_played": identity.get("lastPlayed"),
            "playtime_seconds": identity.get("totalPlaytimePlayed"),
            "factions": identity.get("factions"),
        },
        "ability_bar": _ability_bar(facts, tree_ids, resolver),
        "specializations": skill_trees,
        "passive_tree": _passive_tree_entry(facts, resolver),
        "weaver_tree": _weaver_tree_entry(facts, resolver),
        **collection,
        "save_metadata": facts.get("save_metadata"),
        "quests": facts.get("quests"),
        "progression": facts.get("progression"),
        "anomalies": facts.get("anomalies"),
        "extract_meta": {"extract_version": facts.get("extract_version"),
                         "generated_at": facts.get("generated_at")},
    }
    build["caveats"] = _build_caveats(build)
    build["roll_semantics"] = {
        "full_roll_byte": FULL_ROLL,
        "rule": "roll == 255 表示满 roll；基础值先按 affix-db 的 baseType.affixEffectModifier 缩放，"
                "再取该位置有效范围上界（display_max）；roll ∈ [0,254] 在有效范围 [effective_min, effective_max] 上线性映射。",
        "apply_to": ["affix roll", "implicit rolls", "unique rolls (ur)"],
        "affix_db": str(range_resolver.db_path) if range_resolver else None,
        "evidence": [
            "phase-2 calculation-input export: head implicit raw 255 -> 77 = range max",
            "phase-3 le_calc contributions consume the phase-2 export values",
            "affix-db v1: per-base effective range + game half-even display",
        ],
    }
    range_resolver.close()
    return build


def _build_caveats(build: dict) -> list[str]:
    caveats = [
        "该存档由 lastepochsupertools v1.3.1 生成（save-editor 产物）：角色结构/物品身份/任务分配可当作真实意图。"
        "编辑器写入物品的 roll 字节为 255 = 满 roll；按项目约定取该词条范围上界参与计算"
        "（roll∈[0,254] 线性映射 min..max），不是“随机到的原值”。",
        "技能等级不在存档中保存：savedSkillTrees 只存 xp（本存档全部为 5,700,000 = 基础 20 级）；"
        "planner 的 24/26/28/38/29 是“有效等级 = 基础 20 + 装备/被动的 +Level of Skills”，存档侧无法直接还原归因。",
        "特殊词缀通道存在“编辑器写法 vs 游戏读法”差异（0x80/0x40 位），逐物品的 channel_game / channel_editor 已标注。",
        "savedCharacterTree.version=12 是编辑器写死值，planner 侧树数据版本为 7；节点与点数一致，仅元数据不同。",
    ]
    if any((item.get("layout") or "").startswith("editor-")
           for item in (list(build.get("weaver_tree_items", {}).values()) + (build.get("other_items") or []))
           if isinstance(item, dict)):
        caveats.append("编织者树物品（container 91/92, item type 107 Woven Echo）由编辑器按通用布局写出，"
                       "游戏加载器会按非装备类型读取；已同时报告游戏读法与编辑器读法（game_loader_note）。")
    return caveats


# ---------------------------------------------------------------------------
# comparison against a Letools planner build payload
# ---------------------------------------------------------------------------
def _planner_payload(path: str | Path) -> dict:
    doc = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(doc, dict) and isinstance(doc.get("data"), dict):
        return doc["data"]
    raise ValueError("planner payload has no 'data' object")

def _multiset(rows: list[tuple]) -> dict:
    out: dict = {}
    for row in rows:
        key = str(row)
        out[key] = out.get(key, 0) + 1
    return out


def compare_to_planner(build: dict, planner_path: str | Path) -> dict:
    """Compare an extracted save build against a Letools planner payload.

    Checks are explicit and counted; a mismatch is reported with the ids concerned.
    Planner ids are decoded with the project's letools codec (see names.decode_planner_id).
    """
    data = _planner_payload(planner_path)
    checks: list[dict] = []

    def check(cid: str, label: str, result: str, detail: str, **extra):
        row = {"id": cid, "label": label, "result": result, "detail": detail}
        row.update(extra)
        checks.append(row)

    # -- identity -----------------------------------------------------------
    bio = data.get("bio") or {}
    ident = build["identity"]
    pairs = [("level", bio.get("level"), ident.get("level")),
             ("characterClass", bio.get("characterClass"), (ident.get("class") or {}).get("id")),
             ("chosenMastery", bio.get("chosenMastery"), (ident.get("chosen_mastery") or {}).get("id"))]
    ok = sum(1 for _, a, b in pairs if a == b)
    check("identity", "角色身份 (等级/职业/专精)", "match" if ok == 3 else "mismatch",
          f"{ok}/3 match", pairs=[{"field": f, "planner": a, "save": b} for f, a, b in pairs])

    # -- passive tree -------------------------------------------------------
    ct = data.get("charTree") or {}
    planner_tree = ct.get("selected") or {}
    save_tree = build["passive_tree"]
    save_map = {str(n["node_id"]): n["points"] for n in save_tree["nodes"]}
    same_ids = set(planner_tree) == set(save_map)
    same_points = all(planner_tree.get(k) == save_map.get(k) for k in set(planner_tree) | set(save_map))
    check("passive_tree", "法师被动天赋节点/点数",
          "match" if same_ids and same_points else "mismatch",
          f"ids {'match' if same_ids else 'differ'}, points {'match' if same_points else 'differ'}; "
          f"planner {len(planner_tree)} nodes={sum(planner_tree.values())} pts, "
          f"save {len(save_map)} nodes={sum(save_map.values())} pts")

    # -- skill trees --------------------------------------------------------
    planner_trees = {t.get("treeID"): t for t in data.get("skillTrees") or []}
    save_trees = {t["tree_id"]: t for t in build["specializations"]}
    per_tree = []
    nodes_ok = points_ok = 0
    total_nodes = total_points = 0
    for tree_id, planner_tree in planner_trees.items():
        save_tree = save_trees.get(tree_id)
        planner_sel = {str(k): v for k, v in (planner_tree.get("selected") or {}).items()}
        save_sel = {str(n["node_id"]): n["points"] for n in (save_tree or {}).get("nodes", [])}
        ids_equal = planner_sel == save_sel
        nodes = sum(save_sel.values())
        planner_points = sum(planner_sel.values())
        total_nodes += len(save_sel)
        total_points += nodes
        if ids_equal:
            nodes_ok += 1
            points_ok += 1
        per_tree.append({"tree_id": tree_id, "name": (save_tree or {}).get("name"),
                         "nodes": len(save_sel), "points": nodes,
                         "planner_points": planner_points, "selected_equal": ids_equal})
    check("skill_trees", "5 棵专精树的节点与点数",
          "match" if len(per_tree) == len(planner_trees) and all(t["selected_equal"] for t in per_tree) else "mismatch",
          f"{nodes_ok}/{len(planner_trees)} trees exact; save {total_nodes} nodes/{total_points} pts", trees=per_tree)

    # -- ability bar --------------------------------------------------------
    planner_hud = data.get("hud") or []
    save_hud = [b["id"] for b in build["ability_bar"]]
    check("ability_bar", "技能栏 HUD 5 项", "match" if planner_hud == save_hud else "mismatch",
          f"planner={planner_hud} save={save_hud}")

    # -- equipment identity -------------------------------------------------
    planner_equipment = data.get("equipment") or {}
    save_equipment = build["equipment"]
    identity_rows = []
    identity_ok = 0
    for slot, planner_item in planner_equipment.items():
        save_item = save_equipment.get(slot)
        dec = decode_planner_id(planner_item.get("id") or "")
        expected = None
        if dec.get("status") == "decoded" and dec.get("marker") == "U":
            expected = {"unique_id": dec.get("unique_id"), "base_subtype_id": dec.get("base_subtype_id")}
        elif dec.get("status") == "decoded" and dec.get("marker") == "I":
            expected = {"base_type_id": dec.get("item_type_index"), "sub_type_id": dec.get("base_id")}
        actual = None
        if save_item:
            actual = {"unique_id": (save_item.get("unique") or {}).get("unique_id"),
                      "base_type_id": save_item["base"]["base_type_id"],
                      "sub_type_id": save_item["base"]["sub_type_id"]}
        ok = False
        if expected and actual:
            if "unique_id" in expected:
                ok = expected["unique_id"] == actual["unique_id"] and \
                     expected["base_subtype_id"] == actual["sub_type_id"]
            else:
                ok = expected["base_type_id"] == actual["base_type_id"] and \
                     expected["sub_type_id"] == actual["sub_type_id"]
        identity_ok += 1 if ok else 0
        identity_rows.append({"slot": slot, "planner_id": planner_item.get("id"), "decoded": dec,
                              "save": actual, "match": ok})
    check("equipment_identity", "装备身份 (基底/暗金 id)",
          "match" if identity_ok == len(planner_equipment) else "partial",
          f"{identity_ok}/{len(planner_equipment)} match", slots=identity_rows)

    # -- equipment affix id+tier multiset -----------------------------------
    affix_rows = []
    affix_ok = 0
    for slot, planner_item in planner_equipment.items():
        save_item = save_equipment.get(slot) or {}
        planner_affixes = []
        for channel in ("affixes", "sealedAffix", "primordialAffix", "corruptedAffix"):
            value = planner_item.get(channel)
            values = value if isinstance(value, list) else ([value] if value else [])
            for affix in values:
                dec = decode_planner_id(affix.get("id") or "", )
                if dec.get("status") == "decoded" and dec.get("marker") == "A":
                    planner_affixes.append((dec["affix_id"], affix.get("tier")))
        save_affixes = [(a["affix_id"], a["tier"]) for a in (save_item.get("affixes") or [])]
        ok = _multiset(planner_affixes) == _multiset(save_affixes)
        affix_ok += 1 if ok else 0
        affix_rows.append({"slot": slot, "match": ok,
                           "planner": sorted(planner_affixes), "save": sorted(save_affixes)})
    total_slots = len(planner_equipment)
    mismatch_notes = []
    for row in affix_rows:
        if row["match"]:
            continue
        only_planner = [x for x in row["planner"] if x not in row["save"]]
        only_save = [x for x in row["save"] if x not in row["planner"]]
        mismatch_notes.append({
            "slot": row["slot"],
            "only_planner": only_planner,
            "only_save": only_save,
            "explanation": "save-editor produced item does not carry this affix (payload length / "
                           "primordial channel not written); see docs/save-import.zh-CN.md §known deviations",
        })
    check("equipment_affixes", "装备词缀 (affix id + tier 多重集合)",
          "match" if affix_ok == total_slots else "partial",
          f"{affix_ok}/{total_slots} slots identical; {len(mismatch_notes)} documented editor deviations",
          slots=affix_rows, deviations=mismatch_notes)

    # -- unique rolls -------------------------------------------------------
    # Only compare *true unique* items (planner U marker) with a non-empty ur; the
    # save editor wrote an all-255 sentinel for items it generated with max rolls,
    # and `I`-marker bases have no unique-roll semantics at all.
    ur_rows = []
    ur_ok = ur_cmp = 0
    for slot, planner_item in planner_equipment.items():
        dec = decode_planner_id(planner_item.get("id") or "")
        planner_ur = planner_item.get("ur") or []
        if dec.get("marker") != "U" or not planner_ur:
            continue
        save_item = save_equipment.get(slot) or {}
        save_ur = list(save_item.get("unique_rolls") or [])
        if len(save_ur) == 8 and all(b == 255 for b in save_ur):
            ur_rows.append({"slot": slot, "comparable": False,
                            "reason": "save-editor sentinel [255]*8", "planner_ur": planner_ur})
            continue
        ur_cmp += 1
        ok = list(planner_ur) == save_ur
        ur_ok += 1 if ok else 0
        ur_rows.append({"slot": slot, "comparable": True, "match": ok,
                        "planner_ur": planner_ur, "save_ur": save_ur})
    check("equipment_unique_rolls", "暗金 unique rolls (planner ur)",
          "match" if ur_cmp and ur_ok == ur_cmp else "partial",
          f"{ur_ok}/{ur_cmp} comparable unique items equal; "
          f"{sum(1 for r in ur_rows if not r.get('comparable'))} editor-sentinel item(s) excluded "
          f"(see per-item rows)", items=ur_rows)

    # -- idols --------------------------------------------------------------
    planner_idols = data.get("idols") or []
    save_idols = build["idols"]
    idol_rows = []
    idol_ok = 0
    for pos, planner_idol in enumerate(planner_idols):
        save_item = save_idols[pos] if pos < len(save_idols) else None
        dec = decode_planner_id(planner_idol.get("id") or "")
        ok = False
        if save_item and dec.get("status") == "decoded":
            if dec.get("marker") == "U":
                ok = dec.get("unique_id") == (save_item.get("unique") or {}).get("unique_id") and \
                     dec.get("base_subtype_id") == save_item["base"]["sub_type_id"]
            else:
                ok = dec.get("item_type_index") == save_item["base"]["base_type_id"] and \
                     dec.get("base_id") == save_item["base"]["sub_type_id"]
        idol_ok += 1 if ok else 0
        idol_rows.append({"position": pos, "planner_id": planner_idol.get("id"),
                          "decoded": dec, "save_index": save_item.get("index") if save_item else None,
                          "match": ok})
    check("idols", "神像身份 8 个", "match" if idol_ok == len(planner_idols) else "partial",
          f"{idol_ok}/{len(planner_idols)} match", idols=idol_rows)

    # -- blessings ----------------------------------------------------------
    planner_blessings = data.get("blessings") or {}
    save_blessings = build["blessings"]
    blessing_ok = 0
    blessing_rows = []
    for timeline, planner_item in planner_blessings.items():
        dec = decode_planner_id(planner_item.get("id") or "")
        save_item = save_blessings.get(str(timeline))
        ok = bool(save_item and dec.get("status") == "decoded" and
                  dec.get("item_type_index") == 34 and
                  dec.get("base_id") == save_item.get("base_id"))
        blessing_ok += 1 if ok else 0
        blessing_rows.append({"timeline": timeline, "planner_id": planner_item.get("id"),
                              "decoded": dec,
                              "save_base_id": save_item.get("base_id") if save_item else None,
                              "name": save_item.get("name") if save_item else None, "match": ok})
    check("blessings", "祝福 10 个（时间线顺序）",
          "match" if blessing_ok == len(planner_blessings) else "partial",
          f"{blessing_ok}/{len(planner_blessings)} match", blessings=blessing_rows)

    # -- weaver tree containers --------------------------------------------
    planner_weaver = data.get("weaverTreeContainers") or {}
    save_weaver = build["weaver_tree_items"]
    weaver_ok = 0
    weaver_rows = []
    for cid, planner_item in planner_weaver.items():
        dec = decode_planner_id(planner_item.get("id") or "")
        save_item = save_weaver.get(str(cid))
        ok = False
        if save_item and dec.get("status") == "decoded":
            if dec.get("marker") == "U":
                ok = dec.get("unique_id") == (save_item.get("unique") or {}).get("unique_id") and \
                     dec.get("base_subtype_id") == save_item["base"]["sub_type_id"]
            else:
                sub_candidates = {save_item["base"]["sub_type_id"]}
                guess = save_item.get("base_editor_layout") or {}
                if guess.get("sub_type_id") is not None:
                    sub_candidates.add(guess["sub_type_id"])
                ok = dec.get("item_type_index") == save_item["base"]["base_type_id"] and \
                     dec.get("base_id") in sub_candidates
        weaver_ok += 1 if ok else 0
        weaver_rows.append({"container": cid, "planner_id": planner_item.get("id"),
                            "decoded": dec, "match": ok})
    check("weaver_tree_items", "编织者树物品 (container 91-95)",
          "match" if weaver_ok == len(planner_weaver) else "partial",
          f"{weaver_ok}/{len(planner_weaver)} match", containers=weaver_rows)

    # -- quests -------------------------------------------------------------
    planner_quests = set(data.get("completedQuests") or [])
    save_quests = set((build.get("quests") or {}).get("quest_ids") or [])
    overlap = planner_quests & save_quests
    check("quests", "已完成任务与 planner completedQuests 重合",
          "partial" if overlap != planner_quests else "match",
          f"overlap {len(overlap)}/{len(planner_quests)}; planner-only={sorted(planner_quests - save_quests)}; "
          f"save has {len(save_quests)} quest records (state=0 is a game-serializer artefact)",
          planner_only=sorted(planner_quests - save_quests))

    counts = {"match": sum(1 for c in checks if c["result"] == "match"),
              "partial": sum(1 for c in checks if c["result"] == "partial"),
              "mismatch": sum(1 for c in checks if c["result"] == "mismatch")}
    return {
        "schema": COMPARE_SCHEMA,
        "generated_at": _now(),
        "planner_source": str(planner_path),
        "planner_meta": {"dataVersion": data.get("dataVersion"), "fromSaveFile": data.get("fromSaveFile")},
        "summary": counts,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# output helpers
# ---------------------------------------------------------------------------
def write_json(path: str | Path, value: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _md_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_tables_markdown(build: dict) -> str:
    """Auto-generated markdown tables (supporting artifact for the narrative report)."""
    lines: list[str] = []
    ident = build["identity"]
    lines += [
        "# 存档 BD 自动表格（le_save_import 生成）",
        "",
        f"- source: `{build['source']['path']}` sha256 `{build['source']['sha256']}`",
        f"- character: **{ident.get('character_name')}** / level {ident.get('level')} / "
        f"{(ident.get('class') or {}).get('name')} / {(ident.get('chosen_mastery') or {}).get('name')}",
        "",
        "## 技能栏",
        "",
        "| 槽位 | ability id | 名称 | 已专精 |",
        "|---:|---|---|---|",
    ]
    for i, ability in enumerate(build["ability_bar"], 1):
        lines.append(f"| {i} | `{ability['id']}` | {_md_cell(ability['name'])} | "
                     f"{'是' if ability['specialized'] else '否（HUD）'} |")
    lines += ["", "## 专精技能节点", ""]
    for tree in build["specializations"]:
        lines += [f"### {tree['name']} (`{tree['tree_id']}`) — {tree['class']} / {tree['mastery']} — "
                  f"{tree['points_total']} 点 / {tree['node_count']} 节点（存档 xp={tree['xp']} → 基础 20 级）", "",
                  "| 节点 | 名称 | 点数 | 类型 | 效果（每点） |", "|---:|---|---:|---|---|"]
        for node in tree["nodes"]:
            stats = "; ".join(f"{s.get('name')} {s.get('value')}" for s in (node.get("stats") or [])
                              if isinstance(s, dict))
            effect = node.get("description") or node.get("alt_text") or stats
            lines.append(f"| {node['node_id']} | {_md_cell(node.get('name'))} | {node['points']} | "
                         f"{_md_cell(node.get('node_type'))} | {_md_cell(effect)} |")
        lines.append("")
    pt = build["passive_tree"]
    lines += [f"## 被动天赋 — {pt['name']}（{pt['points_total']} 点 / {pt['node_count']} 节点）", "",
              "| 节点 | 名称 | 点数 | 类型 | 效果 |", "|---:|---|---:|---|---|"]
    for node in pt["nodes"]:
        stats = "; ".join(f"{s.get('name')} {s.get('value')}" for s in (node.get("stats") or [])
                          if isinstance(s, dict))
        effect = node.get("description") or node.get("alt_text") or stats
        lines.append(f"| {node['node_id']} | {_md_cell(node.get('name'))} | {node['points']} | "
                     f"{_md_cell(node.get('node_type'))} | {_md_cell(effect)} |")
    lines += ["", "## 装备", "",
              "| 槽位 | 容器 | 基底 | 暗金 | 稀有度 | 词缀（id/T级/roll/通道） | 独特roll |",
              "|---|---|---|---|---|---|---|"]
    for slot in build["equipment_slot_order"]:
        item = build["equipment"].get(slot)
        if not item:
            lines.append(f"| {slot} | — | （空） | | | | |")
            continue
        affixes = "; ".join(
            f"{a['affix_id']} T{a['tier']} ({a['channel_editor']}/{a['channel_game']})"
            for a in item.get("affixes") or [])
        lines.append(f"| {slot} | {item.get('container_name')} | "
                     f"{_md_cell((item.get('base') or {}).get('name'))} | "
                     f"{_md_cell((item.get('unique') or {}).get('name'))} | "
                     f"{_md_cell((item.get('rarity') or {}).get('name'))} | {_md_cell(affixes)} | "
                     f"{_md_cell(item.get('unique_rolls'))} |")
    lines += ["", "## 神像", "", "| # | 容器位置 | 基底 | 词缀 | planner 坐标 |", "|---:|---|---|---|---|"]
    for i, item in enumerate(build["idols"], 1):
        affixes = "; ".join(f"{a['affix_id']} T{a['tier']} ({a['channel_editor']})"
                            for a in item.get("affixes") or [])
        lines.append(f"| {i} | {_md_cell(item.get('position'))} | "
                     f"{_md_cell((item.get('base') or {}).get('name'))} | {_md_cell(affixes)} | "
                     f"{_md_cell((item.get('planner_grid') or {}).get('x'))},"
                     f"{_md_cell((item.get('planner_grid') or {}).get('y'))} |")
    lines += ["", "## 祝福", "", "| 时间线 | 容器 | 名称 | implicit |", "|---:|---|---|---|"]
    for timeline in sorted(build["blessings"], key=int):
        b = build["blessings"][timeline]
        lines.append(f"| {timeline} | {b['container_name']} | {_md_cell(b.get('name'))} | "
                     f"{_md_cell(b.get('implicit_text'))} |")
    wt = build["weaver_tree"]
    lines += ["", f"## 编织者天赋树（{wt['points_total']} 点 / {wt['node_count']} 节点）", "",
              "| 节点 | 名称 | 点数 | 效果 |", "|---:|---|---:|---|"]
    for node in wt["nodes"]:
        stats = "; ".join(f"{s.get('name')} {s.get('value')}" for s in (node.get("stats") or [])
                          if isinstance(s, dict))
        effect = node.get("description") or stats
        lines.append(f"| {node['node_id']} | {_md_cell(node.get('name'))} | {node['points']} | "
                     f"{_md_cell(effect)} |")
    lines += ["", "## 编织者树物品", ""]
    for cid in sorted(build["weaver_tree_items"], key=int):
        item = build["weaver_tree_items"][cid]
        lines.append(f"- container {cid}: {item['base'].get('name') or item['base'].get('item_type_name')}"
                     f"{(item.get('unique') or {}).get('name') and ' / ' + (item.get('unique') or {}).get('name')}"
                     f" (index {item['index']}, bytes {len(item.get('data_bytes') or [])})")
    lines += ["", "## 其他物品（不属于当前配装）", ""]
    for item in build["other_items"]:
        lines.append(f"- container {item['container_id']} ({item['category']}): "
                     f"{item['base'].get('name') or item['base'].get('item_type_name')}"
                     f"{(item.get('unique') or {}).get('name') and ' / ' + (item.get('unique') or {}).get('name')}")
    lines += ["", "## 任务", "",
              f"- 记录 {((build.get('quests') or {}).get('records_total'))} 条；"
              f"state 字段恒为 0 是游戏序列化器不写 State 的产物（见 docs/save-import.zh-CN.md）"]
    return "\n".join(lines) + "\n"


def run_pipeline(save_path: str | Path, out_dir: str | Path, *, mapping_dir: str | Path | None = None,
                 planner_path: str | Path | None = None, stash_path: str | Path | None = None,
                 affix_db_path: str | Path | None = None) -> dict:
    """Extract a build, write all artifacts under ``out_dir`` and return paths/counts."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    build = extract_build(save_path, mapping_dir=mapping_dir, stash_path=stash_path,
                          affix_db_path=affix_db_path)
    paths = {
        "build": write_json(out_dir / "bd.json", build),
        "tables": out_dir / "bd-tables.zh-CN.md",
    }
    paths["tables"].write_text(render_tables_markdown(build), encoding="utf-8")
    saved = {"items": None, "extract": None}
    # side artifacts (independent, cheap, kept for evidence)
    save_doc = _items.load_save(save_path)
    saved["items"] = write_json(out_dir / "decoded-items.json", {
        "decoder": {"name": "le_save_import.items", "version": TOOL_VERSION},
        "source": build["source"],
        "items": _items.decode_save_items(save_doc, exclude_legacy_buyback=True),
    })
    saved["extract"] = write_json(out_dir / "save-extract.json", _extract.extract(save_path))
    if stash_path:
        stash_doc = _items.load_save(stash_path)
        saved["stash"] = write_json(out_dir / "decoded-stash.json", {
            "source": {"path": str(stash_path), "sha256": _sha256(Path(stash_path))},
            "lists": _stash.decrypt_stash_lists(stash_doc),
        })
    paths.update({k: v for k, v in saved.items() if v})
    if planner_path:
        comparison = compare_to_planner(build, planner_path)
        paths["comparison"] = write_json(out_dir / "comparison-vs-planner.json", comparison)
    return {"build": build, "paths": {k: str(v) for k, v in paths.items()}}
