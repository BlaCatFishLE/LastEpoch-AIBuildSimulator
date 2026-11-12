"""SQLite writer + compact JSON index exporter for the normalized affix DB.

The SQLite database is the canonical artifact (large range table); the JSON files
under ``normalized/index/`` are small, review-friendly projections.  Writes are
deterministic: fixed insertion order, ``sort_keys`` JSON, no wall-clock values,
so rebuilding from the same snapshot rewrites byte-identical files.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import SCHEMA_VERSION
from .normalize import NormalizedData

SCHEMA_SQL = """
PRAGMA journal_mode = DELETE;
PRAGMA synchronous = OFF;
PRAGMA foreign_keys = OFF;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE source_datasets (
    kind          TEXT PRIMARY KEY,
    version       TEXT NOT NULL,
    chunk_file    TEXT NOT NULL,
    chunk_sha256  TEXT NOT NULL,
    char_start    INTEGER NOT NULL,
    char_end      INTEGER NOT NULL,
    byte_start    INTEGER NOT NULL,
    byte_end      INTEGER NOT NULL,
    snippet_sha256 TEXT NOT NULL,
    locator       TEXT NOT NULL
);

CREATE TABLE affixes (
    affix_id            INTEGER PRIMARY KEY,
    slug                TEXT NOT NULL UNIQUE,
    name                TEXT NOT NULL,
    display_name        TEXT NOT NULL,
    display_name_effective TEXT NOT NULL,
    title               TEXT NOT NULL,
    type                INTEGER NOT NULL,
    type_name           TEXT,
    special_affix_type  INTEGER NOT NULL,
    special_affix_type_name TEXT,
    rolls_on            INTEGER NOT NULL,
    rolls_on_name       TEXT,
    group_id            INTEGER NOT NULL,
    level_requirement   INTEGER NOT NULL,
    class_specificity   INTEGER NOT NULL,
    class_specificity_names TEXT NOT NULL,
    standard_affix_effect_modifier REAL NOT NULL,
    reroll_chance       REAL NOT NULL,
    weapon_effect       INTEGER NOT NULL,
    t6_compatibility    INTEGER NOT NULL,
    stat_property       INTEGER,
    stat_property_name  TEXT,
    tags                INTEGER,
    special_tag         INTEGER,
    extra_tag           INTEGER,
    mod_type            INTEGER,
    mod_type_name       TEXT,
    set_property        INTEGER,
    unique_id           INTEGER,
    use_generated_name_for_display_name INTEGER NOT NULL,
    tier_count          INTEGER NOT NULL,
    roll_slot_count     INTEGER NOT NULL,
    can_roll_on_json    TEXT NOT NULL,
    specific_reroll_chances_json TEXT NOT NULL,
    tiers_json          TEXT NOT NULL,
    affix_properties_json TEXT,
    raw_json            TEXT NOT NULL
);

CREATE TABLE affix_tiers (
    affix_id       INTEGER NOT NULL,
    tier           INTEGER NOT NULL,
    tier_count     INTEGER NOT NULL,
    extra_roll_count INTEGER NOT NULL,
    raw_json       TEXT NOT NULL,
    PRIMARY KEY (affix_id, tier)
);

CREATE TABLE equipment_types (
    base_type_id     INTEGER PRIMARY KEY,
    base_type_name   TEXT NOT NULL,
    display_name     TEXT NOT NULL,
    is_equippable    INTEGER NOT NULL,
    slot_type        INTEGER,
    is_weapon        INTEGER NOT NULL,
    affix_effect_modifier REAL NOT NULL,
    maximum_affixes  INTEGER,
    max_sockets      INTEGER,
    grid_x           INTEGER,
    grid_y           INTEGER,
    minimum_drop_level INTEGER,
    gold_cost_modifier REAL,
    added_gamble_cost INTEGER,
    reroll_chance    REAL,
    class_affinity   INTEGER,
    affinity_reroll_chance REAL,
    sub_item_count   INTEGER NOT NULL,
    raw_json         TEXT NOT NULL
);

