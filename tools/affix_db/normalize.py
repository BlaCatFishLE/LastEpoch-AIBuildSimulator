"""Normalization of the raw tunklab datasets into relational rows.

Turns the extracted data literals into:

* ``affixes`` -- one row per affix, with raw fields preserved as JSON,
* ``equipment_types`` / ``equipment_sub_bases`` -- the item bases,
* ``stat_properties`` family -- display metadata used to reproduce tunklab's
  number formatting,
* ``affix_eligible_bases`` -- every ``canRollOn`` reference (including the two
  pseudo base ids tunklab itself excludes from display),
* ``affix_tiers`` -- raw tier rolls,
* ``affix_base_ranges`` -- affix x base type x tier x roll-slot with raw,
  scaled/effective and displayed values.

No I/O happens here; :mod:`affix_db.build` writes the result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import SCHEMA_VERSION
from .enums import (
    AFFIX_TYPE,
    CLASS_SPECIFICITY,
    MOD_TYPE,
    ROUNDING_FOR_ADDED,
    ROLLS_ON,
    SPECIAL_AFFIX_TYPE,
    TUNKLAB_EXCLUDED_BASE_TYPES,
    class_specificity_names,
    name_of,
)
from .extract import Extraction
from .formula import (
    IDOL_ALTAR_FRACTIONAL_TAGS,
    MOD_ADDED,
    PROPERTY_ABILITY_PROPERTY,
    PROPERTY_EFFECT_OF_AILMENT_ON_YOU,
    PROPERTY_IDOL_ALTAR,
    PROPERTY_PLAYER_PROPERTY,
    PropertyDisplayInfo,
    compute_modifier,
    range_summary,
)


class NormalizationError(RuntimeError):
    pass


#: Affix ids for which an Omen-idol special path has direct external evidence.
#: (tunklab's own ``canRollOn`` does *not* list Grand Idol for these, so this is a
#: derived/supplemental path, not a tunklab-provided range.)
OMEN_EVIDENCE_AFFIX_IDS = (111,)  # Idol Fire Resistance

OMEN_EVIDENCE = (
    "tunklab equipment data: baseTypeID 29 subItems 10..14 (Grand * Omen Idol) carry "
    "affixEffectiveness=1 and onlyDropWhenRequiringAffixEffectiveness=true; equipment "
    "constant omenIdolAffixEffectModifier=0; lastepochtools planner runtime: "
    "if item.affixEffectiveness==1 then base affixEffectModifier:=0; project save-import "
    "(the archived corrected calculation input) shows Grand Arcane "
    "Omen Idol (equippable_29:11) with affix:letools:affixList.singleAffixes:111 and raw "
    "range 0.2..0.45"
)


@dataclass
class RollSlot:
    slot_index: int
    property_id: int
    mod_type: int
    tags: int
    special_tag: int
    extra_tag: int
    set_property: int
    mod_display_name: str


@dataclass
class NormalizedData:
    affixes: list[dict[str, Any]] = field(default_factory=list)
    equipment_types: list[dict[str, Any]] = field(default_factory=list)
    equipment_sub_bases: list[dict[str, Any]] = field(default_factory=list)
    equipment_globals: list[dict[str, Any]] = field(default_factory=list)
    affix_eligible_bases: list[dict[str, Any]] = field(default_factory=list)
    affix_tiers: list[dict[str, Any]] = field(default_factory=list)
    affix_base_ranges: list[dict[str, Any]] = field(default_factory=list)
    omen_special_ranges: list[dict[str, Any]] = field(default_factory=list)
    player_properties: list[dict[str, Any]] = field(default_factory=list)
    player_stat_properties: list[dict[str, Any]] = field(default_factory=list)
    ability_properties: list[dict[str, Any]] = field(default_factory=list)
    stat_property_enum: dict[int, str] = field(default_factory=dict)
    unresolved_can_roll_on: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        return {
            "affixes": len(self.affixes),
            "equipment_types": len(self.equipment_types),
            "equipment_sub_bases": len(self.equipment_sub_bases),
            "equipment_globals": len(self.equipment_globals),
            "affix_eligible_bases": len(self.affix_eligible_bases),
            "affix_tiers": len(self.affix_tiers),
            "affix_base_ranges": len(self.affix_base_ranges),
            "omen_special_ranges": len(self.omen_special_ranges),
            "player_properties": len(self.player_properties),
            "player_stat_properties": len(self.player_stat_properties),
            "ability_properties": len(self.ability_properties),
            "unresolved_can_roll_on": len(self.unresolved_can_roll_on),
        }


# --------------------------------------------------------------------------
# Property display metadata
# --------------------------------------------------------------------------


def _prop_info_from_definition(
    property_id: int,
    definition: dict[str, Any],
    property_name: str | None = None,
) -> PropertyDisplayInfo:
    return PropertyDisplayInfo(
        property_id=property_id,
        property_name=property_name or definition.get("propertyName") or "",
        mod_type=MOD_ADDED,
        rounding_for_added=definition.get("roundingForAdded"),
        display_added_as_percentage=bool(definition.get("displayAddedAsPercentage")),
        display_added_as_tenth_of_value=bool(definition.get("displayAddedAsTenthOfValue")),
        display_as_percentage_of=bool(definition.get("displayAsPercentageOf")),
        display_as_added_to=bool(definition.get("displayAsAddedTo")),
        dont_display_plus=bool(definition.get("dontDisplayPlus")),
        less_is_better=bool(definition.get("lessIsBetter")),
        more_rounding=definition.get("moreRounding"),
        resolved=True,
    )


class DisplayResolver:
    """Resolve a stat-property definition the way tunklab's ``Property`` class does."""

    def __init__(self, extraction: Extraction):
        self.extraction = extraction
        data = extraction.datasets
        self.player_properties: dict[int, dict[str, Any]] = {
            int(row["property"]): row for row in data["player_properties"].value
        }
        self.player_stat_properties: dict[int, dict[str, Any]] = {
            index: row for index, row in enumerate(data["player_stat_properties"].value)
        }
        self.ability_properties: dict[int, dict[str, Any]] = {
            int(row["abilityID"]): row for row in data["ability_properties"].value
        }
        self.issues: list[str] = []

    def resolve(
        self,
        property_id: int,
        mod_type: int,
        tags: int,
        special_tag: int,
        extra_tag: int = 0,
    ) -> PropertyDisplayInfo | None:
        property_id = int(property_id)
        if property_id == PROPERTY_PLAYER_PROPERTY:
            definition = self.player_stat_properties.get(int(tags))
            if definition is None:
                return None
            return _prop_info_from_definition(property_id, definition, definition.get("propertyName"))
        if property_id == PROPERTY_ABILITY_PROPERTY:
            ability = self.ability_properties.get(int(tags))
            if ability is None:
                return None
            slots = ability.get("properties") or []
            if not (0 <= int(special_tag) < len(slots)):
                return None
            definition = slots[int(special_tag)]
            return _prop_info_from_definition(property_id, definition, definition.get("propertyName"))
        if property_id == PROPERTY_EFFECT_OF_AILMENT_ON_YOU:
            # tunklab has no propertyInfo here; getDisplayValue special-cases this
            # property to Math.round(100 * value) and getValueUnit to "%".
            return PropertyDisplayInfo(
                property_id=property_id,
                property_name="EffectOfAilmentOnYou",
                mod_type=mod_type,
                rounding_for_added=None,
                display_added_as_percentage=True,
                display_added_as_tenth_of_value=False,
                display_as_percentage_of=False,
                display_as_added_to=False,
                dont_display_plus=False,
                less_is_better=False,
            )
        if property_id == PROPERTY_IDOL_ALTAR:
            tags_value = int(tags)
            if tags_value in IDOL_ALTAR_FRACTIONAL_TAGS:
                return PropertyDisplayInfo(
                    property_id=property_id,
                    property_name="IdolAltarProperty",
                    mod_type=mod_type,
                    rounding_for_added=None,
                    display_added_as_percentage=True,
                    display_added_as_tenth_of_value=False,
                    display_as_percentage_of=False,
                    display_as_added_to=False,
                    dont_display_plus=True,
                    less_is_better=False,
                )
            return PropertyDisplayInfo(
                property_id=property_id,
                property_name="IdolAltarProperty",
                mod_type=mod_type,
                rounding_for_added=None,
                display_added_as_percentage=False,
                display_added_as_tenth_of_value=False,
                display_as_percentage_of=False,
                display_as_added_to=False,
                dont_display_plus=False,
                less_is_better=False,
            )
        definition = self.player_properties.get(property_id)
        if definition is None:
            return None
        return _prop_info_from_definition(property_id, definition, definition.get("propertyName"))


