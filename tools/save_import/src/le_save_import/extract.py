# -*- coding: utf-8 -*-
"""Extract NON-item build facts from a Last Epoch offline character save.

Save format: 5-byte header "EPOCH" + UTF-8 JSON (verified against the live save).
This script never writes the save; it only reads it and emits one structured JSON.

Evidence base:
- Game type/field metadata: IL2CPP dummy assembly
  <game-install>\\MelonLoader\\Dependencies\\
  Il2CppAssemblyGenerator\\Cpp2IL\\cpp2il_out\\LE.dll (decompiled with ilspycmd)
  and the recovered-IL dump that was produced at .scratch/cpp2il-isil/IsilDump/LE/
(that research workspace is archived outside the repository; see 作者本地迁移对照表（不随公开仓库分发）).
  Key types: LE.Data.CharacterData, LE.Data.SavedQuest, LE.Data.SavedSkillTree,
  LE.Data.SavedCharacterTree, LE.Data.SavedWeaverTree, ContainerID,
  CharacterClassID, QuestState, SavedQuestHelper, SpecialisedAbilityManager.
- Planner side (comparison only, in compare.py): LE Tools raw build response
  payloads archived by the caller; never part of the shipped tree.

Usage:
  python extract.py [--save PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
#: No author-local save path is shipped; callers must pass an explicit path.
DEFAULT_SAVE: Path | None = None
DEFAULT_OUT = Path(__file__).resolve().parent / "save-extract.json"
EPOCH_MAGIC = b"EPOCH"

# ---------------------------------------------------------------------------
# Game enums (LE.dll, Cpp2IL dummy assembly; values from enum member order)
# ---------------------------------------------------------------------------

CHARACTER_CLASS_ID = {
    0: "Primalist",
    1: "Mage",
    2: "Sentinel",
    3: "Acolyte",
    4: "Rogue",
}

# ContainerID enum, LE.dll (129 members, 0..128). Full mapping needed because the
# character save stores items in every system container, not only the equipped slots.
CONTAINER_ID = {
    0: "UNDEFINED",
    1: "INVENTORY",
    2: "EQ_HELMET",
    3: "EQ_ARMOR",
    4: "EQ_WEAPON",
    5: "EQ_OFFHAND",
    6: "EQ_GLOVES",
    7: "EQ_BELT",
    8: "EQ_BOOTS",
    9: "EQ_RIGHTRING",
    10: "EQ_LEFTRING",
    11: "EQ_AMULET",
    12: "EQ_RELIC",
    13: "MA_SAHTTERING",
    14: "MA_REFINEMENT",
    15: "MA_REMOVAL",
    16: "MA_CLEANSING",
    17: "MA_GUARDIAN",
    18: "MA_STABILITY",
    19: "CURSOR",
    20: "SWAP_BUFFER",
    21: "STASH",
    22: "SHOP",
    23: "CRAFTING_MAIN",
    24: "CRAFTING_MODIFIER",
    25: "CRAFTING_SUPPORT",
    26: "GAMBLING",
    27: "MA_SHAPING",
    28: "ARENA_KEY",
    29: "IDOLS",
    30: "LEGACY_EQUIPMENT_REDIRECT",
    31: "LEGACY_MATERIALS_REDIRECT",
    32: "SHOP_BUYBACK",
    33: "BLESSING_0",
    34: "BLESSING_1",
    35: "BLESSING_2",
    36: "BLESSING_3",
    37: "BLESSING_4",
    38: "BLESSING_5",
    39: "BLESSING_6",
    40: "BLESSING_OPTION_1",
    41: "BLESSING_OPTION_2",
    42: "BLESSING_OPTION_3",
    43: "BLESSING_7",
    44: "BLESSING_8",
    45: "BLESSING_9",
    46: "BLESSING_OPTION_4",
    47: "BLESSING_OPTION_5",
    48: "MA_ORDER",
    49: "MA_GREATER_HOPE",
    50: "MA_ASCENDANCE",
    51: "MA_ETERRA",
    52: "ETERNITY_CACHE_BEFORE_MAIN",
    53: "ETERNITY_CACHE_BEFORE_OTHER",
    54: "ETERNITY_CACHE_AFTER",
    55: "DUN1_KEY",
    56: "LIGHTLESS_ARBOR_DUNGEON_KEY",
    57: "SOULFIRE_BASTION_DUNGEON_KEY",
    58: "SOUL_GAMBLING",
    59: "GIFTED_ITEMS_LEGACY",
    60: "ITEM_TO_GIFT",
    61: "MA_RESEARCH_NOT_USED",
    62: "MA_INSIGHT_NOT_USED",
    63: "ITEM_TO_LIST",
    64: "ITEM_TO_TRADE_LEGACY_NOT_USED",
    65: "FACTION_GAMBLING_COF",
    66: "FACTION_GAMBLING_MG",
    67: "ITEM_TO_TRADE_PREVIEW_LEGACY_NOT_USED",
    68: "COF_LENS_1_ARCTUS",
    69: "COF_LENS_2_ARCTUS",
    70: "COF_LENS_3_ARCTUS",
    71: "COF_LENS_1_MESEMBRIA",
    72: "COF_LENS_2_MESEMBRIA",
    73: "COF_LENS_3_MESEMBRIA",
    74: "COF_LENS_1_EOS",
    75: "COF_LENS_2_EOS",
    76: "COF_LENS_3_EOS",
    77: "COF_LENS_1_DYSIS",
    78: "COF_LENS_2_DYSIS",
    79: "COF_LENS_3_DYSIS",
    80: "MA_ENVY",
    81: "HARBINGER_PINNACLE_KEY",
    82: "NEMESIS_0",
    83: "NEMESIS_1",
    84: "NEMESIS_2",
    85: "NEMESIS_3",
    86: "NEMESIS_SWAP",
    87: "BLESSING_OPTION_6",
    88: "BLESSING_OPTION_7",
    89: "BLESSING_OPTION_8",
    90: "BLESSING_OPTION_9",
    91: "WEAVER_TREE_ITEM_NODE_SMALL_1",
    92: "WEAVER_TREE_ITEM_NODE_SMALL_2",
    93: "WEAVER_TREE_ITEM_NODE_LARGE_1",
    94: "WEAVER_TREE_ITEM_NODE_LARGE_2",
    95: "WEAVER_TREE_ITEM_NODE_LARGE_3",
    96: "WEAVER_TREE_ITEM_NODE_LARGE_4",
    97: "ECHO_WEB_ISLAND",
    98: "WEAVER_VENDOR",
    99: "WOVEN_ECHOES",
    100: "KEYS",
    101: "GIFTED_ITEMS_TAB",
    102: "DUNGEON_MODIFIER",
    103: "CRAFTING_REROLL_UNIQUE_ITEM_TO_SACRIFICE",
    104: "CRAFTING_REROLL_UNIQUE_ITEM_TO_REROLL",
    105: "CRAFTING_SWAP_ATTRIBUTES",
    106: "CRAFTING_SHATTER_SET_ITEMS",
    107: "CRAFTING_RANDOMIZE_LEGENDARY_POTENTIAL",
    108: "MA_REDEMPTION",
    109: "MA_HAVOC",
    110: "MA_WEAVING",
    111: "DUN1_KEY_TRADER",
    112: "DUN2_KEY_TRADER",
    113: "DUN3_KEY_TRADER",
    114: "CRAFTING_EXCHANGE_WOVEN_ECHOES",
    115: "CRAFTING_GLYPH_OF_INSIGHT_REPLACEMENT",
    116: "CRAFTING_IDOL_ENCHANTMENT",
    117: "MA_EVOLUTION",
    118: "BLACK_MARKET_TOKEN",
    119: "MERCHANT_TOKEN",
    120: "HUNTER_VENDOR",
    121: "TIMEGLASS_VENDOR",
    122: "MA_CORRUPTION",
    123: "IDOLS_INVENTORY",
    124: "RAGEOFMORDITAS_0",
    125: "RAGEOFMORDITAS_1",
    126: "RAGEOFMORDITAS_2",
    127: "RAGEOFMORDITAS_3",
    128: "RAGEOFMORDITAS_4",
}

QUEST_STATE = {
    0: "NotStarted",
    1: "Active",
    2: "Completed",
    3: "Failed",
    4: "NoLongerRelevant",
}

TRACK_STATUS = {
    0: "AUTO_TRACKED",
    1: "AUTO_UNTRACKED",
    2: "FORCE_TRACKED",
    3: "FORCE_UNTRACKED",
}

TIMELINE_ID = {
    0: "None",
    1: "UndeadAbom",
    2: "OsprixWithLance",
    3: "VoidRahyeh",
    4: "FrostLich",
    5: "Lagon",
    6: "UndeadVsVoid",
    7: "Dragons",
    8: "Gaspar",
    9: "Heorot",
    10: "Volcano",
    99: "Activities",
}

# ContainerID value -> (functional category, planner build slot where determinable)
CONTAINER_ROLE = {
    2: ("equipment", "head"),
    3: ("equipment", "chest"),
    4: ("equipment", "weapon1"),
    5: ("equipment", "weapon2/offhand (absent in this save)"),
    6: ("equipment", "hands"),
    7: ("equipment", "waist"),
    8: ("equipment", "feet"),
    9: ("equipment", "ring1 or ring2 (undecoded)"),
    10: ("equipment", "ring2 or ring1 (undecoded)"),
    11: ("equipment", "amulet"),
    12: ("equipment", "relic"),
    29: ("idols", "idols[0..7] (slot positions in save grid)"),
    33: ("blessing", "blessings timeline 1 (BLESSING_0)"),
    34: ("blessing", "blessings timeline 2 (BLESSING_1)"),
    35: ("blessing", "blessings timeline 3 (BLESSING_2)"),
    36: ("blessing", "blessings timeline 4 (BLESSING_3)"),
    37: ("blessing", "blessings timeline 5 (BLESSING_4)"),
    38: ("blessing", "blessings timeline 6 (BLESSING_5)"),
    39: ("blessing", "blessings timeline 7 (BLESSING_6)"),
    43: ("blessing", "blessings timeline 8 (BLESSING_7)"),
    44: ("blessing", "blessings timeline 9 (BLESSING_8)"),
    45: ("blessing", "blessings timeline 10 (BLESSING_9)"),
    82: ("nemesis", "not part of equipped build (NEMESIS_0)"),
    83: ("nemesis", "not part of equipped build (NEMESIS_1)"),
    84: ("nemesis", "not part of equipped build (NEMESIS_2)"),
    85: ("nemesis", "not part of equipped build (NEMESIS_3)"),
    91: ("weaver_tree_item", "weaverTreeContainers 91"),
    92: ("weaver_tree_item", "weaverTreeContainers 92"),
    93: ("weaver_tree_item", "weaverTreeContainers 93"),
    94: ("weaver_tree_item", "weaverTreeContainers 94"),
    95: ("weaver_tree_item", "weaverTreeContainers 95"),
    123: ("idol_altar", "idol_altar (equipment.idol_altar)"),
    124: ("rage_of_morditas", "not part of equipped build (RAGEOFMORDITAS_0)"),
    125: ("rage_of_morditas", "not part of equipped build (RAGEOFMORDITAS_1)"),
    126: ("rage_of_morditas", "not part of equipped build (RAGEOFMORDITAS_2)"),
    127: ("rage_of_morditas", "not part of equipped build (RAGEOFMORDITAS_3)"),
    128: ("rage_of_morditas", "not part of equipped build (RAGEOFMORDITAS_4)"),
}

# Closed-tooltip / tutorial flags persisted in CharacterData (Json names in save).
TOOLTIP_FLAGS = [
    "closedPassivesTooltip",
    "closedSkillsTooltip",
    "closedMonolithTooltip",
    "closedIdolsTooltip",
    "closedTemporalShiftTooltip",
    "closedMinSkillLevelTutorial",
    "closedAddEquipmentTooltip",
    "closedForgingPotentialTooltip",
    "closedUpgradeAffixTooltip",
    "closedShatterEquipmentTooltip",
]

TIMELINE_COMPLETION_FIELDS = [
    "timelineCompletion",
    "timelineDifficultyCompletion",
    "timelineDifficultyUnlocks",
]

DUNGEON_NAMES = {0: "dungeonID 0 (name unresolved)", 1: "dungeonID 1", 2: "dungeonID 2"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_save(path: Path) -> tuple[dict, dict]:
    """Load an EPOCH+JSON save. Returns (parsed JSON object, source metadata)."""
    raw = path.read_bytes()
    if not raw.startswith(EPOCH_MAGIC):
        raise ValueError(f"{path} is not an EPOCH save (first bytes: {raw[:16]!r})")
    payload = raw[len(EPOCH_MAGIC) :]
    data = json.loads(payload.decode("utf-8"))
    meta = {
        "path": str(path),
        "size_bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "magic": EPOCH_MAGIC.decode("ascii"),
        "format": "5-byte ASCII 'EPOCH' + UTF-8 JSON",
        "payload_bytes": len(payload),
        "json_top_level_key_count": len(data),
    }
    return data, meta


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def build_identity(j: dict) -> dict:
    class_id = j.get("characterClass")
    chosen = j.get("chosenMastery")
    original = j.get("originalMastery")
    factions = []
    raw_factions = j.get("factions") or {}
    # faction names from archived LE Tools window.factionList (factionId -> nameKey)
    faction_names = {
        0: "Circle of Fortune",
        1: "Merchant's Guild",
        2: "Forgotten Knights",
        3: "The Weaver",
    }
    for key in sorted(raw_factions, key=lambda x: int(x)):
        f = raw_factions[key]
        factions.append(
            {
                "id": f.get("id"),
                "name": faction_names.get(f.get("id")),
                "isMember": f.get("isMember"),
                "hasEverJoined": f.get("hasEverJoined"),
                "rank": f.get("rank"),
                "reputation": f.get("reputation"),
                "favor": f.get("favor"),
                "factionSpecific": f.get("factionSpecific"),
            }
        )
    return {
        "characterName": j.get("characterName"),
        "level": j.get("level"),
        "currentExp": j.get("currentExp"),
        "class": {
            "id": class_id,
            "name": CHARACTER_CLASS_ID.get(class_id),
            "enum": "CharacterClassID (LE.dll): 0 Primalist, 1 Mage, 2 Sentinel, 3 Acolyte, 4 Rogue",
        },
        "chosenMastery": {
            "id": chosen,
            "name": "Sorcerer" if class_id == 1 and chosen == 1 else None,
            "note": (
                "mastery index is only meaningful inside its class namespace; "
                "Mage chosenMastery=1 -> Sorcerer per BuildSpec resolution "
                "(classInfoForSources[1].characterClass.masteries[1])"
            ),
        },
        "originalMastery": {
            "id": original,
            "name": None,
            "note": "CharacterData.get_OriginalMastery(): byte field; 0 in the Mage namespace would be the base class; exact meaning unresolved",
        },
        "hardcore": j.get("hardcore"),
        "died": j.get("died"),
        "deaths": j.get("deaths"),
        "masochist": j.get("masochist"),
        "respecs": j.get("respecs"),
        "uniquesPickedUp": j.get("uniquesPickedUp"),
        "soloChallenge": j.get("soloChallenge"),
        "soloCharacterChallenge": j.get("soloCharacterChallenge"),
        "portalUnlocked": j.get("portalUnlocked"),
        "reachedTown": j.get("reachedTown"),
        "clickedUnlockMasteriesButton": j.get("clickedUnlockMasteriesButton"),
        "competitiveCharacterVersion": j.get("competitiveCharacterVersion"),
        "cycle": j.get("cycle"),
        "lastPlayed": j.get("lastPlayed"),
        "totalPlaytimePlayed": j.get("totalPlaytimePlayed"),
        "factions": factions,
    }


def tree_to_map(node_ids: list, node_points: list) -> dict:
    """Ordered nodeID -> points map; raises if the two parallel lists differ in length."""
    if len(node_ids) != len(node_points):
        raise ValueError(f"nodeIDs/nodePoints length mismatch: {len(node_ids)} vs {len(node_points)}")
    return OrderedDict((str(nid), int(pts)) for nid, pts in zip(node_ids, node_points))


def build_character_tree(j: dict) -> dict:
    t = j.get("savedCharacterTree") or {}
    ids = t.get("nodeIDs") or []
    pts = t.get("nodePoints") or []
    node_map = tree_to_map(ids, pts)
    return {
        "treeID": t.get("treeID"),
        "version": t.get("version"),
        "unspentPoints": t.get("unspentPoints"),
        "nodesTaken": t.get("nodesTaken"),
        "node_count": len(ids),
        "points_total": sum(node_map.values()),
        "explicit_zero_node_count": sum(1 for v in node_map.values() if v == 0),
        "node_map": node_map,
        "fields_present": sorted(t.keys()),
    }


def xp_level_note() -> dict:
    return {
        "save_field": "SavedSkillTree.xp (int)",
        "game_rule": (
            "SpecialisedAbilityManager.getAbilityLevel(int xp) (recovered IL, .scratch/cpp2il-isil/IsilDump/LE/"
            "SpecialisedAbilityManager.txt): level starts at 1 and increments while xp >= baseXpThresholds[i]; "
            "baseXpThresholds is an int[19] static array for levels 2..20, so the base level from xp is capped at 20. "
            "SpecialisedAbilityManager.getAbilityXPLevelRequirement(level) indexes the same table at level-2."
        ),
        "level_20_xp": 5700000,
        "level_20_xp_evidence": (
            "Last Epoch save-editor reference implementation: SKILL_XP_MAX = 5700000 'skill experience at skill level 20' "
            "(external reference project, archived by the author); all five saved trees carry exactly "
            "this value, i.e. base skill level 20 for every specialized skill."
        ),
        "planner_rule": (
            "LE Tools planner runtime (archived planner-runtime.js, function rrlno) sets the "
            "exported skill level to 20 + count of applicable '+Level of Skills' stat instances; the BuildSpec raw "
            "skillTrees[].level is that computed value, NOT a value stored in the save."
        ),
        "effective_level": "base level (from xp, <=20) + bonuses from '+Level of Skills' modifiers granted by items/etc.",
        "unresolved": (
            "Save does not store the effective level; exact per-tree attribution of the planner's 24/26/28/38/29 "
            "requires decoding the character's item '+Level of Skills' modifiers with the planner tag-filter model. "
            "Item decoding is owned by another task."
        ),
    }


def build_skill_trees(j: dict) -> list[dict]:
    out = []
    for t in j.get("savedSkillTrees") or []:
        ids = t.get("nodeIDs") or []
        pts = t.get("nodePoints") or []
        node_map = tree_to_map(ids, pts)
        out.append(
            {
                "treeID": t.get("treeID"),
                "slotNumber": t.get("slotNumber"),
                "version": t.get("version"),
                "xp": t.get("xp"),
                "node_count": len(ids),
                "points_total": sum(node_map.values()),
                "node_map": node_map,
                "fields_present": sorted(t.keys()),
                "unspentPoints_defaulted": "unspentPoints" not in t,
                "nodesTaken_defaulted": "nodesTaken" not in t,
                "abilityXP_defaulted": "abilityXP" not in t,
            }
        )
    return out


def build_weaver_tree(j: dict) -> dict:
    t = j.get("savedWeaverTree") or {}
    ids = t.get("nodeIDs") or []
    pts = t.get("nodePoints") or []
    node_map = tree_to_map(ids, pts)
    return {
        "version": t.get("version"),
        "node_count": len(ids),
        "points_total": sum(node_map.values()),
        "node_map": node_map,
        "note": "phase-2 import excluded the weaver tree from BuildSpec scope (scope exclusions); raw planner payload still has data.weaverTree",
    }


def build_ability_bar(j: dict) -> dict:
    return {
        "abilityBar": j.get("abilityBar") or [],
        "moveButtonBehaviour": j.get("moveButtonBehaviour"),
        "werebearAbilityBar": j.get("werebearAbilityBar") or [],
        "sprigganFormAbilityBar": j.get("sprigganFormAbilityBar") or [],
        "swarmbladeAbilityBar": j.get("swarmbladeAbilityBar") or [],
        "note": "abilityBar order is independent from the specialization list; may contain unmastered abilities",
    }


def build_tooltips(j: dict) -> dict:
    return {flag: j.get(flag) for flag in TOOLTIP_FLAGS}


def classify_items(j: dict) -> dict:
    items = j.get("savedItems") or []
    by_container: dict[int, list] = {}
    entries = []
    for idx, it in enumerate(items):
        cid = it.get("containerID")
        by_container.setdefault(cid, []).append(idx)
        role = CONTAINER_ROLE.get(cid, ("unknown", None))
        pos = it.get("inventoryPosition") or {}
        entries.append(
            {
                "index": idx,
                "containerID": cid,
                "container_name": CONTAINER_ID.get(cid),
                "category": role[0],
                "planner_slot": role[1],
                "formatVersion": it.get("formatVersion"),
                "quantity": it.get("quantity"),
                "position": {"x": pos.get("x"), "y": pos.get("y")},
                "data_len": len(it.get("data") or []),
                "itemData_is_null": it.get("itemData") is None,
            }
        )
    containers = []
    for cid in sorted(by_container):
        role = CONTAINER_ROLE.get(cid, ("unknown", None))
        containers.append(
            {
                "containerID": cid,
                "container_name": CONTAINER_ID.get(cid),
                "category": role[0],
                "planner_slot": role[1],
                "count": len(by_container[cid]),
                "item_indices": by_container[cid],
            }
        )
    counts = Counter(c["category"] for c in containers for _ in range(c["count"]))
    # category-level item counts (Counters over containers -> recompute properly)
    cat_counts = Counter()
    for e in entries:
        cat_counts[e["category"]] += 1
    return {
        "total_saved_items": len(items),
        "by_category": dict(cat_counts),
        "containers": containers,
        "entries": entries,
        "note": (
            "classification only: containerID -> ContainerID enum -> functional slot. "
            "Item byte payloads are NOT decoded here (owned by the item-decoding task)."
        ),
    }


def build_blessings(j: dict, items: dict) -> dict:
    """Blessing representation.

    In the save, equipped blessings are ordinary savedItems in ContainerID BLESSING_0..BLESSING_9
    (33..39, 43..45). For each one we read only metadata: the encoded payload's base-type and
    base-id bytes, which the planner codec exposes as base ids (see BuildSpec blessings.*). That
    is classification, not full item decoding.
    """
    # data layout observed for blessing items (formatVersion 2): [6,0,0,127,255,34,<baseId>,...]
    # byte 5 = 34 (blessing base type id per planner codec item_type_index=34),
    # byte 6 = item base id, consistent with planner blessings payloads '1' + 034 + <baseId> + '000'.
    timeline_by_container = {33: 1, 34: 2, 35: 3, 36: 4, 37: 5, 38: 6, 39: 7, 43: 8, 44: 9, 45: 10}
    equipped = []
    for e in items["entries"]:
        if e["category"] != "blessing":
            continue
        cid = e["containerID"]
        data = (j["savedItems"][e["index"]].get("data")) or []
        base_type = data[5] if len(data) > 5 else None
        base_id = data[6] if len(data) > 6 else None
        equipped.append(
            {
                "containerID": cid,
                "container_name": CONTAINER_ID.get(cid),
                "timeline": timeline_by_container.get(cid),
                "base_type_id_byte5": base_type,
                "base_id_byte6": base_id,
                "item_index": e["index"],
            }
        )
    discovered = j.get("blessingsDiscovered") or []
    return {
        "blessingsDiscovered_count": len(discovered),
        "blessingsDiscovered_min": min(discovered) if discovered else None,
        "blessingsDiscovered_max": max(discovered) if discovered else None,
        "blessingsDiscovered_is_contiguous_range": bool(discovered) and discovered == list(range(min(discovered), max(discovered) + 1)),
        "openBlessings": j.get("openBlessings") or [],
        "equipped_blessing_items": equipped,
        "equipped_count": len(equipped),
        "encoding_note": (
            "10 active blessings = 10 savedItems in BLESSING_0..6 + BLESSING_7..9 containers; "
            "blessingsDiscovered is the global discovery list (224 ints here, contiguous 0..223); openBlessings empty. "
            "Container order 33,34,35,36,37,38,39,43,44,45 maps to planner blessings keys 1..10."
        ),
    }


def build_quests(j: dict) -> dict:
    qs = j.get("savedQuests") or []
    state_counts = Counter(q.get("state") for q in qs)
    branch_counts = Counter(q.get("questBranch") for q in qs)
    track_counts = Counter(q.get("trackStatus") for q in qs)
    ids = [q.get("questID") for q in qs]
    dup = len(ids) - len(set(ids))
    with_objectives = sum(1 for q in qs if q.get("completeObjectives"))
    return {
        "records_total": len(qs),
        "distinct_quest_ids": len(set(ids)),
        "duplicate_quest_ids": dup,
        "state_counts": {str(k): v for k, v in sorted(state_counts.items())},
        "state_enum": QUEST_STATE,
        "state_field_note": (
            "LE.Data.SavedQuest.State is QuestState (NotStarted=0, Active=1, Completed=2, Failed=3, "
            "NoLongerRelevant=4). In game code SavedQuestHelper.StatefulQuestListToSavedQuests (recovered IL) "
            "creates SavedQuest and fills QuestID, QuestStepID, QuestBranch, CompleteObjectives, FailedObjectives, "
            "NoLongerRelevantObjectives, ObjectiveProgress and trackStatus but NEVER assigns State, so the serialized "
            "'state' field is always the enum default 0 = NotStarted in every save. Completion must therefore be "
            "derived from the record's questStepID vs the quest's completed step, not from 'state'."
        ),
        "quest_branch_counts": {str(k): v for k, v in sorted(branch_counts.items())},
        "track_status_counts": {str(k): v for k, v in sorted(track_counts.items())},
        "track_status_enum": TRACK_STATUS,
        "records_with_nonempty_complete_objectives": with_objectives,
        "records_with_failed_objectives": sum(1 for q in qs if q.get("failedObjectives")),
        "records_with_no_longer_relevant_objectives": sum(1 for q in qs if q.get("nolongerRelevantObjectives")),
        "records_with_objective_progress": sum(1 for q in qs if q.get("objectiveProgress")),
        "quest_ids": ids,
        "quest_step_ids": [q.get("questStepID") for q in qs],
    }


def build_progression(j: dict) -> dict:
    timeline_completion = j.get("timelineCompletion") or []
    timeline_diff_completion = j.get("timelineDifficultyCompletion") or []
    timeline_diff_unlocks = j.get("timelineDifficultyUnlocks") or []
    runs = j.get("monolithRuns") or []
    timeline_summary = []
    for tc in timeline_completion:
        tid = tc.get("timelineID")
        timeline_summary.append(
            {
                "timelineID": tid,
                "timeline_name": TIMELINE_ID.get(tid),
                "progress": tc.get("progress"),
            }
        )
    run_summary = []
    for run in runs:
        run_summary.append(
            {
                "timelineID": run.get("timelineID"),
                "timeline_name": TIMELINE_ID.get(run.get("timelineID")),
                "difficultyIndex": run.get("difficultyIndex"),
                "depth": run.get("depth"),
                "stability": run.get("stability"),
                "questCompletion": run.get("questCompletion"),
                "questBranch": run.get("questBranch"),
                "bossLootDropped": run.get("bossLootDropped"),
                "previousEchoWasQuestEcho": run.get("previousEchoWasQuestEcho"),
            }
        )
    dungeon_completion = []
    for d in j.get("dungeonCompletion") or []:
        dungeon_completion.append(
            {
                "dungeonID": d.get("dungeonID"),
                "name": DUNGEON_NAMES.get(d.get("dungeonID")),
                "tiersCompleted": d.get("tiersCompleted"),
            }
        )
    return {
        "unlockedWaypointScenes_count": len(j.get("unlockedWaypointScenes") or []),
        "unlockedWaypointScenes_sample": (j.get("unlockedWaypointScenes") or [])[:12],
        "oneTimeEvents_count": len(j.get("oneTimeEvents") or []),
        "oneTimeEvents_sample": (j.get("oneTimeEvents") or [])[:12],
        "openedOneShotCaches_count": len(j.get("openedOneShotCaches") or []),
        "sceneProgresses_count": len(j.get("sceneProgresses") or []),
        "savedMonolithQuests_count": len(j.get("savedMonolithQuests") or []),
        "focusedQuest": j.get("focusedQuest"),
        "monolith": {
            "runs_count": len(runs),
            "runs": run_summary,
            "maxCorruption": j.get("maxCorruption"),
            "monolithEchoesConquered": j.get("monolithEchoesConquered"),
            "monolithTimelinesConquered": j.get("monolithTimelinesConquered"),
            "currentMonolithRunTimelineID": j.get("currentMonolithRunTimelineID"),
            "currentMonolithRunDifficultyIndex": j.get("currentMonolithRunDifficultyIndex"),
            "previousMonolithEchoTimelineID": j.get("previousMonolithEchoTimelineID"),
            "monolithDepth": j.get("monolithDepth"),
            "timelineCompletion": timeline_summary,
            "timelineDifficultyCompletion_count": len(timeline_diff_completion),
            "timelineDifficultyUnlocks_count": len(timeline_diff_unlocks),
        },
        "dungeons": dungeon_completion,
        "arena": {
            "maxWave": j.get("maxWave"),
            "currentArenaRunWaves": j.get("currentArenaRunWaves"),
            "previousArenaRunWaves": j.get("previousArenaRunWaves"),
            "arenaTiersCompleted": j.get("arenaTiersCompleted"),
        },
        "other": {
            "omensKilled": j.get("omensKilled"),
            "omenWindowsStarted": j.get("omenWindowsStarted"),
            "omenWindowsCompleted": j.get("omenWindowsCompleted"),
            "lanternLuminance": j.get("lanternLuminance"),
            "soulEmbers": j.get("soulEmbers"),
            "nemesisData": j.get("nemesisData"),
            "timeBeastData": j.get("timeBeastData"),
            "rageOfMorditasData_summary": {
                k: (j.get("rageOfMorditasData") or {}).get(k)
                for k in (
                    "angerLevel",
                    "storedKills",
                    "lifetimeKills",
                    "crystalsShattered",
                    "morditasKills",
                    "slotTypes",
                    "sacrificedItemTypes",
                )
            },
        },
    }


def build_anomalies(j: dict) -> dict:
    """Facts that bear on data quality / how the save was produced (evidence only)."""
    return {
        "editor_or_template_indicators": {
            "characterName": j.get("characterName"),
            "lastPlayed": j.get("lastPlayed"),
            "totalPlaytimePlayed": j.get("totalPlaytimePlayed"),
            "_ts": j.get("_ts"),
            "deaths": j.get("deaths"),
            "respecs": j.get("respecs"),
            "uniquesPickedUp": j.get("uniquesPickedUp"),
            "portalUnlocked": j.get("portalUnlocked"),
            "reachedTown": j.get("reachedTown"),
            "skill_tree_xp_values": [t.get("xp") for t in (j.get("savedSkillTrees") or [])],
        },
        "observation": (
            "characterName 'BlaCFEditor', lastPlayed 0001-01-01T00:00:00, _ts 0, totalPlaytimePlayed ~3.9 s, "
            "deaths/respecs/uniquesPickedUp all 0, portalUnlocked/reachedTown false while 385 waypoints and a "
            "campaign-complete quest set are present, and all five skill trees carry identical max xp 5700000. "
            "Together with the item-side evidence (instance id 0x00007FFF / maxed rolls, reported separately) this "
            "indicates the character file was produced or edited by a save editor rather than played. "
            "Tree/quest allocations are still internally consistent and usable as fixture data."
        ),
    }


def presence_report(j: dict) -> dict:
    present = sorted(j.keys())
    planner_only = [
        "fromSaveFile",
        "gh",
        "newGrid",
        "bio",
        "weaponTypes",
        "completedQuests",
        "charTreeProgression",
        "skillTreesProgression",
        "weaverTreeProgression",
        "conditions",
        "customConditions",
        "activeBuffs",
        "buffSettings",
        "dataVersion",
    ]
    return {
        "top_level_key_count": len(j),
        "save_top_level_keys": present,
        "planner_payload_fields_without_direct_save_counterpart": planner_only,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def extract(save_path: Path) -> dict:
    j, meta = load_save(save_path)
    items = classify_items(j)
    result = {
        "extract_version": "save-non-item-extract-v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": meta,
        "save_metadata": {
            "partitionKey": j.get("partitionKey"),
            "id": j.get("id"),
            "seqNo": j.get("seqNo"),
            "version": j.get("version"),
            "_etag": j.get("_etag"),
            "_ts": j.get("_ts"),
            "createdAt": j.get("createdAt"),
            "lastVisitedTownScene": j.get("lastVisitedTownScene"),
        },
        "identity": build_identity(j),
        "characterTree": build_character_tree(j),
        "skillLevelRule": xp_level_note(),
        "skillTrees": build_skill_trees(j),
        "weaverTree": build_weaver_tree(j),
        "abilityBar": build_ability_bar(j),
        "tooltipFlags": build_tooltips(j),
        "items": items,
        "blessings": build_blessings(j, items),
        "quests": build_quests(j),
        "progression": build_progression(j),
        "anomalies": build_anomalies(j),
        "fieldPresence": presence_report(j),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Extract non-item build facts from a Last Epoch EPOCH save.")
    ap.add_argument("--save", type=Path, required=True,
                    help="path to the character save file (e.g. <save-dir>/1CHARACTERSLOT_BETA_2)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output JSON path")
    args = ap.parse_args(argv)
    if not args.save.exists():
        print(f"save not found: {args.save}", file=sys.stderr)
        return 2
    result = extract(args.save)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {args.out} ({args.out.stat().st_size} bytes)")
    print(
        "summary:",
        json.dumps(
            {
                "level": result["identity"]["level"],
                "class": result["identity"]["class"]["name"],
                "chosenMastery": result["identity"]["chosenMastery"]["id"],
                "charTree": f"{result['characterTree']['node_count']} nodes / {result['characterTree']['points_total']} pts",
                "skillTrees": [
                    f"{t['treeID']}:{t['node_count']}n/{t['points_total']}p/xp{t['xp']}" for t in result["skillTrees"]
                ],
                "weaver": f"{result['weaverTree']['node_count']} nodes / {result['weaverTree']['points_total']} pts",
                "items": result["items"]["by_category"],
                "quests": result["quests"]["records_total"],
            },
            ensure_ascii=False,
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