CREATE TABLE equipment_sub_bases (
    base_type_id     INTEGER NOT NULL,
    sub_type_id      INTEGER NOT NULL,
    slug             TEXT,
    name             TEXT,
    display_name     TEXT,
    level_requirement INTEGER,
    cannot_drop      INTEGER NOT NULL,
    item_tags        INTEGER,
    class_requirement INTEGER,
    sub_class_requirement INTEGER,
    affix_effectiveness INTEGER,
    only_drop_when_requiring_affix_effectiveness INTEGER,
    implicits_json   TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    PRIMARY KEY (base_type_id, sub_type_id)
);

CREATE TABLE equipment_globals (
    key              TEXT PRIMARY KEY,
    value_json       TEXT NOT NULL
);

CREATE TABLE stat_properties (
    property_id      INTEGER PRIMARY KEY,
    property_name    TEXT,
    rounding_for_added INTEGER,
    rounding_for_added_name TEXT,
    display_added_as_percentage INTEGER NOT NULL,
    display_added_as_tenth_of_value INTEGER NOT NULL,
    display_as_percentage_of INTEGER NOT NULL,
    display_as_added_to INTEGER NOT NULL,
    dont_display_plus INTEGER NOT NULL,
    less_is_better   INTEGER NOT NULL,
    default_alt_text TEXT,
    raw_json         TEXT NOT NULL
);

CREATE TABLE player_stat_properties (
    tag_index        INTEGER PRIMARY KEY,
    property_name    TEXT,
    rounding_for_added INTEGER,
    rounding_for_added_name TEXT,
    display_added_as_percentage INTEGER NOT NULL,
    display_added_as_tenth_of_value INTEGER NOT NULL,
    display_as_percentage_of INTEGER NOT NULL,
    display_as_added_to INTEGER NOT NULL,
    dont_display_plus INTEGER NOT NULL,
    less_is_better   INTEGER NOT NULL,
    alt_text         TEXT,
    raw_json         TEXT NOT NULL
);

CREATE TABLE ability_properties (
    ability_id       INTEGER PRIMARY KEY,
    slot_count       INTEGER NOT NULL,
    raw_json         TEXT NOT NULL
);

CREATE TABLE affix_eligible_bases (
    affix_id         INTEGER NOT NULL,
    base_type_id     INTEGER NOT NULL,
    resolved         INTEGER NOT NULL,
    is_equippable    INTEGER NOT NULL,
    excluded_by_tunklab INTEGER NOT NULL,
    base_type_name   TEXT,
    PRIMARY KEY (affix_id, base_type_id)
);

CREATE TABLE unresolved_can_roll_on (
    base_type_id     INTEGER PRIMARY KEY,
    excluded_by_tunklab INTEGER NOT NULL,
    affix_count      INTEGER NOT NULL,
    affix_ids_json   TEXT NOT NULL,
    note             TEXT NOT NULL
);

CREATE TABLE omen_special_ranges (
    base_type_id     INTEGER NOT NULL,
    sub_type_id      INTEGER NOT NULL,
    sub_base_slug    TEXT,
    sub_base_name    TEXT,
    affix_id         INTEGER NOT NULL,
    tier             INTEGER NOT NULL,
    roll_slot        INTEGER NOT NULL,
    property_id      INTEGER NOT NULL,
    raw_min          REAL NOT NULL,
    raw_max          REAL NOT NULL,
    omen_affix_effect_modifier REAL NOT NULL,
    modifier         REAL NOT NULL,
    modifier_decimal TEXT NOT NULL,
    effective_min    REAL NOT NULL,
    effective_max    REAL NOT NULL,
    effective_min_decimal TEXT NOT NULL,
    effective_max_decimal TEXT NOT NULL,
    display_unit     TEXT NOT NULL,
    display_min      REAL,
    display_max      REAL,
    tunklab_display_min REAL,
    tunklab_display_max REAL,
    eligibility_status TEXT NOT NULL,
    evidence         TEXT NOT NULL,
    derived          INTEGER NOT NULL,
    source_version   TEXT NOT NULL,
    PRIMARY KEY (base_type_id, sub_type_id, affix_id, tier, roll_slot)
);

