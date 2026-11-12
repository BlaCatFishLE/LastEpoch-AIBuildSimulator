"""Last Epoch Tools -> canonical schema mapping (phase-1 W3/W4).

This module is loaded by :class:`le_data.normalize.base.Normalizer`.  It turns
one ``SourceRecord`` into one :class:`EntityPlan` (entity + names + typed rows
+ field evidence + issues).  Cross-entity rows whose foreign keys point at
entities produced by *other* plans (tree rows, node rows, mastery rows, set
memberships, node edges, all entity relationships) are written in
:func:`post_process`, after every entity exists.

Identity
--------
``make_entity_id(kind, "letools", record.source_scope, record.source_id)`` is
used everywhere, so ids are stable and never derived from names or row order.
Scopes that matter: skill tree id for nodes, ``classInfoForSources`` for
classes, ``class:<classId>`` for masteries, ``itemList.<group>:<baseTypeId>``
for item bases, the map name for relation records.

Translations
------------
The adapter emits ``entity_kind="translation"`` records; the Normalizer turns
them into a ``key -> english string`` dict passed through ``configure``.
Every display name is resolved through it (``nameKey``/``descriptionKey``/
``statNameKey``/``affixDisplayNameKey``/...).  A missing key never drops the
entity: the key text is used as a provisional name plus a ``missing_translation``
issue.

Known undecoded encodings are kept as raw payload and reported, never guessed:
* ability/node stat ``property`` ids (composed bitmask space, no decoder in the
  data bundle), so ``effect_values.stat_id`` stays NULL and the raw property id
  is kept;
* item mod opaque keys (``AKw...``) referenced by ``itemDB.rel*`` and
  ``planner.itemSkill*/itemAilmentSources``: the mod key itself is preserved as
  an unresolved relationship source until the site's key codec is researched.

Buff id space (phase-1 review P1-1/P1-6)
----------------------------------------
``planner.skillBonuses[..].buffs`` / ``passiveBonuses[..].buffs`` /
``masteryBuffs`` are ``{treeNodeId: [{id, mods: [{prop, value, ...}]}]}``.
The ``id`` values are **window.BuffId** enum values (0..94), which are *not*
``UIBuffs.id`` (the UI list index) and not ``UIBuffIds`` either.  Resolution is
verified by name, never by number:

    BuffId enum value -> enum name -> UIBuffIds[name] -> UIBuffs record id

Example (verified against the 1.5.2 snapshot): ``gl14`` (Glacier) node 13 has
``{"id": 60}``; ``BuffId`` maps 60 to ``Rime``; ``UIBuffIds['Rime'] == 10`` and
``UIBuffs:10`` is Rime.  The number 60 is *not* UIBuffs:60 (Deliverance).
``mods[].prop`` is a **BuffPropId** per-buff parameter index, decoded through the
``BuffPropId`` enum (``"<BuffIdName>_<ParamName>" -> index``), e.g.
``Rime_Duration: 2`` / ``Rime_IncreasedEffect: 3``.

``UIBuffs.maxInstances`` is only the UI display value: it is stored in
``buffs.ui_max_instances`` (``0`` is *not* "uncapped").  Mechanic stack caps and
durations are only written when node/bonus evidence exists
(``max_stacks_basis``/``duration_basis = 'node_text'``).

Equipment semantics (phase-1 review P2-1)
-----------------------------------------
``items.gear_category``/``equip_slot`` are derived from the source's own
item-type record (``item_type`` window: translated display name + equippable
group + ``type``); idols/blessings keep going through ``domain_reshape``.
``affixes.affix_slot`` is derived from the source's binary ``affix.type``: the
LE Tools frontend initialises ``0==type`` into the prefix collection and
``1==type`` into the suffix collection (verified in the site bundle and against
the translated title position), so prefix/suffix is no longer guessed from the
single/multi source category.
"""
from __future__ import annotations

import hashlib
import json
import re

from ..records import SourceRecord
from ..store import insert_row
from .base import EntityPlan, issue_id, make_entity_id, relation_id

SOURCE = "letools"
MAPPING_REVISION = "2"

# Damage array order used by the LE Tools/Tunklab data.  Verified against the
# ability descriptions in the same snapshot: Hungering Souls ("necrotic damage")
# has damage[4], void abilities (Abyssal Orb, Void Rift) have damage[5], and
# Ignite (fire) has damage[1].  NOTE: this is the damage[7] vector order, which
# is NOT the AT tag enum order.
DAMAGE_ARRAY_ORDER = (
    "Physical", "Fire", "Cold", "Lightning", "Necrotic", "Void", "Poison",
)

# Source kinds that deliberately do not become entities of their own.
SKIP_KINDS = {
    # provenance / duplicate evidence
    "duplicate_window",
    "version",
    "version_history",
    "enum",              # enumeration payload; decoded into tags below
    # pure indices / config payloads (complete raw stays in source_records)
    "ability_id_index",
    "item_config",
    "property_config",
    "skill_config",
    "ability_config",
    "planner_config",
    "quest_config",
    "faction_config",
    "endgame_config",
    "skill_tree_ui",     # consumed by post_process (node layout)
    "passive_tree_ui",
    "weaver_tree_ui",
    # handled as entity_relationships in post_process
    "relation_candidate",
}

_CTX: dict = {}
_TRANSLATIONS: dict[str, str] = {}
_ENUMS: dict[str, dict] = {}
_ABILITY: dict[str, dict] = {}          # ability id -> {nameKey, internalName, index}
_ABILITY_IDS: set[str] = set()
_PLAYER_ABILITY_IDS: set[str] = set()
_AILMENT_IDS: set[str] = set()
_CLASS_TREE_BY_CLASS_ID: dict[str, str] = {}
_TAGS_BY_BIT: list[tuple[int, str]] = []

# -- buff id spaces (review P1-1 / P1-6) ------------------------------------
_BUFF_ID_BY_VALUE: dict[int, str] = {}          # BuffId enum value -> name
_UIBUFF_ID_BY_NAME: dict[str, int] = {}         # UIBuffIds name -> UIBuffs id
_BUFF_PROPS_BY_NAME: dict[str, dict[int, str]] = {}  # BuffId name -> {prop index: param}
_BUFF_INFOS: dict[str, dict] = {}               # UIBuffs id -> name/internal/desc
_BUFF_NAME_INDEX: dict[str, list[str]] = {}     # lowercase display name -> UIBuffs ids

# -- equipment semantics (review P2-1) --------------------------------------
_ITEM_TYPES: dict[str, dict] = {}               # base type id -> name/group/is_weapon
_CLASS_NAMES: dict[str, str] = {}               # class id -> class display name
_PASSIVE_TREE_NAMES: dict[str, str] = {}        # passive tree id -> display name
_PROPERTY_DISPLAY: dict[str, dict] = {}         # property id -> name/display unit hints


