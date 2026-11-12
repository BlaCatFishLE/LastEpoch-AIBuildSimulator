# -*- coding: utf-8 -*-
"""Build deterministic Last Epoch id -> human-readable name/effect mappings.

Inputs (all read-only):
  * phase-1 dataset (sqlite)   data/current.json -> data/releases/<id>/data.sqlite
  * LE.dll enum metadata        source-data/enums/*.txt   (decompiled with ilspycmd)
  * lead-verified ContainerID   source-data/enums/container-id-map.json
  * szefo94 save-editor tables  source-data/szefo94/{bases,uniques,affixes}.json
                                (https://github.com/szefo94/last-epoch-save-editor, MIT)
  * the user save pretty copies (LE_SAVE_RECON_DIR, default: the 2026-11-07 research archive)
                                (used only to annotate "observed in save")

Outputs (written next to this script):
  mapping-containers.json, mapping-skill-trees.json, mapping-skills.json,
  mapping-skill-tree-nodes.json, mapping-passive-nodes.json, mapping-weaver-nodes.json,
  mapping-affixes.json, mapping-item-bases.json, mapping-uniques.json, mapping-blessings.json,
  mapping-properties.json, mapping-classes.json, mapping-item-rarity.json, mapping-manifest.json

The output is byte-for-byte deterministic for a fixed set of inputs (sorted keys, stable
ordering, no timestamps).  Re-run:  python build_mappings.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = HERE / "source-data"
# Generated tables live with the runtime mappings, not next to this script.
OUT_DIR = Path(os.environ.get("LE_MAPPINGS_OUT", str(HERE.parent / "data" / "mappings")))
OUT_DIR.mkdir(parents=True, exist_ok=True)
# 归档根默认是仓库的同级目录；可用 LE_ARCHIVE_ROOT 覆盖。研究材料的位置见
# 作者本地归档说明（不随公开仓库分发）（本文件的映射输出被 mapping-manifest.json 的 sha256 绑定）。
_ARCHIVE_ROOT = Path(os.environ.get("LE_ARCHIVE_ROOT") or (ROOT.parent / "LastEpoch-BuildSimulator-archive"))
SAVE_RECON = Path(os.environ.get("LE_SAVE_RECON_DIR", str(_ARCHIVE_ROOT / "2026-11-07" / "scratch" / "save-recon")))

MAPPING_VERSION = "le-id-resolver-mappings-v1"

CONTAINER_GROUPS = {
    # curated functional grouping; the basis is the ContainerID enum member name (see entries.evidence)
    "equipment": {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12},
    "inventory": {1, 123},
    "idols": {29},
    "stash": {21},
    "blessings": {33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 87, 88, 89, 90},
    "crafting_materials": {13, 14, 15, 16, 17, 18, 23, 24, 25, 27, 48, 49, 50, 51, 61, 62, 80, 102,
                           103, 104, 105, 106, 107, 108, 109, 110, 114, 115, 116, 117, 122},
    "vendor_trade": {22, 26, 32, 58, 59, 60, 63, 64, 65, 66, 67, 87, 88, 89, 90, 101, 111, 112, 113,
                     118, 119, 120, 121},
    "weaver": {91, 92, 93, 94, 95, 96, 97, 98, 99},
    "keys": {28, 55, 56, 57, 81, 100},
    "nemesis": {82, 83, 84, 85, 86},
    "morditas": {124, 125, 126, 127, 128},
    "eternity_cache": {52, 53, 54},
    "circle_of_fortune": {68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 78, 79},
    "transient": {19, 20, 30, 31},
    "undefined": {0},
}

# planner/BuildSpec role mapping verified content-wise against build o3ZRm9my (see evidence strings)
PLANNER_ROLES = {
    2: "equipment.head",
    3: "equipment.chest",
    4: "equipment.weapon1",
    5: "equipment.weapon2 (empty in sample build)",
    6: "equipment.hands",
    7: "equipment.waist",
    8: "equipment.feet",
    9: "equipment.ring2",
    10: "equipment.ring1",
    11: "equipment.amulet",
    12: "equipment.relic",
    29: "idols (8 idol grid slots)",
    33: "blessings.timeline.1",
    34: "blessings.timeline.2",
    35: "blessings.timeline.3",
    36: "blessings.timeline.4",
    37: "blessings.timeline.5",
    38: "blessings.timeline.6",
    39: "blessings.timeline.7",
    43: "blessings.timeline.8",
    44: "blessings.timeline.9",
    45: "blessings.timeline.10",
    123: "equipment.idol_altar",
}

CONTAINER_TYPE_BY_GROUP = {
    "equipment": "EQUIPPED",
    "stash": "STASH",
    "blessings": "BLESSING",
    "crafting_materials": "MATERIALS",
    "vendor_trade": "SHOP",
}

RARITY_BYTE = {0: "Normal", 1: "Magic", 2: "Magic", 3: "Rare", 4: "Rare", 5: "Exalted",
               6: "Exalted", 7: "Unique", 8: "Set", 9: "Legendary"}


# --------------------------------------------------------------------------- helpers
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_obj(obj) -> str:
    return hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def write_json(name: str, obj) -> dict:
    text = json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"), indent=None)
    path = OUT_DIR / name
    path.write_text(text, encoding="utf-8")
    return {"path": name, "bytes": len(text.encode("utf-8")), "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def clean_text(text: str | None) -> str | None:
    if not text:
        return None
    return re.sub(r"\{[^}]*\}", "", text).strip()


def parse_enum_names(path: Path) -> list[dict]:
    """Parse an ilspycmd enum dump into [{name, value}] (declaration order -> sequential values
    unless explicit `= N` is present)."""
    members: list[dict] = []
    next_value = 0
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(-?\d+))?,?\s*$", line)
        if not match:
            continue
        name, value = match.group(1), match.group(2)
        if name in ("Token", "Obsolete", "Flags", "public", "enum"):
            continue
        if value is not None:
            next_value = int(value)
        members.append({"name": name, "value": next_value})
        next_value += 1
    return members


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


# --------------------------------------------------------------------------- inputs
def load_inputs() -> dict:
    current = load_json(ROOT / "data" / "current.json")
    db_path = ROOT / current["database"]
    szefo = {
        "bases": load_json(SRC / "szefo94" / "bases.json"),
        "uniques": load_json(SRC / "szefo94" / "uniques.json"),
        "affixes": load_json(SRC / "szefo94" / "affixes.json"),
    }
    container_map = load_json(SRC / "enums" / "container-id-map.json")
    enums = {
        "ContainerType": parse_enum_names(SRC / "enums" / "enum-ContainerType.txt"),
        "TimelineID": parse_enum_names(SRC / "enums" / "enum-TimelineID.txt"),
        "ItemRarity": parse_enum_names(SRC / "enums" / "enum-AchievementsConditionsItemRarity.txt"),
    }
    save = STASH = None
    if (SAVE_RECON / "save.json").is_file():
        save = load_json(SAVE_RECON / "save.json")
    if (SAVE_RECON / "STASH_0.json").is_file():
        STASH = load_json(SAVE_RECON / "STASH_0.json")
    return {
        "current": current,
        "db_path": db_path,
        "szefo": szefo,
        "container_map": container_map,
        "enums": enums,
        "save": save,
        "stash": STASH,
    }


def source_meta(inputs: dict) -> dict:
    current = inputs["current"]
    enums = SRC / "enums"
    szefo = SRC / "szefo94"
    return {
        "mapping_version": MAPPING_VERSION,
        "dataset": {
            "dataset_id": current["dataset_id"],
            "database_path": current["database"],
            "file_sha256": current["file_sha256"],
            "logical_content_sha256": current["logical_content_sha256"],
            "schema_version": current["schema_version"],
            "target_game_version": current["target_game_version"],
        },
        "game_assembly": {
            # Frozen provenance: this literal is embedded in the generated mapping-*.json payloads,
        # whose sha256 values are pinned by data/mappings/mapping-manifest.json. Changing it
        # would break byte-identical regeneration, so it is kept as recorded evidence.
        "path": r"<game-install>\MelonLoader\Dependencies\Il2CppAssemblyGenerator\Cpp2IL\cpp2il_out\LE.dll",
            "note": "IL2CPP dummy assembly; enums/field metadata only (method bodies stripped)",
            "enum_snapshots": {
                "ContainerID": {
                    "path": "source-data/enums/container-id-map.json",
                    "sha256": sha256_file(enums / "container-id-map.json"),
                    "derived_from": "LE.dll enum ContainerID (decompiled with ilspycmd -t ContainerID)",
                    "member_count": len(inputs["container_map"]),
                },
                "ContainerType": {"path": "source-data/enums/enum-ContainerType.txt",
                                  "sha256": sha256_file(enums / "enum-ContainerType.txt")},
                "TimelineID": {"path": "source-data/enums/enum-TimelineID.txt",
                               "sha256": sha256_file(enums / "enum-TimelineID.txt")},
                "ItemRarity": {"path": "source-data/enums/enum-AchievementsConditionsItemRarity.txt",
                               "sha256": sha256_file(enums / "enum-AchievementsConditionsItemRarity.txt")},
            },
        },
        "szefo94_source": {
            "repo": "https://github.com/szefo94/last-epoch-save-editor",
            "license": "MIT",
            "used_files": {
                name: {"path": "source-data/szefo94/" + name + ".json",
                       "sha256": sha256_file(szefo / (name + ".json")),
                       "origin_path": "szefo94/last-epoch-save-editor data/" + name + ".json"}
                for name in ("bases", "uniques", "affixes")
            },
        },
        "save_inputs": {
            "note": "used only to annotate which ids are observed in the user save; not required to rebuild",
            "save_json_sha256": sha256_file(SAVE_RECON / "save.json") if (SAVE_RECON / "save.json").is_file() else None,
            "stash_json_sha256": sha256_file(SAVE_RECON / "STASH_0.json") if (SAVE_RECON / "STASH_0.json").is_file() else None,
        },
        "source_priority": {
            "affix_definition": "phase1 dataset 1.5.2 (ids, scope, slot, tier ranges, stat names); readable tier text: szefo94 tables where present, else composed from dataset",
            "base_item": "phase1 dataset 1.5.2 (primary); szefo94 bases tables cross-check / formatted implicit text",
            "unique_item": "phase1 dataset 1.5.2 (primary; 486 definitions incl. 477/479 absent from szefo94); szefo94 mods text cross-check",
            "blessing": "phase1 dataset 1.5.2 entities + endgame timelineData blessing slots + game translations",
            "tree_node": "phase1 dataset 1.5.2 nodes + raw letools records + game translations",
            "container": "LE.dll ContainerID enum (values verified by Lead against save); role mapping content-validated against build o3ZRm9my",
            "item_rarity_byte": "szefo94 lesave.py RARITY + LE.dll ItemData.rarity field + Achievements.Conditions.ItemRarity enum",
        },
    }


# --------------------------------------------------------------------------- containers
def build_containers(inputs: dict) -> dict:
    container_map: dict[str, int] = inputs["container_map"]
    save = inputs["save"] or {}
    stash = inputs["stash"] or {}
    observed: dict[int, dict] = {}
    for index, item in enumerate(save.get("savedItems") or []):
        cid = item.get("containerID")
        rec = observed.setdefault(cid, {"count": 0, "item_indices": [], "positions": []})
        rec["count"] += 1
        rec["item_indices"].append(index)
        pos = item.get("inventoryPosition") or {}
        rec["positions"].append([pos.get("x"), pos.get("y")])
    for index, item in enumerate(stash.get("wovenEchoesList") or []):
        cid = item.get("containerID")
        if cid is None:
            continue
        rec = observed.setdefault(cid, {"count": 0, "item_indices": [], "positions": []})
        rec["count"] += 1
        rec["item_indices"].append(index)
        pos = item.get("inventoryPosition") or {}
        rec["positions"].append([pos.get("x"), pos.get("y")])
    for index, item in enumerate(stash.get("keysList") or []):
        cid = item.get("containerID")
        if cid is None:
            continue
        rec = observed.setdefault(cid, {"count": 0, "item_indices": [], "positions": []})
        rec["count"] += 1
        rec["item_indices"].append(index)
        pos = item.get("inventoryPosition") or {}
        rec["positions"].append([pos.get("x"), pos.get("y")])
    if stash.get("materialsList"):
        observed["materials(x=-4,y=-4)"] = {
            "count": len(stash["materialsList"]),
            "item_indices": list(range(len(stash["materialsList"]))),
            "positions": [[-4, -4]],
            "note": "materialsList entries carry no containerID; inventoryPosition (-4,-4) is the materials sentinel",
        }
    if stash.get("savedShards"):
        observed["savedShards(shardType)"] = {
            "count": len(stash["savedShards"]),
            "item_indices": [],
            "positions": [],
            "note": "savedShards[].shardType = subTypeID of item type 101 (Affix Shard); quantity is stack size",
        }

    names_by_id = {value: name for name, value in container_map.items()}
    saved_items_index = []
    for index, item in enumerate(save.get("savedItems") or []):
        cid = item.get("containerID")
        pos = item.get("inventoryPosition") or {}
        saved_items_index.append({
            "index": index,
            "container_id": cid,
            "container_name": names_by_id.get(cid),
            "group": next((g for g, ids in CONTAINER_GROUPS.items() if cid in ids), None),
            "planner_role": PLANNER_ROLES.get(cid),
            "position": [pos.get("x"), pos.get("y")],
            "quantity": item.get("quantity"),
            "data_bytes": len(item.get("data") or []),
        })

    names_by_id = {value: name for name, value in container_map.items()}
    entries = []
    for cid in sorted(names_by_id):
        name = names_by_id[cid]
        group = next((g for g, ids in CONTAINER_GROUPS.items() if cid in ids), None)
        if group is None and name.startswith("EQ_"):
            group = "equipment"
        role = PLANNER_ROLES.get(cid)
        obs = observed.get(cid)
        evidence = ["LE.dll enum ContainerID member %r = %d (sequential declaration order; values cross-checked by Lead against the save)" % (name, cid)]
        if role:
            evidence.append("planner role %r validated content-wise against build o3ZRm9my (decoded item base ids/unique ids match the planner slot)" % role)
        if name.startswith("EQ_"):
            evidence.append("ContainerIDExtensions.IsEquipmentContainer exists in LE.dll for EQ_* ids (method body stripped, name-based)")
        entries.append({
            "id": cid,
            "name": name,
            "group": group,
            "container_type": CONTAINER_TYPE_BY_GROUP.get(group),
            "container_type_basis": ("group -> ContainerType enum name; not proven by game method bodies"
                                     if CONTAINER_TYPE_BY_GROUP.get(group) else None),
            "planner_role": role,
            "observed_in_save": obs,
            "evidence": evidence,
        })
    return {
        "meta": {
            "source": "LE.dll enum ContainerID (129 members, values 0..128) + ContainerType enum",
            "container_type_enum": inputs["enums"]["ContainerType"],
        },
        "entries": entries,
        "special_positions": {
            "materials_list": {
                "container": "materialsList in STASH_0",
                "inventory_position": {"x": -4, "y": -4},
                "evidence": "STASH_0.materialsList entries: 19 items, every inventoryPosition == {'x': -4, 'y': -4}, no containerID field",
            },
            "saved_items_index": saved_items_index,
        },
        "observed_container_ids": sorted([k for k in observed if isinstance(k, int)]),
    }


# --------------------------------------------------------------------------- classes/masteries/trees
CLASS_TREE_SOURCE = {0: "pr-1", 1: "mg-1", 2: "kn-1", 3: "ac-1", 4: "rg-1"}


def db_rows(con: sqlite3.Connection, sql: str, args=()) -> list[dict]:
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def build_classes(con: sqlite3.Connection) -> dict:
    classes = db_rows(con, "select c.entity_id, c.source_class_id, e.canonical_name from classes c left join entities e on e.entity_id=c.entity_id order by cast(c.source_class_id as integer)")
    masteries = db_rows(con, "select m.entity_id, m.class_entity_id, m.source_mastery_id, m.branch_kind, m.is_base, e.canonical_name from masteries m left join entities e on e.entity_id=m.entity_id")
    class_name = {int(c["source_class_id"]): c["canonical_name"] for c in classes}
    class_by_entity = {c["entity_id"]: int(c["source_class_id"]) for c in classes}
    out_masteries: dict[str, dict] = {}
    for m in masteries:
        cls_id = class_by_entity.get(m["class_entity_id"])
        if cls_id is None:
            continue
        idx = int(m["source_mastery_id"].split(".")[1])
        out_masteries.setdefault(str(cls_id), {})[str(idx)] = {
            "source_mastery_id": m["source_mastery_id"],
            "name": m["canonical_name"],
            "branch_kind": m["branch_kind"],
            "is_base": bool(m["is_base"]),
            "entity_id": m["entity_id"],
        }
    return {
        "classes": {str(k): v for k, v in sorted(class_name.items())},
        "class_entity_to_source_id": {k: str(v) for k, v in sorted(class_by_entity.items())},
        "masteries": out_masteries,
    }


def build_trees(con: sqlite3.Connection, classes: dict) -> tuple[dict, dict]:
    class_entity = classes["class_entity_to_source_id"]
    class_name = classes["classes"]
    mastery_map = classes["masteries"]
    trees = db_rows(con, "select t.entity_id, t.tree_kind, t.owner_entity_id, t.class_entity_id, t.source_tree_id, t.node_count, t.extensions, e.canonical_name from trees t left join entities e on e.entity_id=t.entity_id order by t.tree_kind, t.source_tree_id")
    skills = {r["entity_id"]: r for r in db_rows(con, "select s.entity_id, s.skill_type, s.class_entity_id, s.mastery_entity_id from skills s")}
    tree_entries: dict[str, dict] = {}
    for t in trees:
        source_id = t["source_tree_id"]
        ext = json.loads(t["extensions"]) if t["extensions"] else {}
        cls_id = class_entity.get(t["class_entity_id"])
        owner = skills.get(t["owner_entity_id"]) if t["owner_entity_id"] else None
        mastery_name = None
        mastery_source_id = None
        if owner and owner.get("mastery_entity_id"):
            for idx, m in (mastery_map.get(str(cls_id)) or {}).items():
                if m["entity_id"] == owner["mastery_entity_id"]:
                    mastery_name, mastery_source_id = m["name"], m["source_mastery_id"]
        tree_entries[source_id] = {
            "source_tree_id": source_id,
            "entity_id": t["entity_id"],
            "kind": t["tree_kind"],
            "name": t["canonical_name"],
            "class": class_name.get(str(cls_id)),
            "class_source_id": str(cls_id) if cls_id is not None else None,
            "mastery": mastery_name,
            "mastery_source_id": mastery_source_id,
            "owner_skill_id": source_id if t["owner_entity_id"] else None,
            "owner_skill_entity_id": t["owner_entity_id"],
            "node_count": t["node_count"],
            "catalog_version": ext.get("version"),
        }
    skills_out = {}
    rows = db_rows(con, "select m.source_id, m.entity_id, e.canonical_name, s.skill_type, s.class_entity_id, s.mastery_entity_id from source_id_map m join entities e on e.entity_id=m.entity_id left join skills s on s.entity_id=m.entity_id where m.kind='ability' and m.source='letools' and m.source_scope='LEAbilities.abilityList' order by m.source_id")
    for r in rows:
        cls_id = class_entity.get(r["class_entity_id"])
        mastery_name = None
        for idx, m in (mastery_map.get(str(cls_id)) or {}).items():
            if m["entity_id"] == r["mastery_entity_id"]:
                mastery_name = m["name"]
        skills_out[r["source_id"]] = {
            "source_id": r["source_id"],
            "entity_id": r["entity_id"],
            "name": r["canonical_name"],
            "skill_type": r["skill_type"],
            "class": class_name.get(str(cls_id)),
            "class_source_id": str(cls_id) if cls_id is not None else None,
            "mastery": mastery_name,
        }
    return tree_entries, skills_out


# --------------------------------------------------------------------------- nodes
def build_translations(con: sqlite3.Connection) -> dict:
    return {r["key"]: r["value"] for r in con.execute("select key, value from translations where namespace='full'").fetchall()}


def node_scope(tree_entity_id: str) -> tuple[str, str, str | None]:
    """Return (kind, raw_source_scope, class_source_id) for a tree entity id."""
    if "LECharTrees.trees:" in tree_entity_id:
        source_id = tree_entity_id.split(":")[-1]
        return "passive", source_id, source_id
    if "LEWeaverTree" in tree_entity_id:
        return "weaver", "weaver", None
    if "LESkillTrees:" in tree_entity_id:
        return "skill", tree_entity_id.split(":")[-1], None
    return "unknown", tree_entity_id.split(":")[-1], None


RAW_KIND = {"passive": "passive_node", "skill": "skill_tree_node", "weaver": "weaver_node"}


def build_nodes(con: sqlite3.Connection, translations: dict, trees: dict) -> tuple[dict, dict, dict, dict]:
    raw_cache: dict[tuple[str, str, str], dict] = {}
    for r in db_rows(con, "select entity_kind, source_scope, source_id, raw_payload from source_records where entity_kind in ('passive_node','skill_tree_node','weaver_node','passive_tree_node')"):
        raw_cache[(r["entity_kind"], r["source_scope"], r["source_id"])] = json.loads(r["raw_payload"])
    nodes = db_rows(con, "select n.entity_id, n.tree_entity_id, n.node_index, n.node_type, n.max_points, n.position_x, n.position_y, n.position_w, n.position_h, n.extensions, e.canonical_name from nodes n left join entities e on e.entity_id=n.entity_id order by n.tree_entity_id, n.node_index")
    out: dict[str, dict] = {}
    stats_out = {"total": 0, "name": 0, "description_or_effect": 0, "stat_values": 0}
    for n in nodes:
        tree_entity = n["tree_entity_id"]
        kind, scope, _cls = node_scope(tree_entity)
        tree_id = trees.get(scope, {}).get("source_tree_id") if kind != "weaver" else "weaver"
        if tree_id is None:
            tree_id = tree_entity.split(":")[-1]
        ext = json.loads(n["extensions"]) if n["extensions"] else {}
        raw = raw_cache.get((RAW_KIND.get(kind, ""), scope, str(n["node_index"])))
        raw_source = "source_records:%s" % RAW_KIND.get(kind) if raw else None
        if raw is None and kind == "passive":
            raw = raw_cache.get(("passive_tree_node", scope, str(n["node_index"])))
            raw_source = "source_records:passive_tree_node(tunklab fallback)" if raw else None
        name_key = (raw or {}).get("nodeNameKey") or ext.get("nodeNameKey")
        desc_key = (raw or {}).get("descriptionKey") or ext.get("descriptionKey")
        alt_key = (raw or {}).get("altTextKey")
        lore_key = (raw or {}).get("loreTextKey")
        name = translations.get(name_key) if name_key else None
        name = name or (raw or {}).get("nodeName") or n["canonical_name"]
        description = translations.get(desc_key) if desc_key else (raw or {}).get("description") or None
        alt_text = translations.get(alt_key) if alt_key else (raw or {}).get("altText") or None
        lore_text = translations.get(lore_key) if lore_key else None
        stats = []
        for raw_stat in ((raw or {}).get("stats") or []):
            stat_key = raw_stat.get("statNameKey")
            stat_name = translations.get(stat_key) if stat_key else raw_stat.get("statName")
            stats.append({
                "name": stat_name,
                "value": raw_stat.get("value"),
                "property": raw_stat.get("property"),
                "downside": bool(raw_stat.get("downside", False)),
                "no_scaling": bool(raw_stat.get("noScaling", False)),
            })
        requirements = (raw or {}).get("requirements") or []
        bucket = out.setdefault(tree_id, {})
        bucket[str(n["node_index"])] = {
            "node_index": n["node_index"],
            "entity_id": n["entity_id"],
            "name": name,
            "node_type": n["node_type"],
            "max_points": n["max_points"],
            "description": description,
            "alt_text": alt_text,
            "lore_text": lore_text,
            "stats": stats,
            "requirements": requirements,
            "position": [n["position_x"], n["position_y"], n["position_w"], n["position_h"]],
            "raw_source": raw_source,
            "effect_text_status": (
                "resolved" if (description or alt_text or (stats and all(s["name"] for s in stats)))
                else "unresolved"),
        }
        stats_out["total"] += 1
        if name:
            stats_out["name"] += 1
        if description or alt_text or stats:
            stats_out["description_or_effect"] += 1
        if stats:
            stats_out["stat_values"] += 1
    skill_out = {tid: nodes for tid, nodes in out.items() if trees.get(tid, {}).get("kind") == "skill"}
    passive_out = {tid: nodes for tid, nodes in out.items() if trees.get(tid, {}).get("kind") == "passive"}
    weaver_out = out.get("weaver") or {}
    return skill_out, passive_out, weaver_out, stats_out


# --------------------------------------------------------------------------- properties
PROPERTY_SCOPES = ["coreDB.propertyList", "coreDB.playerPropertyList", "coreDB.abilityPropertyList",
                   "coreDB.coreAttributes", "coreDB.idolAltarPropertyList.list",
                   "coreDB.idolAltarPropertyList.defaultProperty"]


def build_properties(con: sqlite3.Connection) -> dict:
    namespaces: dict[str, dict[str, str]] = {}
    rows = db_rows(con, "select m.source_scope, m.source_id, e.canonical_name from source_id_map m join entities e on e.entity_id=m.entity_id where m.kind='property' and m.source='letools' order by m.source_scope, cast(m.source_id as integer)")
    for r in rows:
        if r["source_scope"] in PROPERTY_SCOPES:
            namespaces.setdefault(r["source_scope"], {})[str(r["source_id"])] = r["canonical_name"]
    return {"namespaces": namespaces,
            "note": "property id -> display name, used to render node stats / base implicits / unique mods"}


def property_name(properties: dict, property_id) -> str | None:
    if property_id is None:
        return None
    for scope in PROPERTY_SCOPES:
        name = properties["namespaces"].get(scope, {}).get(str(property_id))
        if name:
            return name
    return None


# --------------------------------------------------------------------------- affixes
def build_affixes(con: sqlite3.Connection, szefo: dict, properties: dict) -> dict:
    con.row_factory = sqlite3.Row
    tiers: dict[str, list[dict]] = {}
    for r in con.execute("select affix_entity_id, tier_index, roll_min, roll_max, level_requirement from affix_tiers order by affix_entity_id, tier_index"):
        tiers.setdefault(r["affix_entity_id"], []).append({
            "tier0": r["tier_index"], "tier": r["tier_index"] + 1,
            "roll_min": r["roll_min"], "roll_max": r["roll_max"],
            "level_requirement": r["level_requirement"],
        })
    sz_affixes = szefo["affixes"]
    sz_by_id: dict[int, dict[int, dict]] = {}
    for key, mod in sz_affixes.items():
        try:
            affix_id, tier0 = map(int, key.split("_"))
        except ValueError:
            continue
        sz_by_id.setdefault(affix_id, {})[tier0] = mod
    affixes: dict[str, dict] = {}
    raw_ids: list[str] = []
    text_covered = 0
    slot_conflicts = 0
    az = 0
    for r in con.execute("select entity_id, affix_type, source_affix_id, affix_title, affix_display_name, affix_slot, can_roll_on, extensions from affixes order by affix_type, cast(source_affix_id as integer)"):
        raw_id = r["source_affix_id"]
        try:
            affix_id = int(raw_id)
        except (TypeError, ValueError):
            affix_id = None
        ext = json.loads(r["extensions"]) if r["extensions"] else {}
        props = []
        for prop in (ext.get("affixProperties") or []):
            pid = prop.get("property")
            props.append({"property": pid, "stat_name": property_name(properties, pid)})
        tier_rows = tiers.get(r["entity_id"], [])
        sz_tiers = (sz_by_id.get(affix_id, {}) if affix_id is not None else {})
        merged = []
        for t in tier_rows:
            sz = sz_tiers.get(t["tier0"])
            if sz:
                parts = [clean_text(sz.get(k)) for k in ("1", "2", "3") if sz.get(k)]
                text = " / ".join(parts) if parts else None
                text_source = "szefo94:affixes.json"
                sz_type = sz.get("type")
                if sz_type and r["affix_slot"] and sz_type.lower() != r["affix_slot"].lower():
                    slot_conflicts += 1
            else:
                # no published per-tier text for this affix id: fall back to the dataset stat name
                text = clean_text(r["affix_display_name"]) or None
                text_source = "composed:dataset_affix_display_name" if text else None
                sz_type = None
            merged.append({**t, "text": text, "text_source": text_source, "szefo94_type": sz_type})
            if text:
                text_covered += 1
                az += 1
        entry = {
            "id": affix_id,
            "id_raw": raw_id,
            "scope": r["affix_type"],
            "title": r["affix_title"],
            "name": r["affix_display_name"],
            "slot": r["affix_slot"],
            "can_roll_on": json.loads(r["can_roll_on"]) if r["can_roll_on"] else [],
            "properties": props,
            "tiers": merged,
            "tier_count": len(merged),
            "entity_id": r["entity_id"],
            "readable_text_source": ("szefo94:affixes.json" if any(t["text"] for t in merged) else "none"),
        }
        if affix_id is None:
            raw_ids.append(raw_id)
            key = "raw:" + str(raw_id)
        else:
            key = str(affix_id)
        affixes[key] = entry
    sz_extra_ids = sorted(set(sz_by_id) - {int(k) for k in affixes if k.isdigit()})
    return {
        "meta": {
            "definition_source": "phase1 dataset affixes + affix_tiers (1.5.2)",
            "readable_text_source": "szefo94/last-epoch-save-editor data/affixes.json keyed '<affix_id>_<tier0>'",
            "slot_values": ["prefix", "suffix"],
            "tier_convention": "planner/item tier0 == dataset tier_index (0-based); displayed tier = tier0 + 1",
            "counts": {
                "dataset_affixes": len(affixes),
                "tier_rows": sum(len(t) for t in tiers.values()),
                "tiers_with_szefo94_text": text_covered,
                "szefo94_ids_not_in_dataset": len(sz_extra_ids),
                "prefix_suffix_conflicts": slot_conflicts,
                "non_numeric_dataset_ids": raw_ids,
            },
        },
        "affixes": affixes,
    }


# --------------------------------------------------------------------------- item bases
def build_item_bases(con: sqlite3.Connection, szefo: dict, properties: dict, blessings: dict) -> dict:
    con.row_factory = sqlite3.Row
    item_types: dict[str, str] = {}
    for r in con.execute("select entity_id, canonical_name from entities where kind='item_type' and entity_id like 'item_type:letools:itemList.%' order by entity_id"):
        m = re.match(r"item_type:letools:itemList\.(?:non)?[Ee]quippable:(\d+)$", r["entity_id"])
        if m:
            item_types[m.group(1)] = r["canonical_name"]
    sz_bases = szefo["bases"]
    sz_by_pair = {(int(b["baseTypeID"]), int(b["subTypeID"])): dict(b, name=name) for name, b in sz_bases.items()}
    bases: dict[str, dict] = {}
    conflicts = []
    rows = con.execute(
        "select i.entity_id, i.item_type, i.base_type, i.level_requirement, i.implicit_text, i.extensions, e.canonical_name "
        "from items i join entities e on e.entity_id=i.entity_id where i.item_category='base' order by i.base_type, i.entity_id")
    for r in rows:
        m = re.match(r"item:letools:itemList\.(equippable|nonEquippable)_(\d+):(\d+)$", r["entity_id"])
        if not m:
            continue
        group = "equippable" if m.group(1) == "equippable" else "nonEquippable"
        base_type_id, sub_type_id = int(m.group(2)), int(m.group(3))
        ext = json.loads(r["extensions"]) if r["extensions"] else {}
        structured = []
        for imp in (ext.get("implicits") or json.loads(r["implicit_text"] or "[]") or []):
            structured.append({
                "property": imp.get("property"),
                "stat_name": property_name(properties, imp.get("property")),
                "value": imp.get("implicitValue"),
                "max_value": imp.get("implicitMaxValue"),
                "type": imp.get("type"),
                "tags": imp.get("tags"),
            })
        sz = sz_by_pair.get((base_type_id, sub_type_id))
        name = r["canonical_name"]
        if sz and sz["name"] != name:
            conflicts.append({"type": base_type_id, "sub": sub_type_id, "dataset_name": name, "szefo94_name": sz["name"]})
        bases["%d:%d" % (base_type_id, sub_type_id)] = {
            "base_type_id": base_type_id,
            "sub_type_id": sub_type_id,
            "group": group,
            "name": name,
            "item_type_name": r["item_type"] or item_types.get(str(base_type_id)),
            "level_requirement": r["level_requirement"],
            "implicits_structured": structured,
            "implicit_text": (sz or {}).get("implicits") or [],
            "implicit_text_source": ("szefo94:bases.json" if (sz or {}).get("implicits") else "none"),
            "entity_id": r["entity_id"],
        }
    # idol base items are normalised into the `idols` table (not `items`): add them so
    # (item type 25..33, subTypeID) resolves through the same table.
    raw_implicit_cache = {}
    for r in con.execute("select source_scope, source_id, raw_payload from source_records where entity_kind='item_base' and source_scope like 'itemList.equippable:%'"):
        try:
            key = (int(r["source_scope"].split(":")[-1]), int(r["source_id"]))
        except ValueError:
            continue
        raw_implicit_cache[key] = json.loads(r["raw_payload"]).get("implicits") or []
    idol_count = 0
    for r in con.execute("select i.entity_id, i.idol_size, e.canonical_name from idols i join entities e on e.entity_id=i.entity_id"):
        m = re.match(r"item:letools:itemList\.equippable_(\d+):(\d+)$", r["entity_id"])
        if not m:
            continue
        base_type_id, sub_type_id = int(m.group(1)), int(m.group(2))
        structured = []
        for imp in raw_implicit_cache.get((base_type_id, sub_type_id), []):
            structured.append({
                "property": imp.get("property"),
                "stat_name": property_name(properties, imp.get("property")),
                "value": imp.get("implicitValue"),
                "max_value": imp.get("implicitMaxValue"),
                "type": imp.get("type"),
                "tags": imp.get("tags"),
            })
        sz = sz_by_pair.get((base_type_id, sub_type_id))
        bases["%d:%d" % (base_type_id, sub_type_id)] = {
            "base_type_id": base_type_id,
            "sub_type_id": sub_type_id,
            "group": "equippable",
            "name": r["canonical_name"],
            "item_type_name": item_types.get(str(base_type_id)),
            "level_requirement": None,
            "idol_size": r["idol_size"],
            "implicits_structured": structured,
            "implicit_text": (sz or {}).get("implicits") or [],
            "implicit_text_source": ("szefo94:bases.json" if (sz or {}).get("implicits") else "none"),
            "entity_id": r["entity_id"],
            "role": "idol",
        }
        idol_count += 1
    # blessing base items (item type 34) are normalised outside `items`: add them so (34, id) resolves
    for base_key, b in blessings["blessings"].items():
        bases["34:%s" % b["base_id"]] = {
            "base_type_id": 34,
            "sub_type_id": b["base_id"],
            "group": "equippable",
            "name": b["name"],
            "item_type_name": "Blessing",
            "level_requirement": 0,
            "implicits_structured": b["implicit_structured"],
            "implicit_text": b["implicit_text"],
            "implicit_text_source": b["implicit_text_source"],
            "entity_id": b["entity_id"],
            "role": "blessing",
        }
    sz_pairs = set(sz_by_pair)
    ds_pairs = {tuple(int(x) for x in k.split(":")) for k in bases if k.split(":")[0] != "34"}
    return {
        "meta": {
            "counts": {
                "dataset_bases": len(ds_pairs),
                "szefo94_bases": len(sz_pairs),
                "union": len(ds_pairs | sz_pairs),
                "szefo94_missing_from_dataset": len(sz_pairs - ds_pairs),
                "dataset_only_vs_szefo94": len(ds_pairs - sz_pairs),
                "name_conflicts": len(conflicts),
            },
            "key_format": "<itemTypeIndex>:<baseSubTypeId>",
            "name_conflicts": conflicts[:40],
        },
        "item_types": item_types,
        "bases": bases,
    }


# --------------------------------------------------------------------------- uniques
def build_uniques(con: sqlite3.Connection, szefo: dict, properties: dict, item_bases: dict) -> dict:
    con.row_factory = sqlite3.Row
    sz_uniques = {int(k): v for k, v in szefo["uniques"].items()}
    unique_rows = {}
    for r in con.execute(
        "select e.entity_id, e.canonical_name, i.level_requirement, i.base_type, i.extensions "
        "from entities e join items i on i.entity_id=e.entity_id "
        "where e.kind='item' and e.entity_id like 'item:letools:uniqueList.uniques:%' order by cast(substr(e.entity_id, instr(e.entity_id, '.uniques:')+9) as integer)"):
        m = re.search(r"\.uniques:(\d+)$", r["entity_id"])
        if not m:
            continue
        unique_rows[int(m.group(1))] = r
    out: dict[str, dict] = {}
    name_conflicts = []
    for unique_id in sorted(unique_rows):
        r = unique_rows[unique_id]
        ext = json.loads(r["extensions"]) if r["extensions"] else {}
        sz = sz_uniques.get(unique_id)
        name = r["canonical_name"]
        if sz and sz.get("name") and sz["name"] != name:
            name_conflicts.append({"unique_id": unique_id, "dataset_name": name, "szefo94_name": sz["name"]})
        mods = []
        for mod in (ext.get("mods") or []):
            mods.append({
                "property": mod.get("property"),
                "stat_name": property_name(properties, mod.get("property")),
                "value": mod.get("value"),
                "max_value": mod.get("maxValue"),
                "can_roll": bool(mod.get("canRoll")),
                "hide_in_tooltip": bool(mod.get("hideInTooltip")),
                "roll_id": mod.get("rollId"),
            })
        subtype_ids = ext.get("subTypeIds") or []
        anchor_key = "%d:%d" % (int(ext.get("baseTypeId") or 0), int(subtype_ids[0])) if subtype_ids else None
        anchor = item_bases["bases"].get(anchor_key) if anchor_key else None
        lore = None
        lore_key = ext.get("loreTextKey")
        if lore_key:
            row = con.execute("select value from translations where key=?", (lore_key,)).fetchone()
            lore = row[0] if row else None
        out[str(unique_id)] = {
            "unique_id": unique_id,
            "name": name,
            "base_type_id": ext.get("baseTypeId"),
            "sub_type_ids": subtype_ids,
            "anchor": {
                "base_key": anchor_key,
                "base_name": anchor["name"] if anchor else None,
                "item_type_name": anchor["item_type_name"] if anchor else None,
            },
            "level_requirement": r["level_requirement"],
            "mods": mods,
            "mods_text": (sz or {}).get("mods") or [],
            "mods_text_source": ("szefo94:uniques.json" if (sz or {}).get("mods") else "none"),
            "roll_ids": (sz or {}).get("rollIds"),
            "lore_text": lore,
            "entity_id": r["entity_id"],
            "name_source": "phase1-dataset-003",
        }
    return {
        "meta": {
            "counts": {
                "dataset_uniques": len(out),
                "szefo94_uniques": len(sz_uniques),
                "dataset_ids_missing_from_szefo94": len([u for u in map(int, out) if u not in sz_uniques]),
            },
            "dataset_ids_missing_from_szefo94": sorted(u for u in map(int, out) if u not in sz_uniques),
            "name_conflicts": name_conflicts[:20],
            "key": "planner U-marker unique id == game uniqueId (bytes 12-13 big endian for rarity 7/8/9)",
        },
        "uniques": out,
        "by_anchor": {k: v["unique_id"] for k, v in finalize_unique_anchor_map(out).items()} if False else finalize_unique_anchor_map(out),
    }


def finalize_unique_anchor_map(uniques: dict) -> dict:
    """base-subtype-id -> {unique_id: ...} index: (base_type_id, sub_type_id) -> unique ids that can use it."""
    mapping: dict[str, list[int]] = {}
    for uid, u in uniques.items():
        base_type = u.get("base_type_id")
        for sub in u.get("sub_type_ids") or []:
            mapping.setdefault("%s:%s" % (base_type, sub), []).append(int(uid))
    return {k: sorted(v) for k, v in mapping.items()}


# --------------------------------------------------------------------------- blessings
def build_blessings(con: sqlite3.Connection, szefo: dict, properties: dict, translations: dict) -> dict:
    con.row_factory = sqlite3.Row
    blessing_names: dict[int, str] = {}
    for r in con.execute("select entity_id, canonical_name from entities where kind='blessing'"):
        m = re.search(r":(\d+)$", r["entity_id"])
        if m:
            blessing_names[int(m.group(1))] = r["canonical_name"]
    raw_implicits: dict[int, list[dict]] = {}
    for r in con.execute("select source_id, raw_payload from source_records where entity_kind='item_base' and source_scope='itemList.equippable:34'"):
        try:
            base_id = int(r["source_id"])
        except ValueError:
            continue
        payload = json.loads(r["raw_payload"])
        raw_implicits[base_id] = payload.get("implicits") or []
    # timeline data: blessing ids per timeline/difficulty (tunklab endgame_config)
    row = con.execute("select raw_payload from source_records where entity_kind='endgame_config' and source_id='timelineData'").fetchone()
    timeline_data = json.loads(row["raw_payload"])["value"]
    timeline_entity_names = {int(m.group(1)): r["canonical_name"] for r in con.execute("select entity_id, canonical_name from entities where kind='timeline'") for m in [re.search(r":(\d+)$", r["entity_id"])] if m}
    timelines: dict[str, dict] = {}
    blessing_timelines: dict[int, list[dict]] = {}
    for timeline_id in sorted(timeline_data, key=int):
        t = timeline_data[timeline_id]
        name_key = t.get("nameKey")
        display_name = translations.get(name_key) or timeline_entity_names.get(int(timeline_id)) or t.get("slug")
        difficulties = []
        for diff_index, diff in enumerate(t.get("difficulties") or []):
            entry = {
                "difficulty": diff_index,
                "level": diff.get("level"),
                "first_slot_blessings": diff.get("firstSlotBlessings") or [],
                "other_slot_blessings": diff.get("otherSlotBlessings") or [],
                "any_slot_blessings": diff.get("anySlotBlessings") or [],
            }
            difficulties.append(entry)
            for slot_key in ("first_slot_blessings", "other_slot_blessings", "any_slot_blessings"):
                for base_id in entry[slot_key]:
                    blessing_timelines.setdefault(base_id, []).append({
                        "timeline": int(timeline_id),
                        "timeline_name": display_name,
                        "difficulty": diff_index,
                        "slot": slot_key,
                    })
        timelines[timeline_id] = {
            "timeline_id": int(timeline_id),
            "name": display_name,
            "slug": t.get("slug"),
            "name_key": name_key,
            "difficulties": difficulties,
            "boss_name_key": t.get("bossNameKey"),
            "harbinger_name_key": t.get("harbingerNameKey"),
        }
    sz_bases = szefo["bases"]
    blessings: dict[str, dict] = {}
    for base_id in sorted(blessing_names):
        implicit_structured = []
        for imp in raw_implicits.get(base_id, []):
            implicit_structured.append({
                "property": imp.get("property"),
                "stat_name": property_name(properties, imp.get("property")),
                "value": imp.get("implicitValue"),
                "max_value": imp.get("implicitMaxValue"),
                "type": imp.get("type"),
                "tags": imp.get("tags"),
                "special_tag": imp.get("specialTag"),
            })
        sz = sz_bases.get(blessing_names[base_id])
        implicits_text = (sz or {}).get("implicits") or []
        tl = blessing_timelines.get(base_id, [])
        unique_timelines = sorted({x["timeline"] for x in tl})
        blessings[str(base_id)] = {
            "base_id": base_id,
            "name": blessing_names[base_id],
            "item_type_index": 34,
            "entity_id": "item:letools:itemList.equippable_34:%d" % base_id,
            "implicit_structured": implicit_structured,
            "implicit_text": implicits_text,
            "implicit_text_source": ("szefo94:bases.json" if implicits_text else "none"),
            "timelines": unique_timelines,
            "timeline_slots": tl,
            "grand_variant": "Grand" in blessing_names[base_id],
        }
    covered = sum(1 for b in blessings.values() if b["timelines"])
    return {
        "meta": {
            "counts": {
                "blessings": len(blessings),
                "blessings_with_timeline": covered,
                "timelines": len(timelines),
            },
            "blessing_id_convention": "save blessingsDiscovered[] and blessing item subTypeID == base group 34 sub id; planner I-marker payload '1'+'034'+base_id+'000'",
            "timeline_source": "source_records:endgame_config.timelineData (tunklab 1.5.2 snapshot)",
        },
        "blessings": blessings,
        "timelines": timelines,
    }


# --------------------------------------------------------------------------- main
def main() -> int:
    inputs = load_inputs()
    con = sqlite3.connect(str(inputs["db_path"]))
    con.row_factory = sqlite3.Row
    translations = build_translations(con)
    properties = build_properties(con)
    classes = build_classes(con)
    trees, skills = build_trees(con, classes)
    skill_nodes, passive_nodes, weaver_nodes, node_stats = build_nodes(con, translations, trees)
    affixes = build_affixes(con, inputs["szefo"], properties)
    blessings = build_blessings(con, inputs["szefo"], properties, translations)
    item_bases = build_item_bases(con, inputs["szefo"], properties, blessings)
    uniques = build_uniques(con, inputs["szefo"], properties, item_bases)
    containers = build_containers(inputs)
    meta = source_meta(inputs)

    outputs = {
        "mapping-containers.json": {"meta": {**meta, **containers["meta"]}, "entries": containers["entries"],
                                    "special_positions": containers["special_positions"],
                                    "observed_container_ids": containers["observed_container_ids"]},
        "mapping-classes.json": {"meta": meta, **classes},
        "mapping-skill-trees.json": {"meta": {**meta, "tree_count": len(trees),
                                              "skill_tree_count": sum(1 for t in trees.values() if t["kind"] == "skill"),
                                              "passive_tree_count": sum(1 for t in trees.values() if t["kind"] == "passive")},
                                     "trees": trees},
        "mapping-skills.json": {"meta": {**meta, "ability_count": len(skills)}, "abilities": skills},
        "mapping-skill-tree-nodes.json": {"meta": {**meta, "tree_count": len(skill_nodes),
                                                   "node_count": sum(len(v) for v in skill_nodes.values())},
                                          "trees": skill_nodes},
        "mapping-passive-nodes.json": {"meta": {**meta, "tree_count": len(passive_nodes),
                                                "node_count": sum(len(v) for v in passive_nodes.values())},
                                       "trees": passive_nodes},
        "mapping-weaver-nodes.json": {"meta": {**meta, "node_count": len(weaver_nodes)}, "nodes": weaver_nodes},
        "mapping-affixes.json": {"meta": {**meta, **affixes["meta"]}, "affixes": affixes["affixes"]},
        "mapping-item-bases.json": {"meta": {**meta, **item_bases["meta"]}, "item_types": item_bases["item_types"],
                                    "bases": item_bases["bases"]},
        "mapping-uniques.json": {"meta": {**meta, **uniques["meta"]}, "uniques": uniques["uniques"],
                                  "by_anchor": uniques["by_anchor"]},
        "mapping-blessings.json": {"meta": {**meta, **blessings["meta"]}, "blessings": blessings["blessings"],
                                   "timelines": blessings["timelines"]},
        "mapping-properties.json": {"meta": meta, **properties},
        "mapping-item-rarity.json": {
            "meta": meta,
            "rarity_byte": {str(k): v for k, v in RARITY_BYTE.items()},
            "game_enums": {"Achievements.Conditions.ItemRarity": inputs["enums"]["ItemRarity"]},
            "itemdata_field": "ItemData.rarity (byte, offset 0x30 in the IL2CPP dump)",
            "display_rarity_rule": {
                "unique_with_affixes": "Legendary",
                "unique_without_affixes": "Set if rarity byte == 8 else Unique",
                "normal_item": "Exalted when any affix tier0 >= 5 (displayed T6+) and base type not in {25..33,34,41}; else Rare when affix count >= 3; Magic when affix count >= 1; Normal otherwise",
                "evidence": "szefo94/last-epoch-save-editor lesave.py RARITY + rarity_name() (MIT); consistent with LE.dll ItemData.rarity field and Achievements.Conditions.ItemRarity enum order",
            },
            "note": "the raw rarity byte counts affixes for normal gear (game files use 1-4); displayed rarity should use display_rarity_rule",
        },
    }
    manifest_files = {}
    for name, obj in outputs.items():
        manifest_files[name] = write_json(name, obj)
    manifest = {
        "mapping_version": MAPPING_VERSION,
        "meta": meta,
        "files": manifest_files,
        "node_coverage": node_stats,
    }
    write_json("mapping-manifest.json", manifest)
    print("wrote %d mapping files to %s" % (len(manifest_files) + 1, HERE))
    for name, info in manifest_files.items():
        print("  %-34s %8d bytes  sha256=%s" % (name, info["bytes"], info["sha256"][:16]))
    print("  node coverage:", json.dumps(node_stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