CREATE TABLE affix_base_ranges (
    affix_id         INTEGER NOT NULL,
    base_type_id     INTEGER NOT NULL,
    tier             INTEGER NOT NULL,
    roll_slot        INTEGER NOT NULL,
    property_id      INTEGER NOT NULL,
    property_name    TEXT,
    mod_type         INTEGER NOT NULL,
    tags             INTEGER NOT NULL,
    special_tag      INTEGER NOT NULL,
    raw_min          REAL NOT NULL,
    raw_max          REAL NOT NULL,
    modifier         REAL NOT NULL,
    modifier_decimal TEXT NOT NULL,
    effective_min    REAL NOT NULL,
    effective_max    REAL NOT NULL,
    effective_min_decimal TEXT NOT NULL,
    effective_max_decimal TEXT NOT NULL,
    display_unit     TEXT NOT NULL,
    display_scale    INTEGER NOT NULL,
    display_min      REAL,
    display_max      REAL,
    tunklab_display_min REAL,
    tunklab_display_max REAL,
    display_policy   TEXT NOT NULL,
    tunklab_display_policy TEXT NOT NULL,
    display_resolved INTEGER NOT NULL,
    formula          TEXT NOT NULL,
    source_locator   TEXT NOT NULL,
    source_version   TEXT NOT NULL,
    PRIMARY KEY (affix_id, base_type_id, tier, roll_slot)
);

