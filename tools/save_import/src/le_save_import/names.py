# -*- coding: utf-8 -*-
"""Last Epoch save-side numeric id -> human-readable name/effect resolver.

Deterministic, dependency-free (stdlib only), importable:

    from resolve import Resolver
    r = Resolver()
    r.container(2)                       # {'status': 'resolved', 'name': 'EQ_HELMET', ...}
    r.skill_tree('bh2')                  # Black Hole / Mage / Sorcerer
    r.skill_tree_node('bh2', 2)          # Tidal Force + description + per-point stats
    r.passive_node(1, 0)                 # Mage passive node 0 -> Arcanist
    r.weaver_node(10)
    r.unique(477)                        # Unsated Rage
    r.base_item(0, 23)                   # Runed Visage
    r.affix(13, tier0=6)                 # Fire Resistance, +(61-75)%
    r.blessing(116)                      # Grand Scales of Greed + timeline 1
    r.resolve_planner_id('AwRgzEA')      # planner encoded id -> ids + names
    r.describe_decoded_item({...})       # decoded item fields -> readable identity

All lookups return a dict with an explicit ``status`` of ``resolved`` / ``partial`` /
``unresolved`` and a machine-readable ``reason`` when unresolved.  Names are never
invented: an id that is not present in the mapping tables is reported unresolved.

Mapping tables are produced by ``build_mappings.py`` (see README.md and
mapping-manifest.json for source provenance and sha256).
"""
from __future__ import annotations

import json
from pathlib import Path

#: Mapping tables live at <tool>/data/mappings (generated from the phase-1 dataset +
#: vendored MIT game-data tables; see README.md / mapping-manifest.json).
MAPPING_DIR = Path(__file__).resolve().parents[2] / "data" / "mappings"

RESOLVER_VERSION = "le-id-resolver-v1"


# --------------------------------------------------------------------------- planner id codec
# Ported from the archived planner runtime via the project codec
# tools/build_import/src/le_build/codecs/letools_ids.py (letools-id-codec-v1, sha256
# 3b962b5d3281e4ddd1651cb3f3b82bcf8569b50164777d0f9061541c93dd0b86). Kept inline so this
# module is a standalone, reusable resolver.
PLANNER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+-$"
_PLANNER_ALPHABET_INDEX = {char: index for index, char in enumerate(PLANNER_ALPHABET)}