# --------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------


def _roll_slots(affix: dict[str, Any]) -> list[RollSlot]:
    if affix.get("affixProperties"):
        slots: list[RollSlot] = []
        for index, prop in enumerate(affix["affixProperties"]):
            slots.append(
                RollSlot(
                    slot_index=index,
                    property_id=int(prop["property"]),
                    mod_type=int(prop.get("modType", 0)),
                    tags=int(prop.get("tags", 0)),
                    special_tag=int(prop.get("specialTag", 0)),
                    extra_tag=int(prop.get("extraTag", 0)),
                    set_property=int(prop.get("setProperty", 0)),
                    mod_display_name=str(prop.get("modDisplayName", "")),
                )
            )
        return slots
    if "statProperty" not in affix:
        return []
    return [
        RollSlot(
            slot_index=0,
            property_id=int(affix["statProperty"]),
            mod_type=int(affix.get("modType", 0)),
            tags=int(affix.get("tags", 0)),
            special_tag=int(affix.get("specialTag", 0)),
            extra_tag=int(affix.get("extraTag", 0)),
            set_property=int(affix.get("setProperty", 0)),
            mod_display_name="",
        )
    ]


def _tier_rolls(affix: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield one dict per tier: main roll plus normalized extra roll list."""
    tiers: list[dict[str, Any]] = []
    for tier_index, tier in enumerate(affix["tiers"]):
        extra = []
        for roll in tier.get("extraRolls") or []:
            extra.append({"minRoll": float(roll["minRoll"]), "maxRoll": float(roll["maxRoll"])})
        tiers.append(
            {
                "tier": tier_index + 1,
                "minRoll": float(tier["minRoll"]),
                "maxRoll": float(tier["maxRoll"]),
                "extraRolls": extra,
            }
        )
    return tiers


def _raw_for_slot(tier: dict[str, Any], slot_index: int) -> tuple[float, float] | None:
    if slot_index == 0:
        return tier["minRoll"], tier["maxRoll"]
    extra_index = slot_index - 1
    extra = tier.get("extraRolls") or []
    if extra_index >= len(extra):
        return None
    return extra[extra_index]["minRoll"], extra[extra_index]["maxRoll"]


def _display_name(affix: dict[str, Any], slots: list[RollSlot], resolver: DisplayResolver) -> str:
    base = affix.get("affixDisplayName") or affix.get("affixName") or ""
    if affix.get("useGeneratedNameForDisplayName") and slots:
        slot = slots[0]
        info = resolver.resolve(slot.property_id, slot.mod_type, slot.tags, slot.special_tag, slot.extra_tag)
        if info is not None and info.property_name:
            return info.property_name
    return base


def normalize(extraction: Extraction) -> NormalizedData:
    data = extraction.datasets
    normalized = NormalizedData()
    normalized.stat_property_enum = dict(extraction.stat_property_enum)
    resolver = DisplayResolver(extraction)
    enum_names = extraction.stat_property_enum

    affix_rows: dict[int, dict[str, Any]] = {}
    slots_by_affix: dict[int, list[RollSlot]] = {}
    tiers_by_affix: dict[int, list[dict[str, Any]]] = {}

    for affix in data["affixes"].value:
        affix_id = int(affix["affixId"])
        slots = _roll_slots(affix)
        tiers = _tier_rolls(affix)
        slots_by_affix[affix_id] = slots
        tiers_by_affix[affix_id] = tiers
        property_id = slots[0].property_id if slots else None
        display_name = _display_name(affix, slots, resolver)
        row = {
            "affix_id": affix_id,
            "slug": affix["slug"],
            "name": affix["affixName"],
            "display_name": affix.get("affixDisplayName") or "",
            "display_name_effective": display_name,
            "title": affix.get("affixTitle") or "",
            "type": int(affix["type"]),
            "type_name": name_of(AFFIX_TYPE, affix["type"]),
            "special_affix_type": int(affix.get("specialAffixType", 0)),
            "special_affix_type_name": name_of(SPECIAL_AFFIX_TYPE, affix.get("specialAffixType", 0)),
            "rolls_on": int(affix.get("rollsOn", 0)),
            "rolls_on_name": name_of(ROLLS_ON, affix.get("rollsOn", 0)),
            "group_id": int(affix.get("group", 0)),
            "level_requirement": int(affix.get("levelRequirement", 0)),
            "class_specificity": int(affix.get("classSpecificity", 0)),
            "class_specificity_names": "|".join(class_specificity_names(int(affix.get("classSpecificity", 0)))),
            "standard_affix_effect_modifier": float(affix.get("standardAffixEffectModifier", 0.0)),
            "reroll_chance": float(affix.get("rerollChance", 0.0)),
            "weapon_effect": int(affix.get("weaponEffect", 0)),
            "t6_compatibility": int(affix.get("t6Compatibility", 0)),
            "stat_property": property_id,
            "stat_property_name": enum_names.get(property_id) if property_id is not None else None,
            "tags": int(affix["tags"]) if "tags" in affix else None,
            "special_tag": int(affix["specialTag"]) if "specialTag" in affix else None,
            "extra_tag": int(affix["extraTag"]) if "extraTag" in affix else None,
            "mod_type": int(affix["modType"]) if "modType" in affix else None,
            "mod_type_name": name_of(MOD_TYPE, affix.get("modType")) if "modType" in affix else None,
            "set_property": int(affix.get("setProperty", 0)) if "setProperty" in affix else None,
            "unique_id": int(affix["uniqueId"]) if "uniqueId" in affix else None,
            "use_generated_name_for_display_name": 1 if affix.get("useGeneratedNameForDisplayName") else 0,
            "tier_count": len(tiers),
            "roll_slot_count": len(slots),
            "can_roll_on": list(affix["canRollOn"]),
            "specific_reroll_chances": affix.get("specificRerollChances") or [],
            "tiers": tiers,
            "affix_properties": affix.get("affixProperties"),
            "raw": affix,
        }
        affix_rows[affix_id] = row
        normalized.affix_tiers.append(
            {
                "affix_id": affix_id,
                "tier_count": len(tiers),
                "raw": affix["tiers"],
                "tiers": tiers,
            }
        )

    # -- equipment types and sub bases ------------------------------------
    equipment = data["equipment"].value
    for dataset_key, is_equippable in (("equippableItems", 1), ("nonEquippableItems", 0)):
        for equip in equipment.get(dataset_key, []):
            grid = equip.get("gridSize") or {}
            normalized.equipment_types.append(
                {
                    "base_type_id": int(equip["baseTypeID"]),
                    "base_type_name": equip["baseTypeName"],
                    "display_name": equip["displayName"],
                    "is_equippable": is_equippable,
                    "slot_type": equip.get("type"),
                    "is_weapon": 1 if equip.get("isWeapon") else 0,
                    "affix_effect_modifier": float(equip.get("affixEffectModifier", 0.0)),
                    "maximum_affixes": equip.get("maximumAffixes"),
                    "max_sockets": equip.get("maxSockets"),
                    "grid_x": grid.get("x"),
                    "grid_y": grid.get("y"),
                    "minimum_drop_level": equip.get("minimumDropLevel"),
                    "gold_cost_modifier": equip.get("goldCostModifier"),
                    "added_gamble_cost": equip.get("addedGambleCost"),
                    "reroll_chance": equip.get("rerollChance"),
                    "class_affinity": equip.get("classAffinity"),
                    "affinity_reroll_chance": equip.get("affinityRerollChance"),
                    "sub_item_count": len(equip.get("subItems") or []),
                    "raw": equip,
                }
            )
            for sub in equip.get("subItems") or []:
                normalized.equipment_sub_bases.append(
                    {
                        "base_type_id": int(equip["baseTypeID"]),
                        "sub_type_id": int(sub["subTypeID"]),
                        "slug": sub.get("slug"),
                        "name": sub.get("name"),
                        "display_name": sub.get("displayName"),
                        "level_requirement": sub.get("levelRequirement"),
                        "cannot_drop": 1 if sub.get("cannotDrop") else 0,
                        "item_tags": sub.get("itemTags"),
                        "class_requirement": sub.get("classRequirement"),
                        "sub_class_requirement": sub.get("subClassRequirement"),
                        "affix_effectiveness": sub.get("affixEffectiveness"),
                        "only_drop_when_requiring_affix_effectiveness": (
                            1 if sub.get("onlyDropWhenRequiringAffixEffectiveness") else 0
                        ),
                        "implicits": sub.get("implicits") or [],
                        "raw": sub,
                    }
                )

    # Equipment-level scalars/flags (e.g. omenIdolAffixEffectModifier) are part of
    # the source data and are needed to understand the Omen-idol special path.
    for key in sorted(equipment):
        if key in ("equippableItems", "nonEquippableItems"):
            continue
        normalized.equipment_globals.append({"key": key, "value": equipment[key]})

    equipment_by_id = {row["base_type_id"]: row for row in normalized.equipment_types}
    equippable = {row["base_type_id"] for row in normalized.equipment_types if row["is_equippable"]}
    excluded = set(TUNKLAB_EXCLUDED_BASE_TYPES)

    # -- properties --------------------------------------------------------
    for row in data["player_properties"].value:
        normalized.player_properties.append(
            {
                "property_id": int(row["property"]),
                "property_name": row.get("propertyName"),
                "rounding_for_added": row.get("roundingForAdded"),
                "rounding_for_added_name": name_of(ROUNDING_FOR_ADDED, row.get("roundingForAdded")),
                "display_added_as_percentage": 1 if row.get("displayAddedAsPercentage") else 0,
                "display_added_as_tenth_of_value": 1 if row.get("displayAddedAsTenthOfValue") else 0,
                "display_as_percentage_of": 1 if row.get("displayAsPercentageOf") else 0,
                "display_as_added_to": 1 if row.get("displayAsAddedTo") else 0,
                "dont_display_plus": 1 if row.get("dontDisplayPlus") else 0,
                "less_is_better": 1 if row.get("lessIsBetter") else 0,
                "default_alt_text": row.get("defaultAltText"),
                "raw": row,
            }
        )
    for index, row in enumerate(data["player_stat_properties"].value):
        normalized.player_stat_properties.append(
            {
                "tag_index": index,
                "property_name": row.get("propertyName"),
                "rounding_for_added": row.get("roundingForAdded"),
                "rounding_for_added_name": name_of(ROUNDING_FOR_ADDED, row.get("roundingForAdded")),
                "display_added_as_percentage": 1 if row.get("displayAddedAsPercentage") else 0,
                "display_added_as_tenth_of_value": 1 if row.get("displayAddedAsTenthOfValue") else 0,
                "display_as_percentage_of": 1 if row.get("displayAsPercentageOf") else 0,
                "display_as_added_to": 1 if row.get("displayAsAddedTo") else 0,
                "dont_display_plus": 1 if row.get("dontDisplayPlus") else 0,
                "less_is_better": 1 if row.get("lessIsBetter") else 0,
                "alt_text": row.get("altText"),
                "raw": row,
            }
        )
    for row in data["ability_properties"].value:
        normalized.ability_properties.append(
            {
                "ability_id": int(row["abilityID"]),
                "slot_count": len(row.get("properties") or []),
                "raw": row,
            }
        )

    # -- eligible bases ----------------------------------------------------
    unresolved_seen: dict[int, dict[str, Any]] = {}
    for affix_id in sorted(affix_rows):
        row = affix_rows[affix_id]
        for base_id in _unique(row["can_roll_on"]):
            resolved = int(base_id) in equipment_by_id
            is_equippable = int(base_id) in equippable
            excluded_row = int(base_id) in excluded
            normalized.affix_eligible_bases.append(
                {
                    "affix_id": affix_id,
                    "base_type_id": int(base_id),
                    "resolved": 1 if resolved else 0,
                    "is_equippable": 1 if is_equippable else 0,
                    "excluded_by_tunklab": 1 if excluded_row else 0,
                    "base_type_name": equipment_by_id.get(int(base_id), {}).get("base_type_name"),
                }
            )
            if not resolved:
                unresolved_seen.setdefault(
                    int(base_id),
                    {
                        "base_type_id": int(base_id),
                        "excluded_by_tunklab": 1 if excluded_row else 0,
                        "affix_count": 0,
                        "affix_ids_sample": [],
                        "note": "no tunklab item base for this canRollOn id; tunklab also excludes it from Scaled Values",
                    },
                )
                entry = unresolved_seen[int(base_id)]
                entry["affix_count"] += 1
                if len(entry["affix_ids_sample"]) < 10:
                    entry["affix_ids_sample"].append(affix_id)

    # -- ranges ------------------------------------------------------------
    for affix_id in sorted(affix_rows):
        row = affix_rows[affix_id]
        slots = slots_by_affix[affix_id]
        tiers = tiers_by_affix[affix_id]
        if not slots:
            normalized.issues.append(f"affix {affix_id} has no stat property slot")
            continue
        for base_id in _unique(row["can_roll_on"]):
            base_id = int(base_id)
            if base_id not in equippable or base_id in excluded:
                continue
            base = equipment_by_id[base_id]
            for tier in tiers:
                for slot in slots:
                    raw = _raw_for_slot(tier, slot.slot_index)
                    if raw is None:
                        normalized.issues.append(
                            f"affix {affix_id} tier {tier['tier']} is missing extraRolls for slot {slot.slot_index}"
                        )
                        continue
                    info = resolver.resolve(slot.property_id, slot.mod_type, slot.tags, slot.special_tag, slot.extra_tag)
                    summary = range_summary(
                        raw[0],
                        raw[1],
                        base["affix_effect_modifier"],
                        row["standard_affix_effect_modifier"],
                        info,
                        slot.mod_type,
                    )
                    if info is None:
                        display_property_name = enum_names.get(slot.property_id)
                    else:
                        display_property_name = info.property_name or enum_names.get(slot.property_id)
                    normalized.affix_base_ranges.append(
                        {
                            "affix_id": affix_id,
                            "base_type_id": base_id,
                            "tier": tier["tier"],
                            "roll_slot": slot.slot_index,
                            "property_id": slot.property_id,
                            "property_name": display_property_name,
                            "mod_type": slot.mod_type,
                            "tags": slot.tags,
                            "special_tag": slot.special_tag,
                            **summary,
                            "source_locator": data["affixes"].locator(),
                            "source_version": data["affixes"].version,
                        }
                    )

    # -- Omen idol special path -------------------------------------------
    # tunklab ships the underlying fields (subItem.affixEffectiveness,
    # equipment.omenIdolAffixEffectModifier) but does not implement the path in its
    # page code and its canRollOn for affix 111 excludes Grand Idol.  We therefore
    # store this as an explicitly derived, evidence-linked side table.
    omen_modifier_constant = equipment.get("omenIdolAffixEffectModifier", 0.0)
    if omen_modifier_constant is None:
        omen_modifier_constant = 0.0
    omen_sub_bases = [
        sub
        for sub in normalized.equipment_sub_bases
        if sub["base_type_id"] == 29 and sub["affix_effectiveness"] == 1
    ]
    for affix_id in OMEN_EVIDENCE_AFFIX_IDS:
        row = affix_rows.get(affix_id)
        if row is None:
            normalized.issues.append(f"Omen evidence affix {affix_id} not found in dataset")
            continue
        slots = slots_by_affix[affix_id]
        tiers = tiers_by_affix[affix_id]
        in_can_roll_on = 29 in row["can_roll_on"]
        eligibility_status = (
            "eligible-in-tunklab-canrollon"
            if in_can_roll_on
            else "unresolved: tunklab canRollOn does not include baseTypeID 29 (Grand Idol)"
        )
        for sub in omen_sub_bases:
            for tier in tiers:
                for slot in slots:
                    raw = _raw_for_slot(tier, slot.slot_index)
                    if raw is None:
                        continue
                    info = resolver.resolve(slot.property_id, slot.mod_type, slot.tags, slot.special_tag, slot.extra_tag)
                    summary = range_summary(
                        raw[0],
                        raw[1],
                        omen_modifier_constant,
                        row["standard_affix_effect_modifier"],
                        info,
                        slot.mod_type,
                    )
                    normalized.omen_special_ranges.append(
                        {
                            "base_type_id": 29,
                            "sub_type_id": sub["sub_type_id"],
                            "sub_base_slug": sub["slug"],
                            "sub_base_name": sub["name"],
                            "affix_id": affix_id,
                            "tier": tier["tier"],
                            "roll_slot": slot.slot_index,
                            "property_id": slot.property_id,
                            "raw_min": summary["raw_min"],
                            "raw_max": summary["raw_max"],
                            "omen_affix_effect_modifier": float(omen_modifier_constant),
                            "modifier": summary["modifier"],
                            "modifier_decimal": summary["modifier_decimal"],
                            "effective_min": summary["effective_min"],
                            "effective_max": summary["effective_max"],
                            "effective_min_decimal": summary["effective_min_decimal"],
                            "effective_max_decimal": summary["effective_max_decimal"],
                            "display_unit": summary["display_unit"],
                            "display_min": summary["display_min"],
                            "display_max": summary["display_max"],
                            "tunklab_display_min": summary["tunklab_display_min"],
                            "tunklab_display_max": summary["tunklab_display_max"],
                            "eligibility_status": eligibility_status,
                            "evidence": OMEN_EVIDENCE,
                            "derived": 1,
                            "source_version": data["equipment"].version,
                        }
                    )

    normalized.affixes = [affix_rows[key] for key in sorted(affix_rows)]
    normalized.unresolved_can_roll_on = [unresolved_seen[key] for key in sorted(unresolved_seen)]
    normalized.issues.extend(resolver.issues)

    # -- statistics --------------------------------------------------------
    covered_affixes = {
        row["affix_id"]
        for row in normalized.affix_base_ranges
    }
    normalized.stats = {
        "schema_version": SCHEMA_VERSION,
        "source_versions": {kind: ds.version for kind, ds in sorted(extraction.datasets.items())},
        "counts": normalized.counts(),
        "affixes_with_ranges": len(covered_affixes),
        "affixes_without_ranges": len(normalized.affixes) - len(covered_affixes),
        "tiers_total": sum(row["tier_count"] for row in normalized.affixes),
        "roll_slots_total": sum(row["roll_slot_count"] for row in normalized.affixes),
        "base_type_ids_referenced": len(
            {row["base_type_id"] for row in normalized.affix_eligible_bases}
        ),
        "range_rows_by_slot": _count_by(normalized.affix_base_ranges, "roll_slot"),
        "range_rows_by_tier": _count_by(normalized.affix_base_ranges, "tier"),
        "display_unresolved_ranges": sum(1 for row in normalized.affix_base_ranges if not row["display_resolved"]),
        "special_affix_type_counts": _count_by(normalized.affixes, "special_affix_type_name"),
        "affix_type_counts": _count_by(normalized.affixes, "type_name"),
        "standard_affix_effect_modifier_counts": _count_by(normalized.affixes, "standard_affix_effect_modifier"),
        "equipment_globals": {row["key"]: row["value"] for row in normalized.equipment_globals},
        "omen_idol_affix_effect_modifier": float(omen_modifier_constant),
        "omen_sub_bases": [
            {
                "base_type_id": sub["base_type_id"],
                "sub_type_id": sub["sub_type_id"],
                "slug": sub["slug"],
                "name": sub["name"],
                "affix_effectiveness": sub["affix_effectiveness"],
            }
            for sub in omen_sub_bases
        ],
        "omen_special_range_status": sorted({row["eligibility_status"] for row in normalized.omen_special_ranges}),
        "issues": normalized.issues,
    }
    return normalized


def _count_by(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(key)
        label = str(value)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _unique(values: list[Any]) -> list[Any]:
    """Stable de-duplication (tunklab data contains repeated canRollOn ids)."""
    seen: set[Any] = set()
    out: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out