CREATE INDEX idx_ranges_affix_base ON affix_base_ranges (affix_id, base_type_id);
CREATE INDEX idx_ranges_base ON affix_base_ranges (base_type_id, affix_id);
CREATE INDEX idx_ranges_affix_tier ON affix_base_ranges (affix_id, tier);
CREATE INDEX idx_eligible_base ON affix_eligible_bases (base_type_id, affix_id);
"""


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(block)
    return hasher.hexdigest()


def write_sqlite(db_path: Path, normalized: NormalizedData, source_datasets: Iterable[dict[str, Any]]) -> None:
    """Create the SQLite database from scratch (existing file is replaced)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = db_path.with_suffix(db_path.suffix + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()
    connection = sqlite3.connect(tmp_path)
    try:
        connection.executescript(SCHEMA_SQL)
        cursor = connection.cursor()

        meta = {
            "schema": "affix-db",
            "schema_version": str(SCHEMA_VERSION),
            "canonical_format": "sqlite",
            "source": "tunklab",
            "source_data_version": normalized.stats["source_versions"].get("affixes", ""),
            "range_formula": "effective = raw * (1 + base.affixEffectModifier) / (1 + affix.standardAffixEffectModifier)",
            "display_policy_in_game": "unity-dotnet-round-half-to-even (game/Unity tooltip convention)",
            "display_policy_tunklab": "tunklab-js-math-round-half-up (tunklab Scaled Values table)",
            "generated_by": "tools/affix_db/build.py",
        }
        cursor.executemany("INSERT INTO meta (key, value) VALUES (?, ?)", sorted(meta.items()))

        for dataset in source_datasets:
            cursor.execute(
                """
                INSERT INTO source_datasets
                (kind, version, chunk_file, chunk_sha256, char_start, char_end,
                 byte_start, byte_end, snippet_sha256, locator)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    dataset["kind"],
                    dataset["version"],
                    dataset["chunk_file"],
                    dataset["chunk_sha256"],
                    dataset["char_start"],
                    dataset["char_end"],
                    dataset["byte_start"],
                    dataset["byte_end"],
                    dataset["snippet_sha256"],
                    dataset["locator"],
                ),
            )

        cursor.executemany(
            """
            INSERT INTO affixes VALUES (
                :affix_id, :slug, :name, :display_name, :display_name_effective, :title,
                :type, :type_name, :special_affix_type, :special_affix_type_name,
                :rolls_on, :rolls_on_name, :group_id, :level_requirement, :class_specificity,
                :class_specificity_names, :standard_affix_effect_modifier, :reroll_chance,
                :weapon_effect, :t6_compatibility, :stat_property, :stat_property_name,
                :tags, :special_tag, :extra_tag, :mod_type, :mod_type_name, :set_property,
                :unique_id, :use_generated_name_for_display_name, :tier_count, :roll_slot_count,
                :can_roll_on_json, :specific_reroll_chances_json, :tiers_json,
                :affix_properties_json, :raw_json)
            """,
            [
                {
                    "affix_id": row["affix_id"],
                    "slug": row["slug"],
                    "name": row["name"],
                    "display_name": row["display_name"],
                    "display_name_effective": row["display_name_effective"],
                    "title": row["title"],
                    "type": row["type"],
                    "type_name": row["type_name"],
                    "special_affix_type": row["special_affix_type"],
                    "special_affix_type_name": row["special_affix_type_name"],
                    "rolls_on": row["rolls_on"],
                    "rolls_on_name": row["rolls_on_name"],
                    "group_id": row["group_id"],
                    "level_requirement": row["level_requirement"],
                    "class_specificity": row["class_specificity"],
                    "class_specificity_names": row["class_specificity_names"],
                    "standard_affix_effect_modifier": row["standard_affix_effect_modifier"],
                    "reroll_chance": row["reroll_chance"],
                    "weapon_effect": row["weapon_effect"],
                    "t6_compatibility": row["t6_compatibility"],
                    "stat_property": row["stat_property"],
                    "stat_property_name": row["stat_property_name"],
                    "tags": row["tags"],
                    "special_tag": row["special_tag"],
                    "extra_tag": row["extra_tag"],
                    "mod_type": row["mod_type"],
                    "mod_type_name": row["mod_type_name"],
                    "set_property": row["set_property"],
                    "unique_id": row["unique_id"],
                    "use_generated_name_for_display_name": row["use_generated_name_for_display_name"],
                    "tier_count": row["tier_count"],
                    "roll_slot_count": row["roll_slot_count"],
                    "can_roll_on_json": _dumps(row["can_roll_on"]),
                    "specific_reroll_chances_json": _dumps(row["specific_reroll_chances"]),
                    "tiers_json": _dumps(row["tiers"]),
                    "affix_properties_json": _dumps(row["affix_properties"]) if row["affix_properties"] is not None else None,
                    "raw_json": _dumps(row["raw"]),
                }
                for row in normalized.affixes
            ],
        )

        cursor.executemany(
            "INSERT INTO affix_tiers (affix_id, tier, tier_count, extra_roll_count, raw_json) VALUES (?,?,?,?,?)",
            [
                (
                    row["affix_id"],
                    index + 1,
                    row["tier_count"],
                    len(row["raw"][index].get("extraRolls") or []),
                    _dumps(row["raw"][index]),
                )
                for row in normalized.affix_tiers
                for index in range(row["tier_count"])
            ],
        )

        cursor.executemany(
            """
            INSERT INTO equipment_types VALUES (
                :base_type_id, :base_type_name, :display_name, :is_equippable, :slot_type,
                :is_weapon, :affix_effect_modifier, :maximum_affixes, :max_sockets,
                :grid_x, :grid_y, :minimum_drop_level, :gold_cost_modifier, :added_gamble_cost,
                :reroll_chance, :class_affinity, :affinity_reroll_chance, :sub_item_count, :raw_json)
            """,
            [
                {
                    **{key: row[key] for key in (
                        "base_type_id", "base_type_name", "display_name", "is_equippable",
                        "slot_type", "is_weapon", "affix_effect_modifier", "maximum_affixes",
                        "max_sockets", "grid_x", "grid_y", "minimum_drop_level",
                        "gold_cost_modifier", "added_gamble_cost", "reroll_chance",
                        "class_affinity", "affinity_reroll_chance", "sub_item_count")},
                    "raw_json": _dumps(row["raw"]),
                }
                for row in normalized.equipment_types
            ],
        )

        cursor.executemany(
            """
            INSERT INTO equipment_sub_bases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    row["base_type_id"], row["sub_type_id"], row["slug"], row["name"],
                    row["display_name"], row["level_requirement"], row["cannot_drop"],
                    row["item_tags"], row["class_requirement"], row["sub_class_requirement"],
                    row["affix_effectiveness"], row["only_drop_when_requiring_affix_effectiveness"],
                    _dumps(row["implicits"]), _dumps(row["raw"]),
                )
                for row in normalized.equipment_sub_bases
            ],
        )

        cursor.executemany(
            "INSERT INTO equipment_globals (key, value_json) VALUES (?,?)",
            [(row["key"], _dumps(row["value"])) for row in normalized.equipment_globals],
        )

        cursor.executemany(
            """
            INSERT INTO stat_properties VALUES (
                :property_id, :property_name, :rounding_for_added, :rounding_for_added_name,
                :display_added_as_percentage, :display_added_as_tenth_of_value,
                :display_as_percentage_of, :display_as_added_to, :dont_display_plus,
                :less_is_better, :default_alt_text, :raw_json)
            """,
            [
                {
                    **{key: row[key] for key in (
                        "property_id", "property_name", "rounding_for_added",
                        "rounding_for_added_name", "display_added_as_percentage",
                        "display_added_as_tenth_of_value", "display_as_percentage_of",
                        "display_as_added_to", "dont_display_plus", "less_is_better",
                        "default_alt_text")},
                    "raw_json": _dumps(row["raw"]),
                }
                for row in normalized.player_properties
            ],
        )

        cursor.executemany(
            """
            INSERT INTO player_stat_properties VALUES (
                :tag_index, :property_name, :rounding_for_added, :rounding_for_added_name,
                :display_added_as_percentage, :display_added_as_tenth_of_value,
                :display_as_percentage_of, :display_as_added_to, :dont_display_plus,
                :less_is_better, :alt_text, :raw_json)
            """,
            [
                {
                    **{key: row[key] for key in (
                        "tag_index", "property_name", "rounding_for_added",
                        "rounding_for_added_name", "display_added_as_percentage",
                        "display_added_as_tenth_of_value", "display_as_percentage_of",
                        "display_as_added_to", "dont_display_plus", "less_is_better",
                        "alt_text")},
                    "raw_json": _dumps(row["raw"]),
                }
                for row in normalized.player_stat_properties
            ],
        )

        cursor.executemany(
            "INSERT INTO ability_properties VALUES (?,?,?)",
            [(row["ability_id"], row["slot_count"], _dumps(row["raw"])) for row in normalized.ability_properties],
        )

        cursor.executemany(
            """
            INSERT INTO affix_eligible_bases VALUES (?,?,?,?,?,?)
            """,
            [
                (
                    row["affix_id"], row["base_type_id"], row["resolved"],
                    row["is_equippable"], row["excluded_by_tunklab"], row["base_type_name"],
                )
                for row in normalized.affix_eligible_bases
            ],
        )

        cursor.executemany(
            "INSERT INTO unresolved_can_roll_on VALUES (?,?,?,?,?)",
            [
                (
                    row["base_type_id"], row["excluded_by_tunklab"], row["affix_count"],
                    _dumps(row["affix_ids_sample"]), row["note"],
                )
                for row in normalized.unresolved_can_roll_on
            ],
        )

        range_columns = (
            "affix_id", "base_type_id", "tier", "roll_slot", "property_id",
            "property_name", "mod_type", "tags", "special_tag", "raw_min", "raw_max",
            "modifier", "modifier_decimal", "effective_min", "effective_max",
            "effective_min_decimal", "effective_max_decimal", "display_unit",
            "display_scale", "display_min", "display_max", "tunklab_display_min",
            "tunklab_display_max", "display_policy", "tunklab_display_policy",
            "display_resolved", "formula", "source_locator", "source_version",
        )
        placeholders = ",".join("?" for _ in range_columns)
        cursor.executemany(
            f"INSERT INTO affix_base_ranges ({','.join(range_columns)}) VALUES ({placeholders})",
            [tuple(row[column] for column in range_columns) for row in normalized.affix_base_ranges],
        )

        omen_columns = (
            "base_type_id", "sub_type_id", "sub_base_slug", "sub_base_name", "affix_id",
            "tier", "roll_slot", "property_id", "raw_min", "raw_max",
            "omen_affix_effect_modifier", "modifier", "modifier_decimal", "effective_min",
            "effective_max", "effective_min_decimal", "effective_max_decimal", "display_unit",
            "display_min", "display_max", "tunklab_display_min", "tunklab_display_max",
            "eligibility_status", "evidence", "derived", "source_version",
        )
        omen_placeholders = ",".join("?" for _ in omen_columns)
        cursor.executemany(
            f"INSERT INTO omen_special_ranges ({','.join(omen_columns)}) VALUES ({omen_placeholders})",
            [tuple(row[column] for column in omen_columns) for row in normalized.omen_special_ranges],
        )

        connection.commit()
    finally:
        connection.close()
    if db_path.exists():
        db_path.unlink()
    tmp_path.replace(db_path)