def _lz_decompress(payload: str, bits_per_char: int = 6) -> str | None:
    if payload is None:
        return None
    if payload == "":
        return ""
    length = len(payload)
    reset_value = 1 << (bits_per_char - 1)
    result: list[str] = []
    dictionary: list[str | None] = [None, None, None]
    enlarge_in = 4
    dict_size = 4
    num_bits = 3
    data_val = _PLANNER_ALPHABET_INDEX.get(payload[0], -1)
    data_position = reset_value
    data_index = 1

    def read_bits(count: int) -> int:
        nonlocal data_val, data_position, data_index
        bits = 0
        power = 1
        max_power = 1 << count
        while power != max_power:
            bit = data_val & data_position
            data_position >>= 1
            if data_position == 0:
                data_position = reset_value
                data_val = _PLANNER_ALPHABET_INDEX.get(payload[data_index], -1) if data_index < length else 0
                data_index += 1
            if bit > 0:
                bits |= power
            power <<= 1
        return bits

    code = read_bits(2)
    if code == 0:
        character = chr(read_bits(8))
    elif code == 1:
        character = chr(read_bits(16))
    elif code == 2:
        return ""
    else:
        return None
    dictionary.append(character)
    w = character
    result.append(character)
    while True:
        if data_index - 1 > length:
            return ""
        code = read_bits(num_bits)
        if code == 0:
            dictionary.append(chr(read_bits(8)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 1:
            dictionary.append(chr(read_bits(16)))
            code = dict_size
            dict_size += 1
            enlarge_in -= 1
        elif code == 2:
            return "".join(result)
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        if code < len(dictionary) and dictionary[code] is not None:
            entry = dictionary[code]
        elif code == dict_size:
            entry = w + w[0]
        else:
            return None
        result.append(entry)
        dictionary.append(w + entry[0])
        dict_size += 1
        enlarge_in -= 1
        if enlarge_in == 0:
            enlarge_in = 1 << num_bits
            num_bits += 1
        w = entry


def decode_planner_id(encoded_id: str) -> dict:
    """Decode a lastepochtools planner id.  Never raises.

    Marker rules (letools-id-codec-v1):
      * ``A`` -> zero-padded decimal affix source id
      * ``U`` -> 3-digit base subtype id + 3-digit unique id
      * ``I`` -> ``1`` + 3-digit item-type list index + 3-digit base id + uninterpreted tail
    """
    result: dict = {"resolver_version": RESOLVER_VERSION, "encoded_id": encoded_id,
                    "status": "unsupported", "reason": None}
    if not isinstance(encoded_id, str) or len(encoded_id) < 2:
        result.update(status="malformed", reason="id too short")
        return result
    marker, payload_b64 = encoded_id[0], encoded_id[1:]
    result["marker"] = marker
    if marker not in ("A", "U", "I"):
        result.update(reason="unsupported marker %r (known markers: A, U, I)" % marker)
        return result
    payload = _lz_decompress(payload_b64, bits_per_char=6)
    if payload is None:
        result.update(status="malformed", reason="LZ decompression failed")
        return result
    result["payload"] = payload
    if not payload.isdigit():
        result.update(status="malformed", reason="payload is not a decimal digit string")
        return result
    if marker == "A":
        result.update(status="decoded", affix_id=int(payload))
    elif marker == "U":
        if len(payload) < 6:
            result.update(status="malformed", reason="expected subtype id + 3-digit unique id")
            return result
        result.update(status="decoded", base_subtype_id=int(payload[:-3]), unique_id=int(payload[-3:]))
    else:  # I
        if len(payload) < 10 or payload[0] != "1":
            result.update(status="malformed", reason="expected 1 + 3-digit item-type index + 3-digit base id + tail")
            return result
        result.update(status="decoded", item_type_index=int(payload[1:4]), base_id=int(payload[4:7]),
                      tail=payload[7:])
    return result


# --------------------------------------------------------------------------- resolver
def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json_default(value):
    raise TypeError("not JSON serialisable: %r" % (value,))


class Resolver:
    """Loads the generated mapping-*.json tables once and answers id lookups."""

    def __init__(self, mapping_dir: str | Path | None = None):
        self.mapping_dir = Path(mapping_dir) if mapping_dir else MAPPING_DIR
        self._cache: dict[str, dict] = {}
        self.manifest = self._load("mapping-manifest.json")
        self._containers = {entry["id"]: entry for entry in self._load("mapping-containers.json")["entries"]}
        classes = self._load("mapping-classes.json")
        self._class_names = {int(k): v for k, v in classes["classes"].items()}
        self._masteries = {int(k): v for k, v in classes["masteries"].items()}
        self._trees = self._load("mapping-skill-trees.json")["trees"]
        self._abilities = self._load("mapping-skills.json")["abilities"]
        self._skill_nodes = self._load("mapping-skill-tree-nodes.json")["trees"]
        self._passive_nodes = self._load("mapping-passive-nodes.json")["trees"]
        self._weaver_nodes = self._load("mapping-weaver-nodes.json")["nodes"]
        self._affixes = self._load("mapping-affixes.json")["affixes"]
        self._bases = self._load("mapping-item-bases.json")["bases"]
        item_types = self._load("mapping-item-bases.json")["item_types"]
        self._item_types = {int(k): v for k, v in item_types.items()}
        uniques = self._load("mapping-uniques.json")
        self._uniques = uniques["uniques"]
        self._uniques_by_anchor = uniques["by_anchor"]
        blessings = self._load("mapping-blessings.json")
        self._blessings = blessings["blessings"]
        self._timelines = blessings["timelines"]
        self._properties = self._load("mapping-properties.json")["namespaces"]
        self._rarity = self._load("mapping-item-rarity.json")

    # -- internals ---------------------------------------------------------
    def _load(self, name: str) -> dict:
        if name not in self._cache:
            self._cache[name] = json.loads((self.mapping_dir / name).read_text(encoding="utf-8"))
        return self._cache[name]

    @staticmethod
    def _unresolved(kind: str, reason: str, **extra) -> dict:
        return {"status": "unresolved", "kind": kind, "reason": reason, **extra}

    # -- containers --------------------------------------------------------
    def container(self, container_id) -> dict:
        cid = _as_int(container_id)
        entry = self._containers.get(cid)
        if not entry:
            return self._unresolved("container", "ContainerID %r not present in LE.dll enum ContainerID" % (container_id,),
                                    container_id=container_id)
        return {"status": "resolved", "kind": "container", **entry}

    def container_name(self, container_id):
        entry = self.container(container_id)
        return entry.get("name") if entry["status"] == "resolved" else None

    # -- classes / masteries / trees ---------------------------------------
    def class_name(self, source_class_id) -> dict:
        cid = _as_int(source_class_id)
        name = self._class_names.get(cid)
        if name is None:
            return self._unresolved("class", "class index %r not in mapping-classes.json" % (source_class_id,),
                                    source_class_id=source_class_id)
        return {"status": "resolved", "kind": "class", "source_class_id": cid, "name": name}

    def mastery(self, source_class_id, mastery_index) -> dict:
        cid, midx = _as_int(source_class_id), _as_int(mastery_index)
        entry = (self._masteries.get(cid) or {}).get(str(midx))
        if not entry:
            return self._unresolved("mastery", "mastery index %r not found for class %r" % (mastery_index, source_class_id),
                                    source_class_id=source_class_id, mastery_index=mastery_index)
        return {"status": "resolved", "kind": "mastery", "source_class_id": cid, "mastery_index": midx, **entry}

    def class_passive_tree_id(self, source_class_id) -> dict:
        for tree_id, tree in self._trees.items():
            if tree.get("kind") == "passive" and tree.get("class_source_id") == str(_as_int(source_class_id)):
                return {"status": "resolved", "kind": "passive_tree", "source_class_id": _as_int(source_class_id),
                        "tree_id": tree_id, "name": tree.get("name")}
        return self._unresolved("passive_tree", "no passive tree registered for class %r" % (source_class_id,),
                                source_class_id=source_class_id)

    def skill_tree(self, tree_id) -> dict:
        tree = self._trees.get(tree_id)
        if not tree:
            return self._unresolved("skill_tree", "tree id %r not in mapping-skill-trees.json" % (tree_id,),
                                    tree_id=tree_id)
        return {"status": "resolved", "kind": "skill_tree", **tree}

    def ability(self, ability_id) -> dict:
        entry = self._abilities.get(ability_id)
        if not entry:
            return self._unresolved("ability", "ability id %r not in mapping-skills.json" % (ability_id,),
                                    ability_id=ability_id)
        return {"status": "resolved", "kind": "ability", **entry}

    # -- nodes -------------------------------------------------------------
    def skill_tree_node(self, tree_id, node_index) -> dict:
        node = (self._skill_nodes.get(tree_id) or {}).get(str(_as_int(node_index)))
        if not node:
            return self._unresolved("skill_tree_node", "node %r not found in skill tree %r" % (node_index, tree_id),
                                    tree_id=tree_id, node_index=node_index)
        return {"status": "resolved", "kind": "skill_tree_node", "tree_id": tree_id, **node}

    def passive_node(self, class_or_tree_id, node_index) -> dict:
        tree_id = class_or_tree_id
        if isinstance(class_or_tree_id, int) or (isinstance(class_or_tree_id, str) and class_or_tree_id.isdigit()):
            resolved = self.class_passive_tree_id(class_or_tree_id)
            if resolved["status"] != "resolved":
                return resolved
            tree_id = resolved["tree_id"]
        node = (self._passive_nodes.get(tree_id) or {}).get(str(_as_int(node_index)))
        if not node:
            return self._unresolved("passive_node", "node %r not found in passive tree %r" % (node_index, tree_id),
                                    tree_id=tree_id, node_index=node_index)
        return {"status": "resolved", "kind": "passive_node", "tree_id": tree_id, **node}

    def weaver_node(self, node_index) -> dict:
        node = self._weaver_nodes.get(str(_as_int(node_index)))
        if not node:
            return self._unresolved("weaver_node", "weaver node %r not in mapping-weaver-nodes.json" % (node_index,),
                                    node_index=node_index)
        return {"status": "resolved", "kind": "weaver_node", "tree_id": "weaver", **node}

    # -- item identity -----------------------------------------------------
    def base_item(self, base_type_id, sub_type_id) -> dict:
        bt, st = _as_int(base_type_id), _as_int(sub_type_id)
        key = "%s:%s" % (bt, st)
        entry = self._bases.get(key)
        if not entry:
            return self._unresolved("base_item", "no base item for (baseTypeID=%r, subTypeID=%r)" % (base_type_id, sub_type_id),
                                    base_type_id=base_type_id, sub_type_id=sub_type_id)
        return {"status": "resolved", "kind": "base_item", "key": key, **entry}

    def item_type_name(self, base_type_id) -> dict:
        bt = _as_int(base_type_id)
        name = self._item_types.get(bt)
        if name is None:
            return self._unresolved("item_type", "item type index %r unknown" % (base_type_id,), base_type_id=base_type_id)
        return {"status": "resolved", "kind": "item_type", "base_type_id": bt, "name": name}

    def unique(self, unique_id, base_type_id=None, sub_type_id=None) -> dict:
        uid = _as_int(unique_id)
        entry = self._uniques.get(str(uid))
        if not entry:
            return self._unresolved("unique", "unique id %r not in mapping-uniques.json" % (unique_id,),
                                    unique_id=unique_id)
        result = {"status": "resolved", "kind": "unique", **entry}
        sub = _as_int(sub_type_id)
        if sub is not None and base_type_id is not None and (sub not in (entry.get("sub_type_ids") or [])):
            result["status"] = "partial"
            result["reason"] = ("decoded base subtype %r is not one of the unique's allowed subtypes %r "
                                "(name still resolved)" % (sub, entry.get("sub_type_ids")))
        return result

    def uniques_for_base(self, base_type_id, sub_type_id) -> dict:
        key = "%s:%s" % (_as_int(base_type_id), _as_int(sub_type_id))
        ids = self._uniques_by_anchor.get(key) or []
        return {"status": "resolved" if ids else "unresolved", "kind": "uniques_for_base", "base_key": key,
                "unique_ids": ids, "names": [self._uniques[str(i)]["name"] for i in ids]}

    def affix(self, affix_id, tier0=None, tier=None) -> dict:
        aid = _as_int(affix_id)
        entry = self._affixes.get(str(aid))
        if not entry:
            return self._unresolved("affix", "affix id %r not in mapping-affixes.json" % (affix_id,),
                                    affix_id=affix_id)
        result = {"status": "resolved", "kind": "affix",
                  "id": entry["id"], "name": entry["name"], "title": entry["title"], "slot": entry["slot"],
                  "scope": entry["scope"], "properties": entry["properties"],
                  "tier_count": entry["tier_count"]}
        if tier0 is None and tier is not None:
            tier0 = _as_int(tier) - 1
        if tier0 is not None:
            tier0 = _as_int(tier0)
            row = next((t for t in entry["tiers"] if t["tier0"] == tier0), None)
            if not row:
                result.update(status="partial", reason="tier0 %r outside known tier range 0..%d" % (tier0, entry["tier_count"] - 1))
            else:
                result["tier0"] = row["tier0"]
                result["tier"] = row["tier"]
                result["roll_min"] = row["roll_min"]
                result["roll_max"] = row["roll_max"]
                result["level_requirement"] = row["level_requirement"]
                result["text"] = row["text"] or entry["name"]
                result["text_source"] = row["text_source"] or "composed:dataset_affix_display_name"
        return result

    def blessing(self, base_id) -> dict:
        bid = _as_int(base_id)
        entry = self._blessings.get(str(bid))
        if not entry:
            return self._unresolved("blessing", "blessing sub type id %r not in mapping-blessings.json" % (base_id,),
                                    base_id=base_id)
        return {"status": "resolved", "kind": "blessing", **entry}

    def timeline(self, timeline_id) -> dict:
        entry = self._timelines.get(str(_as_int(timeline_id)))
        if not entry:
            return self._unresolved("timeline", "timeline id %r unknown" % (timeline_id,), timeline_id=timeline_id)
        return {"status": "resolved", "kind": "timeline", **entry}

    # -- rarity ------------------------------------------------------------
    def rarity_byte_name(self, rarity_byte) -> dict:
        name = self._rarity["rarity_byte"].get(str(_as_int(rarity_byte)))
        if name is None:
            return self._unresolved("rarity_byte", "rarity byte %r not in 0..9" % (rarity_byte,), rarity_byte=rarity_byte)
        return {"status": "resolved", "kind": "rarity_byte", "rarity_byte": _as_int(rarity_byte), "name": name}

    def display_rarity(self, decoded_item: dict) -> dict:
        """Game-displayed rarity (derived rule: see mapping-item-rarity.json)."""
        item = decoded_item or {}
        rarity = _as_int(item.get("rarity"))
        unique_id = _as_int(item.get("unique", item.get("unique_id")))
        affixes = item.get("affixes") or []
        base_type = _as_int(item.get("base", item.get("base_type_id")))
        no_exalted = set(range(25, 34)) | {34, 41}
        if unique_id is not None:
            if affixes:
                return {"status": "resolved", "kind": "display_rarity", "name": "Legendary",
                        "basis": "unique with extra affixes"}
            if rarity == 8:
                return {"status": "resolved", "kind": "display_rarity", "name": "Set", "basis": "rarity byte 8"}
            return {"status": "resolved", "kind": "display_rarity", "name": "Unique", "basis": "unique id present"}
        if base_type not in no_exalted and any(_as_int(a.get("tier0", (a.get("tier") or 1) - 1)) is not None
                                               and _as_int(a.get("tier0", (a.get("tier") or 1) - 1)) >= 5
                                               for a in affixes):
            return {"status": "resolved", "kind": "display_rarity", "name": "Exalted",
                    "basis": "an affix has tier0 >= 5 (displayed T6+)"}
        count = len(affixes)
        name = "Rare" if count >= 3 else "Magic" if count else "Normal"
        return {"status": "resolved", "kind": "display_rarity", "name": name,
                "basis": "affix count %d (rule from szefo94 lesave.py rarity_name)" % count}

    def property_name(self, property_id) -> dict:
        pid = _as_int(property_id)
        for scope, names in self._properties.items():
            if str(pid) in names:
                return {"status": "resolved", "kind": "property", "namespace": scope, "property": pid,
                        "name": names[str(pid)]}
        return self._unresolved("property", "property id %r not in mapping-properties.json" % (property_id,),
                                property_id=property_id)

    # -- planner encoded ids ----------------------------------------------
    def resolve_planner_id(self, encoded_id: str) -> dict:
        decoded = decode_planner_id(encoded_id)
        result = {"status": decoded["status"], "kind": "planner_id", "decode": decoded}
        if decoded["status"] != "decoded":
            result["reason"] = decoded.get("reason")
            return result
        marker = decoded["marker"]
        if marker == "A":
            resolved = self.affix(decoded["affix_id"])
            resolved["tier"] = None
        elif marker == "U":
            resolved = self.unique(decoded["unique_id"], sub_type_id=decoded["base_subtype_id"])
        else:
            if decoded["item_type_index"] == 34:
                resolved = self.blessing(decoded["base_id"])
            else:
                resolved = self.base_item(decoded["item_type_index"], decoded["base_id"])
        result["resolution"] = resolved
        result["status"] = resolved["status"]
        return result

    # -- decoded item description -----------------------------------------
    def describe_decoded_item(self, decoded_item: dict) -> dict:
        """Resolve the numeric fields of one decoded item into readable identity.

        Accepted field names (aliases in parentheses): base (base_type_id), sub (sub_type_id),
        rarity, unique (unique_id), affixes [{id, tier0|tier, roll, sealed, corrupted}].
        """
        item = decoded_item or {}
        base_type = _as_int(item.get("base", item.get("base_type_id")))
        sub_type = _as_int(item.get("sub", item.get("sub_type_id")))
        unique_id = _as_int(item.get("unique", item.get("unique_id")))
        rarity_byte = _as_int(item.get("rarity"))
        out: dict = {
            "status": "resolved",
            "kind": "decoded_item",
            "input": {"base_type_id": base_type, "sub_type_id": sub_type, "rarity": rarity_byte,
                      "unique_id": unique_id, "affix_count": len(item.get("affixes") or [])},
            "unresolved": [],
        }
        if rarity_byte is not None:
            out["rarity_byte"] = self.rarity_byte_name(rarity_byte)
        out["display_rarity"] = self.display_rarity(item)
        base = self.base_item(base_type, sub_type)
        out["base"] = base
        if base["status"] != "resolved":
            out["unresolved"].append({"field": "base", "base_type_id": base_type, "sub_type_id": sub_type})
        if base_type == 34:
            out["blessing"] = self.blessing(sub_type)
        if unique_id is not None:
            out["unique"] = self.unique(unique_id, base_type, sub_type)
            if out["unique"]["status"] == "unresolved":
                out["unresolved"].append({"field": "unique", "unique_id": unique_id})
        affixes = []
        for index, affix in enumerate(item.get("affixes") or []):
            aid = _as_int(affix.get("id", affix.get("affix_id")))
            tier0 = affix.get("tier0")
            if tier0 is None and affix.get("tier") is not None:
                tier0 = _as_int(affix.get("tier")) - 1
            resolved = self.affix(aid, tier0=tier0)
            resolved["index"] = index
            resolved["roll"] = affix.get("roll")
            resolved["sealed"] = bool(affix.get("sealed", False))
            resolved["corrupted"] = bool(affix.get("corrupted", False))
            affixes.append(resolved)
            if resolved["status"] == "unresolved":
                out["unresolved"].append({"field": "affix", "index": index, "affix_id": aid})
        out["affixes"] = affixes
        out["status"] = "partial" if out["unresolved"] else "resolved"
        if out["unresolved"]:
            out["reason"] = "%d field(s) could not be resolved" % len(out["unresolved"])
        return out

    # -- item byte layout (reference decoder) ------------------------------
    # Layout for ItemData serialisation version 6, reconstructed from two independent public
    # implementations (szefo94/last-epoch-save-editor lesave.py::decode and IgorBabayan/
    # le-save-editor ItemDataReader.cs) and validated against build o3ZRm9my:
    #   [0]=version, [1..4]=instance id (v>=3), [5]=baseTypeID, [6]=subTypeID, [7]=rarity byte,
    #   [8]=flags (0x10 corrupted), [9..11]=implicit rolls, pos=12;
    #   rarity in (7,8,9): uniqueId big-endian (2 bytes), 8 unique roll bytes, then
    #     rarity 9: affix count byte (low nibble; 0x80 -> last affix corrupted), 3 bytes/affix;
    #     rarity 7/8: tail byte (legendary potential);
    #   else: forging potential byte, affix count byte (low nibble; 0x80 -> first sealed,
    #     0x40 -> corruption), 3 bytes/affix: head=(tier0<<4|affixId>>8), affixId&0xFF, roll.
    @staticmethod
    def decode_item_bytes(data, decode_affixes: bool = True) -> dict:
        try:
            data = list(data)
            if not data:
                return {"status": "unresolved", "reason": "empty byte array"}
            version = data[0]
            start = 1 if version <= 2 else 5
            # game-faithful non-equippable item layouts (Item.firstNonEquippableItemType = 101)
            if len(data) >= start + 2 and data[start] > 100:
                base = data[start]
                off = start + 1
                wide = base in (101, 105, 107)  # subTypeID stored as uint16 big-endian
                if wide and len(data) > off + 1:
                    sub = ((data[off] & 0xFF) << 8) | (data[off + 1] & 0xFF)
                    consumed = off + 2
                else:
                    sub = data[off]
                    consumed = off + 1
                tail = {"base": base, "sub": sub, "sub_type_id": sub, "sub_type_width": "uint16-be" if wide else "byte",
                        "base_type_id": base, "item_kind": "non-equippable"}
                if base == 105 and len(data) > consumed:
                    tail["memory_affixes"] = data[consumed:min(consumed + 4, len(data))]
                    consumed = min(consumed + 4, len(data))
                return {"status": "resolved" if consumed == len(data) else "partial", "layout":
                        "game-faithful non-equippable item (Item.firstNonEquippableItemType=101)", "version": version,
                        "instance_id": data[1:start], **tail, "rarity": None, "flags": None, "corrupted": False,
                        "implicits": [], "fp": None, "unique": None, "rolls": [], "legendary_potential": None,
                        "affixes": [], "end_pos": consumed, "leftover": data[consumed:],
                        "reason": (None if consumed == len(data) else "%d trailing byte(s) not read by the game loader"
                                   % (len(data) - consumed))}
            if len(data) < start + 8:
                # non-equippable "stack" items use a short form: version, instance id (v>=3),
                # baseTypeID, subTypeID and nothing else (observed on materialsList / keysList /
                # RAGEOFMORDITAS stack items in the user save).  No rarity/affix fields exist.
                if len(data) >= start + 2:
                    return {"status": "resolved", "short_form": True, "version": version,
                            "instance_id": data[1:start], "base": data[start], "sub": data[start + 1],
                            "rarity": None, "flags": None, "corrupted": False, "implicits": [],
                            "fp": None, "unique": None, "rolls": [], "legendary_potential": None,
                            "affixes": [], "end_pos": start + 2, "leftover": data[start + 2:],
                            "layout": "short-form non-equippable item (baseTypeID/subTypeID only)"}
                return {"status": "unresolved", "reason": "byte array too short for ItemData layout",
                        "version": version, "length": len(data)}
            item = {
                "version": version,
                "instance_id": data[1:start],
                "base": data[start],
                "sub": data[start + 1],
                "rarity": data[start + 2],
                "flags": data[start + 3],
                "corrupted": bool(data[start + 3] & 0x10),
                "implicits": data[start + 4:start + 7],
                "fp": None,
                "unique": None,
                "rolls": [],
                "legendary_potential": None,
                "affixes": [],
            }
            pos = start + 7
            if item["rarity"] in (7, 8, 9):
                if len(data) < pos + 2 + 8:
                    return {"status": "unresolved", "reason": "unique item data truncated", "version": version,
                            "length": len(data)}
                item["unique"] = (data[pos] << 8) | data[pos + 1]
                pos += 2
                item["rolls"] = data[pos:pos + 8]
                pos += 8
                if item["rarity"] == 9 and pos < len(data):
                    count_byte = data[pos]
                    pos += 1
                    item["affixes"], pos, item["affix_flags"] = Resolver._read_affixes(data, pos, count_byte, legendary=True)
                elif pos < len(data):
                    item["legendary_potential"] = data[pos]
                    pos += 1
            else:
                if len(data) <= pos:
                    return {"status": "unresolved", "reason": "normal item data truncated", "version": version,
                            "length": len(data)}
                item["fp"] = data[pos]
                pos += 1
                if pos < len(data):
                    count_byte = data[pos]
                    pos += 1
                    item["affixes"], pos, item["affix_flags"] = Resolver._read_affixes(data, pos, count_byte, legendary=False)
            item["end_pos"] = pos
            item["leftover"] = data[pos:]
            item["status"] = "resolved" if pos == len(data) else "partial"
            if pos != len(data):
                item["reason"] = "%d unparsed trailing byte(s)" % (len(data) - pos)
            if not decode_affixes:
                item.pop("affixes", None)
            return item
        except (IndexError, TypeError, ValueError) as exc:
            return {"status": "unresolved", "reason": "decode error: %s" % exc}

    @staticmethod
    def _read_affixes(data, pos: int, count_byte: int, legendary: bool):
        count = count_byte & 0x0F
        affixes = []
        flags = {"count_byte": count_byte, "sealed": bool(count_byte & 0x80),
                 "corruption_index": None, "count": count}
        if legendary:
            flags["corruption_index"] = count - 1 if (count_byte & (0x80 | 0x40)) else None
        else:
            sealed_index = 0 if (count_byte & 0x80) else -1
            flags["sealed_index"] = sealed_index
            flags["corruption_index"] = (1 if (count_byte & 0x80) else 0) if (count_byte & 0x40) else None
        for index in range(count):
            if pos + 3 > len(data):
                break
            head, low, roll = data[pos], data[pos + 1], data[pos + 2]
            affix_id = ((head & 0x0F) << 8) | low
            tier0 = (head >> 4) & 0x07
            affix = {"id": affix_id, "tier0": tier0, "tier": tier0 + 1, "roll": roll,
                     "sealed": index == flags.get("sealed_index", -1),
                     "corrupted": index == flags.get("corruption_index")}
            affixes.append(affix)
            pos += 3
        return affixes, pos, flags


# --------------------------------------------------------------------------- CLI
def _print(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default))


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Last Epoch id -> name resolver (read mapping tables)")
    parser.add_argument("kind", nargs="?", help="container|tree|node|passive|weaver|unique|base|affix|blessing|planner|item|rarity|class")
    parser.add_argument("args", nargs="*", help="ids; node/passive/weaver take <tree|class> <index>")
    parser.add_argument("--tier0", type=int, default=None, help="affix tier0 for kind=affix")
    parser.add_argument("--mapping-dir", default=None)
    args = parser.parse_args(argv)
    resolver = Resolver(args.mapping_dir)
    kind = args.kind
    if not kind:
        print("resolver version:", RESOLVER_VERSION)
        print("mapping version:", resolver.manifest.get("mapping_version"))
        print("mapping dir:", resolver.mapping_dir)
        return 0
    values = args.args
    if kind == "container":
        result = resolver.container(values[0]) if values else resolver._unresolved("container", "missing id")
    elif kind == "class":
        result = resolver.class_name(values[0]) if values else resolver._unresolved("class", "missing id")
    elif kind == "tree":
        result = resolver.skill_tree(values[0]) if values else resolver._unresolved("tree", "missing id")
    elif kind == "ability":
        result = resolver.ability(values[0]) if values else resolver._unresolved("ability", "missing id")
    elif kind == "node":
        result = resolver.skill_tree_node(values[0], values[1]) if len(values) >= 2 else resolver._unresolved("node", "usage: node <tree> <index>")
    elif kind == "passive":
        result = resolver.passive_node(values[0], values[1]) if len(values) >= 2 else resolver._unresolved("passive", "usage: passive <class|tree> <index>")
    elif kind == "weaver":
        result = resolver.weaver_node(values[0]) if values else resolver._unresolved("weaver", "missing id")
    elif kind == "unique":
        result = resolver.unique(values[0]) if values else resolver._unresolved("unique", "missing id")
    elif kind == "base":
        result = resolver.base_item(values[0], values[1]) if len(values) >= 2 else resolver._unresolved("base", "usage: base <baseTypeID> <subTypeID>")
    elif kind == "affix":
        result = resolver.affix(values[0], tier0=args.tier0) if values else resolver._unresolved("affix", "missing id")
    elif kind == "blessing":
        result = resolver.blessing(values[0]) if values else resolver._unresolved("blessing", "missing id")
    elif kind == "planner":
        result = resolver.resolve_planner_id(values[0]) if values else resolver._unresolved("planner", "missing encoded id")
    elif kind == "rarity":
        result = resolver.display_rarity({"rarity": values[0]}) if values else resolver._unresolved("rarity", "missing byte")
    elif kind == "item":
        data = json.loads(values[0]) if values and values[0].strip().startswith("[") else None
        if data is None:
            result = resolver._unresolved("item", "usage: item '[byte, ...]' (JSON byte array)")
        else:
            decoded = Resolver.decode_item_bytes(data)
            result = resolver.describe_decoded_item(decoded) if decoded.get("status") != "unresolved" else decoded
    else:
        parser.error("unknown kind %r" % kind)
    _print(result)
    return 0 if result.get("status") == "resolved" else 1


if __name__ == "__main__":
    raise SystemExit(main())