# =========================================================================
# configure / helpers
# =========================================================================
def configure(context: dict) -> None:
    global _CTX, _TRANSLATIONS, _ENUMS, _ABILITY, _ABILITY_IDS
    global _PLAYER_ABILITY_IDS, _AILMENT_IDS, _CLASS_TREE_BY_CLASS_ID, _TAGS_BY_BIT
    global _BUFF_ID_BY_VALUE, _UIBUFF_ID_BY_NAME, _BUFF_PROPS_BY_NAME
    global _BUFF_INFOS, _BUFF_NAME_INDEX, _ITEM_TYPES, _CLASS_NAMES, _PASSIVE_TREE_NAMES
    global _PROPERTY_DISPLAY
    _CTX = context
    _TRANSLATIONS = context.get("translations") or {}
    conn = context["conn"]
    _ENUMS = _load_enums(conn)
    _ABILITY = _load_ability_index(conn)
    _ABILITY_IDS = set(_ABILITY)
    _PLAYER_ABILITY_IDS = _load_id_list(conn, "playerAbilityIdList")
    _AILMENT_IDS = {row[0] for row in conn.execute(
        "SELECT source_id FROM source_records WHERE source=? AND entity_kind='ailment'",
        (SOURCE,),
    )}
    _CLASS_TREE_BY_CLASS_ID = _load_class_tree_index(conn)
    # BuffId / UIBuffIds / BuffPropId enum spaces and the UIBuffs display table.
    _BUFF_ID_BY_VALUE = {
        int(value): str(name)
        for name, value in (_ENUMS.get("BuffId") or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    _UIBUFF_ID_BY_NAME = {
        str(name): int(value)
        for name, value in (_ENUMS.get("UIBuffIds") or {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    _BUFF_PROPS_BY_NAME = {}
    for name, value in (_ENUMS.get("BuffPropId") or {}).items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        buff_name, _, prop_name = str(name).partition("_")
        if buff_name and prop_name:
            _BUFF_PROPS_BY_NAME.setdefault(buff_name, {})[int(value)] = prop_name
    _BUFF_INFOS, _BUFF_NAME_INDEX = _load_buff_index(conn)
    _ITEM_TYPES = _load_item_types(conn)
    _CLASS_NAMES, _PASSIVE_TREE_NAMES = _load_class_names(conn)
    _PROPERTY_DISPLAY = _load_property_display(conn)
    at = _ENUMS.get("AT") or {}
    _TAGS_BY_BIT = sorted(
        ((int(bit), name) for name, bit in at.items() if isinstance(bit, int) and bit > 0),
        key=lambda pair: pair[0],
    )
    _ensure_tag_rows(conn)


def _load_enums(conn) -> dict[str, dict]:
    enums: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='enum'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover - source_records always exists in pipeline
        return enums
    for row in rows:
        try:
            payload = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            enums[row["source_id"]] = payload
    return enums


def _load_ability_index(conn) -> dict[str, dict]:
    index: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='ability'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover
        return index
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        index[str(row["source_id"])] = {
            "nameKey": raw.get("nameKey"),
            "internalName": raw.get("internalName"),
            "index": raw.get("index"),
        }
    return index


def _load_id_list(conn, source_id: str) -> set[str]:
    try:
        row = conn.execute(
            "SELECT raw_payload FROM source_records WHERE source=? AND entity_kind='ability_id_index' AND source_id=?",
            (SOURCE, source_id),
        ).fetchone()
    except Exception:  # pragma: no cover
        return set()
    if row is None:
        return set()
    payload = json.loads(row["raw_payload"])
    values = payload.get(source_id) if isinstance(payload, dict) else None
    return {str(value) for value in (values or []) if value} if isinstance(values, list) else set()


def _load_class_tree_index(conn) -> dict[str, str]:
    index: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='passive_tree'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover
        return index
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        character_class = ((raw or {}).get("characterTree") or {}).get("characterClass") or {}
        class_id = character_class.get("classID")
        tree_id = character_class.get("treeID")
        if class_id is not None and tree_id:
            index[str(class_id)] = str(tree_id)
    return index


def _load_buff_index(conn) -> tuple[dict[str, dict], dict[str, list[str]]]:
    """UIBuffs display table: id -> {name, internal, description, name_key}.

    Also builds a lowercase display-name index used by the node-text buff
    matcher (review P1-1/P1-6).  Names shorter than 4 characters are indexed but
    only matched in strong contexts (see ``_buff_mentions_for_node``).
    """
    infos: dict[str, dict] = {}
    name_index: dict[str, list[str]] = {}
    try:
        rows = conn.execute(
            "SELECT source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='buff'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover - source_records always exists in pipeline
        return infos, name_index
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        uid = str(raw.get("id", row["source_id"]))
        key = raw.get("displayNameKey") or ""
        internal = key
        for prefix in ("Ailments.NonAilment_", "Ailments.Ailment_", "Ailments."):
            if internal.startswith(prefix):
                internal = internal[len(prefix):]
                break
        internal = re.sub(r"_(Name|Description)$", "", internal)
        name = _tr(key)
        description = _tr(raw.get("descriptionKey"))
        infos[uid] = {
            "id": raw.get("id"),
            "internal": internal,
            "name": name,
            "description": description,
            "name_key": key,
            "internal_name_key": key,
        }
        if name:
            name_index.setdefault(name.lower(), []).append(uid)
    return infos, name_index


def _load_item_types(conn) -> dict[str, dict]:
    """Base type id -> source item-type record (translated name, group, weapon flag)."""
    types: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT source_scope, source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='item_type'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover
        return types
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        base_type = raw.get("baseTypeId")
        if base_type is None:
            base_type = row["source_id"]
        scope = str(row["source_scope"])
        group = "equippable" if scope.endswith("equippable") else (
            "nonEquippable" if scope.endswith("nonEquippable") else "unknown"
        )
        types[str(base_type)] = {
            "base_type": str(base_type),
            "name": _tr(raw.get("displayNameKey")),
            "name_key": raw.get("displayNameKey"),
            "group": group,
            "is_weapon": raw.get("isWeapon"),
            "source_type": raw.get("type"),
            "scope": scope,
        }
    return types


def _load_class_names(conn) -> tuple[dict[str, str], dict[str, str]]:
    """class id -> display name and passive tree id -> '<class> Passive Tree'."""
    classes: dict[str, str] = {}
    trees: dict[str, str] = {}
    try:
        rows = conn.execute(
            "SELECT entity_kind, raw_payload FROM source_records WHERE source=? "
            "AND entity_kind IN ('class','passive_tree')",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover
        return classes, trees
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        if row["entity_kind"] == "class":
            character_class = raw.get("characterClass") or {}
            class_id = character_class.get("classID")
            name = _tr(character_class.get("classNameKey"))
            if class_id is not None and name:
                classes[str(class_id)] = name
        else:
            character_tree = raw.get("characterTree") or {}
            character_class = character_tree.get("characterClass") or {}
            tree_id = character_class.get("treeID")
            name = _tr(character_class.get("classNameKey"))
            if tree_id and name:
                trees[str(tree_id)] = f"{name} Passive Tree"
    return classes, trees


def _load_property_display(conn) -> dict[str, dict]:
    """property id -> display name / percent-display flags (for item property units).

    Used by the affix-property rows so units are taken from the source property
    table instead of guessed from the value magnitude.
    """
    displays: dict[str, dict] = {}
    try:
        rows = conn.execute(
            "SELECT source_scope, source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='property'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover
        return displays
    for row in rows:
        try:
            raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        scope = str(row["source_scope"])
        if scope == "coreDB.abilityPropertyList":
            continue  # nested ability property rows are not primary property ids
        key = raw.get("property")
        if key is None:
            key = row["source_id"]
        displays[str(key)] = {
            "property": key,
            "name": _tr(raw.get("propertyNameKey")),
            "name_key": raw.get("propertyNameKey"),
            "display_as_percentage": bool(raw.get("displayAddedAsPercentage")),
            "display_as_percentage_of": bool(raw.get("displayAsPercentageOf")),
            "round_added_to_int": raw.get("roundAddedToInt"),
            "unit": _stat_unit(raw),
            "scope": scope,
        }

    # specialTag-specific display names are not present on the raw property row.
    # Single-property affixes carry the concrete display name for the exact
    # (property, specialTag) combination (e.g. property 43 + specialTag 1 =
    # "Fire Penetration with Ignite" in 1.5.2), so derive the mapping from the
    # archived affix definitions (source-faithful, no hard-coded names).
    try:
        affix_rows = conn.execute(
            "SELECT source_scope, source_id, raw_payload FROM source_records "
            "WHERE source=? AND entity_kind='affix'",
            (SOURCE,),
        ).fetchall()
    except Exception:  # pragma: no cover - defensive
        affix_rows = []
    for row in affix_rows:
        try:
            affix_raw = json.loads(row["raw_payload"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(affix_raw, dict):
            continue
        affix_props = affix_raw.get("affixProperties")
        if not isinstance(affix_props, list) or len(affix_props) != 1:
            continue
        prop = affix_props[0]
        if not isinstance(prop, dict):
            continue
        try:
            property_id = int(prop.get("property"))
        except (TypeError, ValueError):
            continue
        try:
            special_tag = int(prop.get("specialTag") or 0)
        except (TypeError, ValueError):
            special_tag = 0
        if special_tag == 0:
            continue
        # Only tag-neutral, single-effect affixes describe the property itself;
        # tag-scoped variants (e.g. "Bow Damage") would mislabel the generic stat.
        try:
            if int(prop.get("tags") or 0) != 0 or int(prop.get("extraTag") or 0) != 0:
                continue
        except (TypeError, ValueError):
            continue
        display_key = prop.get("modDisplayNameKey") or affix_raw.get("affixDisplayNameKey")
        display = (_tr(display_key) or "").strip()
        if not display:
            continue
        entry = displays.setdefault(str(property_id), {"property": property_id})
        tag_map = entry.setdefault("special_tag_display", {})
        existing = tag_map.get(str(special_tag))
        if existing is not None and existing != display:
            # more than one single-affix name for the same (property, specialTag):
            # do not guess, keep the key unresolved for the display layer.
            entry.setdefault("special_tag_display_conflicts", []).append(
                {"special_tag": special_tag, "names": sorted({existing, display})}
            )
            continue
        tag_map[str(special_tag)] = display
        entry.setdefault("special_tag_display_source", []).append(
            {
                "affix_source_id": str(row["source_id"]),
                "source_scope": str(row["source_scope"]),
                "display_name_key": display_key,
                "special_tag": special_tag,
            }
        )
    for entry in displays.values():
        if not entry.get("special_tag_display"):
            entry.pop("special_tag_display", None)
    return displays


def _buff_resolve(raw_buff_id) -> dict:
    """Resolve a planner buff id through the three id spaces, by name.

    ``BuffId`` value -> ``BuffId`` name -> ``UIBuffIds`` name -> ``UIBuffs.id``.
    Returns a dict with every intermediate value and the canonical buff entity
    id (None when the BuffId name has no UIBuffs entry).
    """
    result = {
        "raw_buff_id": raw_buff_id,
        "buff_id_name": None,
        "uibuff_id": None,
        "buff_entity_id": None,
    }
    try:
        numeric = int(raw_buff_id)
    except (TypeError, ValueError):
        return result
    buff_name = _BUFF_ID_BY_VALUE.get(numeric)
    result["buff_id_name"] = buff_name
    if not buff_name:
        return result
    uibuff_id = _UIBUFF_ID_BY_NAME.get(buff_name)
    result["uibuff_id"] = uibuff_id
    if uibuff_id is not None:
        result["buff_entity_id"] = _entity_id("buff", "UIBuffs", uibuff_id)
    return result


def _buff_prop_name(buff_id_name: str | None, prop_index) -> str | None:
    if not buff_id_name:
        return None
    try:
        index = int(prop_index)
    except (TypeError, ValueError):
        return None
    return (_BUFF_PROPS_BY_NAME.get(buff_id_name) or {}).get(index)


def _buff_internal_name(uibuff_id) -> str | None:
    info = _BUFF_INFOS.get(str(uibuff_id))
    return info.get("internal") if info else None


def _ensure_tag_rows(conn) -> None:
    for bit, name in _TAGS_BY_BIT:
        tag_id = f"at:{name}"
        try:
            insert_row(
                conn,
                "tags",
                {
                    "tag_id": tag_id,
                    "name": name,
                    "semantics": "damage/mechanic tag bitmask from LE Tools AT enum",
                    "raw_value": str(bit),
                    "extensions": json.dumps({"bit": bit, "enum": "AT"}),
                },
                or_ignore=True,
            )
        except Exception:  # pragma: no cover - tags table always migrated
            return


def _tr(key: str | None) -> str | None:
    if not key:
        return None
    value = _TRANSLATIONS.get(str(key))
    if not isinstance(value, str) or not value:
        return None
    # The site's translation JSON encodes apostrophes as doubled single quotes
    # ("Erza''s Ledger"); that is an encoding artifact, not part of the name.
    return value.replace("''", "'")


def _entity_id(kind: str, scope: str, source_id) -> str:
    return make_entity_id(kind, SOURCE, str(scope), str(source_id))


def _skill_id(ability_id) -> str:
    return _entity_id("skill", "LEAbilities.abilityList", ability_id)


def _ailment_id(ailment_id) -> str:
    return _entity_id("ailment", "LEAbilities.ailmentList", ailment_id)


def _class_id(class_id) -> str:
    return _entity_id("class", "classInfoForSources", class_id)


def _mastery_id(class_id, mastery_index) -> str:
    return _entity_id("mastery", f"class:{class_id}", f"{class_id}.{mastery_index}")


def _node_id(tree_scope, node_id) -> str:
    return _entity_id("node", str(tree_scope), node_id)


def _minion_id(source_id) -> str:
    return _entity_id("minion", "LEAbilities.entityList", source_id)


def _tree_id(tree_kind: str, tree_id) -> str:
    scope = {"skill": "LESkillTrees", "passive": "LECharTrees.trees", "weaver": "LEWeaverTree"}[tree_kind]
    return _entity_id("tree", scope, tree_id)


def _evidence(
    record: SourceRecord,
    field_path: str,
    *,
    raw_value=None,
    normalized_value=None,
    unit=None,
    status: str = "known",
    method: str = "parsed",
    notes: str | None = None,
) -> dict:
    return {
        "field_path": field_path,
        "raw_value": raw_value,
        "normalized_value": normalized_value,
        "unit": unit,
        "extraction_method": method,
        "evidence_status": status,
        "notes": notes,
        "locator": record.locator,
    }


def _issue(
    record: SourceRecord,
    issue_type: str,
    severity: str,
    message: str,
    *,
    field_path: str | None = None,
    expected: str | None = None,
    actual: str | None = None,
) -> dict:
    return {
        "issue_type": issue_type,
        "severity": severity,
        "message": message,
        "field_path": field_path or "",
        "expected": expected,
        "actual": actual,
        "locator": record.locator,
    }


def _plan(
    record: SourceRecord,
    *,
    kind: str,
    scope: str | None = None,
    source_id: str | None = None,
    name: str | None = None,
    name_key: str | None = None,
    status: str = "active",
    extensions: dict | None = None,
) -> EntityPlan:
    scope = scope if scope is not None else record.source_scope
    sid = source_id if source_id is not None else record.source_id
    plan = EntityPlan(
        source_record=record,
        kind=kind,
        entity_id=_entity_id(kind, scope, sid),
        canonical_name=name,
        record_status=status,
        extensions=extensions or {},
    )
    if name:
        plan.names.append(
            {"language": "en", "name": name, "is_alias": False, "name_key": name_key}
        )
    return plan


def _resolve_name(
    record: SourceRecord,
    plan: EntityPlan,
    raw: dict,
    *,
    key_fields: tuple[str, ...],
    fallback: str | None = None,
    field_path: str = "nameKey",
) -> str:
    """Resolve the first present translation key; fallback to key text + issue."""
    for key_field in key_fields:
        key = raw.get(key_field)
        if not key:
            continue
        translated = _tr(key)
        if translated:
            plan.canonical_name = translated
            plan.names.append(
                {"language": "en", "name": translated, "is_alias": False, "name_key": key}
            )
            plan.evidence.append(
                _evidence(record, f"{key_field}->name", raw_value=key, normalized_value=translated)
            )
            return translated
        # key exists but has no translation: keep the text, report it.
        plan.issues.append(
            _issue(
                record,
                "missing_translation",
                "warning",
                f"translation key {key!r} not found in the translation resources",
                field_path=key_field,
                expected="translation entry",
                actual="missing",
            )
        )
        plan.canonical_name = fallback or str(key)
        plan.names.append(
            {"language": "en", "name": fallback or str(key), "is_alias": False, "name_key": key}
        )
        plan.evidence.append(
            _evidence(
                record, f"{key_field}->name", raw_value=key,
                normalized_value=plan.canonical_name, status="unparsed",
                method="key_fallback", notes="translation missing; key text used as provisional name",
            )
        )
        return plan.canonical_name
    if fallback:
        plan.canonical_name = fallback
        plan.names.append({"language": "en", "name": fallback, "is_alias": False})
        # internal generated abilities legitimately have no display name; that is
        # expected and recorded as absent evidence rather than an issue per row.
        if record.entity_kind != "ability":
            plan.issues.append(
                _issue(
                    record,
                    "missing_name_key",
                    "info",
                    "source object has no translatable name key; provisional name used",
                    field_path=field_path,
                    actual=fallback,
                )
            )
        plan.evidence.append(
            _evidence(record, field_path, status="absent",
                      notes="source object has no name key; provisional name used")
        )
        return fallback
    return ""


def _description(record: SourceRecord, plan: EntityPlan, raw: dict, key_field: str = "descriptionKey") -> str | None:
    key = raw.get(key_field)
    if not key:
        plan.evidence.append(
            _evidence(record, key_field + "->description", status="absent",
                      notes="source object has no description key")
        )
        return None
    translated = _tr(key)
    if translated:
        plan.evidence.append(
            _evidence(record, key_field + "->description", raw_value=key, normalized_value=translated)
        )
        return translated
    plan.issues.append(
        _issue(record, "missing_translation", "warning",
               f"description translation key {key!r} not found", field_path=key_field,
               expected="translation entry", actual="missing")
    )
    plan.evidence.append(
        _evidence(record, key_field + "->description", raw_value=key, status="unparsed",
                  method="key_fallback", notes="translation missing")
    )
    return None


def _parse_numeric_text(text) -> tuple[float | None, bool]:
    """Return (number, is_percent) from a stat value string like '+15%' / '3'."""
    if text is None:
        return None, False
    if isinstance(text, (int, float)):
        return float(text), False
    match = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(%?)\s*$", str(text))
    if not match:
        return None, "%" in str(text)
    return float(match.group(1)), match.group(2) == "%"


def _decode_tags(tags_raw) -> list[tuple[int, str]]:
    if not isinstance(tags_raw, int) or tags_raw <= 0:
        return []
    decoded = []
    for bit, name in _TAGS_BY_BIT:
        if tags_raw & bit:
            decoded.append((bit, name))
    return decoded


def _tag_rows(plan: EntityPlan, entity_id: str, tags_raw) -> None:
    for bit, name in _decode_tags(tags_raw):
        plan.typed_rows.append(
            ("entity_tags", {
                "entity_id": entity_id,
                "tag_id": f"at:{name}",
                "source": SOURCE,
                "raw_bits": str(bit),
            })
        )
    if isinstance(tags_raw, int):
        plan.evidence.append(
            _evidence(plan.source_record, "tags", raw_value=tags_raw,
                      normalized_value=[name for _, name in _decode_tags(tags_raw)],
                      notes="AT bitmask decoded; raw bitmask stored in skills.tags_raw")
        )


def _damage_from_array(damage_array) -> tuple[str | None, float | None, list[dict]]:
    """Return (damage_type, base_damage, all non-zero entries) for damage[7]."""
    entries = []
    if not isinstance(damage_array, list):
        return None, None, entries
    for index, value in enumerate(damage_array):
        if isinstance(value, (int, float)) and value:
            damage_type = DAMAGE_ARRAY_ORDER[index] if index < len(DAMAGE_ARRAY_ORDER) else None
            entries.append({"index": index, "damage_type": damage_type, "value": value})
    if not entries:
        return None, None, entries
    if len(entries) == 1:
        return entries[0]["damage_type"], float(entries[0]["value"]), entries
    return None, None, entries


def _num(value, cast=float):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return cast(value)
    return None


# =========================================================================
# ability -> skill
# =========================================================================
def map_ability(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    ability_id = str(raw.get("id", record.source_id))
    extensions = {
        "internalName": raw.get("internalName"),
        "index": raw.get("index"),
        "abilitySprite": raw.get("abilitySprite"),
        "skillTreeConversionDamageTags": raw.get("skillTreeConversionDamageTags"),
        "fakeTags": raw.get("fakeTags"),
        "minionTagsDisplay": raw.get("minionTagsDisplay"),
        "areaTagDisplay": raw.get("areaTagDisplay"),
        "as": raw.get("as"),
        "useDelay": raw.get("useDelay"),
        "speedScaler": raw.get("speedScaler"),
        "speedMultiplier": raw.get("speedMultiplier"),
        "maxCharges": raw.get("maxCharges"),
        "chargesGainedPerSecond": raw.get("chargesGainedPerSecond"),
        "minimumManaCost": raw.get("minimumManaCost"),
        "attributeScaling": raw.get("attributeScaling"),
        "levelScaling": raw.get("levelScaling"),
        "comboAbilities": raw.get("comboAbilities"),
        "sharedCooldownAbilities": raw.get("sharedCooldownAbilities"),
        "flags": {
            key: raw.get(key)
            for key in (
                "instantCastForPlayer", "stopMovementOnKeyRelease", "channelled",
                "transform", "companion", "transformHasMovementSkill",
                "countsAsMovementAbility", "movementDuringUse", "comboAbility",
                "comboTimeLimit", "traversalSkill",
            )
            if key in raw
        },
        "altTextKey": raw.get("altTextKey"),
        "skillLoreKey": raw.get("skillLoreKey"),
    }
    is_player = ability_id in _PLAYER_ABILITY_IDS
    if raw.get("channelled"):
        skill_type = "channelled"
    elif raw.get("companion"):
        skill_type = "summon"
    elif raw.get("transform"):
        skill_type = "transform"
    elif raw.get("traversalSkill") or raw.get("countsAsMovementAbility"):
        skill_type = "movement"
    elif is_player:
        skill_type = "active"
    else:
        skill_type = "internal"
    extensions["skill_type_basis"] = {
        "isPlayerAbility": is_player,
        "channelled": raw.get("channelled"),
        "companion": raw.get("companion"),
        "transform": raw.get("transform"),
        "traversalSkill": raw.get("traversalSkill"),
        "countsAsMovementAbility": raw.get("countsAsMovementAbility"),
    }
    plan = _plan(record, kind="skill", name=None, extensions=extensions)
    _resolve_name(
        record, plan, raw,
        key_fields=("nameKey",),
        fallback=raw.get("internalName") or ability_id,
    )
    use_duration = _num(raw.get("useDuration"))
    cast_time_ms = int(round(use_duration * 1000)) if use_duration is not None else None
    mana_cost = _num(raw.get("manaCost"))
    skill_entity_id = plan.entity_id
    plan.typed_rows.append(
        ("skills", {
            "entity_id": skill_entity_id,
            "skill_type": skill_type,
            "class_entity_id": None,
            "mastery_entity_id": None,
            "source_skill_id": ability_id,
            "cast_time_ms": cast_time_ms,
            "mana_cost": mana_cost,
            "cooldown_ms": None,
            "channel_time_ms": None,
            "tags_raw": json.dumps(raw.get("tags"), ensure_ascii=False)
            if raw.get("tags") is not None else None,
            "extensions": json.dumps(extensions, ensure_ascii=False),
        })
    )
    if cast_time_ms is not None:
        plan.evidence.append(
            _evidence(record, "useDuration->cast_time_ms", raw_value=raw.get("useDuration"),
                      normalized_value=cast_time_ms, unit="ms",
                      notes="source unit seconds converted x1000")
        )
    else:
        plan.evidence.append(
            _evidence(record, "useDuration->cast_time_ms", status="absent",
                      notes="source ability has no useDuration")
        )
    if mana_cost is not None:
        plan.evidence.append(
            _evidence(record, "manaCost->mana_cost", raw_value=raw.get("manaCost"),
                      normalized_value=mana_cost)
        )
    else:
        plan.evidence.append(_evidence(record, "manaCost->mana_cost", status="absent"))
    plan.evidence.append(
        _evidence(record, "cooldown->cooldown_ms", status="absent",
                  notes="LE Tools ability object does not expose a cooldown value; "
                        "maxCharges/chargesGainedPerSecond are kept in extensions")
    )
    plan.evidence.append(
        _evidence(record, "channelled->channel_time_ms", status="absent",
                  notes="no separate channel duration supplied; useDuration kept as cast_time_ms")
    )
    _tag_rows(plan, skill_entity_id, raw.get("tags"))
    # skill components (prefab.components)
    prefab = raw.get("prefab") or {}
    components = prefab.get("components") if isinstance(prefab, dict) else None
    if isinstance(components, list):
        for index, component in enumerate(components):
            if not isinstance(component, dict):
                continue
            component_type = component.get("type")
            component_id = f"{skill_entity_id}#component#{index}"
            base_damage_stats = component.get("baseDamageStats") or {}
            damage_array = base_damage_stats.get("damage") if isinstance(base_damage_stats, dict) else None
            damage_type, base_damage, damage_entries = _damage_from_array(damage_array)
            added_scaling = _num(base_damage_stats.get("addedDamageScaling")) if isinstance(base_damage_stats, dict) else None
            added_effectiveness = (
                round(added_scaling * 100, 6) if added_scaling is not None else None
            )
            plan.typed_rows.append(
                ("skill_components", {
                    "component_id": component_id,
                    "skill_entity_id": skill_entity_id,
                    "component_type": component_type,
                    "component_index": index,
                    "locator": record.locator,
                    "damage_type": damage_type,
                    "base_damage": base_damage,
                    "added_damage_effectiveness": added_effectiveness,
                    "config": json.dumps(component, ensure_ascii=False),
                    "extensions": None,
                })
            )
            plan.evidence.append(
                _evidence(record, f"prefab.components[{index}].type", raw_value=component_type,
                          normalized_value=component_type)
            )
            if damage_array:
                plan.evidence.append(
                    _evidence(record, f"prefab.components[{index}].baseDamageStats.damage",
                              raw_value=damage_array,
                              normalized_value=damage_entries,
                              notes="damage[7] array; index->damage type order recorded in "
                                    "mapping_letools.DAMAGE_ARRAY_ORDER")
                )
            if added_scaling is not None:
                plan.evidence.append(
                    _evidence(record, f"prefab.components[{index}].baseDamageStats.addedDamageScaling",
                              raw_value=added_scaling, normalized_value=added_effectiveness,
                              unit="percent_raw",
                              notes="stored as source percent number (100 == 100%)")
                )
    return plan


# =========================================================================
# skill tree / nodes
# =========================================================================
def map_skill_tree(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    tree_id = str(raw.get("treeID", record.source_id))
    ability_id = raw.get("ability")
    nodes = raw.get("nodes") or {}
    extensions = {
        "version": raw.get("version"),
        "ability": ability_id,
        "node_count": len(nodes) if isinstance(nodes, dict) else None,
        "tree_file": "skills bundle window.LESkillTrees",
    }
    plan = _plan(record, kind="tree", source_id=tree_id, extensions=extensions)
    ability_info = _ABILITY.get(str(ability_id)) if ability_id else None
    ability_name = None
    if ability_info:
        ability_name = _tr(ability_info.get("nameKey"))
    if ability_name:
        plan.canonical_name = ability_name
        plan.names.append(
            {"language": "en", "name": ability_name, "is_alias": True,
             "name_key": ability_info.get("nameKey")}
        )
        plan.evidence.append(
            _evidence(record, "ability->tree name", raw_value=ability_id,
                      normalized_value=ability_name,
                      notes="tree display name equals the owning skill's name")
        )
    else:
        plan.canonical_name = f"{tree_id} skill tree"
        plan.names.append({"language": "en", "name": plan.canonical_name, "is_alias": False})
        plan.issues.append(
            _issue(record, "missing_tree_owner_name", "info",
                   f"could not resolve display name for tree owner ability {ability_id!r}",
                   field_path="ability", actual=str(ability_id))
        )
    plan.evidence.append(
        _evidence(record, "nodes", raw_value=f"{len(nodes)} nodes" if isinstance(nodes, dict) else None,
                  normalized_value=len(nodes) if isinstance(nodes, dict) else None,
                  notes="node rows are written in post_process; complete tree raw kept in source_records")
    )
    return plan


def map_passive_tree(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    character_tree = raw.get("characterTree") or {}
    character_class = character_tree.get("characterClass") or {}
    tree_id = str(character_class.get("treeID") or record.source_id)
    nodes = character_tree.get("nodes") or {}
    class_name = _tr(character_class.get("classNameKey"))
    extensions = {
        "thirdMasteryUnlocked": character_tree.get("thirdMasteryUnlocked"),
        "characterClass": character_class,
        "unspentPointsText": character_tree.get("unspentPointsText"),
        "masteryButtons": character_tree.get("masteryButtons"),
        "node_count": len(nodes) if isinstance(nodes, dict) else None,
        "ref": raw.get("ref"),
    }
    plan = _plan(record, kind="tree", source_id=tree_id, extensions=extensions)
    name = f"{class_name} Passive Tree" if class_name else f"{tree_id} passive tree"
    plan.canonical_name = name
    plan.names.append({"language": "en", "name": name, "is_alias": False,
                       "name_key": character_class.get("classNameKey")})
    plan.evidence.append(
        _evidence(record, "characterClass.classNameKey->tree name",
                  raw_value=character_class.get("classNameKey"), normalized_value=name)
    )
    plan.evidence.append(
        _evidence(record, "characterClass.treeID", raw_value=character_class.get("treeID"),
                  normalized_value=tree_id)
    )
    return plan


def map_weaver_tree(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    tree_id = str(raw.get("treeID", "weaver"))
    nodes = raw.get("nodes") or {}
    plan = _plan(record, kind="tree", source_id=tree_id,
                 extensions={"version": raw.get("version"),
                             "node_count": len(nodes) if isinstance(nodes, dict) else None})
    name = "Weaver Tree"
    plan.canonical_name = name
    plan.names.append({"language": "en", "name": name, "is_alias": False})
    plan.evidence.append(
        _evidence(record, "treeID", raw_value=raw.get("treeID"), normalized_value=tree_id)
    )
    plan.issues.append(
        _issue(record, "hardcoded_display_name", "info",
               "weaver tree display name is not carried in the payload; 'Weaver Tree' "
               "taken from window.LEWeaverTreeUI.weaver.name",
               field_path="treeID", actual=tree_id)
    )
    return plan


def _node_plan(record: SourceRecord, tree_kind: str) -> EntityPlan:
    raw = record.raw
    scope = str(record.source_scope)
    node_index = raw.get("id", record.source_id)
    max_points = raw.get("maxPoints")
    related_abilities = raw.get("relatedAbilities") or []
    if max_points == 0:
        node_type = "root"
        node_type_basis = "maxPoints==0"
    elif related_abilities:
        node_type = "notable"
        node_type_basis = "relatedAbilities non-empty"
    else:
        node_type = "normal"
        node_type_basis = "default"
    stats = raw.get("stats") or []
    extensions = {
        "tree_kind": tree_kind,
        "nodeNameKey": raw.get("nodeNameKey"),
        "loreTextKey": raw.get("loreTextKey"),
        "altTextKey": raw.get("altTextKey"),
        "stats_raw": stats,
        "requirements": raw.get("requirements") or [],
        "relatedAbilities": related_abilities,
        "relatedAilments": raw.get("relatedAilments") or [],
        "triggeredAbilities": raw.get("triggeredAbilities") or {},
        "triggeredAilments": raw.get("triggeredAilments") or {},
        "noScalingType": raw.get("noScalingType"),
        "noScalingPointThreshold": raw.get("noScalingPointThreshold"),
        "pointBonusDescriptionKey": raw.get("pointBonusDescriptionKey"),
        "baseLockNode": raw.get("baseLockNode"),
        "lockNode": raw.get("lockNode"),
        "mastery": raw.get("mastery"),
        "masteryRequirement": raw.get("masteryRequirement"),
        "node_type_basis": node_type_basis,
    }
    plan = _plan(record, kind="node", scope=scope, source_id=node_index, extensions=extensions)
    _resolve_name(record, plan, raw, key_fields=("nodeNameKey",),
                  fallback=f"{scope}:{node_index}")
    description = _description(record, plan, raw)
    extensions["description"] = description
    plan.evidence.append(
        _evidence(record, "maxPoints", raw_value=max_points, normalized_value=max_points)
    )
    plan.evidence.append(
        _evidence(record, "node id", raw_value=raw.get("id"), normalized_value=node_index)
    )
    plan.evidence.append(
        _evidence(record, "node_type", raw_value={"maxPoints": max_points,
                                                   "relatedAbilities": related_abilities},
                  normalized_value=node_type, status="inferred", method="inferred",
                  notes=node_type_basis)
    )
    # node stat effects (owner is the node entity itself -> inserted with the plan)
    for stat_index, stat in enumerate(stats):
        if not isinstance(stat, dict):
            continue
        stat_name_key = stat.get("statNameKey")
        stat_name = _tr(stat_name_key)
        value_text = stat.get("value")
        numeric, is_percent = _parse_numeric_text(value_text)
        effect_id = f"{plan.entity_id}#stat#{stat_index}"
        if value_text is None:
            value_kind = None
            normalized = None
            unit = None
        elif is_percent and numeric is not None:
            value_kind = "percent_modifier"
            normalized = round(numeric / 100.0, 8)
            unit = "%"
        elif numeric is not None:
            value_kind = "flat"
            normalized = numeric
            unit = None
        else:
            value_kind = "unparsed"
            normalized = None
            unit = None
        parse_status = "parsed" if numeric is not None else "unparsed"
        plan.typed_rows.append(
            ("effects", {
                "effect_id": effect_id,
                "owner_entity_id": plan.entity_id,
                "owner_component_id": None,
                "source_entity_id": None,
                "effect_category": "stat_mod",
                "raw_text": stat_name or (stat_name_key or str(value_text)),
                "parse_status": parse_status,
                "extensions": json.dumps(
                    {"stat": stat, "stat_name_key": stat_name_key,
                     "stat_name": stat_name, "value_text": value_text},
                    ensure_ascii=False,
                ),
            })
        )
        plan.typed_rows.append(
            ("effect_values", {
                "value_id": effect_id + "#v0",
                "effect_id": effect_id,
                "stat_id": None,
                "value_kind": value_kind,
                "value_numeric": normalized,
                "value_min": None,
                "value_max": None,
                "per_level": None,
                "level": None,
                "tier": None,
                "unit": unit,
                "expression": value_text if numeric is None else None,
                "raw": json.dumps(stat, ensure_ascii=False),
            })
        )
        if numeric is not None:
            plan.evidence.append(
                _evidence(record, f"stats[{stat_index}].value", raw_value=value_text,
                          normalized_value=normalized, unit=unit,
                          notes="percent values normalised to ratio; raw property id kept "
                                "in effect_values.raw because the property space is undecoded")
            )
        elif value_text is None:
            plan.evidence.append(
                _evidence(record, f"stats[{stat_index}].value", status="absent",
                          notes="source stat has no numeric value text; stat definition "
                                "kept in effects.extensions")
            )
        else:
            plan.issues.append(
                _issue(record, "unparsed_stat_value", "warning",
                       f"stat value {value_text!r} is not numeric; kept as expression text",
                       field_path=f"stats[{stat_index}].value", actual=str(value_text))
            )
    requirements = raw.get("requirements") or []
    if requirements:
        plan.evidence.append(
            _evidence(record, "requirements", raw_value=requirements,
                      normalized_value=len(requirements),
                      notes="node_edges rows written in post_process",
                      method="parsed")
        )
    return plan


def map_skill_tree_node(record: SourceRecord) -> EntityPlan:
    return _node_plan(record, "skill")


def map_passive_node(record: SourceRecord) -> EntityPlan:
    return _node_plan(record, "passive")


def map_weaver_node(record: SourceRecord) -> EntityPlan:
    return _node_plan(record, "weaver")


# =========================================================================
# buffs / ailments
# =========================================================================
def map_buff(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    positives = bool(raw.get("positive"))
    name_key = raw.get("displayNameKey")
    ui_id = str(raw.get("id", record.source_id))
    internal = _buff_internal_name(ui_id)
    extensions = {
        "icon": raw.get("icon"),
        "positive": raw.get("positive"),
        "displayNameKey": name_key,
        "descriptionKey": raw.get("descriptionKey"),
        "noFixedDuration": raw.get("noFixedDuration"),
        "newStacksRefreshDuration": raw.get("newStacksRefreshDuration"),
        "source_window": "planner.UIBuffs",
        "uibuffs_id": raw.get("id", record.source_id),
        # cross-check: UIBuffs ids are indices into window.UIBuffIds, which is a
        # different enum from window.BuffId used by skill/passive bonus config.
        "uibuff_ids_name": internal,
        "id_space_note": "UIBuffs.id is the UIBuffIds index; planner buff config ids "
                         "are window.BuffId values resolved by name (see mapping docstring)",
    }
    plan = _plan(record, kind="buff", source_id=raw.get("id", record.source_id),
                 extensions=extensions)
    _resolve_name(record, plan, raw, key_fields=("displayNameKey",),
                  fallback=f"buff_{record.source_id}")
    desc = _description(record, plan, raw)
    if desc:
        extensions["description"] = desc
        plan.extensions = extensions
    max_instances = raw.get("maxInstances")
    ui_max_instances = None
    max_stacks = None
    max_stacks_basis = "unknown"
    if isinstance(max_instances, (int, float)) and not isinstance(max_instances, bool):
        ui_max_instances = int(max_instances)
        max_stacks_basis = "raw_ui"
        if max_instances > 0:
            # A positive UI value is a display cap; it is still only UI-sourced,
            # so the basis stays raw_ui.  Node/bonus evidence may overwrite it
            # with basis=node_text in post_process.
            max_stacks = int(max_instances)
    ui_flags = {
        "noFixedDuration": raw.get("noFixedDuration"),
        "newStacksRefreshDuration": raw.get("newStacksRefreshDuration"),
        "raw_max_instances": max_instances,
        "max_instances_interpretation": (
            "raw_ui" if ui_max_instances == 0 else "numeric"
            if ui_max_instances is not None else "absent"
        ),
        "note": "maxInstances is the UI display value only; 0 is a display marker and "
                "does not imply an unlimited stack rule (review P1-6).",
    }
    plan.typed_rows.append(
        ("buffs", {
            "entity_id": plan.entity_id,
            "buff_category": "buff" if positives else "debuff",
            "duration_ms": None,
            "max_stacks": max_stacks,
            "target_scope": "unknown",
            "lifecycle": json.dumps(ui_flags, ensure_ascii=False),
            "extensions": json.dumps(extensions, ensure_ascii=False),
            "ui_max_instances": ui_max_instances,
            "max_stacks_basis": max_stacks_basis,
            "base_duration_ms": None,
            "duration_basis": "unknown",
            "ui_flags": json.dumps(ui_flags, ensure_ascii=False),
        })
    )
    if ui_max_instances == 0:
        plan.evidence.append(
            _evidence(record, "maxInstances->ui_max_instances", raw_value=max_instances,
                      normalized_value=0, status="known",
                      notes="LE Tools renders maxInstances=0 as '*' in the UI; the 0 is kept "
                            "as a raw UI value in buffs.ui_max_instances and does not "
                            "define a mechanic stack rule (review P1-6)")
        )
        plan.evidence.append(
            _evidence(record, "max_stacks", raw_value=max_instances, normalized_value=None,
                      status="absent",
                      notes="mechanic stack cap is not established by the UI payload; node/"
                            "bonus evidence is required before buffs.max_stacks is filled")
        )
    elif ui_max_instances is not None:
        plan.evidence.append(
            _evidence(record, "maxInstances->max_stacks", raw_value=max_instances,
                      normalized_value=ui_max_instances, status="known", method="inferred",
                      notes="UI display value stored as ui_max_instances and mirrored into "
                            "max_stacks with basis=raw_ui; 1 == non-stacking display")
        )
    else:
        plan.evidence.append(
            _evidence(record, "maxInstances->ui_max_instances", status="absent",
                      notes="UI buff payload has no maxInstances value")
        )
    plan.evidence.append(
        _evidence(record, "duration->base_duration_ms", status="absent",
                  notes="UI buff payload has no duration value; only noFixedDuration flag. "
                        "Node/bonus config evidence is added in post_process when available.")
    )
    plan.evidence.append(
        _evidence(record, "ui_flags", raw_value={
            "noFixedDuration": raw.get("noFixedDuration"),
            "newStacksRefreshDuration": raw.get("newStacksRefreshDuration"),
        }, status="known",
            notes="raw lifecycle flags preserved; they do not by themselves define the "
                  "complete stacking/refresh rule")
    )
    plan.evidence.append(
        _evidence(record, "target_scope", status="absent",
                  notes="source does not state whether the buff applies to player/minion/enemy")
    )
    return plan


def map_ailment(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name_key = raw.get("displayNameKey")
    base_damage_raw = raw.get("baseDamage") or {}
    damage_array = base_damage_raw.get("damage") if isinstance(base_damage_raw, dict) else None
    damage_type, base_damage, damage_entries = _damage_from_array(damage_array)
    duration = _num(raw.get("duration"))
    duration_ms = int(round(duration * 1000)) if duration is not None else None
    max_instances = raw.get("maxInstances")
    tags = raw.get("tags")
    extensions = {
        "internalName": raw.get("internalName"),
        "positive": raw.get("positive"),
        "icon": raw.get("icon"),
        "displayNameKey": name_key,
        "descriptionKey": raw.get("descriptionKey"),
        "tags_raw": tags,
        "tags_decoded": [name for _, name in _decode_tags(tags)],
        "base_damage_array": damage_array,
        "damage_entries": damage_entries,
        "dealsDamage": raw.get("dealsDamage"),
        "dealsAllDamageAtEnd": raw.get("dealsAllDamageAtEnd"),
        "effectOfIncreasedEffectiveness": raw.get("effectOfIncreasedEffectiveness"),
        "buffScalingType": raw.get("buffScalingType"),
        "maxStacksThatApplyBuffs": raw.get("maxStacksThatApplyBuffs"),
        "buffs": raw.get("buffs"),
        "spreads": raw.get("spreads"),
        "spreadRange": raw.get("spreadRange"),
        "spreadDelay": raw.get("spreadDelay"),
        "procsAbilityWhenStacksReached": raw.get("procsAbilityWhenStacksReached"),
        "abilityToProcWhenStacksReached": raw.get("abilityToProcWhenStacksReached"),
        "stacksRequiredForProc": raw.get("stacksRequiredForProc"),
        "curse": raw.get("curse"),
        "movementAilment": raw.get("movementAilment"),
        "mutationType": raw.get("mutationType"),
        "abilityForMutation": raw.get("abilityForMutation"),
    }
    plan = _plan(record, kind="ailment", source_id=raw.get("id", record.source_id),
                 extensions=extensions)
    _resolve_name(record, plan, raw, key_fields=("displayNameKey",),
                  fallback=raw.get("internalName") or f"ailment_{record.source_id}")
    desc = _description(record, plan, raw)
    if desc:
        extensions["description"] = desc
        plan.extensions = extensions
    stack_rule = None
    if isinstance(max_instances, (int, float)) and max_instances > 0:
        stack_rule = json.dumps({"max_instances": max_instances}, ensure_ascii=False)
    plan.typed_rows.append(
        ("ailments", {
            "entity_id": plan.entity_id,
            "damage_type": damage_type,
            "base_damage": base_damage,
            "duration_ms": duration_ms,
            "stack_rule": stack_rule,
            "scaling_tags": json.dumps(
                {"tags_raw": tags, "tags_decoded": [name for _, name in _decode_tags(tags)]},
                ensure_ascii=False),
            "extensions": json.dumps(extensions, ensure_ascii=False),
        })
    )
    if damage_entries:
        plan.evidence.append(
            _evidence(record, "baseDamage.damage", raw_value=damage_array,
                      normalized_value=damage_entries,
                      notes="damage[7] array decoded with DAMAGE_ARRAY_ORDER; multiple "
                            "non-zero entries keep damage_type NULL")
        )
    else:
        plan.evidence.append(
            _evidence(record, "baseDamage.damage", status="absent",
                      notes="ailment deals no damage")
        )
    if duration_ms is not None:
        plan.evidence.append(
            _evidence(record, "duration->duration_ms", raw_value=raw.get("duration"),
                      normalized_value=duration_ms, unit="ms",
                      notes="source unit seconds converted x1000")
        )
    else:
        plan.evidence.append(_evidence(record, "duration->duration_ms", status="absent"))
    if stack_rule:
        plan.evidence.append(
            _evidence(record, "maxInstances->stack_rule", raw_value=max_instances,
                      normalized_value=stack_rule)
        )
    else:
        plan.evidence.append(
            _evidence(record, "maxInstances->stack_rule", status="absent",
                      notes="maxInstances=0 in source (no explicit cap provided)")
        )
    return plan


# =========================================================================
# items / affixes / uniques / sets
# =========================================================================
_EQUIP_GROUP_RE = re.compile(r"^itemList\.(?P<group>[A-Za-z]+):(?P<base_type>\d+)$")

# Review P2-1: game-semantic item classification derived from the source's own
# item-type record (translated display name + equippable group + isWeapon flag),
# not from a hardcoded id table.  Keywords are checked against the translated
# type name; the matched name and id are kept in evidence.
_WEAPON_NAME_WORDS = (
    "axe", "sword", "mace", "dagger", "sceptre", "wand", "bow", "spear",
    "staff", "club", "hammer", "blade",
)
_SLOT_BY_TYPE_WORD = (
    (("helmet", "helm", "casque", "circlet", "cap"), "helm"),
    (("body armor", "body armour", "chest", "armor", "armour", "robe"), "body"),
    (("belt", "sash"), "belt"),
    (("boots", "boot", "greaves"), "boots"),
    (("gloves", "glove", "bracers", "gauntlets"), "gloves"),
    (("quiver", "shield", "catalyst", "tome"), "offhand"),
    (("amulet",), "amulet"),
    (("ring",), "ring"),
    (("relic",), "relic"),
    (("idol",), "idol"),
    (("blessing",), "blessing"),
    (("lens",), "lens"),
)


def _equip_semantics(type_id) -> dict:
    """Derive gear_category / equip_slot from the source item-type record.

    Returns ``{"gear_category", "equip_slot", "item_type_name", "basis"}`` with
    None values when the source item type is unknown (never guessed).
    """
    key = None if type_id is None else str(type_id)
    info = _ITEM_TYPES.get(key) if key is not None else None
    result = {
        "gear_category": None,
        "equip_slot": None,
        "item_type_name": None,
        "item_type_id": key,
        "group": info.get("group") if info else None,
        "basis": "unknown_item_type",
    }
    if not info:
        return result
    name = (info.get("name") or "").strip()
    lowered = name.lower()
    result["item_type_name"] = name or None
    if not name:
        result["basis"] = f"item type {key} has no translated display name"
        return result
    group = info.get("group")
    if group == "nonEquippable":
        result.update(gear_category="other", equip_slot=None,
                      basis=f"source item type {key} '{name}' is in the nonEquippable group")
        return result
    if "altar" in lowered:
        result.update(gear_category="other", equip_slot="idol_altar",
                      basis=f"source item type {key} '{name}' is an idol altar")
        return result
    if "idol" in lowered:
        result.update(gear_category="idol", equip_slot="idol",
                      basis=f"source item type {key} '{name}' is an idol size")
        return result
    if "blessing" in lowered:
        result.update(gear_category="blessing", equip_slot="blessing",
                      basis=f"source item type {key} '{name}' is the blessing base type")
        return result
    if "lens" in lowered:
        result.update(gear_category="lens", equip_slot="lens",
                      basis=f"source item type {key} '{name}' is a lens type")
        return result
    is_weapon = info.get("is_weapon")
    if is_weapon or any(word in lowered for word in _WEAPON_NAME_WORDS):
        result.update(gear_category="weapon", equip_slot="weapon",
                      basis=f"source item type {key} '{name}' is a weapon "
                            f"(isWeapon={is_weapon!r})")
        return result
    for words, slot in _SLOT_BY_TYPE_WORD:
        if any(word in lowered for word in words):
            result.update(gear_category="accessory" if slot in
                          ("offhand", "amulet", "ring", "relic") else "armor",
                          equip_slot=slot,
                          basis=f"source item type {key} '{name}' matched slot keyword "
                                f"{slot!r}")
            return result
    result.update(gear_category="other", equip_slot=None,
                  basis=f"source item type {key} '{name}' has no confirmed slot mapping")
    return result


def _gear_evidence(record: SourceRecord, plan: EntityPlan, semantics: dict,
                   field_prefix: str = "baseTypeId") -> None:
    """Record field evidence for the derived gear category / slot."""
    status = "inferred" if semantics["gear_category"] else "absent"
    plan.evidence.append(
        _evidence(record, f"{field_prefix}->gear_category",
                  raw_value=semantics["item_type_id"],
                  normalized_value=semantics["gear_category"],
                  status=status, method="derived" if status == "inferred" else "parsed",
                  notes=semantics["basis"])
    )
    plan.evidence.append(
        _evidence(record, f"{field_prefix}->equip_slot",
                  raw_value=semantics["item_type_id"],
                  normalized_value=semantics["equip_slot"],
                  status=status, method="derived" if status == "inferred" else "parsed",
                  notes=semantics["basis"])
    )


def _affix_slot_evidence(record: SourceRecord, plan: EntityPlan, raw: dict,
                         extensions: dict) -> str:
    """Derive prefix/suffix from the source binary ``type`` field (review P2-1).

    LE Tools' own frontend initialises the affix collections with
    ``0 == affix.type`` into the prefix map and ``1 == affix.type`` into the
    suffix map; the translated title position agrees (every title beginning with
    "of " is a type==1 affix).  single/multiAffixes stays in ``affix_type``.
    """
    type_value = raw.get("type")
    if type_value in (0, 1):
        slot = "prefix" if type_value == 0 else "suffix"
        plan.evidence.append(
            _evidence(record, "type->affix_slot", raw_value=type_value,
                      normalized_value=slot, status="inferred", method="inferred",
                      notes="source binary affix.type: 0 = prefix collection, "
                            "1 = suffix collection (LE Tools frontend affix init; "
                            "translated title position agrees: all 'of ...' titles are "
                            "type==1). single/multiAffixes is a different dimension and is "
                            "kept in affixes.affix_type")
        )
    else:
        slot = "unknown"
        plan.evidence.append(
            _evidence(record, "type->affix_slot", raw_value=type_value,
                      normalized_value=None, status="absent",
                      notes="source has no recognised binary affix type; prefix/suffix "
                            "not assumed")
        )
    plan.evidence.append(
        _evidence(record, "titleType/affixMorphology",
                  raw_value={"titleType": raw.get("titleType"),
                             "affixMorphology": raw.get("affixMorphology")},
                  status="unparsed",
                  notes="raw classification fields kept in extensions; they describe the "
                        "affix title/pool shape, not the prefix/suffix slot")
    )
    return slot


def map_item_base(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    match = _EQUIP_GROUP_RE.match(str(record.source_scope))
    group = match.group("group") if match else "unknown"
    base_type = match.group("base_type") if match else None
    level_requirement = raw.get("levelRequirement")
    class_requirement = {
        "classRequirement": raw.get("classRequirement"),
        "subClassRequirement": raw.get("subClassRequirement"),
    }
    extensions = {
        "sprite": raw.get("sprite"),
        "subTypeId": raw.get("subTypeId"),
        "itemTags": raw.get("itemTags"),
        "attackRate": raw.get("attackRate"),
        "addedWeaponRange": raw.get("addedWeaponRange"),
        "affixEffectiveness": raw.get("affixEffectiveness"),
        "IMSetTier": raw.get("IMSetTier"),
        "cannotDrop": raw.get("cannotDrop"),
        "cannotEquipOnPickup": raw.get("cannotEquipOnPickup"),
        "isCorruptedSubtype": raw.get("isCorruptedSubtype"),
        "isLegacySubType": raw.get("isLegacySubType"),
        "onlyDropWhenRequiringAffixEffectiveness": raw.get("onlyDropWhenRequiringAffixEffectiveness"),
        "implicits": raw.get("implicits"),
        "uniques": raw.get("uniques"),
        "displayNameKey": raw.get("displayNameKey"),
        "base_type": base_type,
        "equipment_group": group,
    }
    plan = _plan(record, kind="item", extensions=extensions)
    _resolve_name(record, plan, raw, key_fields=("displayNameKey",),
                  fallback=f"item_{base_type}_{raw.get('subTypeId', record.source_id)}")
    semantics = _equip_semantics(base_type)
    extensions["gear_semantics"] = {
        "gear_category": semantics["gear_category"],
        "equip_slot": semantics["equip_slot"],
        "item_type_name": semantics["item_type_name"],
        "basis": semantics["basis"],
    }
    plan.extensions = extensions
    implicit_text = None
    implicits = raw.get("implicits")
    if isinstance(implicits, list) and implicits:
        implicit_text = json.dumps(implicits, ensure_ascii=False)
    plan.typed_rows.append(
        ("items", {
            "entity_id": plan.entity_id,
            "item_category": "base",
            "item_type": semantics["item_type_name"] or group,
            "base_type": base_type,
            "is_unique": 0,
            "level_requirement": level_requirement if isinstance(level_requirement, (int, float)) else None,
            "class_requirement": json.dumps(class_requirement, ensure_ascii=False),
            "implicit_text": implicit_text,
            "extensions": json.dumps(extensions, ensure_ascii=False),
            "is_set": 0,
            "gear_category": semantics["gear_category"],
            "equip_slot": semantics["equip_slot"],
        })
    )
    _gear_evidence(record, plan, semantics)
    if semantics["gear_category"] is None:
        plan.issues.append(
            _issue(record, "unresolved_item_type", "warning",
                   f"could not derive gear category/slot for base type {base_type!r}",
                   field_path="baseTypeId", actual=str(base_type))
        )
    if isinstance(level_requirement, (int, float)):
        plan.evidence.append(
            _evidence(record, "levelRequirement", raw_value=level_requirement,
                      normalized_value=level_requirement)
        )
    # implicit stat effects (kept structured so phase 2 can use them)
    if isinstance(implicits, list):
        for index, implicit in enumerate(implicits):
            if not isinstance(implicit, dict):
                continue
            effect_id = f"{plan.entity_id}#implicit#{index}"
            plan.typed_rows.append(
                ("effects", {
                    "effect_id": effect_id,
                    "owner_entity_id": plan.entity_id,
                    "owner_component_id": None,
                    "source_entity_id": None,
                    "effect_category": "implicit_stat",
                    "raw_text": json.dumps(implicit, ensure_ascii=False),
                    "parse_status": "parsed",
                    "extensions": json.dumps({"implicit": implicit}, ensure_ascii=False),
                })
            )
            plan.typed_rows.append(
                ("effect_values", {
                    "value_id": effect_id + "#v0",
                    "effect_id": effect_id,
                    "stat_id": None,
                    "value_kind": "flat" if implicit.get("type") == 0 else "increased",
                    "value_numeric": implicit.get("implicitValue"),
                    "value_min": implicit.get("implicitValue"),
                    "value_max": implicit.get("implicitMaxValue"),
                    "per_level": None,
                    "level": None,
                    "tier": None,
                    "unit": None,
                    "expression": None,
                    "raw": json.dumps(implicit, ensure_ascii=False),
                })
            )
            plan.evidence.append(
                _evidence(record, f"implicits[{index}]", raw_value=implicit,
                          normalized_value={"value": implicit.get("implicitValue"),
                                            "max": implicit.get("implicitMaxValue")},
                          status="inferred", method="inferred",
                          notes="property id space undecoded; raw property kept")
            )
    return plan


def map_item_type(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    extensions = {
        key: value for key, value in raw.items() if key != "subItems"
    }
    extensions["sub_type_count"] = len(raw.get("subItems") or {}) if isinstance(raw.get("subItems"), dict) else None
    extensions["note"] = "subItems expansion lives in the item_base records"
    plan = _plan(record, kind="item_type", extensions=extensions)
    _resolve_name(record, plan, raw, key_fields=("displayNameKey",),
                  fallback=f"item type {raw.get('baseTypeId', record.source_id)}")
    plan.issues.append(
        _issue(record, "item_type_no_typed_table", "info",
               "schema v1 has no item_types table; category/type/size preserved in entities.extensions",
               field_path="baseTypeId", actual=str(raw.get("baseTypeId")))
    )
    return plan


def map_unique(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    unique_id = raw.get("uniqueId", record.source_id)
    level_requirement = raw.get("levelRequirement")
    extensions = {
        "uniqueId": unique_id,
        "setId": raw.get("setId"),
        "isSetItem": raw.get("isSetItem"),
        "baseTypeId": raw.get("baseTypeId"),
        "subTypeIds": raw.get("subTypeIds"),
        "legendaryType": raw.get("legendaryType"),
        "loreTextKey": raw.get("loreTextKey"),
        "tooltipDescriptions": raw.get("tooltipDescriptions"),
        "tooltipEntries": raw.get("tooltipEntries"),
        "mods": raw.get("mods"),
        "rarityTier": raw.get("rarityTier"),
        "isPreCorrupted": raw.get("isPreCorrupted"),
        "canDropAsLegendary": raw.get("canDropAsLegendary"),
        "droppableLegendaryAffixes": raw.get("droppableLegendaryAffixes"),
        "effectiveLevelForLegendaryPotential": raw.get("effectiveLevelForLegendaryPotential"),
        "displayNameKey": raw.get("displayNameKey"),
        "alternativeSearchNameKey": raw.get("alternativeSearchNameKey"),
    }
    plan = _plan(record, kind="item", source_id=unique_id, extensions=extensions)
    semantics = _equip_semantics(raw.get("baseTypeId"))
    extensions["gear_semantics"] = {
        "gear_category": semantics["gear_category"],
        "equip_slot": semantics["equip_slot"],
        "item_type_name": semantics["item_type_name"],
        "basis": semantics["basis"],
    }
    plan.extensions = extensions
    _resolve_name(record, plan, raw, key_fields=("displayNameKey",),
                  fallback=None) if raw.get("displayNameKey") else None
    if not plan.canonical_name:
        _resolve_name(record, plan, raw,
                      key_fields=("alternativeSearchNameKey",),
                      fallback=f"unique {unique_id}")
    lore_key = raw.get("loreTextKey")
    if lore_key:
        lore = _tr(lore_key)
        if lore:
            extensions["lore_text"] = lore
            plan.evidence.append(
                _evidence(record, "loreTextKey->lore", raw_value=lore_key, normalized_value=lore)
            )
        else:
            plan.issues.append(
                _issue(record, "missing_translation", "warning",
                       f"lore translation key {lore_key!r} not found", field_path="loreTextKey",
                       expected="translation entry", actual="missing")
            )
    plan.typed_rows.append(
        ("items", {
            "entity_id": plan.entity_id,
            "item_category": "unique",
            "item_type": semantics["item_type_name"],
            "base_type": str(raw.get("baseTypeId")) if raw.get("baseTypeId") is not None else None,
            "is_unique": 1,
            "level_requirement": level_requirement if isinstance(level_requirement, (int, float)) else None,
            "class_requirement": None,
            "implicit_text": None,
            "extensions": json.dumps(extensions, ensure_ascii=False),
            "is_set": 1 if raw.get("isSetItem") else 0,
            "gear_category": semantics["gear_category"],
            "equip_slot": semantics["equip_slot"],
        })
    )
    # review P2-1: is_set comes from the raw unique flag; set membership rows are
    # still written in post_process.  gear_category/equip_slot come from the
    # source item-type record of baseTypeId (Calamity: baseTypeId 0 -> Helmet).
    plan.evidence.append(
        _evidence(record, "isSetItem->is_set", raw_value=raw.get("isSetItem"),
                  normalized_value=1 if raw.get("isSetItem") else 0, status="known",
                  method="parsed",
                  notes="source unique isSetItem flag; item_category stays 'unique' so the "
                        "source category is preserved")
    )
    _gear_evidence(record, plan, semantics)
    if semantics["gear_category"] is None:
        plan.issues.append(
            _issue(record, "unresolved_item_type", "warning",
                   f"could not derive gear category/slot for unique base type "
                   f"{raw.get('baseTypeId')!r}",
                   field_path="baseTypeId", actual=str(raw.get("baseTypeId")))
        )
    if isinstance(level_requirement, (int, float)):
        plan.evidence.append(
            _evidence(record, "levelRequirement", raw_value=level_requirement,
                      normalized_value=level_requirement)
        )
    mods = raw.get("mods") or []
    if isinstance(mods, list):
        for index, mod in enumerate(mods):
            if not isinstance(mod, dict):
                continue
            effect_id = f"{plan.entity_id}#mod#{index}"
            mod_type = mod.get("type")
            prior = mod.get("property")
            plan.typed_rows.append(
                ("effects", {
                    "effect_id": effect_id,
                    "owner_entity_id": plan.entity_id,
                    "owner_component_id": None,
                    "source_entity_id": None,
                    "effect_category": "item_mod",
                    "raw_text": json.dumps(
                        {k: mod.get(k) for k in ("value", "maxValue", "property", "type", "tags")},
                        ensure_ascii=False),
                    "parse_status": "parsed",
                    "extensions": json.dumps({"mod": mod}, ensure_ascii=False),
                })
            )
            plan.typed_rows.append(
                ("effect_values", {
                    "value_id": effect_id + "#v0",
                    "effect_id": effect_id,
                    "stat_id": None,
                    "value_kind": "flat" if mod_type == 0 else ("increased" if mod_type == 1 else "unknown"),
                    "value_numeric": mod.get("value"),
                    "value_min": mod.get("value"),
                    "value_max": mod.get("maxValue"),
                    "per_level": None,
                    "level": None,
                    "tier": None,
                    "unit": None,
                    "expression": None,
                    "raw": json.dumps(mod, ensure_ascii=False),
                })
            )
            plan.evidence.append(
                _evidence(record, f"mods[{index}]", raw_value=mod,
                          normalized_value={"value": mod.get("value"),
                                            "max": mod.get("maxValue"),
                                            "mod_type": mod_type,
                                            "property": prior},
                          status="inferred", method="inferred",
                          notes="mod type 0/1 interpreted as flat/increased; property id space undecoded")
            )
    if raw.get("isSetItem") and raw.get("setId"):
        # set membership row is written in post_process once set entities exist
        extensions["set_membership_pending"] = True
        plan.extensions = extensions
    return plan


def map_set_bonus(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    set_id = raw.get("setID", record.source_id)
    extensions = {
        "setID": set_id,
        "setName": raw.get("setName"),
        "nameKey": raw.get("nameKey"),
        "mods": raw.get("mods"),
        "tooltipDescriptions": raw.get("tooltipDescriptions"),
        "tooltipEntries": raw.get("tooltipEntries"),
    }
    plan = _plan(record, kind="set", source_id=set_id, extensions=extensions)
    name_key = raw.get("nameKey")
    if name_key and _tr(name_key):
        _resolve_name(record, plan, raw, key_fields=("nameKey",), fallback=raw.get("setName"))
    else:
        fallback = raw.get("setName") or f"Set {set_id}"
        plan.canonical_name = fallback
        plan.names.append({"language": "en", "name": fallback, "is_alias": False,
                           "name_key": name_key})
        plan.evidence.append(
            _evidence(record, "nameKey->set name", raw_value=name_key,
                      normalized_value=fallback, status="unparsed", method="key_fallback")
        )
        if name_key:
            plan.issues.append(
                _issue(record, "missing_translation", "warning",
                       f"set name translation key {name_key!r} not found",
                       field_path="nameKey", expected="translation entry", actual="missing")
            )
    plan.issues.append(
        _issue(record, "set_bonus_no_typed_table", "info",
               "schema v1 has no set bonus table; bonus mods preserved in entities.extensions",
               field_path="mods", actual=str(len(raw.get("mods") or [])))
    )
    return plan


#: Affix ``modifierType``: 0 adds the value directly (flat), 1 scales the stat
#: (increased).  Derived from the source data itself: e.g. Void Penetration /
#: Fire Resistance are modifierType 0 while Increased Armor / Increased Damage
#: are modifierType 1.  Raw modifierType is always kept; semantics is evidence.
_MODIFIER_TYPE_KINDS = {0: "flat", 1: "increased", 2: "more"}


def _roll_range_for_property(tiers, property_index: int):
    """Tier roll range (min of mins, max of maxes) for one affix property index."""
    pairs = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        rolls = tier.get("rolls") or []
        if property_index >= len(rolls) or not isinstance(rolls[property_index], dict):
            continue
        roll_a = rolls[property_index].get("min")
        roll_b = rolls[property_index].get("max")
        if isinstance(roll_a, (int, float)) and isinstance(roll_b, (int, float)):
            pairs.append((min(roll_a, roll_b), max(roll_a, roll_b)))
    if not pairs:
        return None, None
    return min(pair[0] for pair in pairs), max(pair[1] for pair in pairs)


def _affix_property_rows(record: SourceRecord, plan: EntityPlan, raw: dict,
                         tiers: list, extensions: dict) -> None:
    """Write affixProperties -> effects/effect_values (review P1-2/P2-1).

    The property id + modifiers are source facts; the tier roll ranges come from
    the matching tier roll position.  stat_link later resolves the property id
    against the stat definitions using ``effect_values.raw.property``.
    """
    properties = raw.get("affixProperties")
    if properties is None:
        plan.evidence.append(
            _evidence(record, "affixProperties", status="absent",
                      notes="source affix has no affixProperties list"))
        return
    if not isinstance(properties, list):
        plan.issues.append(
            _issue(record, "unexpected_affix_properties_shape", "warning",
                   f"affixProperties is {type(properties).__name__}, expected list",
                   field_path="affixProperties", actual=str(properties)[:200])
        )
        return
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            plan.issues.append(
                _issue(record, "unexpected_affix_property_shape", "warning",
                       f"affixProperties[{index}] is {type(prop).__name__}, "
                       f"expected object", field_path=f"affixProperties[{index}]")
            )
            continue
        property_id = prop.get("property")
        display = _PROPERTY_DISPLAY.get(str(property_id)) or {}
        modifier_type = prop.get("modifierType")
        value_kind = _MODIFIER_TYPE_KINDS.get(modifier_type, "unknown")
        percent_display = bool(display.get("display_as_percentage")
                               or display.get("display_as_percentage_of"))
        unit = "%" if value_kind == "increased" or percent_display else None
        value_min, value_max = _roll_range_for_property(tiers, index)
        effect_id = f"{plan.entity_id}#affixprop#{index}"
        plan.typed_rows.append(
            ("effects", {
                "effect_id": effect_id,
                "owner_entity_id": plan.entity_id,
                "owner_component_id": None,
                "source_entity_id": None,
                "effect_category": "affix_property",
                "raw_text": display.get("name") or f"property {property_id}",
                "parse_status": "parsed",
                "extensions": json.dumps(
                    {"affix_property": prop, "property": property_id,
                     "property_name": display.get("name"),
                     "property_name_key": display.get("name_key"),
                     "modifierType": modifier_type,
                     "tags": prop.get("tags"),
                     "specialTag": prop.get("specialTag"),
                     "extraTag": prop.get("extraTag"),
                     "setProperty": prop.get("setProperty"),
                     "affixPropertyIndex": index,
                     "raw_affix_id": raw.get("affixId")},
                    ensure_ascii=False),
            })
        )
        plan.typed_rows.append(
            ("effect_values", {
                "value_id": effect_id + "#v0",
                "effect_id": effect_id,
                "stat_id": None,
                "value_kind": value_kind,
                "value_numeric": None,
                "value_min": value_min,
                "value_max": value_max,
                "per_level": None,
                "level": None,
                "tier": None,
                "unit": unit,
                "expression": None,
                "raw": json.dumps(prop, ensure_ascii=False),
            })
        )
        plan.evidence.append(
            _evidence(record, f"affixProperties[{index}]", raw_value=prop,
                      normalized_value={
                          "property": property_id,
                          "property_name": display.get("name"),
                          "value_kind": value_kind,
                          "unit": unit,
                          "tier_value_min": value_min,
                          "tier_value_max": value_max,
                      },
                      status="inferred" if modifier_type in _MODIFIER_TYPE_KINDS
                      else "unparsed",
                      method="inferred",
                      notes="source affix property reference kept for effect->stat "
                            "linking; modifierType 0 interpreted as flat and 1 as "
                            "increased from the source conventions (raw value kept); "
                            "unit from the source property display flags; tier ranges "
                            "from the matching tier roll position")
        )
    plan.evidence.append(
        _evidence(record, "affixProperties", raw_value=f"{len(properties)} properties",
                  normalized_value=len(properties),
                  notes="affix property references written as effect_category=affix_property")
    )


def map_affix(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    affix_id = raw.get("affixId", record.source_id)
    category = str(record.source_scope).split(".")[-1]
    name_key = raw.get("affixDisplayNameKey")
    title_key = raw.get("affixTitleKey")
    tiers = raw.get("tiers") or []
    extensions = {
        "affixId": affix_id,
        "category": category,
        "titleType": raw.get("titleType"),
        "displayCategory": raw.get("displayCategory"),
        "specialAffixType": raw.get("specialAffixType"),
        "affixMorphology": raw.get("affixMorphology"),
        "rollsOn": raw.get("rollsOn"),
        "classSpecificity": raw.get("classSpecificity"),
        "weighting": raw.get("weighting"),
        "rerollChance": raw.get("rerollChance"),
        "forUniqueId": raw.get("forUniqueId"),
        "singleSetAffix": raw.get("singleSetAffix"),
        "standardAffixEffectModifier": raw.get("standardAffixEffectModifier"),
        "t6Compatibility": raw.get("t6Compatibility"),
        "useGeneratedName": raw.get("useGeneratedName"),
        "rarityTier": raw.get("rarityTier"),
        "affixProperties": raw.get("affixProperties"),
        "lootFilterOverrideNameKey": raw.get("lootFilterOverrideNameKey"),
        "specificRerollChances": raw.get("specificRerollChances"),
    }
    plan = _plan(record, kind="affix", source_id=affix_id, extensions=extensions)
    display_name = _tr(name_key) if name_key else None
    title = _tr(title_key) if title_key else None
    if display_name:
        plan.canonical_name = display_name
        plan.names.append({"language": "en", "name": display_name, "is_alias": False,
                           "name_key": name_key})
    elif title:
        plan.canonical_name = title
        plan.names.append({"language": "en", "name": title, "is_alias": True,
                           "name_key": title_key})
    else:
        fallback = f"{category}:{affix_id}"
        plan.canonical_name = fallback
        plan.names.append({"language": "en", "name": fallback, "is_alias": False})
        if name_key:
            plan.issues.append(
                _issue(record, "missing_translation", "warning",
                       f"affix name translation key {name_key!r} not found",
                       field_path="affixDisplayNameKey", expected="translation entry",
                       actual="missing")
            )
    if title:
        extensions["affix_title"] = title
        plan.extensions = extensions
    affix_slot = _affix_slot_evidence(record, plan, raw, extensions)
    extensions["affix_slot"] = affix_slot
    plan.extensions = extensions
    levels = [tier.get("requiredLevel") for tier in tiers if isinstance(tier, dict)]
    plan.typed_rows.append(
        ("affixes", {
            "entity_id": plan.entity_id,
            "affix_type": category,
            "source_affix_id": str(affix_id),
            "affix_title": title,
            "group_id": str(raw.get("group")) if raw.get("group") is not None else None,
            "levels": json.dumps(levels, ensure_ascii=False),
            "can_roll_on": json.dumps(raw.get("canRollOn"), ensure_ascii=False)
            if raw.get("canRollOn") is not None else None,
            "affix_display_name": display_name,
            "extensions": json.dumps(extensions, ensure_ascii=False),
            "affix_slot": affix_slot,
        })
    )
    for tier_index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            continue
        rolls = tier.get("rolls") or []
        first = rolls[0] if rolls and isinstance(rolls[0], dict) else {}
        extra_rolls = rolls[1:] if len(rolls) > 1 else None
        roll_a = first.get("min")
        roll_b = first.get("max")
        inverted = (
            isinstance(roll_a, (int, float)) and isinstance(roll_b, (int, float))
            and roll_a > roll_b
        )
        if inverted:
            # Downside mods: the source orders "min"/"max" by magnitude, so the
            # numeric lower bound is the second value.  Normalise to numeric
            # min/max and keep the source order in evidence/extensions.
            roll_min, roll_max = roll_b, roll_a
        else:
            roll_min, roll_max = roll_a, roll_b
        plan.typed_rows.append(
            ("affix_tiers", {
                "tier_id": f"{plan.entity_id}#tier#{tier_index}",
                "affix_entity_id": plan.entity_id,
                "tier_index": tier_index,
                "roll_min": roll_min,
                "roll_max": roll_max,
                "extra_rolls": json.dumps(extra_rolls, ensure_ascii=False) if extra_rolls else None,
                "level_requirement": tier.get("requiredLevel"),
                "extensions": json.dumps({"tier": tier, "source_inverted_range": inverted},
                                         ensure_ascii=False),
            })
        )
        if inverted:
            plan.evidence.append(
                _evidence(record, f"tiers[{tier_index}].rolls[0]", raw_value=first,
                          normalized_value={"roll_min": roll_min, "roll_max": roll_max},
                          status="known", method="converted",
                          notes="source range is ordered by magnitude for a downside mod; "
                                "normalised to numeric min/max")
            )
    plan.evidence.append(
        _evidence(record, "tiers", raw_value=f"{len(tiers)} tiers",
                  normalized_value=len(tiers),
                  notes="tier_index is 0-based; rolls[0] -> roll_min/roll_max, "
                        "additional rolls kept in affix_tiers.extra_rolls")
    )
    _affix_property_rows(record, plan, raw, tiers, extensions)
    # affix_type keeps the source single/multi category; affix_slot now carries
    # the prefix/suffix dimension derived from the source binary type (review P2-1).
    return plan


# =========================================================================
# class / mastery / stats / minions
# =========================================================================
def map_class(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    character_class = raw.get("characterClass") or {}
    class_id = character_class.get("classID", record.source_id)
    extensions = {
        "thirdMasteryUnlocked": raw.get("thirdMasteryUnlocked"),
        "classNameKey": character_class.get("classNameKey"),
        "characterClass": character_class,
    }
    plan = _plan(record, kind="class", source_id=class_id, extensions=extensions)
    _resolve_name(record, plan, character_class, key_fields=("classNameKey",),
                  fallback=f"class_{class_id}")
    plan.typed_rows.append(
        ("classes", {
            "entity_id": plan.entity_id,
            "source_class_id": str(class_id),
            "unlock_notes": None,
            "extensions": json.dumps(extensions, ensure_ascii=False),
        })
    )
    for field in ("baseHealth", "baseMana", "healthPerLevel", "manaPerLevel",
                  "baseStrength", "baseDexterity", "baseIntelligence",
                  "baseAttunement", "baseVitality", "basicAttackReplacer"):
        if field in character_class:
            plan.evidence.append(
                _evidence(record, f"characterClass.{field}",
                          raw_value=character_class.get(field),
                          normalized_value=character_class.get(field))
            )
    return plan


def map_mastery(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    scope_match = re.match(r"^class:(?P<class_id>.+)$", str(record.source_scope))
    class_id = scope_match.group("class_id") if scope_match else ""
    name = raw.get("name")
    name_key = raw.get("nameKey")
    resolved = _tr(name_key)
    branch_kind, is_base, branch_basis = _mastery_branch_kind(
        raw, record.source_id, class_id, resolved or name
    )
    extensions = {
        "nameKey": name_key,
        "icon": raw.get("icon"),
        "iconLarge": raw.get("iconLarge"),
        "masteryAbility": raw.get("masteryAbility"),
        "abilities": raw.get("abilities"),
        "classId": class_id,
        "branch_kind": branch_kind,
        "is_base": is_base,
    }
    plan = _plan(record, kind="mastery", extensions=extensions,
                 name=resolved or name or f"mastery {record.source_id}")
    if plan.canonical_name:
        plan.names.append({"language": "en", "name": plan.canonical_name,
                           "is_alias": False, "name_key": name_key})
    plan.evidence.append(
        _evidence(record, "name", raw_value=name or name_key,
                  normalized_value=plan.canonical_name)
    )
    plan.evidence.append(
        _evidence(record, "source_mastery_id->branch_kind",
                  raw_value=str(record.source_id),
                  normalized_value={"branch_kind": branch_kind, "is_base": is_base},
                  status="known", method="parsed", notes=branch_basis)
    )
    if name_key and not resolved:
        plan.evidence.append(
            _evidence(record, "nameKey", raw_value=name_key, status="absent",
                      notes="mastery display name is provided directly by the source; "
                            "class name key is separate")
        )
    return plan


def _mastery_branch_kind(raw: dict, source_id, class_id: str,
                         display_name: str | None) -> tuple[str, int, str]:
    """Classify a mastery row as base-class branch or selectable mastery (P2-1).

    Two independent source-backed signals: the mastery ordinal ``N.0`` and the
    display name being equal to the owning class name.
    """
    source_id = str(source_id)
    ordinal = source_id.rsplit(".", 1)[-1]
    class_name = _CLASS_NAMES.get(str(class_id))
    name = display_name or raw.get("name")
    if ordinal == "0":
        return ("base_class", 1,
                f"source mastery id {source_id!r} has ordinal 0 (base class branch)")
    if class_name and name and name.strip().lower() == class_name.strip().lower():
        return ("base_class", 1,
                f"mastery display name {name!r} equals the class name {class_name!r}")
    return ("mastery", 0,
            f"source mastery id {source_id!r} has non-zero ordinal and a name distinct "
            f"from the base class")


def map_property(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    scope = str(record.source_scope)
    source_id = record.source_id
    extensions = dict(raw) if isinstance(raw, dict) else {"value": raw}
    if scope == "coreDB.abilityPropertyList":
        ability_id = raw.get("abilityID")
        ability_info = _ABILITY.get(str(ability_id)) or {}
        ability_name = _tr(ability_info.get("nameKey")) or str(ability_id)
        name = f"{ability_name} properties"
        plan = _plan(record, kind="stat_group", name=name, extensions={
            "abilityID": ability_id,
            "ability_name": ability_name,
            "properties": raw.get("properties"),
        })
        plan.names.append({"language": "en", "name": name, "is_alias": False,
                           "name_key": ability_info.get("nameKey")})
        properties = raw.get("properties") or []
        for index, prop in enumerate(properties):
            if not isinstance(prop, dict):
                continue
            stat_id = _entity_id("stat", scope, f"{ability_id}.{index}")
            prop_name_key = prop.get("propertyNameKey")
            prop_name = _tr(prop_name_key)
            if prop_name_key and not prop_name:
                plan.issues.append(
                    _issue(record, "missing_translation", "warning",
                           f"ability property translation key {prop_name_key!r} not found",
                           field_path=f"properties[{index}].propertyNameKey",
                           expected="translation entry", actual="missing")
                )
            plan.typed_rows.append(
                ("stats", {
                    "stat_id": stat_id,
                    "name": prop_name,
                    "unit": _stat_unit(prop),
                    "description": None,
                    "raw_enum": f"{ability_id}.{index}",
                    "source_scope": scope,
                    "extensions": json.dumps(prop, ensure_ascii=False),
                })
            )
        return plan
    # simple property tables: one stat row per record
    name_key = raw.get("propertyNameKey") if isinstance(raw, dict) else None
    special_tag_display: dict | None = None
    special_tag_display_source: list | None = None
    stats_extensions = raw
    if scope == "coreDB.coreAttributes":
        attribute_name = raw.get("attributeName")
        name = f"attribute_{attribute_name}"
        raw_enum = str(attribute_name)
    elif scope == "coreDB.playerPropertyList":
        name = _tr(name_key) or f"player_property_{source_id}"
        raw_enum = str(source_id)
    elif scope == "coreDB.propertyList":
        name = _tr(name_key) or f"property_{raw.get('property', source_id)}"
        raw_enum = str(raw.get("property", source_id))
        display = _PROPERTY_DISPLAY.get(raw_enum) or {}
        special_tag_display = display.get("special_tag_display")
        special_tag_display_source = display.get("special_tag_display_source")
        if special_tag_display:
            stats_extensions = dict(raw)
            stats_extensions["special_tag_display"] = special_tag_display
            if special_tag_display_source:
                stats_extensions["special_tag_display_source"] = special_tag_display_source
            extensions = dict(extensions)
            extensions["special_tag_display"] = special_tag_display
    elif scope.startswith("coreDB.idolAltarPropertyList"):
        name = _tr(name_key) or f"idol_altar_property_{source_id}"
        raw_enum = str(source_id)
    elif scope == "coreDB.defaultProperty":
        name = "default property display config"
        raw_enum = str(raw.get("property"))
    else:
        name = _tr(name_key) or f"property_{source_id}"
        raw_enum = str(source_id)
    plan = _plan(record, kind="stat", name=name, extensions=extensions)
    plan.names.append({"language": "en", "name": name, "is_alias": False, "name_key": name_key})
    if special_tag_display:
        plan.evidence.append(
            _evidence(
                record,
                "specialTag->displayName (derived from single-property affix definitions)",
                raw_value=special_tag_display,
                normalized_value=special_tag_display,
                status="inferred",
                method="inferred",
                notes="specialTag-specific display names are taken from the "
                      "archived single-property affix rows whose affixProperties "
                      "carry the concrete modDisplayNameKey/affixDisplayNameKey; "
                      "they are not present on the raw property row itself",
            )
        )
    if name_key and not _tr(name_key):
        plan.issues.append(
            _issue(record, "missing_translation", "warning",
                   f"property translation key {name_key!r} not found",
                   field_path="propertyNameKey", expected="translation entry", actual="missing")
        )
    unit = _stat_unit(raw)
    plan.typed_rows.append(
        ("stats", {
            "stat_id": plan.entity_id,
            "name": name,
            "unit": unit,
            "description": None,
            "raw_enum": raw_enum,
            "source_scope": scope,
            "extensions": json.dumps(stats_extensions, ensure_ascii=False)
            if isinstance(stats_extensions, dict)
            else None,
        })
    )
    if name_key:
        plan.evidence.append(
            _evidence(record, "propertyNameKey->name", raw_value=name_key,
                      normalized_value=_tr(name_key))
        )
    if unit:
        plan.evidence.append(
            _evidence(record, "displayAddedAsPercentage->unit",
                      raw_value={k: raw.get(k) for k in
                                 ("displayAddedAsPercentage", "displayAsPercentageOf")},
                      normalized_value=unit, status="inferred", method="inferred",
                      notes="percent/flat display flags interpreted as unit hints")
        )
    return plan


def _stat_unit(raw: dict) -> str | None:
    if not isinstance(raw, dict):
        return None
    if raw.get("displayAddedAsPercentage") or raw.get("displayAsPercentageOf"):
        return "percent"
    return "flat"


def map_minion_entity(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    actor = raw.get("actor") or {}
    actor_data = actor.get("data") or {}
    gen_id = actor_data.get("genId")
    name_key = actor_data.get("nameKey")
    base_stats = {
        "health": actor.get("health"),
        "mana": actor.get("mana"),
        "protection": actor.get("protection"),
        "stats": actor.get("stats"),
        "actor_id": actor_data.get("id"),
        "genId": gen_id,
    }
    extensions = {
        "actor_nameKey": name_key,
        "actor_data": actor_data,
        "remaining_keys": {k: v for k, v in raw.items() if k not in
                           ("actor", "health", "mana", "protection", "stats")},
    }
    plan = _plan(record, kind="minion", source_id=record.source_id, extensions=extensions)
    _resolve_name(record, plan, actor_data, key_fields=("nameKey",),
                  fallback=gen_id or f"minion_{record.source_id}")
    plan.typed_rows.append(
        ("minions", {
            "entity_id": plan.entity_id,
            "minion_type": gen_id,
            "base_stats": json.dumps(base_stats, ensure_ascii=False),
            "inherits": None,
            "extensions": json.dumps(extensions, ensure_ascii=False),
        })
    )
    plan.evidence.append(
        _evidence(record, "actor.data.genId", raw_value=gen_id, normalized_value=gen_id)
    )
    plan.issues.append(
        _issue(record, "minion_inheritance_unknown", "info",
               "source does not state how minion stats inherit from the player",
               field_path="inherits", actual="unknown")
    )
    return plan


# =========================================================================
# generic / auxiliary kinds
# =========================================================================
def map_quest(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name_key = raw.get("displayNameKey")
    name = _tr(name_key) or f"quest {raw.get('id', record.source_id)}"
    plan = _plan(record, kind="quest", name=name, status="provisional",
                 extensions={"raw": raw})
    plan.names.append({"language": "en", "name": name, "is_alias": False, "name_key": name_key})
    plan.typed_rows = []
    for field in ("experienceReward", "goldReward", "passivePointsReward",
                  "attributesReward", "idolUnlockReward", "chapter", "main"):
        if field in raw:
            plan.evidence.append(
                _evidence(record, field, raw_value=raw.get(field), normalized_value=raw.get(field))
            )
    return plan


def map_faction(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name_key = raw.get("factionNameKey")
    name = _tr(name_key) or f"faction {raw.get('factionId', record.source_id)}"
    plan = _plan(record, kind="faction", name=name, status="provisional",
                 extensions={"raw": raw})
    plan.names.append({"language": "en", "name": name, "is_alias": False, "name_key": name_key})
    return plan


def map_generic(record: SourceRecord) -> EntityPlan:
    """Preserve auxiliary sources as provisional entities with complete extensions."""
    raw = record.raw
    kind = record.entity_kind
    plan = _plan(record, kind=kind, status="provisional", extensions={"raw": raw})
    name = None
    for field in ("displayNameKey", "nameKey", "name", "internalName", "id"):
        value = raw.get(field) if isinstance(raw, dict) else None
        if isinstance(value, str) and value:
            translated = _tr(value)
            name = translated or value
            break
    if name is None:
        name = f"{kind} {record.source_id}"
    plan.canonical_name = name
    plan.names.append({"language": "en", "name": name, "is_alias": False})
    plan.issues.append(
        _issue(record, "generic_mapping", "info",
               f"kind {kind!r} has no typed table in schema v1; complete raw kept in extensions",
               actual=kind)
    )
    return plan


# =========================================================================
# skill / passive bonus configs (review P1-1)
# =========================================================================
_BONUS_SECTIONS = ("stats", "damageTags", "mods", "abilityMods", "buffs",
                   "buffAbilities", "masteryBuffs")


def _section_shape(value, section: str) -> tuple[str, dict]:
    if value is None:
        return "absent", {}
    if section == "damageTags" and isinstance(value, (int, float)) \
            and not isinstance(value, bool):
        # damageTags is an AT bitmask scalar, not a mapping; it stays in the raw
        # payload (tags decoded by the node/skill mappers) and is recorded as known.
        return "scalar", {"type": type(value).__name__, "value": value}
    if isinstance(value, (dict, list)):
        return "present", {"type": type(value).__name__, "count": len(value)}
    return "unparsed", {"type": type(value).__name__}


def _bonus_extensions(raw: dict, scope: str) -> dict:
    sections = {}
    for section in _BONUS_SECTIONS:
        shape, detail = _section_shape(raw.get(section), section)
        sections[section] = {"shape": shape, **detail}
    extensions = {
        "source_window": scope,
        "sections": sections,
        "structured_rows": "effects/effect_values/entity_relationships are written in "
                           "post_process (the buff id space is resolved there by name)",
    }
    return extensions


def _bonus_plan(record: SourceRecord, *, kind: str, owner_id: str,
                owner_entity_id: str, owner_name: str, label: str,
                scope: str, extra: dict) -> EntityPlan:
    raw = record.raw
    extensions = _bonus_extensions(raw, scope)
    extensions.update(extra)
    plan = _plan(record, kind=kind, scope=scope, status="provisional",
                 extensions=extensions)
    plan.canonical_name = f"{owner_name} {label}"
    plan.names.append({"language": "en", "name": plan.canonical_name,
                       "is_alias": False})
    plan.evidence.append(
        _evidence(record, "sections",
                  raw_value={name: raw.get(name) is not None for name in _BONUS_SECTIONS},
                  normalized_value=extensions["sections"], status="known",
                  notes="skill/passive bonus record parsed into structured rows in "
                        "post_process; every non-empty buff configuration becomes an "
                        "entity relationship and/or a data issue")
    )
    for section in _BONUS_SECTIONS:
        shape = extensions["sections"][section]["shape"]
        if shape == "present":
            plan.evidence.append(
                _evidence(record, section, raw_value=extensions["sections"][section],
                          status="known",
                          notes="non-empty section; rows in post_process")
            )
        elif shape == "scalar":
            plan.evidence.append(
                _evidence(record, section, raw_value=raw.get(section),
                          normalized_value=extensions["sections"][section],
                          status="known",
                          notes="AT bitmask scalar; kept in the raw payload and decoded "
                                "elsewhere (not a node->value mapping)")
            )
        elif shape == "unparsed":
            plan.issues.append(
                _issue(record, "unexpected_bonus_section_shape", "warning",
                       f"section {section!r} has unsupported type "
                       f"{extensions['sections'][section]['type']!r}",
                       field_path=section, actual=str(raw.get(section))[:200])
            )
    plan.evidence.append(
        _evidence(record, "owner", raw_value=owner_id,
                  normalized_value=owner_entity_id, status="known",
                  notes=f"owning {kind.split('_')[0]} id from the source key")
    )
    return plan


def map_skill_bonus(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    scope = str(record.source_scope)
    ability_id = str(record.source_id)
    partial = scope.endswith("partialSkillBonuses")
    ability_info = _ABILITY.get(ability_id) or {}
    ability_name = _tr(ability_info.get("nameKey")) or ability_id
    return _bonus_plan(
        record,
        kind="skill_bonus",
        owner_id=ability_id,
        owner_entity_id=_skill_id(ability_id),
        owner_name=ability_name,
        label="partial skill bonuses" if partial else "skill bonuses",
        scope=scope,
        extra={
            "ability": ability_id,
            "ability_name": ability_name,
            "partial": partial,
            "skill_entity_id": _skill_id(ability_id)
            if ability_id in _ABILITY_IDS else None,
            "tree_entity_id": _entity_id("tree", "LESkillTrees", ability_id),
            "tree_id_source": "skill tree treeID equals the owning ability id",
        },
    )


def map_passive_bonus(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    scope = str(record.source_scope)
    tree_id = str(record.source_id)
    tree_name = _PASSIVE_TREE_NAMES.get(tree_id) or f"{tree_id} passive tree"
    return _bonus_plan(
        record,
        kind="passive_bonus",
        owner_id=tree_id,
        owner_entity_id=_entity_id("tree", "LECharTrees.trees", tree_id),
        owner_name=tree_name,
        label="passive bonuses",
        scope=scope,
        extra={
            "tree_id": tree_id,
            "tree_name": tree_name,
            "tree_entity_id": _entity_id("tree", "LECharTrees.trees", tree_id),
        },
    )


def map_invocation(record: SourceRecord) -> EntityPlan:
    return map_generic(record)


# =========================================================================
# post_process: deferred rows + relationships + layout + edges
# =========================================================================
def _rows(conn, kinds):
    marks = ",".join("?" for _ in kinds)
    sql = (
        "SELECT entity_kind, source_scope, source_id, locator, raw_payload "
        f"FROM source_records WHERE source=? AND entity_kind IN ({marks})"
    )
    for row in conn.execute(sql, (SOURCE, *kinds)):
        try:
            raw = json.loads(row["raw_payload"])
        except json.JSONDecodeError:
            continue
        yield row["entity_kind"], row["source_scope"], row["source_id"], row["locator"], raw


def _entity_exists(conn, entity_id: str | None) -> bool:
    if not entity_id:
        return False
    return conn.execute(
        "SELECT 1 FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone() is not None


def _insert(conn, table: str, row: dict) -> None:
    insert_row(conn, table, row, or_ignore=True)


def _record_issue(conn, record_locator: str, issue_type: str, severity: str,
                  message: str, *, entity_id: str | None = None,
                  field_path: str | None = None) -> None:
    snapshot_id = _CTX.get("source_snapshot_id", "")
    _insert(conn, "data_issues", {
        "issue_id": issue_id(SOURCE, snapshot_id, issue_type, entity_id or "", field_path or "", message),
        "issue_type": issue_type,
        "severity": severity,
        "source": SOURCE,
        "source_snapshot_id": snapshot_id,
        "entity_id": entity_id,
        "field_path": field_path,
        "message": message,
        "expected": None,
        "actual": None,
        "locator": record_locator,
        "status": "open",
        "created_at": _now(),
    })


def _now() -> str:
    from ..store import now_utc
    return now_utc()


def post_process(conn, context: dict) -> None:
    layout = _node_layout(conn)
    _insert_trees(conn)
    _insert_nodes(conn, layout)
    _insert_masteries(conn)
    _insert_set_memberships(conn)
    _insert_node_edges(conn)
    _apply_skill_class_mastery(conn)
    _update_tree_classes(conn)
    _insert_relationships(conn)
    # review P1-1 / P1-6 / P2-1 follow-ups: these need node/tree/buff/skill
    # entities that only exist after the loops above.
    node_text = _node_text_index(conn)
    bonus_candidates = _insert_bonus_buff_config(conn, node_text)
    node_candidates = _insert_buff_param_evidence(conn, node_text)
    merged: dict = {}
    for source_candidates in (bonus_candidates, node_candidates):
        for buff_entity, entry in source_candidates.items():
            target = merged.setdefault(
                buff_entity, {"duration_ms": {}, "max_stacks": {}, "locators": []})
            for param in ("duration_ms", "max_stacks"):
                for value, count in (entry.get(param) or {}).items():
                    target[param][value] = target[param].get(value, 0) + count
            target["locators"].extend(entry.get("locators") or [])
    _apply_buff_base_candidates(conn, merged)


def _node_layout(conn) -> dict[str, dict]:
    """node entity id -> {rect, nodeSize, tree_id, node_id} from UI source records."""
    layout: dict[str, dict] = {}
    for kind, scope, source_id, locator, raw in _rows(
        conn, ("skill_tree_ui", "passive_tree_ui", "weaver_tree_ui")
    ):
        if kind == "skill_tree_ui" or kind == "weaver_tree_ui":
            containers = [raw]
        else:
            containers = []
            nodes = raw.get("nodes") if isinstance(raw, dict) else None
            if isinstance(nodes, list):
                containers = [entry for entry in nodes if isinstance(entry, dict)]
        for container in containers:
            children = container.get("children") if isinstance(container, dict) else None
            if not isinstance(children, list):
                continue
            for child in children:
                if not isinstance(child, dict):
                    continue
                tree_id = child.get("treeId")
                node_id = child.get("nodeId")
                if tree_id is None or node_id is None:
                    continue
                entity_id = _node_id(tree_id, node_id)
                layout[entity_id] = {
                    "rect": child.get("rect"),
                    "nodeSize": child.get("nodeSize"),
                    "icon": child.get("icon"),
                    "tree_id": tree_id,
                    "node_id": node_id,
                    "locator": locator,
                }
    return layout


def _tree_entity_for_scope(tree_kind: str, scope: str) -> str:
    if tree_kind == "skill":
        return _entity_id("tree", "LESkillTrees", scope)
    if tree_kind == "passive":
        return _entity_id("tree", "LECharTrees.trees", scope)
    return _entity_id("tree", "LEWeaverTree", scope)


def _insert_trees(conn) -> None:
    snapshot_id = _CTX.get("source_snapshot_id", "")
    for kind, scope, source_id, locator, raw in _rows(
        conn, ("skill_tree", "passive_tree", "weaver_tree")
    ):
        if kind == "skill_tree":
            tree_id = str(raw.get("treeID", source_id))
            tree_entity = _entity_id("tree", "LESkillTrees", tree_id)
            ability_id = raw.get("ability")
            owner_id = _skill_id(ability_id) if ability_id and str(ability_id) in _ABILITY_IDS else None
            nodes = raw.get("nodes") or {}
            _insert(conn, "trees", {
                "entity_id": tree_entity,
                "tree_kind": "skill",
                "owner_entity_id": owner_id if _entity_exists(conn, owner_id) else None,
                "class_entity_id": None,
                "source_tree_id": tree_id,
                "node_count": len(nodes) if isinstance(nodes, dict) else None,
                "extensions": json.dumps(
                    {"version": raw.get("version"), "ability": ability_id},
                    ensure_ascii=False),
            })
            if owner_id and _entity_exists(conn, owner_id):
                _add_relationship(
                    conn, owner_id, "has_tree", tree_entity, None, "resolved",
                    locator, {"tree_kind": "skill"})
        elif kind == "passive_tree":
            character_tree = raw.get("characterTree") or {}
            character_class = character_tree.get("characterClass") or {}
            tree_id = str(character_class.get("treeID") or source_id)
            tree_entity = _entity_id("tree", "LECharTrees.trees", tree_id)
            class_id = character_class.get("classID")
            nodes = character_tree.get("nodes") or {}
            _insert(conn, "trees", {
                "entity_id": tree_entity,
                "tree_kind": "passive",
                "owner_entity_id": None,
                "class_entity_id": _class_id(class_id) if class_id is not None
                and _entity_exists(conn, _class_id(class_id)) else None,
                "source_tree_id": tree_id,
                "node_count": len(nodes) if isinstance(nodes, dict) else None,
                "extensions": json.dumps(
                    {"thirdMasteryUnlocked": character_tree.get("thirdMasteryUnlocked"),
                     "classNameKey": character_class.get("classNameKey")},
                    ensure_ascii=False),
            })
        else:
            tree_id = str(raw.get("treeID", "weaver"))
            tree_entity = _entity_id("tree", "LEWeaverTree", tree_id)
            nodes = raw.get("nodes") or {}
            _insert(conn, "trees", {
                "entity_id": tree_entity,
                "tree_kind": "weaver",
                "owner_entity_id": None,
                "class_entity_id": None,
                "source_tree_id": tree_id,
                "node_count": len(nodes) if isinstance(nodes, dict) else None,
                "extensions": json.dumps({"version": raw.get("version")}, ensure_ascii=False),
            })


def _insert_nodes(conn, layout: dict[str, dict]) -> None:
    for kind, scope, source_id, locator, raw in _rows(
        conn, ("skill_tree_node", "passive_node", "weaver_node")
    ):
        tree_kind = {"skill_tree_node": "skill", "passive_node": "passive",
                     "weaver_node": "weaver"}[kind]
        tree_entity = _tree_entity_for_scope(tree_kind, str(scope))
        if not _entity_exists(conn, tree_entity):
            _record_issue(conn, locator, "missing_tree_for_node", "error",
                          f"tree entity {tree_entity} not found for node {source_id}",
                          entity_id=_node_id(scope, raw.get("id", source_id)),
                          field_path="tree_entity_id")
            continue
        node_entity = _node_id(scope, raw.get("id", source_id))
        layout_info = layout.get(node_entity) or {}
        rect = layout_info.get("rect") or [None, None, None, None]
        max_points = raw.get("maxPoints")
        related = raw.get("relatedAbilities") or []
        if max_points == 0:
            node_type = "root"
        elif related:
            node_type = "notable"
        else:
            node_type = "normal"
        description_key = raw.get("descriptionKey")
        description = _tr(description_key)
        extensions = {
            "nodeNameKey": raw.get("nodeNameKey"),
            "descriptionKey": description_key,
            "node_size": layout_info.get("nodeSize"),
            "loreTextKey": raw.get("loreTextKey"),
            "requirements": raw.get("requirements") or [],
            "relatedAbilities": related,
            "relatedAilments": raw.get("relatedAilments") or [],
            "triggeredAbilities": raw.get("triggeredAbilities") or {},
            "triggeredAilments": raw.get("triggeredAilments") or {},
            "noScalingType": raw.get("noScalingType"),
            "noScalingPointThreshold": raw.get("noScalingPointThreshold"),
            "pointBonusDescriptionKey": raw.get("pointBonusDescriptionKey"),
            "baseLockNode": raw.get("baseLockNode"),
            "lockNode": raw.get("lockNode"),
            "mastery": raw.get("mastery"),
            "masteryRequirement": raw.get("masteryRequirement"),
            "has_layout": bool(layout_info),
        }
        _insert(conn, "nodes", {
            "entity_id": node_entity,
            "tree_entity_id": tree_entity,
            "node_type": node_type,
            "max_points": max_points if isinstance(max_points, (int, float)) else None,
            "node_index": raw.get("id"),
            "position_x": rect[0] if len(rect) > 0 else None,
            "position_y": rect[1] if len(rect) > 1 else None,
            "position_w": rect[2] if len(rect) > 2 else None,
            "position_h": rect[3] if len(rect) > 3 else None,
            "description": description,
            "extensions": json.dumps(extensions, ensure_ascii=False),
        })
        # field evidence for the deferred fields (layout from the UI window,
        # requirements from the node object)
        snapshot_id = _CTX.get("source_snapshot_id", "")
        if layout_info:
            _insert(conn, "field_evidence", {
                "entity_id": node_entity,
                "owner_type": "node",
                "owner_id": node_entity,
                "field_path": "layout.rect",
                "source_record_id": None,
                "source": SOURCE,
                "source_snapshot_id": snapshot_id,
                "raw_value": json.dumps(layout_info.get("rect"), ensure_ascii=False),
                "normalized_value": json.dumps(
                    {"x": rect[0] if len(rect) > 0 else None,
                     "y": rect[1] if len(rect) > 1 else None,
                     "w": rect[2] if len(rect) > 2 else None,
                     "h": rect[3] if len(rect) > 3 else None}, ensure_ascii=False),
                "unit": None,
                "extraction_method": "parsed",
                "evidence_status": "known",
                "locator": layout_info.get("locator") or locator,
                "notes": "rect from the tree UI window (LESkillTreesUI / LECharTreesUI / "
                         "LEWeaverTreeUI children)",
            })
        requirements = raw.get("requirements") or []
        _insert(conn, "field_evidence", {
            "entity_id": node_entity,
            "owner_type": "node",
            "owner_id": node_entity,
            "field_path": "requirements",
            "source_record_id": None,
            "source": SOURCE,
            "source_snapshot_id": snapshot_id,
            "raw_value": json.dumps(requirements, ensure_ascii=False),
            "normalized_value": str(len(requirements)) if requirements else None,
            "unit": None,
            "extraction_method": "parsed",
            "evidence_status": "known" if requirements else "absent",
            "locator": locator,
            "notes": "node_edges rows written from this list",
        })


def _insert_masteries(conn) -> None:
    for kind, scope, source_id, locator, raw in _rows(conn, ("mastery",)):
        match = re.match(r"^class:(?P<class_id>.+)$", str(scope))
        class_id = match.group("class_id") if match else None
        class_entity = _class_id(class_id) if class_id is not None else None
        class_name = _CLASS_NAMES.get(str(class_id)) if class_id is not None else None
        branch_kind, is_base, branch_basis = _mastery_branch_kind(
            raw, source_id, str(class_id or ""), raw.get("name"))
        _insert(conn, "masteries", {
            "entity_id": _mastery_id(class_id, str(source_id).split(".")[-1])
            if class_id is not None else _entity_id("mastery", scope, source_id),
            "class_entity_id": class_entity if _entity_exists(conn, class_entity) else None,
            "source_mastery_id": str(source_id),
            "unlock_notes": None,
            "extensions": json.dumps(
                {"name": raw.get("name"), "nameKey": raw.get("nameKey"),
                 "class_name": class_name,
                 "masteryAbility": raw.get("masteryAbility"),
                 "abilities": raw.get("abilities"),
                 "branch_kind": branch_kind, "is_base": is_base,
                 "branch_kind_basis": branch_basis},
                ensure_ascii=False),
            "branch_kind": branch_kind,
            "is_base": is_base,
        })
        _record_evidence(
            conn, _mastery_id(class_id, str(source_id).split(".")[-1])
            if class_id is not None else _entity_id("mastery", scope, source_id),
            "source_mastery_id->branch_kind",
            raw_value={"source_mastery_id": str(source_id),
                       "name": raw.get("name"), "class_name": class_name},
            normalized_value={"branch_kind": branch_kind, "is_base": is_base},
            status="known", method="parsed", locator=locator,
            notes=branch_basis)


def _insert_set_memberships(conn) -> None:
    for kind, scope, source_id, locator, raw in _rows(conn, ("unique",)):
        if not raw.get("isSetItem") or not raw.get("setId"):
            continue
        set_entity = _entity_id("set", "setBonusList", raw.get("setId"))
        item_entity = _entity_id("item", "uniqueList.uniques", raw.get("uniqueId", source_id))
        if not _entity_exists(conn, set_entity):
            _record_issue(conn, locator, "unresolved_set_membership", "warning",
                          f"set entity {set_entity} not present; membership not stored",
                          entity_id=item_entity, field_path="setId")
            continue
        _insert(conn, "set_memberships", {
            "set_entity_id": set_entity,
            "item_entity_id": item_entity,
            "raw": json.dumps({"setId": raw.get("setId"),
                               "uniqueId": raw.get("uniqueId", source_id)},
                              ensure_ascii=False),
        })


def _insert_node_edges(conn) -> None:
    for kind, scope, source_id, locator, raw in _rows(
        conn, ("skill_tree_node", "passive_node", "weaver_node")
    ):
        tree_kind = {"skill_tree_node": "skill", "passive_node": "passive",
                     "weaver_node": "weaver"}[kind]
        tree_entity = _tree_entity_for_scope(tree_kind, str(scope))
        to_entity = _node_id(scope, raw.get("id", source_id))
        requirements = raw.get("requirements") or []
        if not isinstance(requirements, list):
            continue
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            required_node = requirement.get("nodeId")
            required_points = requirement.get("requirement")
            from_entity = _node_id(scope, required_node) if required_node is not None else None
            resolved = _entity_exists(conn, from_entity)
            edge_key = f"{tree_entity}|{from_entity}|{to_entity}|node_points"
            _insert(conn, "node_edges", {
                "edge_id": "edge-" + hashlib.sha256(edge_key.encode("utf-8")).hexdigest()[:24],
                "tree_entity_id": tree_entity,
                "from_node_entity_id": from_entity if resolved else None,
                "to_node_entity_id": to_entity if _entity_exists(conn, to_entity) else None,
                "requirement_type": "node_points",
                "points_required": required_points if isinstance(required_points, int) else None,
                "raw": json.dumps(requirement, ensure_ascii=False),
            })
            if not resolved:
                _record_issue(conn, locator, "node_edge_unresolved_source", "warning",
                              f"requirement node {required_node} not found in tree {scope}",
                              entity_id=to_entity, field_path="requirements")


def _apply_skill_class_mastery(conn) -> None:
    snapshot_id = _CTX.get("source_snapshot_id", "")
    for kind, scope, source_id, locator, raw in _rows(conn, ("relation_candidate",)):
        if scope == "planner.classSkillSources":
            ability_id = raw.get("key")
            target = raw.get("value") or {}
            if not isinstance(target, dict):
                continue
            class_entity = _class_id(target.get("classId"))
            skill_entity = _skill_id(ability_id)
            if _entity_exists(conn, skill_entity) and _entity_exists(conn, class_entity):
                conn.execute(
                    "UPDATE skills SET class_entity_id=? WHERE entity_id=? AND class_entity_id IS NULL",
                    (class_entity, skill_entity),
                )
                _add_relationship(
                    conn, skill_entity, "belongs_to_class", class_entity, None, "resolved",
                    locator, {"reqCharLevel": target.get("reqCharLevel"),
                              "mapping": "classSkillSources"})
        elif scope == "planner.masterySkillSources":
            ability_id = raw.get("key")
            target = raw.get("value") or {}
            if not isinstance(target, dict):
                continue
            class_id = target.get("classId")
            mastery_id = target.get("mastery")
            class_entity = _class_id(class_id)
            mastery_entity = _mastery_id(class_id, mastery_id)
            skill_entity = _skill_id(ability_id)
            if _entity_exists(conn, skill_entity) and _entity_exists(conn, mastery_entity):
                conn.execute(
                    "UPDATE skills SET mastery_entity_id=?, "
                    "class_entity_id=COALESCE(class_entity_id, ?) WHERE entity_id=?",
                    (mastery_entity, class_entity, skill_entity),
                )
                _add_relationship(
                    conn, skill_entity, "belongs_to_mastery", mastery_entity, None, "resolved",
                    locator, {"reqLevel": target.get("reqLevel"),
                              "reqMastery": target.get("reqMastery"),
                              "mapping": "masterySkillSources"})


def _update_tree_classes(conn) -> None:
    conn.execute(
        "UPDATE trees SET class_entity_id = ("
        "  SELECT s.class_entity_id FROM skills s WHERE s.entity_id = trees.owner_entity_id"
        ") WHERE tree_kind='skill' AND class_entity_id IS NULL AND owner_entity_id IS NOT NULL"
    )


# -- relationship helpers ---------------------------------------------------
_REL_TYPES = {
    "planner.itemSkillSources": "skill_has_item_source_candidate",
    "planner.itemSkillLevelSources": "skill_has_item_level_source_candidate",
    "planner.itemAilmentSources": "ailment_has_item_source_candidate",
    "planner.classSkillSources": "belongs_to_class",
    "planner.masterySkillSources": "belongs_to_mastery",
    "planner.passiveSkillSources": "passive_node_modifies_skill",
    "planner.treeSkillSources": "tree_node_modifies_skill",
    "planner.procSkillSources": "triggers_skill",
    "planner.entityProcSkillSources": "skill_procs_on_entity_candidate",
    "planner.transformSkillSources": "transforms_to_skill",
    "planner.masteryBonusSkillSources": "skill_has_mastery_bonus_candidate",
    "planner.ailmentSkillSources": "skill_applies_ailment",
    "planner.passiveAilmentSources": "passive_node_applies_ailment",
    "planner.treeAilmentSources": "tree_node_applies_ailment",
    "planner.skillAilmentSources": "skill_applies_ailment",
    "planner.invocationAilmentSources": "invocation_applies_ailment",
    "planner.ailmentAilmentSources": "ailment_triggers_ailment",
    "planner.masteryBonusAilmentSources": "mastery_bonus_applies_ailment_candidate",
    "itemDB.relIncLevelAbilities": "item_mod_key_increases_skill_level",
    "itemDB.relModAbilities": "item_mod_key_modifies_skill",
    "itemDB.relModAilments": "item_mod_key_applies_ailment",
    "itemDB.triggeredAbilities": "item_mod_key_triggers_skill",
    "itemDB.triggeredAilments": "item_mod_key_triggers_ailment",
    "LEAbilities.ailmentTriggeredAbilities": "ailment_triggers_skill",
    "LEAbilities.ailmentTriggeredAilments": "ailment_triggers_ailment",
}

# maps whose resolved side is a list of ability ids
_ABILITY_TARGET_MAPS = {
    "itemDB.relIncLevelAbilities",
    "itemDB.relModAbilities",
    "itemDB.triggeredAbilities",
}
# maps whose resolved side is a list of ailment ids
_AILMENT_TARGET_MAPS = {
    "itemDB.relModAilments",
    "itemDB.triggeredAilments",
}


def _add_relationship(conn, from_entity, rel_type, to_entity, raw_target_ref,
                      status, locator, qualifiers, hard_status: str | None = None) -> None:
    if not _entity_exists(conn, from_entity):
        from_entity = None
    if not _entity_exists(conn, to_entity):
        to_entity = None
    if hard_status is not None:
        status = hard_status
    elif from_entity is None and to_entity is None:
        status = "unresolved"
    elif from_entity is None or to_entity is None:
        status = "unresolved"
    rid = relation_id(SOURCE, from_entity or "none", rel_type,
                      (to_entity or "none") + "|" + (raw_target_ref or ""))
    _insert(conn, "entity_relationships", {
        "relationship_id": rid,
        "source_snapshot_id": _CTX.get("source_snapshot_id"),
        "source": SOURCE,
        "from_entity_id": from_entity,
        "relationship_type": rel_type,
        "to_entity_id": to_entity,
        "raw_target_ref": raw_target_ref,
        "resolution_status": status,
        "evidence": locator,
        "qualifiers": json.dumps(qualifiers or {}, ensure_ascii=False),
    })


def _node_entity_from_ref(tree_id, node_id) -> str | None:
    return _node_id(tree_id, node_id)


def _insert_relationships(conn) -> None:
    # 1) class info (unlock/known/default/mastery ability) relations
    for kind, scope, source_id, locator, raw in _rows(conn, ("relation_candidate",)):
        if not scope.startswith("classInfoForSources."):
            continue
        class_id = raw.get("classId")
        class_entity = _class_id(class_id)
        entry = raw.get("entry") if isinstance(raw.get("entry"), dict) else raw
        if scope == "classInfoForSources.unlockableAbilities":
            ability_id = entry.get("ability")
            _add_relationship(
                conn, class_entity, "class_provides_skill", _skill_id(ability_id),
                None, "resolved", locator,
                {"required_level": entry.get("level"), "mapping": "classInfoForSources.unlockableAbilities"})
        elif scope == "classInfoForSources.knownAbilities":
            ability_id = raw.get("ability") or entry.get("ability")
            _add_relationship(
                conn, class_entity, "class_known_skill", _skill_id(ability_id),
                None, "resolved", locator,
                {"mapping": "classInfoForSources.knownAbilities"})
        elif scope == "classInfoForSources.defaultAbilities":
            ability_id = raw.get("ability") or entry.get("ability")
            _add_relationship(
                conn, class_entity, "class_default_skill", _skill_id(ability_id),
                None, "resolved", locator,
                {"index": raw.get("index"), "mapping": "classInfoForSources.defaultAbilities"})
        elif scope == "classInfoForSources.startingItems":
            item = raw.get("item") or {}
            base_type = item.get("baseType") if isinstance(item, dict) else None
            sub_type = item.get("subtype") if isinstance(item, dict) else None
            item_entity = _item_base_entity(conn, base_type, sub_type)
            _add_relationship(
                conn, class_entity, "class_starting_item", item_entity,
                json.dumps(item, ensure_ascii=False) if item_entity is None else None,
                "resolved" if item_entity else "unresolved", locator,
                {"mapping": "classInfoForSources.startingItems", "item": item})
        elif scope == "classInfoForSources.masteryAbilities":
            ability_id = entry.get("ability")
            mastery_index = raw.get("mastery")
            mastery_entity = _mastery_id(class_id, mastery_index)
            _add_relationship(
                conn, mastery_entity, "mastery_provides_skill", _skill_id(ability_id),
                None, "resolved", locator,
                {"required_level": entry.get("level"),
                 "mastery_name": raw.get("masteryName"),
                 "mapping": "classInfoForSources.masteryAbilities"})

    # 2) planner/itemDB source maps
    for kind, scope, source_id, locator, raw in _rows(conn, ("relation_candidate",)):
        if scope.startswith("classInfoForSources."):
            continue
        rel_type = _REL_TYPES.get(scope)
        if rel_type is None:
            continue
        key = raw.get("key")
        value = raw.get("value")
        method = "parsed"
        if scope in ("planner.skillAilmentSources", "planner.invocationAilmentSources"):
            # key = ailment id, value = ability ids that apply it
            for ability_id in value or []:
                _add_relationship(
                    conn, _skill_id(ability_id), rel_type, _ailment_id(key), None,
                    "resolved", locator,
                    {"mapping": scope, "inference": "abilities listed apply this ailment",
                     "method": "inferred"})
        elif scope == "planner.ailmentSkillSources":
            # key = ability id, value = numeric ailment ids applied by it
            for ailment in value or []:
                _add_relationship(
                    conn, _skill_id(key), rel_type, _ailment_id(ailment), None,
                    "resolved", locator, {"mapping": scope, "method": "inferred"})
        elif scope == "planner.ailmentAilmentSources":
            # key = ailment id, value = ailment ids it triggers
            for ailment in value or []:
                _add_relationship(
                    conn, _ailment_id(key), rel_type, _ailment_id(ailment), None,
                    "resolved", locator, {"mapping": scope, "method": "inferred"})
        elif scope == "LEAbilities.ailmentTriggeredAbilities" and isinstance(value, dict):
            for target, chance in value.items():
                _add_relationship(
                    conn, _ailment_id(key), rel_type, _skill_id(target), None,
                    "resolved", locator, {"mapping": scope, "chance": chance})
        elif scope in _ABILITY_TARGET_MAPS and isinstance(value, list):
            # itemDB.rel* : key is an opaque item-mod key (unresolved source side),
            # value lists the ability/ailment ids it grants.  Keep the key in
            # raw_target_ref so the unresolved side is never lost.
            raw_ref = json.dumps(
                {"item_mod_key": key, "unresolved_side": "from", "mapping": scope},
                ensure_ascii=False)
            for target in value:
                to_entity = _skill_id(target)
                _add_relationship(
                    conn, None, rel_type, to_entity, raw_ref,
                    "resolved" if _entity_exists(conn, to_entity) else "unresolved",
                    locator, {"mapping": scope, "item_mod_key": key})
        elif scope in _AILMENT_TARGET_MAPS and isinstance(value, list):
            raw_ref = json.dumps(
                {"item_mod_key": key, "unresolved_side": "from", "mapping": scope},
                ensure_ascii=False)
            for target in value:
                to_entity = _ailment_id(target)
                _add_relationship(
                    conn, None, rel_type, to_entity, raw_ref,
                    "resolved" if _entity_exists(conn, to_entity) else "unresolved",
                    locator, {"mapping": scope, "item_mod_key": key})
        elif scope in ("itemDB.triggeredAbilities", "itemDB.triggeredAilments") and isinstance(value, dict):
            raw_ref = json.dumps(
                {"item_mod_key": key, "unresolved_side": "from", "mapping": scope},
                ensure_ascii=False)
            for target, chance in value.items():
                to_entity = (_skill_id(target) if scope.endswith("Abilities")
                             else _ailment_id(target))
                _add_relationship(
                    conn, None, rel_type, to_entity, raw_ref,
                    "resolved" if _entity_exists(conn, to_entity) else "unresolved",
                    locator, {"mapping": scope, "item_mod_key": key, "chance": chance})
        elif scope in ("LEAbilities.ailmentTriggeredAilments",) and isinstance(value, dict):
            for target, chance in value.items():
                _add_relationship(conn, _ailment_id(key), rel_type, _ailment_id(target),
                                  None, "resolved", locator,
                                  {"mapping": scope, "chance": chance})
        elif scope in ("planner.itemSkillSources", "planner.itemSkillLevelSources",
                       "planner.itemAilmentSources") and isinstance(value, list):
            from_entity = (_skill_id(key) if scope.startswith("planner.itemSkill")
                           else _ailment_id(key))
            for mod_key in value:
                _add_relationship(
                    conn, from_entity, rel_type, None,
                    json.dumps({"item_mod_key": mod_key}, ensure_ascii=False),
                    "unresolved", locator,
                    {"mapping": scope, "note": "item mod key codec not decoded"})
        elif scope in ("planner.classSkillSources", "planner.masterySkillSources"):
            continue  # handled in _apply_skill_class_mastery
        elif scope == "planner.passiveSkillSources" and isinstance(value, dict):
            for class_tree_key, node_ids in value.items():
                class_index = str(class_tree_key).split("-")[0]
                tree_id = _CLASS_TREE_BY_CLASS_ID.get(class_index)
                for node_id in node_ids or []:
                    to_entity = _skill_id(key)
                    from_entity = _node_id(tree_id, node_id) if tree_id else None
                    _add_relationship(
                        conn, from_entity, rel_type, to_entity, None,
                        "resolved" if from_entity and _entity_exists(conn, from_entity) else "unresolved",
                        locator, {"mapping": scope, "class_tree": class_tree_key})
        elif scope == "planner.passiveAilmentSources" and isinstance(value, dict):
            for class_tree_key, node_ids in value.items():
                class_index = str(class_tree_key).split("-")[0]
                tree_id = _CLASS_TREE_BY_CLASS_ID.get(class_index)
                for node_id in node_ids or []:
                    from_entity = _node_id(tree_id, node_id) if tree_id else None
                    _add_relationship(
                        conn, from_entity, rel_type, _ailment_id(key), None,
                        "resolved" if from_entity and _entity_exists(conn, from_entity) else "unresolved",
                        locator, {"mapping": scope, "class_tree": class_tree_key})
        elif scope in ("planner.treeSkillSources", "planner.treeAilmentSources") and isinstance(value, dict):
            target_id = _skill_id(key) if scope.endswith("SkillSources") else _ailment_id(key)
            for tree_id, node_ids in value.items():
                for node_id in node_ids or []:
                    from_entity = _node_id(tree_id, node_id)
                    _add_relationship(
                        conn, from_entity, rel_type, target_id, None,
                        "resolved" if _entity_exists(conn, from_entity) else "unresolved",
                        locator, {"mapping": scope, "tree": tree_id})
        elif scope == "planner.procSkillSources" and isinstance(value, list):
            proc_entity = _skill_id(key)
            for base in value:
                _add_relationship(
                    conn, _skill_id(base), "triggers_skill", proc_entity, None,
                    "resolved" if _entity_exists(conn, _skill_id(base)) else "unresolved",
                    locator, {"mapping": scope, "inference": "value ability triggers key proc ability",
                              "method": "inferred"})
        elif scope == "planner.transformSkillSources" and isinstance(value, list):
            variant_entity = _skill_id(key)
            for base in value:
                _add_relationship(
                    conn, _skill_id(base), rel_type, variant_entity, None,
                    "resolved" if _entity_exists(conn, _skill_id(base)) else "unresolved",
                    locator, {"mapping": scope, "method": "inferred"})
        elif scope == "planner.entityProcSkillSources" and isinstance(value, list):
            proc_entity = _skill_id(key)
            for actor_id in value:
                minion = _minion_entity_from_actor(conn, actor_id)
                _add_relationship(
                    conn, proc_entity, rel_type, minion, None,
                    "resolved" if minion else "unresolved", locator,
                    {"mapping": scope, "actor_id": actor_id})
        elif scope == "planner.masteryBonusSkillSources" and isinstance(value, list):
            for bonus_ref in value:
                _add_relationship(
                    conn, _skill_id(key), rel_type, None,
                    json.dumps({"mastery_bonus_ref": bonus_ref}, ensure_ascii=False),
                    "unresolved", locator,
                    {"mapping": scope, "note": "mastery bonus table reference not decodable"})
        elif isinstance(value, list):
            # unknown shape but resolvable key: preserve as unresolved evidence
            _add_relationship(
                conn, None, rel_type, None,
                json.dumps({"key": key, "value": value}, ensure_ascii=False),
                "unresolved", locator, {"mapping": scope})
        else:
            _add_relationship(
                conn, None, rel_type, None,
                json.dumps({"key": key, "value": value}, ensure_ascii=False),
                "unresolved", locator, {"mapping": scope})


# =========================================================================
# review P1-1 / P1-6: bonus buff config, buff parameters and node evidence
# =========================================================================
_GRANT_KEYWORD_RE = re.compile(
    r"\b(grants?|granted|granting|gains?|gained|gaining)\b", re.I)
_MODIFY_KEYWORD_RE = re.compile(
    r"\b(increased|longer|additional|more|multiple|reduced|less|lasts|duration|effect)\b",
    re.I)
_STACK_REFERENCE_RE = re.compile(
    r"\b(each|per|for each|every)\s+(active\s+)?stacks?\b", re.I)
_BUFF_REL_TYPES = ("grants_buff_candidate", "modifies_buff_candidate")
_BONUS_STAT_TYPES = {0: "flat", 1: "increased", 2: "more"}


def _slug(value, limit: int = 48) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value))
    return text[:limit] or "_"


def _record_evidence(conn, entity_id, field_path, raw_value, normalized_value=None,
                     unit=None, status="known", method="parsed", notes=None,
                     locator=None, owner_type="entity") -> None:
    _insert(conn, "field_evidence", {
        "entity_id": entity_id,
        "owner_type": owner_type,
        "owner_id": entity_id,
        "field_path": field_path,
        "source_record_id": None,
        "source": SOURCE,
        "source_snapshot_id": _CTX.get("source_snapshot_id", ""),
        "raw_value": raw_value if not isinstance(raw_value, (dict, list))
        else json.dumps(raw_value, ensure_ascii=False),
        "normalized_value": normalized_value if not isinstance(normalized_value, (dict, list))
        else json.dumps(normalized_value, ensure_ascii=False),
        "unit": unit,
        "extraction_method": method,
        "evidence_status": status,
        "locator": locator,
        "notes": notes,
    })


def _insert_effect_with_values(conn, *, effect_id, owner_entity_id, category, scope,
                               raw_text, parse_status, extensions, values,
                               source_entity_id=None) -> None:
    _insert(conn, "effects", {
        "effect_id": effect_id,
        "owner_entity_id": owner_entity_id,
        "owner_component_id": None,
        "source_entity_id": source_entity_id,
        "effect_category": category,
        "raw_text": raw_text,
        "parse_status": parse_status,
        "extensions": json.dumps(extensions, ensure_ascii=False),
        "effect_scope": scope,
    })
    for index, value in enumerate(values):
        _insert(conn, "effect_values", {
            "value_id": f"{effect_id}#v{index}",
            "effect_id": effect_id,
            "stat_id": None,
            "value_kind": value.get("value_kind"),
            "value_numeric": value.get("value_numeric"),
            "value_min": value.get("value_min"),
            "value_max": value.get("value_max"),
            "per_level": None,
            "level": value.get("level"),
            "tier": None,
            "unit": value.get("unit"),
            "expression": value.get("expression"),
            "raw": value.get("raw"),
        })


def _node_text_index(conn) -> dict[str, dict]:
    """node entity id -> translated name/description + raw stats list."""
    index: dict[str, dict] = {}
    for kind, scope, source_id, locator, raw in _rows(
        conn, ("skill_tree_node", "passive_node", "weaver_node")
    ):
        node_id = raw.get("id", source_id)
        node_entity = _node_id(scope, node_id)
        stats = []
        for stat in raw.get("stats") or []:
            if not isinstance(stat, dict):
                continue
            stats.append({
                "stat_name": _tr(stat.get("statNameKey")),
                "stat_name_key": stat.get("statNameKey"),
                "value": stat.get("value"),
                "property": stat.get("property"),
                "noScaling": stat.get("noScaling"),
                "downside": stat.get("downside"),
                "tags": stat.get("tags"),
                "raw": stat,
            })
        index[node_entity] = {
            "entity_id": node_entity,
            "scope": str(scope),
            "node_id": node_id,
            "name": _tr(raw.get("nodeNameKey")),
            "description": _tr(raw.get("descriptionKey")),
            "stats": stats,
            "max_points": raw.get("maxPoints"),
            "locator": locator,
        }
    return index


def _text_window(text: str | None, name: str | None, radius: int = 160) -> str:
    if not text or not name:
        return ""
    lowered = text.lower()
    target = name.lower()
    position = lowered.find(target)
    if position < 0:
        return ""
    return text[max(0, position - radius):position + len(name) + radius]


def _looks_like_fraction(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value != 0 and abs(value) < 1)


def _buff_link_kind(description, node_name, buff_name, mods=None) -> tuple[str, str]:
    """Classify a node/bonus buff reference as grant or modification candidate."""
    if buff_name:
        window = _text_window(description, buff_name)
        if node_name and node_name.strip().lower() == buff_name.strip().lower():
            return ("grants_buff_candidate",
                    f"node name equals buff display name {buff_name!r}")
        if window and _STACK_REFERENCE_RE.search(window):
            return ("modifies_buff_candidate",
                    f"description refers to stacks of {buff_name!r} (the node modifies an "
                    f"existing buff, it does not grant it)")
        if window and _GRANT_KEYWORD_RE.search(window):
            return ("grants_buff_candidate",
                    f"description contains a grant keyword near {buff_name!r}")
        if window and _MODIFY_KEYWORD_RE.search(window):
            return ("modifies_buff_candidate",
                    f"description contains a modification keyword near {buff_name!r}")
    base_like = False
    modify_like = False
    for mod in mods or []:
        param_name = mod.get("param_name") or ""
        value = mod.get("value")
        lowered = param_name.lower()
        if ("duration" in lowered or "stack" in lowered) and not _looks_like_fraction(value):
            if mod.get("noScaling") is True:
                base_like = True
            else:
                modify_like = True
        elif _looks_like_fraction(value):
            modify_like = True
    if base_like and not modify_like:
        return ("grants_buff_candidate",
                "plain non-scaling buff parameter values (base values)")
    if modify_like and not base_like:
        return ("modifies_buff_candidate",
                "fractional/percentage buff parameter values")
    return ("grants_buff_candidate",
            "no description keyword matched; kind is a candidate only")


def _param_kind_from_prop_name(prop_name: str | None) -> str:
    lowered = (prop_name or "").lower()
    if not lowered:
        return "unknown"
    if "duration" in lowered:
        return "duration"
    if "stack" in lowered:
        return "max_stacks"
    if "chance" in lowered:
        return "chance"
    if "effect" in lowered or "increased" in lowered or "multi" in lowered:
        return "increased_effect"
    if "penetration" in lowered:
        return "penetration"
    if "damage" in lowered or "dot" in lowered:
        return "damage"
    if "speed" in lowered:
        return "speed"
    if "armor" in lowered or "armour" in lowered:
        return "armor"
    if "health" in lowered or "ward" in lowered or "mana" in lowered:
        return "resource"
    if "shared" in lowered:
        return "shared"
    return "unknown"


def _param_kind_from_stat_name(stat_name: str | None) -> str | None:
    lowered = (stat_name or "").lower()
    if not lowered:
        return None
    if "duration" in lowered:
        return "duration"
    if re.search(r"max(imum)?\s+stacks?", lowered) or "stack" in lowered:
        return "max_stacks"
    if "chance" in lowered:
        return "chance"
    if "effect" in lowered or "increased" in lowered:
        return "increased_effect"
    return None


def _normalise_config_param(param_kind: str, value) -> tuple[str, float | None, str | None]:
    """Normalise a planner buff-config value (already numeric) for effect_values."""
    numeric = float(value)
    if param_kind == "duration":
        return "duration", int(round(numeric * 1000)), "ms"
    if param_kind == "max_stacks":
        if _looks_like_fraction(numeric):
            return "max_stacks_increased", numeric, "%"
        return "max_stacks", int(round(numeric)), None
    if param_kind == "chance":
        return "chance", numeric, "%" if _looks_like_fraction(numeric) else None
    if _looks_like_fraction(numeric):
        return "percent_modifier", numeric, "%"
    return "flat", numeric, None


def _normalise_node_param(param_kind: str, numeric, is_percent: bool
                          ) -> tuple[str, float | None, str | None]:
    """Normalise a node stat display value ('+25%', '4') for effect_values."""
    if numeric is None:
        return "unparsed", None, None
    if param_kind == "duration":
        if is_percent:
            return "duration_increased", round(numeric / 100.0, 8), "%"
        return "duration", int(round(numeric * 1000)), "ms"
    if param_kind == "max_stacks":
        if is_percent:
            return "max_stacks_increased", round(numeric / 100.0, 8), "%"
        return "max_stacks", int(round(numeric)), None
    if param_kind == "chance":
        return "chance", (round(numeric / 100.0, 8) if is_percent else numeric), "%" \
            if is_percent else None
    if param_kind == "increased_effect":
        return "increased_effect", (round(numeric / 100.0, 8) if is_percent else numeric), \
            "%" if is_percent else None
    return ("percent_modifier", round(numeric / 100.0, 8), "%") if is_percent \
        else ("flat", numeric, None)


def _owner_class_source(conn, kind: str, tree_id: str) -> str | None:
    """Resolve the owning class source id for a bonus record's tree/ability."""
    if kind == "skill_bonus":
        owner_entity = _skill_id(tree_id)
        sql = "SELECT class_entity_id FROM skills WHERE entity_id=?"
    else:
        owner_entity = _entity_id("tree", "LECharTrees.trees", tree_id)
        sql = "SELECT class_entity_id FROM trees WHERE entity_id=?"
    row = conn.execute(sql, (owner_entity,)).fetchone()
    class_entity = row["class_entity_id"] if row else None
    if not class_entity:
        return None
    mapped = conn.execute(
        "SELECT source_id FROM source_id_map WHERE entity_id=? AND kind='class' AND source=?",
        (class_entity, SOURCE),
    ).fetchone()
    return mapped["source_id"] if mapped else None


def _buff_relationship_exists(conn, from_entity, to_entity) -> bool:
    if not from_entity or not to_entity:
        return False
    marks = ",".join("?" for _ in _BUFF_REL_TYPES)
    row = conn.execute(
        f"SELECT 1 FROM entity_relationships WHERE from_entity_id=? AND to_entity_id=? "
        f"AND relationship_type IN ({marks}) LIMIT 1",
        (from_entity, to_entity, *_BUFF_REL_TYPES),
    ).fetchone()
    return row is not None


def _collect_base_candidate(candidates: dict, buff_entity: str, param: str, value,
                            locator: str) -> None:
    key = {"duration": "duration_ms", "max_stacks": "max_stacks"}.get(param, param)
    entry = candidates.setdefault(
        buff_entity, {"duration_ms": {}, "max_stacks": {}, "locators": []})
    entry[key][value] = entry[key].get(value, 0) + 1
    if locator:
        entry["locators"].append(locator)


def _apply_buff_base_candidates(conn, candidates: dict) -> None:
    for buff_entity, entry in candidates.items():
        if not _entity_exists(conn, buff_entity):
            continue
        locator = next(iter(entry.get("locators") or []), None)
        durations = entry.get("duration_ms") or {}
        if durations:
            if len(durations) == 1:
                value = next(iter(durations))
                conn.execute(
                    "UPDATE buffs SET duration_ms=?, base_duration_ms=?, "
                    "duration_basis='node_text' WHERE entity_id=?",
                    (value, value, buff_entity))
                _record_evidence(
                    conn, buff_entity, "base_duration_ms->duration_ms",
                    raw_value=sorted(durations), normalized_value=value, unit="ms",
                    status="known", method="converted", locator=locator,
                    notes="buff base duration taken from the granting node/bonus config "
                          "evidence (source seconds x1000). It is a base value, not a "
                          "build's final duration; node modifiers are separate effects.")
            else:
                _record_issue(
                    conn, locator or "", "conflicting_buff_base_duration", "warning",
                    f"buff {buff_entity} has conflicting base duration candidates "
                    f"{sorted(durations)}; base_duration_ms left NULL",
                    entity_id=buff_entity, field_path="base_duration_ms")
        stacks = entry.get("max_stacks") or {}
        if stacks:
            if len(stacks) == 1:
                value = next(iter(stacks))
                conn.execute(
                    "UPDATE buffs SET max_stacks=?, max_stacks_basis='node_text' "
                    "WHERE entity_id=?", (value, buff_entity))
                _record_evidence(
                    conn, buff_entity, "max_stacks", raw_value=sorted(stacks),
                    normalized_value=value, status="known", method="converted",
                    locator=locator,
                    notes="mechanic base stack cap from the granting node/bonus config "
                          "evidence, replacing the UI-only value with basis=node_text; "
                          "build modifiers are separate effects.")
            else:
                _record_issue(
                    conn, locator or "", "conflicting_buff_base_stacks", "warning",
                    f"buff {buff_entity} has conflicting base max-stack candidates "
                    f"{sorted(stacks)}; max_stacks left NULL",
                    entity_id=buff_entity, field_path="max_stacks")


def _insert_bonus_buff_config(conn, node_text: dict) -> dict:
    """Structured rows for skillBonuses / passiveBonuses (review P1-1)."""
    candidates: dict = {}
    for kind, scope, source_id, locator, raw in _rows(conn, ("skill_bonus", "passive_bonus")):
        bonus_entity = _entity_id(kind, scope, source_id)
        if not _entity_exists(conn, bonus_entity):
            continue
        tree_id = str(source_id)
        class_source = _owner_class_source(conn, kind, tree_id)
        _insert_bonus_stats(conn, kind, scope, source_id, locator, raw, tree_id,
                            bonus_entity)
        _insert_bonus_mods(conn, kind, scope, source_id, locator, raw, tree_id,
                           bonus_entity)
        _insert_bonus_buff_entries(
            conn, kind=kind, scope=scope, source_id=source_id, locator=locator,
            raw=raw, tree_id=tree_id, bonus_entity=bonus_entity, section="buffs",
            class_source=class_source, node_text=node_text, candidates=candidates)
        _insert_bonus_buff_entries(
            conn, kind=kind, scope=scope, source_id=source_id, locator=locator,
            raw=raw, tree_id=tree_id, bonus_entity=bonus_entity, section="masteryBuffs",
            class_source=class_source, node_text=node_text, candidates=candidates)
        _insert_bonus_buff_abilities(conn, kind, scope, source_id, locator, raw,
                                     tree_id, bonus_entity)
    return candidates


def _insert_bonus_stats(conn, kind, scope, source_id, locator, raw, tree_id,
                        bonus_entity) -> None:
    stats = raw.get("stats")
    if stats is None:
        return
    if not isinstance(stats, dict):
        _record_issue(conn, locator, "unexpected_bonus_section_shape", "warning",
                      f"{kind} stats section is {type(stats).__name__}, expected object "
                      f"mapping node id -> modifier list",
                      entity_id=bonus_entity, field_path="stats")
        return
    for node_key, mods in stats.items():
        if not isinstance(mods, list):
            _record_issue(conn, locator, "unexpected_bonus_section_shape", "warning",
                          f"stats[{node_key}] is {type(mods).__name__}, expected list",
                          entity_id=bonus_entity, field_path=f"stats[{node_key}]")
            continue
        node_entity = _node_id(tree_id, node_key)
        owner = node_entity if _entity_exists(conn, node_entity) else bonus_entity
        for index, mod in enumerate(mods):
            if not isinstance(mod, dict):
                _record_issue(conn, locator, "unexpected_stat_mod_shape", "warning",
                              f"stats[{node_key}][{index}] is {type(mod).__name__}, "
                              f"expected object", entity_id=owner,
                              field_path=f"stats[{node_key}][{index}]")
                continue
            effect_id = f"{bonus_entity}#stat#{_slug(node_key)}#{index}"
            raw_value = mod.get("value")
            mod_type = mod.get("type")
            value_kind = _BONUS_STAT_TYPES.get(mod_type, "unknown")
            numeric = raw_value if isinstance(raw_value, (int, float)) \
                and not isinstance(raw_value, bool) else None
            unit = "%" if value_kind in ("increased", "more") and numeric is not None \
                and _looks_like_fraction(numeric) else None
            _insert_effect_with_values(
                conn, effect_id=effect_id, owner_entity_id=owner,
                category="stat_mod", scope=f"{kind}_config",
                raw_text=json.dumps(
                    {k: mod.get(k) for k in ("value", "type", "property", "tags",
                                              "conditions", "per", "specialTag")},
                    ensure_ascii=False),
                parse_status="parsed" if numeric is not None else "unparsed",
                extensions={
                    "node_key": node_key,
                    "node_entity_id": node_entity,
                    "tree_id": tree_id,
                    "source_section": "stats",
                    "property": mod.get("property"),
                    "specialTag": mod.get("specialTag"),
                    "tags": mod.get("tags"),
                    "conditions": mod.get("conditions"),
                    "per": mod.get("per"),
                    "type_semantics": "0=flat,1=increased,2=more (inferred from the item "
                                      "mod convention; raw type kept)",
                    "note": "planner config stat row mirroring the node stat; owner is the "
                            "node entity when resolvable",
                },
                values=[{
                    "value_kind": value_kind,
                    "value_numeric": numeric,
                    "value_min": None,
                    "value_max": None,
                    "unit": unit,
                    "expression": None if numeric is not None else json.dumps(raw_value),
                    "raw": json.dumps(mod, ensure_ascii=False),
                }],
                source_entity_id=None,
            )
        _record_evidence(
            conn, bonus_entity, f"stats[{node_key}]", raw_value=mods,
            normalized_value={"count": len(mods), "node_entity_id": node_entity},
            status="known", locator=locator,
            notes="planner node stat config mirrored into effects/effect_values")


def _insert_bonus_mods(conn, kind, scope, source_id, locator, raw, tree_id,
                       bonus_entity) -> None:
    for section in ("mods", "abilityMods"):
        section_value = raw.get(section)
        if section_value is None:
            continue
        if not isinstance(section_value, dict):
            _record_issue(conn, locator, "unexpected_bonus_section_shape", "warning",
                          f"{kind} {section} section is {type(section_value).__name__}, "
                          f"expected object mapping node id -> flag object",
                          entity_id=bonus_entity, field_path=section)
            continue
        for node_key, flags in section_value.items():
            node_entity = _node_id(tree_id, node_key)
            owner = node_entity if _entity_exists(conn, node_entity) else bonus_entity
            if not isinstance(flags, dict):
                _record_issue(conn, locator, "unexpected_config_mod_shape", "warning",
                              f"{section}[{node_key}] is {type(flags).__name__}, "
                              f"expected object", entity_id=owner,
                              field_path=f"{section}[{node_key}]")
                continue
            for flag_key, flag_value in flags.items():
                numeric = flag_value if isinstance(flag_value, (int, float)) \
                    and not isinstance(flag_value, bool) else None
                if isinstance(flag_value, bool):
                    value_kind = "flag"
                elif numeric is not None:
                    value_kind = "numeric"
                else:
                    value_kind = "unparsed"
                effect_id = f"{bonus_entity}#{section}#{_slug(node_key)}#{_slug(flag_key)}"
                _insert_effect_with_values(
                    conn, effect_id=effect_id, owner_entity_id=owner,
                    category="config_mod", scope=f"{kind}_config",
                    raw_text=json.dumps({flag_key: flag_value}, ensure_ascii=False),
                    parse_status="parsed",
                    extensions={
                        "node_key": node_key,
                        "node_entity_id": node_entity,
                        "tree_id": tree_id,
                        "source_section": section,
                        "flag": flag_key,
                        "value": flag_value,
                    },
                    values=[{
                        "value_kind": value_kind,
                        "value_numeric": numeric,
                        "value_min": None,
                        "value_max": None,
                        "unit": None,
                        "expression": None if value_kind != "unparsed"
                        else json.dumps(flag_value),
                        "raw": json.dumps({"key": flag_key, "value": flag_value},
                                          ensure_ascii=False),
                    }],
                )
            _record_evidence(
                conn, bonus_entity, f"{section}[{node_key}]", raw_value=flags,
                normalized_value={"flags": sorted(flags.keys()),
                                  "node_entity_id": node_entity},
                status="known", locator=locator,
                notes="planner config flag object mirrored into effects/effect_values")


def _insert_bonus_buff_entries(conn, *, kind, scope, source_id, locator, raw, tree_id,
                               bonus_entity, section, class_source, node_text,
                               candidates) -> None:
    entries = raw.get(section)
    if entries is None:
        return
    if not isinstance(entries, dict):
        _record_issue(conn, locator, "unexpected_bonus_section_shape", "warning",
                      f"{kind} {section} section is {type(entries).__name__}, expected "
                      f"object mapping node/mastery id -> buff entry list",
                      entity_id=bonus_entity, field_path=section)
        return
    for owner_key, buff_entries in entries.items():
        if not isinstance(buff_entries, list):
            _record_issue(conn, locator, "unexpected_buff_entry_shape", "warning",
                          f"{section}[{owner_key}] is {type(buff_entries).__name__}, "
                          f"expected list", entity_id=bonus_entity,
                          field_path=f"{section}[{owner_key}]")
            continue
        if section == "buffs":
            node_entity = _node_id(tree_id, owner_key)
            from_entity = node_entity if _entity_exists(conn, node_entity) \
                else bonus_entity
            node_info = node_text.get(node_entity) or {}
        else:
            node_entity = None
            mastery_entity = _mastery_id(class_source, owner_key) \
                if class_source is not None else None
            from_entity = mastery_entity if _entity_exists(conn, mastery_entity) \
                else bonus_entity
            node_info = {}
        for index, entry in enumerate(buff_entries):
            if not isinstance(entry, dict):
                _record_issue(conn, locator, "unexpected_buff_entry_shape", "warning",
                              f"{section}[{owner_key}][{index}] is "
                              f"{type(entry).__name__}, expected object",
                              entity_id=from_entity,
                              field_path=f"{section}[{owner_key}][{index}]")
                continue
            resolved = _buff_resolve(entry.get("id"))
            target = resolved["buff_entity_id"] \
                if _entity_exists(conn, resolved["buff_entity_id"]) else None
            buff_info = _BUFF_INFOS.get(str(resolved["uibuff_id"])) or {}
            buff_name = buff_info.get("name")
            mods = [mod for mod in (entry.get("mods") or []) if isinstance(mod, dict)]
            mods_for_kind = [{
                "param_name": _buff_prop_name(resolved["buff_id_name"], mod.get("prop")),
                "value": mod.get("value"),
                "noScaling": mod.get("noScaling"),
            } for mod in mods]
            rel_type, kind_basis = _buff_link_kind(
                node_info.get("description"), node_info.get("name"), buff_name,
                mods_for_kind)
            if section == "masteryBuffs":
                rel_type = "grants_buff_candidate"
                kind_basis = "mastery bonus config entry"
            raw_ref = json.dumps({
                "bonus_entity_id": bonus_entity,
                "class_source_id": class_source,
                "section": section,
                "owner_key": owner_key,
                "entry_index": index,
                "buff_id": entry.get("id"),
                "buff_id_name": resolved["buff_id_name"],
                "uibuff_id": resolved["uibuff_id"],
            }, ensure_ascii=False)
            status = "resolved" if target else "unresolved"
            _add_relationship(
                conn, from_entity, rel_type, target, raw_ref, status, locator,
                {"mapping": f"{scope}.{section}",
                 "buff_id_space": "window.BuffId value -> BuffId name -> UIBuffIds "
                                  "name -> UIBuffs.id",
                 "buff_id": entry.get("id"),
                 "buff_id_name": resolved["buff_id_name"],
                 "uibuff_id": resolved["uibuff_id"],
                 "owner_key": owner_key,
                 "relationship_kind_basis": kind_basis,
                 "grant_or_modify_candidate": True,
                 "config_mods": mods},
                hard_status=status)
            for mod_index, mod in enumerate(entry.get("mods") or []):
                if not isinstance(mod, dict):
                    _record_issue(conn, locator, "unexpected_buff_mod_shape", "warning",
                                  f"{section}[{owner_key}][{index}] mod {mod_index} is "
                                  f"{type(mod).__name__}, expected object",
                                  entity_id=from_entity,
                                  field_path=f"{section}[{owner_key}][{index}].mods")
                    continue
                prop = mod.get("prop")
                prop_name = _buff_prop_name(resolved["buff_id_name"], prop)
                param_kind = _param_kind_from_prop_name(prop_name)
                raw_value = mod.get("value")
                numeric = raw_value if isinstance(raw_value, (int, float)) \
                    and not isinstance(raw_value, bool) else None
                if numeric is not None:
                    value_kind, value_numeric, unit = _normalise_config_param(
                        param_kind, numeric)
                else:
                    value_kind, value_numeric, unit = "unparsed", None, None
                effect_id = f"{bonus_entity}#buff#{section}#{_slug(owner_key)}" \
                            f"#{index}#{mod_index}"
                _insert_effect_with_values(
                    conn, effect_id=effect_id, owner_entity_id=from_entity,
                    category="buff_mod_config", scope=f"{kind}_config",
                    raw_text=prop_name or f"prop {prop}",
                    parse_status="parsed" if numeric is not None else "unparsed",
                    extensions={
                        "node_key": owner_key,
                        "node_entity_id": node_entity,
                        "bonus_entity_id": bonus_entity,
                        "tree_id": tree_id,
                        "source_section": section,
                        "entry_index": index,
                        "buff_id": entry.get("id"),
                        "buff_id_name": resolved["buff_id_name"],
                        "uibuff_id": resolved["uibuff_id"],
                        "buff_entity_id": target,
                        "prop": prop,
                        "prop_name": prop_name,
                        "parameter": param_kind,
                        "noScaling": mod.get("noScaling"),
                        "pointThreshold": mod.get("pointThreshold"),
                        "relationship_kind": rel_type,
                        "note": "buff parameter config from the planner bonus table; "
                                "prop is a BuffPropId per-buff parameter index",
                    },
                    values=[{
                        "value_kind": value_kind,
                        "value_numeric": value_numeric,
                        "value_min": None,
                        "value_max": None,
                        "unit": unit,
                        "expression": None if numeric is not None
                        else json.dumps(raw_value),
                        "raw": json.dumps(mod, ensure_ascii=False),
                    }],
                    source_entity_id=target,
                )
                if (numeric is not None and target and rel_type == "grants_buff_candidate"
                        and param_kind in ("duration", "max_stacks")
                        and not _looks_like_fraction(numeric)
                        and mod.get("noScaling") is True):
                    base_value = 4000 if param_kind == "duration" else int(round(numeric))
                    if param_kind == "duration":
                        base_value = int(round(numeric * 1000))
                    _collect_base_candidate(candidates, target, param_kind, base_value,
                                            locator)
            _record_evidence(
                conn, from_entity, f"{section}[{owner_key}][{index}]", raw_value=entry,
                normalized_value={
                    "buff_id": entry.get("id"),
                    "buff_id_name": resolved["buff_id_name"],
                    "uibuff_id": resolved["uibuff_id"],
                    "buff_entity_id": target,
                    "param_names": [m.get("param_name") for m in mods_for_kind],
                },
                status="known" if target else "unparsed", locator=locator,
                notes="planner buff configuration parsed; the buff id was resolved by "
                      "name across BuffId/UIBuffIds/UIBuffs (never by numeric equality)")
            if target is None:
                _record_issue(
                    conn, locator, "unresolved_buff_reference", "warning",
                    f"{section}[{owner_key}][{index}] buff id {entry.get('id')} is "
                    f"BuffId {resolved['buff_id_name']!r}, which has no UIBuffs entry; "
                    f"relationship kept unresolved with raw reference",
                    entity_id=from_entity, field_path=f"{section}[{owner_key}]")


def _insert_bonus_buff_abilities(conn, kind, scope, source_id, locator, raw, tree_id,
                                 bonus_entity) -> None:
    entries = raw.get("buffAbilities")
    if entries is None:
        return
    if not isinstance(entries, dict):
        _record_issue(conn, locator, "unexpected_bonus_section_shape", "warning",
                      f"{kind} buffAbilities section is {type(entries).__name__}, "
                      f"expected object mapping node id -> ability id list",
                      entity_id=bonus_entity, field_path="buffAbilities")
        return
    for node_key, ability_ids in entries.items():
        if not isinstance(ability_ids, list):
            _record_issue(conn, locator, "unexpected_buff_ability_shape", "warning",
                          f"buffAbilities[{node_key}] is {type(ability_ids).__name__}, "
                          f"expected list", entity_id=bonus_entity,
                          field_path=f"buffAbilities[{node_key}]")
            continue
        node_entity = _node_id(tree_id, node_key)
        from_entity = node_entity if _entity_exists(conn, node_entity) else bonus_entity
        for index, ability_id in enumerate(ability_ids):
            target = _skill_id(ability_id)
            exists = _entity_exists(conn, target)
            raw_ref = json.dumps({
                "bonus_entity_id": bonus_entity,
                "section": "buffAbilities",
                "owner_key": node_key,
                "entry_index": index,
                "ability_id": ability_id,
            }, ensure_ascii=False)
            status = "resolved" if (exists and _entity_exists(conn, node_entity)) \
                else "unresolved"
            _add_relationship(
                conn, from_entity, "node_grants_ability_candidate", target, raw_ref,
                status, locator,
                {"mapping": f"{scope}.buffAbilities", "owner_key": node_key,
                 "ability_id": ability_id},
                hard_status=status)
            if not exists:
                _record_issue(conn, locator, "unresolved_ability_reference", "warning",
                              f"buffAbilities[{node_key}][{index}] ability "
                              f"{ability_id!r} not found", entity_id=from_entity,
                              field_path=f"buffAbilities[{node_key}]")
        _record_evidence(
            conn, bonus_entity, f"buffAbilities[{node_key}]", raw_value=ability_ids,
            normalized_value={"node_entity_id": node_entity,
                              "abilities": [str(a) for a in ability_ids]},
            status="known", locator=locator,
            notes="buff ability config mirrored into "
                  "node_grants_ability_candidate relationships")


def _buff_mentions_for_node(description, node_name, stat_names) -> list[tuple]:
    """Find known UIBuffs display names mentioned by a node (candidate links)."""
    lowered_description = (description or "").lower()
    lowered_name = (node_name or "").strip().lower()
    lowered_stats = [(name or "").lower() for name in stat_names]
    stat_haystack = " \\n ".join(lowered_stats)
    hits: list[tuple] = []
    seen: set[str] = set()
    for name_lower, ui_ids in _BUFF_NAME_INDEX.items():
        if len(ui_ids) != 1:
            continue  # ambiguous display names are never linked by name
        if len(name_lower) < 4:
            continue
        basis = None
        if lowered_name and lowered_name == name_lower:
            basis = "node_name"
        elif stat_haystack and name_lower in stat_haystack:
            if re.search(r"(?<![a-z])" + re.escape(name_lower) + r"(?![a-z])",
                         stat_haystack):
                basis = "stat_name"
        if basis is None and len(name_lower) >= 5 and name_lower in lowered_description:
            if re.search(r"(?<![a-z])" + re.escape(name_lower) + r"(?![a-z])",
                         lowered_description):
                basis = "node_description"
        if basis is None:
            continue
        buff_entity = _entity_id("buff", "UIBuffs", ui_ids[0])
        if buff_entity in seen:
            continue
        seen.add(buff_entity)
        info = _BUFF_INFOS.get(str(ui_ids[0])) or {}
        hits.append((buff_entity, info.get("name") or name_lower, basis))
    return hits


def _insert_buff_param_evidence(conn, node_text: dict) -> dict:
    """Node-text buff parameters as candidate effects + candidate relationships.

    Also the P1-1/P1-6 acceptance path for Flurry/Adrenaline Rush: the node
    stats of flur3 nodes 7/10/11 (properties 20067/5093/54 with statNameKey
    translations) become traceable buff parameter evidence even though
    ``partialSkillBonuses['flur3'].buffs`` is empty.
    """
    candidates: dict = {}
    for node_entity, info in node_text.items():
        stat_names = [stat.get("stat_name") or "" for stat in info["stats"]]
        mentions = _buff_mentions_for_node(
            info.get("description"), info.get("name"), stat_names)
        if not mentions:
            continue
        for buff_entity, buff_name, match_basis in mentions:
            rel_type, kind_basis = _buff_link_kind(
                info.get("description"), info.get("name"), buff_name, [])
            if not _buff_relationship_exists(conn, node_entity, buff_entity):
                raw_ref = json.dumps({
                    "node_entity_id": node_entity,
                    "node_id": info["node_id"],
                    "tree_id": info["scope"],
                    "buff_entity_id": buff_entity,
                    "buff_name": buff_name,
                    "match": match_basis,
                }, ensure_ascii=False)
                _add_relationship(
                    conn, node_entity, rel_type, buff_entity, raw_ref, "candidate",
                    info["locator"],
                    {"mapping": "node_text.buff_mention",
                     "match": match_basis,
                     "relationship_kind_basis": kind_basis,
                     "buff_name": buff_name,
                     "comment": "candidate only: buff identity comes from a name mention "
                                "in the node text/stat label, not from an id-space "
                                "confirmation"},
                    hard_status="candidate")
                _record_evidence(
                    conn, node_entity, "buff_mention",
                    raw_value={"buff_name": buff_name, "match": match_basis},
                    normalized_value={"buff_entity_id": buff_entity,
                                      "relationship_type": rel_type,
                                      "resolution_status": "candidate"},
                    status="inferred", method="inferred", locator=info["locator"],
                    notes="node text/stat label mentions a known UIBuffs display name; "
                          "stored as a candidate relationship only")
            for stat_index, stat in enumerate(info["stats"]):
                param_kind = _param_kind_from_stat_name(stat.get("stat_name"))
                if not param_kind:
                    continue
                stat_name_lower = (stat.get("stat_name") or "").lower()
                if len(mentions) > 1 and buff_name.lower() not in stat_name_lower:
                    continue
                per_stack_reference = bool(_STACK_REFERENCE_RE.search(
                    _text_window(info.get("description"), buff_name)))
                if per_stack_reference and buff_name.lower() not in stat_name_lower:
                    # "Each stack of <buff> grants ..." nodes describe the buff's own
                    # per-stack effect; their stats are not buff parameters.
                    continue
                numeric, is_percent = _parse_numeric_text(stat.get("value"))
                value_kind, value_numeric, unit = _normalise_node_param(
                    param_kind, numeric, is_percent)
                effect_id = f"{node_entity}#buffparam#{_slug(buff_entity.split(':')[-1])}" \
                            f"#{stat_index}"
                _insert_effect_with_values(
                    conn, effect_id=effect_id, owner_entity_id=node_entity,
                    category="buff_param_candidate", scope="node_stat",
                    raw_text=stat.get("stat_name") or "",
                    parse_status="parsed" if numeric is not None else "unparsed",
                    extensions={
                        "node_entity_id": node_entity,
                        "node_id": info["node_id"],
                        "tree_id": info["scope"],
                        "buff_entity_id": buff_entity,
                        "buff_name": buff_name,
                        "parameter": param_kind,
                        "property": stat.get("property"),
                        "stat_name_key": stat.get("stat_name_key"),
                        "stat_name": stat.get("stat_name"),
                        "value_text": stat.get("value"),
                        "noScaling": stat.get("noScaling"),
                        "relationship_type": rel_type,
                        "relationship_kind_basis": kind_basis,
                        "note": "candidate buff parameter from the node stat display; "
                                "the raw property id is not a decoded parameter "
                                "namespace (property 5093/20067 are shared placeholders)",
                    },
                    values=[{
                        "value_kind": value_kind,
                        "value_numeric": value_numeric,
                        "value_min": None,
                        "value_max": None,
                        "unit": unit,
                        "expression": None if numeric is not None
                        else json.dumps(stat.get("value")),
                        "raw": json.dumps(stat.get("raw"), ensure_ascii=False),
                    }],
                    source_entity_id=buff_entity,
                )
                _record_evidence(
                    conn, node_entity,
                    f"stats[{stat_index}]->buff_parameter",
                    raw_value={"statNameKey": stat.get("stat_name_key"),
                               "value": stat.get("value"),
                               "property": stat.get("property")},
                    normalized_value={"parameter": param_kind,
                                      "buff_entity_id": buff_entity,
                                      "value_kind": value_kind,
                                      "value_numeric": value_numeric,
                                      "unit": unit},
                    status="inferred" if numeric is not None else "unparsed",
                    method="inferred", locator=info["locator"],
                    notes=f"candidate {param_kind} parameter for buff {buff_name!r} "
                          f"from the node stat translation (property id kept raw)")
                if (numeric is not None and rel_type == "grants_buff_candidate"
                        and param_kind in ("duration", "max_stacks")
                        and not is_percent and stat.get("noScaling") is True):
                    base_value = int(round(numeric * 1000)) if param_kind == "duration" \
                        else int(round(numeric))
                    _collect_base_candidate(candidates, buff_entity, param_kind,
                                            base_value, info["locator"])
    return candidates


def _item_base_entity(conn, base_type, sub_type) -> str | None:
    if base_type is None or sub_type is None:
        return None
    for group in ("equippable", "nonEquippable"):
        scope = f"itemList.{group}:{base_type}"
        entity = _entity_id("item", scope, sub_type)
        if _entity_exists(conn, entity):
            return entity
    return None


def _minion_entity_from_actor(conn, actor_id) -> str | None:
    candidates = [str(actor_id)]
    try:
        candidates.append(str(float(actor_id)))
        candidates.append(str(int(float(actor_id))))
    except (TypeError, ValueError):
        pass
    for candidate in candidates:
        entity = _minion_id(candidate)
        if _entity_exists(conn, entity):
            return entity
    # entityList keys are floats ("100000.0") while the relation uses an int
    try:
        target = int(float(actor_id))
    except (TypeError, ValueError):
        return None
    rows = conn.execute(
        "SELECT source_id FROM source_records WHERE source=? AND entity_kind='minion_entity'",
        (SOURCE,),
    ).fetchall()
    for row in rows:
        try:
            if int(float(row["source_id"])) == target:
                return _minion_id(row["source_id"])
        except ValueError:
            continue
    return None