# --------------------------------------------------------------------------
# JSON projections
# --------------------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False) + "\n", encoding="utf-8")


def write_json_indexes(index_dir: Path, normalized: NormalizedData) -> list[Path]:
    """Write the compact JSON projections (SQLite remains canonical)."""
    index_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    affix_index = [
        {
            "affixId": row["affix_id"],
            "slug": row["slug"],
            "name": row["name"],
            "displayName": row["display_name"],
            "displayNameEffective": row["display_name_effective"],
            "title": row["title"],
            "type": row["type"],
            "typeName": row["type_name"],
            "specialAffixType": row["special_affix_type"],
            "specialAffixTypeName": row["special_affix_type_name"],
            "rollsOn": row["rolls_on"],
            "group": row["group_id"],
            "levelRequirement": row["level_requirement"],
            "classSpecificity": row["class_specificity"],
            "classSpecificityNames": row["class_specificity_names"],
            "standardAffixEffectModifier": row["standard_affix_effect_modifier"],
            "statProperty": row["stat_property"],
            "statPropertyName": row["stat_property_name"],
            "modType": row["mod_type"],
            "modTypeName": row["mod_type_name"],
            "tierCount": row["tier_count"],
            "rollSlotCount": row["roll_slot_count"],
            "canRollOn": row["can_roll_on"],
        }
        for row in normalized.affixes
    ]
    path = index_dir / "affixes.json"
    _write_json(path, affix_index)
    written.append(path)

    equipment_index = [
        {
            "baseTypeID": row["base_type_id"],
            "baseTypeName": row["base_type_name"],
            "displayName": row["display_name"],
            "isEquippable": row["is_equippable"],
            "type": row["slot_type"],
            "isWeapon": row["is_weapon"],
            "affixEffectModifier": row["affix_effect_modifier"],
            "maximumAffixes": row["maximum_affixes"],
            "gridSize": {"x": row["grid_x"], "y": row["grid_y"]},
            "subItemCount": row["sub_item_count"],
        }
        for row in normalized.equipment_types
    ]
    path = index_dir / "equipment-types.json"
    _write_json(path, equipment_index)
    written.append(path)

    path = index_dir / "equipment-sub-bases.json"
    _write_json(
        path,
        [
            {
                "baseTypeID": row["base_type_id"],
                "subTypeID": row["sub_type_id"],
                "slug": row["slug"],
                "name": row["name"],
                "displayName": row["display_name"],
                "levelRequirement": row["level_requirement"],
                "cannotDrop": row["cannot_drop"],
                "itemTags": row["item_tags"],
                "classRequirement": row["class_requirement"],
                "affixEffectiveness": row["affix_effectiveness"],
                "onlyDropWhenRequiringAffixEffectiveness": bool(row["only_drop_when_requiring_affix_effectiveness"]),
            }
            for row in normalized.equipment_sub_bases
        ],
    )
    written.append(path)

    path = index_dir / "equipment-globals.json"
    _write_json(path, {row["key"]: row["value"] for row in normalized.equipment_globals})
    written.append(path)

    path = index_dir / "omen-special-ranges.json"
    _write_json(
        path,
        [
            {
                "baseTypeID": row["base_type_id"],
                "subTypeID": row["sub_type_id"],
                "subBaseSlug": row["sub_base_slug"],
                "subBaseName": row["sub_base_name"],
                "affixId": row["affix_id"],
                "tier": row["tier"],
                "rollSlot": row["roll_slot"],
                "propertyId": row["property_id"],
                "rawMin": row["raw_min"],
                "rawMax": row["raw_max"],
                "omenAffixEffectModifier": row["omen_affix_effect_modifier"],
                "modifier": row["modifier"],
                "effectiveMinDecimal": row["effective_min_decimal"],
                "effectiveMaxDecimal": row["effective_max_decimal"],
                "displayMin": row["display_min"],
                "displayMax": row["display_max"],
                "tunklabDisplayMin": row["tunklab_display_min"],
                "tunklabDisplayMax": row["tunklab_display_max"],
                "eligibilityStatus": row["eligibility_status"],
                "derived": bool(row["derived"]),
                "evidence": row["evidence"],
            }
            for row in normalized.omen_special_ranges
        ],
    )
    written.append(path)

    path = index_dir / "affix-eligible-bases.json"
    _write_json(
        path,
        [
            {
                "affixId": row["affix_id"],
                "baseTypeID": row["base_type_id"],
                "baseTypeName": row["base_type_name"],
                "resolved": row["resolved"],
                "isEquippable": row["is_equippable"],
                "excludedByTunklab": row["excluded_by_tunklab"],
            }
            for row in normalized.affix_eligible_bases
        ],
    )
    written.append(path)

    path = index_dir / "affix-tiers.json"
    _write_json(
        path,
        [
            {
                "affixId": row["affix_id"],
                "tierCount": row["tier_count"],
                "tiers": row["raw"],
            }
            for row in normalized.affix_tiers
        ],
    )
    written.append(path)

    path = index_dir / "stat-properties.json"
    _write_json(
        path,
        {
            "playerProperties": [
                {
                    "property": row["property_id"],
                    "propertyName": row["property_name"],
                    "roundingForAdded": row["rounding_for_added"],
                    "roundingForAddedName": row["rounding_for_added_name"],
                    "displayAddedAsPercentage": bool(row["display_added_as_percentage"]),
                    "displayAddedAsTenthOfValue": bool(row["display_added_as_tenth_of_value"]),
                    "displayAsPercentageOf": bool(row["display_as_percentage_of"]),
                    "displayAsAddedTo": bool(row["display_as_added_to"]),
                    "dontDisplayPlus": bool(row["dont_display_plus"]),
                    "lessIsBetter": bool(row["less_is_better"]),
                }
                for row in normalized.player_properties
            ],
            "playerStatProperties": [
                {
                    "tagIndex": row["tag_index"],
                    "propertyName": row["property_name"],
                    "roundingForAdded": row["rounding_for_added"],
                    "displayAddedAsPercentage": bool(row["display_added_as_percentage"]),
                }
                for row in normalized.player_stat_properties
            ],
            "abilityProperties": [
                {"abilityID": row["ability_id"], "slotCount": row["slot_count"]}
                for row in normalized.ability_properties
            ],
            "statPropertyEnum": {str(key): value for key, value in sorted(normalized.stat_property_enum.items())},
        },
    )
    written.append(path)

    path = index_dir / "summary.json"
    _write_json(
        path,
        {
            "schemaVersion": SCHEMA_VERSION,
            "source": "tunklab",
            "sourceDataVersion": normalized.stats["source_versions"].get("affixes"),
            "counts": normalized.stats["counts"],
            "stats": normalized.stats,
            "rangeFormula": "effective = raw * (1 + base.affixEffectModifier) / (1 + affix.standardAffixEffectModifier)",
            "displayPolicies": {
                "inGame": "unity-dotnet-round-half-to-even",
                "tunklabPage": "tunklab-js-math-round-half-up",
            },
        },
    )
    written.append(path)
    return written
