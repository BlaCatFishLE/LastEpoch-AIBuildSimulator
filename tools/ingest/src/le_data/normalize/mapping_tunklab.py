"""Normalization mapping for Tunklab source records.

Primary-source policy: Last Epoch Tools records are authoritative for shared
game entities (trees/nodes/abilities/buffs/ailments/affixes/uniques), so a
Tunklab record whose canonical id is in ``data/mappings/crosswalk.json`` is
stored as a secondary member: it contributes names, evidence and relationship
candidates but not duplicate typed rows.  Tunklab-only entities (monsters,
quests, timelines, monolith modifiers, item types/sets when unmatched) are kept
as typed-or-generic entities so nothing is dropped.

Tunklab already provides display text (name/description/nodeName/...) in its
data modules, so no translation layer is needed for this source.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..records import SourceRecord
from .base import (
    Crosswalk,
    EntityPlan,
    issue_id,
    make_entity_id,
    relation_id,
)

AT_DAMAGE_TYPES = [
    "Physical", "Fire", "Cold", "Lightning", "Necrotic", "Void", "Poison",
]

SKIP_KINDS = {
    "relation",          # handled in post_process as entity_relationships
    "cache",             # internal UI cache/config payload, no entity semantics
    "world_config",
    "monolith_config",
    "loot_group",        # loot-table configuration, not an entity
    "config_block",
}

_CONTEXT: dict[str, Any] = {}
_TRANSLATIONS: dict[str, str] = {}


def configure(context: dict[str, Any]) -> None:
    global _CONTEXT, _TRANSLATIONS
    _CONTEXT = context
    _TRANSLATIONS = context.get("translations", {})


def _crosswalk() -> Crosswalk:
    crosswalk = _CONTEXT.get("crosswalk")
    if isinstance(crosswalk, Crosswalk):
        return crosswalk
    return Crosswalk()


def _canonical_or_local(kind: str, scope: str, source_id: Any) -> str:
    sid = str(source_id)
    canonical = _crosswalk().canonical_for("tunklab", kind, scope, sid)
    if canonical:
        return canonical
    return make_entity_id(kind, "tunklab", scope, sid)


def _evidence(
    field_path: str,
    raw_value: Any,
    normalized_value: Any = None,
    *,
    unit: str | None = None,
    status: str = "known",
    method: str = "parsed",
    locator: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    # Keep known / absent / unparsed honestly separated: a missing source value
    # must never be recorded as a known value with a NULL normalized value.
    if status == "known":
        if normalized_value is None or normalized_value == "":
            if raw_value is None or raw_value == "":
                status = "absent"
            else:
                status = "unparsed"
    row = {
        "field_path": field_path,
        "raw_value": raw_value,
        "normalized_value": normalized_value,
        "unit": unit,
        "evidence_status": status,
        "extraction_method": method,
        "notes": notes,
    }
    if locator:
        row["locator"] = locator
    return row


_PERCENT_RE = re.compile(r"^([+-]?)(\d+(?:\.\d+)?)\s*%$")
_NUMBER_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?)$")


def _parse_stat_value(text: Any) -> tuple[str | None, float | None, str | None]:
    """Return (value_kind, normalized_value, unit) for a stat value string."""
    if text is None:
        return None, None, None
    raw = str(text).strip()
    match = _PERCENT_RE.match(raw)
    if match:
        sign = -1.0 if match.group(1) == "-" else 1.0
        return "percent_modifier", sign * float(match.group(2)) / 100.0, "ratio"
    match = _NUMBER_RE.match(raw)
    if match:
        return "flat", float(match.group(1)), "number"
    return "unparsed", None, None


def _effect_rows(
    owner_entity_id: str,
    owner_kind: str,
    stats: list[dict[str, Any]],
    *,
    source_id: str,
    scope: str,
    field_prefix: str,
) -> tuple[list[tuple[str, dict[str, Any]]], list[dict[str, Any]], list[dict[str, Any]]]:
    typed: list[tuple[str, dict[str, Any]]] = []
    evidence: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    for index, stat in enumerate(stats):
        if not isinstance(stat, dict):
            continue
        text = stat.get("value")
        value_kind, normalized, unit = _parse_stat_value(text)
        stat_name = stat.get("statName")
        property_id = stat.get("property")
        effect_id = "effect:" + hashlib.sha256(
            f"{source_id}|{scope}|{field_prefix}|{index}".encode("utf-8")
        ).hexdigest()[:24]
        typed.append(
            (
                "effects",
                {
                    "effect_id": effect_id,
                    "owner_entity_id": owner_entity_id,
                    "effect_category": "stat_mod",
                    "raw_text": stat_name,
                    "parse_status": "parsed" if normalized is not None else "unparsed",
                    "extensions": json.dumps(
                        {
                            "property": property_id,
                            "noScaling": stat.get("noScaling"),
                            "downside": stat.get("downside"),
                            "sprite": stat.get("sprite"),
                            "tags": stat.get("tags"),
                        },
                        ensure_ascii=False,
                    ),
                },
            )
        )
        typed.append(
            (
                "effect_values",
                {
                    "value_id": f"{effect_id}:0",
                    "effect_id": effect_id,
                    "value_kind": value_kind,
                    "value_numeric": normalized,
                    "unit": unit,
                    "raw": json.dumps({"value": text, "property": property_id}, ensure_ascii=False),
                },
            )
        )
        evidence.append(
            _evidence(
                f"{field_prefix}[{index}].value",
                text,
                normalized,
                unit=unit,
                status="known" if normalized is not None else "unparsed",
                method="parsed" if normalized is not None else "unparsed",
            )
        )
        if normalized is None:
            issues.append(
                {
                    "issue_type": "unparsed_stat_value",
                    "severity": "info",
                    "field_path": f"{field_prefix}[{index}].value",
                    "message": f"stat value {text!r} is not a plain flat/percent value",
                    "actual": str(text),
                }
            )
    return typed, evidence, issues


# -- classes / masteries ----------------------------------------------------
def map_class(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("className")
    entity_id = make_entity_id("class", "tunklab", record.source_scope, record.source_id)
    extensions = {
        key: raw.get(key)
        for key in (
            "classID", "treeID", "baseHealth", "baseMana", "healthPerLevel",
            "manaPerLevel", "manaRegen", "healthRegen", "baseStrength",
            "baseDexterity", "baseIntelligence", "baseAttunement", "baseVitality",
            "baseStunAvoidance", "baseEndurance", "baseEnduranceThreshold",
            "baseMoreDoTDamageTaken", "firstLevelForMinionScaling",
        )
        if raw.get(key) is not None
    }
    plan = EntityPlan(
        source_record=record,
        kind="class",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        typed_rows=[
            (
                "classes",
                {
                    "entity_id": entity_id,
                    "source_class_id": raw.get("classID"),
                    "extensions": json.dumps(extensions, ensure_ascii=False),
                },
            )
        ],
        evidence=[
            _evidence("canonical_name", raw.get("className"), name),
            _evidence("source_class_id", raw.get("classID"), raw.get("classID")),
        ],
    )
    if name:
        plan.relationships.append(
            {
                "relationship_id": relation_id("tunklab", record.source_id, "class_has_tree", str(raw.get("treeID"))),
                "source": "tunklab",
                "source_snapshot_id": record.source_snapshot_id,
                "from_entity_id": entity_id,
                "relationship_type": "has_tree",
                "to_entity_id": None,
                "raw_target_ref": json.dumps({"treeId": raw.get("treeID")}, ensure_ascii=False),
                "resolution_status": "unresolved",
                "evidence": "class.treeID",
            }
        )
    return plan


def map_mastery(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("name")
    class_name = record.source_scope
    entity_id = make_entity_id("mastery", "tunklab", class_name, record.source_id)
    plan = EntityPlan(
        source_record=record,
        kind="mastery",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
    )
    plan.evidence.append(_evidence("canonical_name", name, name))
    if class_name:
        class_entity_id = _canonical_or_local("class", "", class_name)
        plan.relationships.append(
            {
                "relationship_id": relation_id("tunklab", record.source_id, "belongs_to_class", class_name),
                "source": "tunklab",
                "source_snapshot_id": record.source_snapshot_id,
                "from_entity_id": entity_id,
                "relationship_type": "belongs_to_class",
                "to_entity_id": class_entity_id,
                "raw_target_ref": class_name,
                "resolution_status": "resolved",
                "evidence": "mastery.class",
            }
        )
    # typed row written even when secondary is harmless: persist skips typed rows
    # for merged members, so include it for the primary path.
    plan.typed_rows.append(
        (
            "masteries",
            {
                "entity_id": entity_id,
                "source_mastery_id": raw.get("name"),
                "extensions": json.dumps(
                    {"masteryAbility": raw.get("masteryAbility"), "abilities": raw.get("abilities")},
                    ensure_ascii=False,
                ),
            },
        )
    )
    return plan


# -- abilities --------------------------------------------------------------
def _ability_flags(raw: dict[str, Any]) -> list[str]:
    flags = []
    for key, flag in (
        ("channelled", "channelled"),
        ("channelledAbility", "channelled"),
        ("transform", "transform"),
        ("companion", "companion"),
        ("traversalSkill", "traversal"),
        ("countsAsMovementAbility", "movement"),
        ("instantCastForPlayer", "instant_cast"),
    ):
        if raw.get(key):
            flags.append(flag)
    return flags


def map_ability(record: SourceRecord) -> EntityPlan:
    """Tunklab abilities are kept as source-level ability records.

    Last Epoch Tools is the primary source for the ``skill`` entity kind; a
    Tunklab player ability is merged onto its LE Tools skill through the
    explicit crosswalk (playerAbilityID == LE Tools ability id).  Tunklab
    abilities without a counterpart stay generic ``ability`` entities so that
    monster/sub-abilities are not misrepresented as player skills.
    """
    raw = record.raw
    name = raw.get("name") or raw.get("id")
    entity_id = make_entity_id("ability", "tunklab", record.source_scope, record.source_id)
    player_id = raw.get("playerAbilityID")
    extensions = {
        key: raw.get(key)
        for key in (
            "id", "name", "abilityId", "playerAbilityID", "description", "manaCost",
            "charges", "tags", "fakeTags", "extraTags", "damage", "speed", "range", "image",
        )
        if raw.get(key) is not None
    }
    flags = _ability_flags(raw)
    if flags:
        extensions["flags"] = flags
    plan = EntityPlan(
        source_record=record,
        kind="ability",
        entity_id=entity_id,
        canonical_name=name,
        record_status="provisional",
        names=[{"name": name, "language": "en"}] if name else [],
        extensions=extensions,
        evidence=[
            _evidence("canonical_name", raw.get("name"), name),
            _evidence("player_ability_id", player_id, player_id),
            _evidence("ability_id", raw.get("abilityId"), raw.get("abilityId")),
        ],
    )
    if raw.get("description"):
        _attach_description_effect(plan, record, raw.get("description"), "ability")
    return plan


def map_ability_properties(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = f"Ability property list {record.source_id}"
    entity_id = make_entity_id("ability_properties", "tunklab", record.source_scope, record.source_id)
    return EntityPlan(
        source_record=record,
        kind="ability_properties",
        entity_id=entity_id,
        canonical_name=name,
        record_status="provisional",
        names=[{"name": name, "language": "en", "is_alias": True, "source": "tunklab"}],
        extensions={"raw": raw},
        evidence=[
            _evidence(
                "canonical_name",
                None,
                None,
                status="absent",
                notes="source provides property names but no list entity name",
            )
        ],
    )


# -- trees and nodes --------------------------------------------------------
def _tree_entity_id(tree_id: str) -> str:
    crosswalk = _crosswalk()
    for kind in ("skill_tree", "passive_tree"):
        canonical = crosswalk.canonical_for("tunklab", kind, "", str(tree_id))
        if canonical:
            return canonical
    return make_entity_id("tree", "tunklab", "", str(tree_id))


def map_skill_tree(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    tree_id = raw.get("treeId") or record.source_id
    if not tree_id:
        # The Tunklab payload contains one empty skill-tree key; it carries no
        # nodes and no id, so it is not an entity (the source record is kept).
        return None
    node_count = raw.get("nodeCount")
    if node_count is None and isinstance(raw.get("nodes"), list):
        node_count = len(raw["nodes"])
    entity_id = make_entity_id("tree", "tunklab", record.source_scope, str(tree_id))
    name = f"Skill tree {tree_id}" if tree_id else None
    plan = EntityPlan(
        source_record=record,
        kind="tree",
        entity_id=entity_id,
        canonical_name=None,
        record_status="provisional",
        extensions={"tree_id": tree_id, "node_count": node_count},
        evidence=[
            _evidence("source_tree_id", tree_id, str(tree_id)),
            _evidence("node_count", node_count, node_count),
            _evidence(
                "canonical_name",
                None,
                None,
                status="absent",
                notes="Tunklab tree objects carry no display name; name comes from the primary source",
            ),
        ],
    )
    plan.typed_rows.append(
        (
            "trees",
            {
                "entity_id": entity_id,
                "tree_kind": "skill",
                "source_tree_id": str(tree_id),
                "node_count": node_count,
            },
        )
    )
    if name:
        plan.names.append({"name": name, "language": "en", "is_alias": True, "source": "tunklab"})
    return plan


def map_passive_tree(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    tree_id = raw.get("treeId") or record.source_id
    nodes = raw.get("nodes") if isinstance(raw.get("nodes"), list) else []
    if not tree_id:
        return None
    entity_id = make_entity_id("tree", "tunklab", record.source_scope, str(tree_id))
    plan = EntityPlan(
        source_record=record,
        kind="tree",
        entity_id=entity_id,
        record_status="provisional",
        extensions={"tree_id": tree_id, "node_count": len(nodes)},
        evidence=[
            _evidence("source_tree_id", tree_id, str(tree_id)),
            _evidence("node_count", len(nodes), len(nodes)),
        ],
    )
    plan.typed_rows.append(
        (
            "trees",
            {
                "entity_id": entity_id,
                "tree_kind": "passive",
                "source_tree_id": str(tree_id),
                "node_count": len(nodes),
            },
        )
    )
    return plan


def _map_node(record: SourceRecord, tree_kind: str) -> EntityPlan:
    raw = record.raw
    tree_id = record.source_scope or str(raw.get("treeId") or "")
    node_id = raw.get("id", record.source_id)
    entity_id = make_entity_id("node", "tunklab", tree_id, str(node_id))
    name = raw.get("nodeName")
    description = raw.get("description")
    stats = raw.get("stats") if isinstance(raw.get("stats"), list) else []
    requirements = raw.get("requirements") if isinstance(raw.get("requirements"), list) else []
    plan = EntityPlan(
        source_record=record,
        kind="node",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={
            "mastery": raw.get("mastery"),
            "mastery_requirement": raw.get("masteryReq", raw.get("masteryRequirement")),
            "alt_text": raw.get("altText"),
            "no_scaling_type": raw.get("noScalingType"),
            "no_scaling_point_threshold": raw.get("noScalingPointThreshold"),
            "tree_kind": tree_kind,
        },
        evidence=[
            _evidence("canonical_name", name, name),
            _evidence("description", description, description),
            _evidence("max_points", raw.get("maxPoints"), raw.get("maxPoints")),
        ],
    )
    plan.typed_rows.append(
        (
            "nodes",
            {
                "entity_id": entity_id,
                "tree_entity_id": _tree_entity_id(tree_id),
                "node_type": tree_kind,
                "max_points": raw.get("maxPoints"),
                "node_index": node_id,
                "description": description,
            },
        )
    )
    effect_rows, effect_evidence, issues = _effect_rows(
        entity_id,
        "node",
        stats,
        source_id=record.source_id,
        scope=record.source_scope,
        field_prefix="stats",
    )
    plan.typed_rows.extend(effect_rows)
    plan.evidence.extend(effect_evidence)
    plan.issues.extend(issues)
    for index, requirement in enumerate(requirements):
        if not isinstance(requirement, dict):
            continue
        plan.evidence.append(
            _evidence(
                f"requirements[{index}]",
                requirement,
                requirement,
                method="parsed",
            )
        )
    if description is None or description == "":
        # already covered by the description evidence row above (absent status)
        pass
    return plan


def map_skill_tree_node(record: SourceRecord) -> EntityPlan:
    return _map_node(record, "skill")


def map_passive_tree_node(record: SourceRecord) -> EntityPlan:
    return _map_node(record, "passive")


# -- buffs and ailments -----------------------------------------------------
_UI_MAX_INSTANCES_ZERO_NOTE = (
    "UI displays maxInstances=0; the mechanic meaning of a 0 in the UI display "
    "table is not established, so no numeric stack cap is stored. The raw 0 is "
    "preserved in lifecycle/stack_rule."
)


def _ui_max_instances(raw_value: Any) -> int | None:
    """Return the UI display value as an int, or None when the source has none."""
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return None
    return int(raw_value)


def map_buff(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("displayName")
    entity_id = make_entity_id("buff", "tunklab", record.source_scope, record.source_id)
    raw_max = raw.get("maxInstances")
    ui_max_instances = _ui_max_instances(raw_max)
    if ui_max_instances is None:
        # The source does not give a stack value at all: NULL, absent evidence.
        max_stacks = None
        max_stacks_basis = "unknown"
        max_stacks_evidence = _evidence(
            "max_stacks", raw_max, None, status="absent",
            notes="source does not provide maxInstances",
        )
    elif ui_max_instances == 0:
        # P1-6: 0 is a display-table value, not evidence of an unlimited mechanic.
        max_stacks = None
        max_stacks_basis = "raw_ui"
        max_stacks_evidence = _evidence(
            "max_stacks", raw_max, None, status="unparsed", method="unparsed",
            notes=_UI_MAX_INSTANCES_ZERO_NOTE,
        )
    else:
        max_stacks = ui_max_instances
        max_stacks_basis = "raw_ui"
        max_stacks_evidence = _evidence(
            "max_stacks", raw_max, max_stacks,
            notes="raw UI display value of the stack cap; no separate mechanic "
                  "evidence is available for this source",
        )
    lifecycle = {
        "noFixedDuration": raw.get("noFixedDuration"),
        "newStacksRefreshDuration": raw.get("newStacksRefreshDuration"),
        "raw_max_instances": raw_max,
    }
    if ui_max_instances == 0:
        lifecycle["ui_semantics"] = "unknown"
    elif ui_max_instances is not None:
        lifecycle["max_instances_interpretation"] = "numeric"
    plan = EntityPlan(
        source_record=record,
        kind="buff",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        evidence=[
            _evidence("canonical_name", name, name),
            max_stacks_evidence,
            _evidence(
                "ui_max_instances",
                raw_max,
                ui_max_instances,
                status="known" if ui_max_instances is not None else "absent",
                notes="UI display value kept separate from confirmed mechanic stacks",
            ),
            _evidence(
                "duration_ms",
                None,
                None,
                status="absent",
                notes=f"no duration given; noFixedDuration={raw.get('noFixedDuration')}",
            ),
            _evidence("buff_category", raw.get("positive"), "buff" if raw.get("positive") else "debuff",
                      method="inferred"),
        ],
    )
    plan.typed_rows.append(
        (
            "buffs",
            {
                "entity_id": entity_id,
                "buff_category": "buff" if raw.get("positive") else "debuff",
                "max_stacks": max_stacks,
                "ui_max_instances": ui_max_instances,
                "max_stacks_basis": max_stacks_basis,
                "target_scope": "unknown",
                "lifecycle": json.dumps(lifecycle, ensure_ascii=False),
            },
        )
    )
    _attach_description_effect(plan, record, raw.get("description"), "buff")
    return plan


def _attach_description_effect(plan: EntityPlan, record: SourceRecord, text: Any, owner_kind: str) -> None:
    if not text:
        plan.evidence.append(
            _evidence("description", text, None, status="absent", notes="not provided")
        )
        return
    effect_id = "effect:" + hashlib.sha256(
        f"tunklab|{record.entity_kind}|{record.source_id}|description".encode("utf-8")
    ).hexdigest()[:24]
    plan.typed_rows.append(
        (
            "effects",
            {
                "effect_id": effect_id,
                "owner_entity_id": plan.entity_id,
                "effect_category": "description",
                "raw_text": text,
                "parse_status": "unparsed",
            },
        )
    )
    plan.evidence.append(
        _evidence("description", text, text, method="parsed")
    )


def map_ailment(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("displayName") or raw.get("name")
    entity_id = make_entity_id("ailment", "tunklab", record.source_scope, record.source_id)
    damage = raw.get("baseDamage") if isinstance(raw.get("baseDamage"), dict) else {}
    damage_values = damage.get("damage") if isinstance(damage.get("damage"), list) else []
    damage_type = None
    base_damage = None
    for index, value in enumerate(damage_values):
        if value:
            damage_type = AT_DAMAGE_TYPES[index] if index < len(AT_DAMAGE_TYPES) else str(index)
            base_damage = value
            break
    duration_ms = None
    if raw.get("duration") is not None:
        duration_ms = int(round(float(raw["duration"]) * 1000))
    max_instances = raw.get("maxInstances")
    ui_max_instances = _ui_max_instances(max_instances)
    if ui_max_instances == 0:
        max_stacks_evidence = _evidence(
            "max_stacks", max_instances, None, status="unparsed", method="unparsed",
            notes=_UI_MAX_INSTANCES_ZERO_NOTE,
        )
    elif ui_max_instances is None:
        max_stacks_evidence = _evidence(
            "max_stacks", max_instances, None, status="absent",
            notes="source does not provide maxInstances",
        )
    else:
        max_stacks_evidence = _evidence(
            "max_stacks", max_instances, ui_max_instances,
            notes="raw UI display value of the stack cap",
        )
    stack_rule = {
        "maxInstances": max_instances,
        "dealsAllDamageAtEnd": raw.get("dealsAllDamageAtEnd"),
    }
    if ui_max_instances == 0:
        # P1-6: the UI's 0 does not establish an unlimited mechanic.
        stack_rule["ui_semantics"] = "unknown"
    elif ui_max_instances is not None:
        stack_rule["max_instances_interpretation"] = "numeric"
    plan = EntityPlan(
        source_record=record,
        kind="ailment",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        evidence=[
            _evidence("canonical_name", name, name),
            _evidence("duration_ms", raw.get("duration"), duration_ms, unit="ms"),
            _evidence("base_damage", damage_values, base_damage, unit="flat", notes=damage_type),
            max_stacks_evidence,
        ],
    )
    plan.typed_rows.append(
        (
            "ailments",
            {
                "entity_id": entity_id,
                "damage_type": damage_type,
                "base_damage": base_damage,
                "duration_ms": duration_ms,
                "stack_rule": json.dumps(stack_rule, ensure_ascii=False),
                "scaling_tags": json.dumps(raw.get("tags"), ensure_ascii=False)
                if raw.get("tags") is not None
                else None,
            },
        )
    )
    _attach_description_effect(plan, record, raw.get("description"), "ailment")
    return plan


# -- items, affixes, stats --------------------------------------------------
def map_affix(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("affixDisplayName") or raw.get("affixName")
    entity_id = make_entity_id("affix", "tunklab", record.source_scope, record.source_id)
    tiers = raw.get("tiers") if isinstance(raw.get("tiers"), list) else []
    plan = EntityPlan(
        source_record=record,
        kind="affix",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={
            "affixName": raw.get("affixName"),
            "affixTitle": raw.get("affixTitle"),
            "slug": raw.get("slug"),
            "rollsOn": raw.get("rollsOn"),
            "type": raw.get("type"),
            "specialAffixType": raw.get("specialAffixType"),
            "canRollOn": raw.get("canRollOn"),
        },
        evidence=[
            _evidence("canonical_name", raw.get("affixName"), name),
            _evidence("affix_title", raw.get("affixTitle"), raw.get("affixTitle")),
            _evidence(
                "affix_type",
                {"rollsOn": raw.get("rollsOn"), "type": raw.get("type")},
                None,
                status="unparsed",
                notes="prefix/suffix semantics not documented by this source",
            ),
            _evidence("tier_count", len(tiers), len(tiers)),
        ],
    )
    plan.typed_rows.append(
        (
            "affixes",
            {
                "entity_id": entity_id,
                "source_affix_id": str(raw.get("affixId")) if raw.get("affixId") is not None else None,
                "affix_title": raw.get("affixTitle"),
                "group_id": str(raw.get("group")) if raw.get("group") is not None else None,
                "affix_display_name": raw.get("affixDisplayName"),
                "extensions": json.dumps(
                    {"rollsOn": raw.get("rollsOn"), "type": raw.get("type"),
                     "canRollOn": raw.get("canRollOn")},
                    ensure_ascii=False,
                ),
            },
        )
    )
    for index, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            continue
        extra = tier.get("extraRolls") or []
        roll_min = tier.get("minRoll")
        roll_max = tier.get("maxRoll")
        inverted = (
            roll_min is not None
            and roll_max is not None
            and roll_min > roll_max
        )
        if inverted:
            # Some source tiers store an inverted range; normalise the numeric
            # columns but keep the raw values in evidence and the tier extension.
            roll_min, roll_max = roll_max, roll_min
            plan.evidence.append(
                _evidence(
                    f"tiers[{index}].range",
                    {"minRoll": tier.get("minRoll"), "maxRoll": tier.get("maxRoll")},
                    {"min": roll_min, "max": roll_max},
                    method="converted",
                    notes="source tier range had min > max; bounds swapped for the normalized columns",
                )
            )
        tier_id = "tier:" + hashlib.sha256(
            f"tunklab|{record.source_scope}|{record.source_id}|{index}".encode("utf-8")
        ).hexdigest()[:24]
        plan.typed_rows.append(
            (
                "affix_tiers",
                {
                    "tier_id": tier_id,
                    "affix_entity_id": entity_id,
                    "tier_index": index,
                    "roll_min": roll_min,
                    "roll_max": roll_max,
                    "extra_rolls": json.dumps(extra, ensure_ascii=False) if extra else None,
                    "extensions": json.dumps(
                        {"raw_min_roll": tier.get("minRoll"), "raw_max_roll": tier.get("maxRoll"),
                         "range_inverted": inverted},
                        ensure_ascii=False,
                    ),
                },
            )
        )
    if not tiers:
        plan.evidence.append(
            _evidence("tiers", None, None, status="absent", notes="no tier data in source")
        )
    return plan


def map_unique_item(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("name") or raw.get("displayName")
    entity_id = make_entity_id("item", "tunklab", record.source_scope, record.source_id)
    mods = raw.get("mods") if isinstance(raw.get("mods"), list) else []
    plan = EntityPlan(
        source_record=record,
        kind="item",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={
            "uniqueId": raw.get("uniqueId"),
            "setId": raw.get("setId"),
            "baseType": raw.get("baseType"),
            "subTypes": raw.get("subTypes"),
            "legendaryType": raw.get("legendaryType"),
        },
        evidence=[
            _evidence("canonical_name", name, name),
            _evidence("is_unique", True, True),
            _evidence("level_requirement", raw.get("levelRequirement"), raw.get("levelRequirement")),
            _evidence("mod_count", len(mods), len(mods)),
        ],
    )
    plan.typed_rows.append(
        (
            "items",
            {
                "entity_id": entity_id,
                "item_category": "unique",
                "is_unique": 1,
                "level_requirement": raw.get("levelRequirement"),
                "base_type": str(raw.get("baseType")) if raw.get("baseType") is not None else None,
                "extensions": json.dumps(
                    {"uniqueId": raw.get("uniqueId"), "setId": raw.get("setId"),
                     "subTypes": raw.get("subTypes")},
                    ensure_ascii=False,
                ),
            },
        )
    )
    for index, mod in enumerate(mods):
        if not isinstance(mod, dict):
            continue
        effect_id = "effect:" + hashlib.sha256(
            f"tunklab|unique|{record.source_id}|{index}".encode("utf-8")
        ).hexdigest()[:24]
        plan.typed_rows.append(
            (
                "effects",
                {
                    "effect_id": effect_id,
                    "owner_entity_id": entity_id,
                    "effect_category": "unique_mod",
                    "parse_status": "parsed",
                    "extensions": json.dumps(mod, ensure_ascii=False),
                },
            )
        )
        plan.typed_rows.append(
            (
                "effect_values",
                {
                    "value_id": f"{effect_id}:0",
                    "effect_id": effect_id,
                    "value_kind": "modifier",
                    "value_numeric": mod.get("value"),
                    "value_min": mod.get("value"),
                    "value_max": mod.get("maxValue"),
                    "raw": json.dumps(mod, ensure_ascii=False),
                },
            )
        )
    return plan


def map_item_type(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("baseTypeName") or raw.get("displayName")
    entity_id = make_entity_id("item_type", "tunklab", record.source_scope, record.source_id)
    return EntityPlan(
        source_record=record,
        kind="item_type",
        entity_id=entity_id,
        canonical_name=name,
        record_status="provisional",
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": raw},
        evidence=[_evidence("canonical_name", name, name)],
    )


def map_item_set(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("setName") or f"Set {raw.get('setID')}"
    entity_id = make_entity_id("set", "tunklab", record.source_scope, record.source_id)
    plan = EntityPlan(
        source_record=record,
        kind="set",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": raw},
        evidence=[
            _evidence("canonical_name", raw.get("setName"), name),
            _evidence("set_id", raw.get("setID"), raw.get("setID")),
        ],
    )
    if raw.get("setID") is not None:
        plan.evidence.append(_evidence("set_membership", raw.get("setID"), raw.get("setID")))
    return plan


def map_property(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("propertyName")
    entity_id = make_entity_id("stat", "tunklab", record.source_scope, record.source_id)
    unit = None
    if raw.get("displayAddedAsPercentage") or raw.get("displayAsPercentageOf"):
        unit = "percent"
    plan = EntityPlan(
        source_record=record,
        kind="stat",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": raw},
        evidence=[
            _evidence("canonical_name", name, name),
            _evidence("unit", raw.get("displayAddedAsPercentage"), unit, method="inferred"),
            _evidence("raw_enum", raw.get("property"), raw.get("property")),
        ],
    )
    plan.typed_rows.append(
        (
            "stats",
            {
                "stat_id": entity_id,
                "name": name,
                "unit": unit,
                "description": raw.get("defaultAltText"),
                "raw_enum": str(raw.get("property")) if raw.get("property") is not None else None,
                "source_scope": record.source_scope,
            },
        )
    )
    return plan


def map_property_text(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("propertyName")
    entity_id = make_entity_id("property_text", "tunklab", record.source_scope, record.source_id)
    return EntityPlan(
        source_record=record,
        kind="property_text",
        entity_id=entity_id,
        canonical_name=name,
        record_status="provisional",
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": raw},
    )


def map_monster(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("name") or record.source_id
    entity_id = make_entity_id("monster", "tunklab", record.source_scope, record.source_id)
    return EntityPlan(
        source_record=record,
        kind="monster",
        entity_id=entity_id,
        canonical_name=name,
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": raw},
        evidence=[
            _evidence("canonical_name", raw.get("name"), name),
            _evidence("health", raw.get("health"), raw.get("health")),
            _evidence("armour", raw.get("armor"), raw.get("armor")),
        ],
    )


def _generic(record: SourceRecord, kind: str, name: str | None = None) -> EntityPlan:
    entity_id = make_entity_id(kind, "tunklab", record.source_scope, record.source_id)
    return EntityPlan(
        source_record=record,
        kind=kind,
        entity_id=entity_id,
        canonical_name=name,
        record_status="provisional",
        names=[{"name": name, "language": "en"}] if name else [],
        extensions={"raw": record.raw},
        evidence=[_evidence("canonical_name", name, name)],
    )


def map_quest(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("displayName") or raw.get("name") or raw.get("questName") or record.source_id
    return _generic(record, "quest", name)


def map_timeline(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("name") or raw.get("timelineName") or record.source_id
    return _generic(record, "timeline", name)


def map_dungeon(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("displayName") or raw.get("name") or record.source_id
    return _generic(record, "dungeon", name)


def map_monolith_mod(record: SourceRecord) -> EntityPlan:
    raw = record.raw
    name = raw.get("name") or raw.get("modName") or record.source_id
    return _generic(record, "monolith_mod", name)


# -- post processing: relationships and node edges --------------------------
def _id_map(conn, snapshot_id: str) -> dict[tuple[str, str, str], str]:
    rows = conn.execute(
        "SELECT kind, source_scope, source_id, entity_id FROM source_id_map "
        "WHERE source_snapshot_id = ? AND source = 'tunklab'",
        (snapshot_id,),
    )
    return {(row["kind"], row["source_scope"], row["source_id"]): row["entity_id"] for row in rows}


def _resolve(mapping: dict, kind: str, scope: str, source_id: Any) -> str | None:
    if source_id is None:
        return None
    return mapping.get((kind, scope, str(source_id)))


def post_process(conn, context: dict[str, Any]) -> None:
    snapshot_id = context["source_snapshot_id"]
    records: list[SourceRecord] = context.get("records", [])
    mapping = _id_map(conn, snapshot_id)

    # 1) node requirement edges (source structure, not speculative)
    for record in records:
        if record.entity_kind not in {"skill_tree_node", "passive_tree_node"}:
            continue
        raw = record.raw
        requirements = raw.get("requirements") if isinstance(raw.get("requirements"), list) else []
        node_entity = _resolve(mapping, record.entity_kind, record.source_scope, record.source_id)
        if node_entity is None:
            continue
        for requirement in requirements:
            if not isinstance(requirement, dict):
                continue
            prereq_id = requirement.get("nodeID", requirement.get("nodeId"))
            prereq_entity = _resolve(
                mapping, record.entity_kind, record.source_scope, prereq_id
            )
            points = requirement.get("requirement")
            edge_id = "edge:" + hashlib.sha256(
                f"tunklab|{record.source_scope}|{prereq_id}|{record.source_id}".encode("utf-8")
            ).hexdigest()[:24]
            conn.execute(
                "INSERT OR IGNORE INTO node_edges (edge_id, tree_entity_id, "
                "from_node_entity_id, to_node_entity_id, requirement_type, points_required, raw) "
                "VALUES (?,?,?,?,?,?,?)",
                (
                    edge_id,
                    _tree_entity_id(record.source_scope),
                    prereq_entity,
                    node_entity,
                    "node_points",
                    points,
                    json.dumps(requirement, ensure_ascii=False),
                ),
            )

    # 2) relation records: resolved where both ends are known, otherwise kept raw.
    #    requires_node is deliberately NOT duplicated here: node prerequisites are
    #    stored once, in node_edges (from = prerequisite, to = dependent node).
    inserted = 0
    for record in records:
        if record.entity_kind != "relation":
            continue
        raw = record.raw
        rel_type = raw.get("relationship")
        if rel_type == "requires_node":
            continue
        from_ref = raw.get("from") or {}
        to_ref = raw.get("to") or {}
        from_entity = _resolve(
            mapping, from_ref.get("kind", ""), from_ref.get("scope", ""), from_ref.get("id")
        )
        to_entity = _resolve(
            mapping, to_ref.get("kind", ""), to_ref.get("scope", ""), to_ref.get("id")
        )
        if from_entity is None or to_entity is None:
            # A single missing end is "unresolved" (the target/reference is
            # unknown); "ambiguous" is reserved for multiple candidate targets,
            # which this source does not express.
            status = "unresolved"
        else:
            status = "resolved"
        raw_target_ref = None
        if status != "resolved":
            raw_target_ref = json.dumps({"from": from_ref, "to": to_ref}, ensure_ascii=False)
        rel_id = relation_id(
            "tunklab",
            f"{from_ref.get('kind')}:{from_ref.get('scope')}:{from_ref.get('id')}",
            str(rel_type),
            f"{to_ref.get('kind')}:{to_ref.get('scope')}:{to_ref.get('id')}",
        )
        conn.execute(
            "INSERT OR IGNORE INTO entity_relationships (relationship_id, source_snapshot_id, "
            "source, from_entity_id, relationship_type, to_entity_id, raw_target_ref, "
            "resolution_status, evidence, qualifiers) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                rel_id,
                snapshot_id,
                "tunklab",
                from_entity,
                rel_type,
                to_entity,
                raw_target_ref,
                status,
                raw.get("source_field"),
                json.dumps(
                    {"method": raw.get("method"), "value": raw.get("value")},
                    ensure_ascii=False,
                ),
            ),
        )
        inserted += 1

    # 3) mastery -> class ownership.  Mastery records are crosswalk secondary in
    #    the combined dataset, so their plan.relationships are not written by the
    #    framework; emit the ownership relation from the source records' scope.
    for record in records:
        if record.entity_kind != "mastery":
            continue
        mastery_entity = _resolve(mapping, "mastery", record.source_scope, record.source_id)
        class_entity = _resolve(mapping, "class", "", record.source_scope)
        if not mastery_entity or not class_entity:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO entity_relationships (relationship_id, source_snapshot_id, "
            "source, from_entity_id, relationship_type, to_entity_id, raw_target_ref, "
            "resolution_status, evidence, qualifiers) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                relation_id(
                    "tunklab",
                    f"mastery:{record.source_scope}:{record.source_id}",
                    "belongs_to_class",
                    f"class::{record.source_scope}",
                ),
                snapshot_id,
                "tunklab",
                mastery_entity,
                "belongs_to_class",
                class_entity,
                None,
                "resolved",
                "mastery.scope",
                json.dumps({"source_scope": record.source_scope}, ensure_ascii=False),
            ),
        )
    context["tunklab_relations_inserted"] = inserted
