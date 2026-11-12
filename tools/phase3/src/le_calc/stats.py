"""Static character snapshot for the phase-3 Black Hole calculator (v1).

Owner: input-ledger.  Consumers: ``le_calc.cli`` / ``le_calc.damage`` /
``le_calc.ignite`` / ``le_calc.cooldown`` / ``le_calc.report``.

``build_snapshot(bundle)`` turns the frozen calculator export + pinned build
revision (revision pin = ``calculation-input.revision_id``) + read-only catalog
into a :class:`le_calc.model.Snapshot`:

* ``character`` — level / class / mastery + the class base values from the
  catalog class record;
* ``attributes`` — base + equipment / blessing / idol + selected passive-node
  contributions (``model.ATTRIBUTE_ORDER``);
* ``resistances`` — applied totals in **percentage points** (``cap=75.0``) plus
  the raw ``elemental`` / ``all`` source pools;
* ``contributions`` — one row per effect line: every calculator-export mod
  (148 in the 2026-11-04 re-review revision, of which 11 are description lines;
  none are silently dropped), the 19 Mage passive nodes, the 17 selected Black
  Hole skill nodes, the class base values and the documented derivations;
  unresolved / out-of-scope lines are kept with ``included=False`` or an
  inactive ``condition_status``;
* ``derived`` — scalar summary consumed by the engine and the HTML report;
* ``issues`` and ``excluded_sources`` — assumptions / conflicts / scope ledger.

The module is deterministic: no wall-clock timestamps, stable sort order, JSON
written with sorted keys and ``ensure_ascii=False``.

Self-check entry point (does not touch ``le_calc.cli``)::

    python -m le_calc.stats --dump tools/phase3/work/snapshot.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Optional

from . import model
from .catalog import Catalog, REPO_ROOT
from .inputs import InputBundle, load_bundle

# ---------------------------------------------------------------------------
# Evidence / assumption constants
# ---------------------------------------------------------------------------

ATTRIBUTES_URL = "https://support.lastepoch.com/hc/en-us/articles/46361892354843-Attributes"

#: official per-point base attribute effects (public support article; recorded
#: for the static snapshot documentation — only intelligence/attunement affect
#: the v1 damage model directly, vitality resistance is aggregated as well)
ATTRIBUTE_PER_POINT_EFFECTS: dict[str, dict[str, Any]] = {
    "strength": {
        "description": "+4% increased Armor per point",
        "armor_increased_per_point": 0.04,
    },
    "dexterity": {
        "description": "+4 Dodge Rating per point",
        "dodge_rating_per_point": 4.0,
    },
    "intelligence": {
        "description": "+2% Ward Retention per point",
        "ward_retention_increased_per_point": 0.02,
    },
    "attunement": {
        "description": "+2 Mana per point",
        "mana_added_per_point": 2.0,
    },
    "vitality": {
        "description": "+6 Health and +1% Poison and Necrotic Resistance per point",
        "health_added_per_point": 6.0,
        "poison_resistance_per_point": 0.01,
        "necrotic_resistance_per_point": 0.01,
    },
}

#: LE Tools ``specialTag`` -> ailment/resistance-shred identity.  The numbers are
#: the ``LEAbilities.ailmentList`` indexes (catalog ``entities`` kind=ailment);
#: the six Apex-of-Thought rows follow the composed tooltip order (Ignite, Chill,
#: Shock, Fire/Cold/Lightning Resistance Shred), which cross-checks the mapping.
SPECIAL_TAG_AILMENTS: dict[int, str] = {
    1: "ignite",
    3: "chill",
    5: "shock",
    24: "spreading_flames",
    42: "fire_resistance_shred",
    74: "cold_resistance_shred",
    75: "lightning_resistance_shred",
}

RESISTANCE_BY_STAT_NAME: dict[str, str] = {
    "Fire Resistance": "fire",
    "Cold Resistance": "cold",
    "Lightning Resistance": "lightning",
    "Physical Resistance": "physical",
    "Void Resistance": "void",
    "Necrotic Resistance": "necrotic",
    "Poison Resistance": "poison",
    "Elemental Resistance": "elemental",
    "All Resistances": "all",
}

ATTRIBUTE_BY_STAT_NAME: dict[str, str] = {
    "Strength": "strength",
    "Dexterity": "dexterity",
    "Intelligence": "intelligence",
    "Vitality": "vitality",
    "Attunement": "attunement",
}

#: stats that are real but outside the v1 Black Hole damage model; kept for the
#: source ledger with an explicit reason (never silently dropped).
EXCLUDED_STAT_REASONS: dict[str, str] = {
    "Armor": "defensive stat; no effect on Black Hole / Ignite damage in v1",
    "Mana Spent Gained as Ward": "ward generation; ward is not modelled in v1",
    "Mana Regen": "v1 ignores the mana resource (user rule 6); mana regeneration does not affect damage",
    "Ward Decay Threshold": "ward is not modelled in v1",
    "Ward per Second": "ward is not modelled in v1",
    "Ward Gain": "ward is not modelled in v1",
    "Ward per Second per Equipped Heretical Idol": "ward is not modelled in v1",
    "Potion Slots": "potion system is out of scope",
    "Movement Speed": "movement speed has no damage effect in v1",
    "Endurance": "defensive mitigation stat; not part of the damage model",
    "Endurance Threshold": "defensive mitigation stat; not part of the damage model",
    "Increased Drop Rate": "loot stat; not part of the damage model",
    "Chance to Receive a Critical Strike": "defensive crit stat; Black Hole body hit=false so crit is not modelled",
    "reduced Bonus Damage Taken from Critical Strikes": "defensive crit stat; not part of the outgoing damage model",
    "Critical Strike Chance": "Black Hole body hit=false -> no critical strikes (execution plan A1)",
    "Critical Strike Multiplier": "Black Hole body hit=false -> no critical strikes (execution plan A1)",
    "Critical Strike Multiplier with Flame Whip per 10% Uncapped Necrotic Resistance": "Flame Whip is an out-of-scope skill; crit is not modelled",
    "Chance to cast Fire Trail when you are crit": "trigger depends on being crit / out of scope",
    "Effect Of Ailment On You": "buff effect on the player; not an outgoing damage modifier",
    "Increased Area With Supernova": "conditional on Elemental Nova casts (out-of-scope skill)",
    "Added Spell Damage With Supernova": "conditional on Elemental Nova casts (out-of-scope skill)",
    "Chance for Chthonic Fissures to cast Flame Whip instead of releasing Spirits": "Chthonic Fissures / Flame Whip are out-of-scope skills",
    "Chthonic Fissure Tree applies to Flame Whip": "Chthonic Fissure / Flame Whip are out-of-scope skills",
    "Weaver Idols equipped at Maximum": "idol-slot grid rule; not part of the damage model in v1",
    "Effect of Prefixes for Idols in Refracted Slots": "requires the unverified idol grid slot model (refracted slots)",
    "Effect of Suffixes for Idols in Refracted Slots": "requires the unverified idol grid slot model (refracted slots)",
    "Health per Idol in a Refracted Slot": "health stat; grid slot model unverified",
    "Maximum Omen Idols": "idol grid configuration; not part of the damage model",
    "Health per Equipped Omen Idol": "health stat; not part of the damage model",
}

#: more-groups for the Black Hole skill tree are ``node:bh2:<node_id>`` (Lead contract)
SKILL_GROUP_PREFIX = "node:bh2:"

DEFAULT_DERIVED_DUMP = Path("tools/phase3/work/snapshot.json")

#: bh2 cast-time inputs from archived LEAbilities data (1.5.2); mirrored in
#: ruleset.cast.  Reference formula: cast_time = 1 / (as * speedMultiplier * (1 + inc cast speed)).
BH2_ANIMATION_SPEED = 1.8333375  # ability.as
BH2_SPEED_MULTIPLIER = 0.8  # ability.speedMultiplier

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _round(value: float, digits: int = 9) -> float:
    return round(float(value), digits)


def _ev(
    kind: str,
    detail: str,
    *,
    locator: str | None = None,
    snippet: str | None = None,
) -> model.Evidence:
    return model.Evidence(kind=kind, detail=detail, locator=locator, snippet=snippet)


def _contribution(
    contribution_id: str,
    *,
    source_kind: str,
    source_label: str,
    source_path: str,
    stat_name: str,
    stat_key: str,
    operation: str,
    value: float,
    unit: str = "flat",
    damage_tags: Iterable[str] = (),
    target: str | None = None,
    condition_code: str | None = None,
    condition_status: str = model.CONDITION_ALWAYS,
    group: str | None = None,
    included: bool = True,
    exclusion_reason: str | None = None,
    value_source: str = "source",
    status: str = model.STATUS_CONFIRMED,
    evidence: Iterable[model.Evidence] = (),
    notes: str | None = None,
    raw: dict[str, Any] | None = None,
) -> model.Contribution:
    return model.Contribution(
        contribution_id=contribution_id,
        source_kind=source_kind,
        source_label=source_label,
        source_path=source_path,
        stat_name=stat_name,
        stat_key=stat_key,
        operation=operation,
        value=_round(value),
        unit=unit,
        damage_tags=sorted(set(damage_tags)),
        target=target,
        condition_code=condition_code,
        condition_status=condition_status,
        group=group,
        included=bool(included),
        exclusion_reason=exclusion_reason,
        value_source=value_source,
        status=status,
        evidence=list(evidence),
        notes=notes,
        raw=raw,
    )


# ---------------------------------------------------------------------------
# Snapshot builder
# ---------------------------------------------------------------------------


class SnapshotError(RuntimeError):
    """Raised when the frozen inputs cannot be turned into a snapshot."""


class _SnapshotBuilder:
    def __init__(self, bundle: InputBundle, catalog: Catalog) -> None:
        self.bundle = bundle
        self.catalog = catalog
        self.contributions: list[model.Contribution] = []
        self.issues: list[dict[str, Any]] = []
        self._issue_seq = 0

        self.mods: list[dict[str, Any]] = bundle.mods
        self._mod_positions = {mod["mod_id"]: i for i, mod in enumerate(self.mods)}
        self._items: dict[str, dict[str, Any]] = {
            str(item.get("instance_id")): item for item in bundle.items
        }
        self._passive_nodes: dict[str, dict[str, Any]] = {
            str(node.get("node_id")): node for node in bundle.passive_nodes
        }
        self._passive_bonus: dict[str, list[dict[str, Any]]] = catalog.passive_bonus_stats("mg-1")
        self._passive_tree = catalog.tree_by_source_id("mg-1")
        self._bh_tree = catalog.tree_by_source_id("bh2")
        self._bh_nodes = {
            str(node["node_index"]): node
            for node in catalog.nodes_for_tree(self._bh_tree["entity_id"])
        }
        self._bh_spec = bundle.specialization_for_tree("bh2") or {}
        self._bh_selected: dict[str, int] = {
            str(node.get("node_id")): int(node.get("points") or 0)
            for node in (self._bh_spec.get("nodes") or [])
            if int(node.get("points") or 0) > 0
        }

        self.mod_count = len(self.mods)
        self._description_rows = 0
        self._special_tag_names: dict[int, str] = {}
        for tag_number in sorted(SPECIAL_TAG_AILMENTS):
            entity_id = f"ailment:letools:LEAbilities.ailmentList:{tag_number}"
            entity = catalog.entity(entity_id)
            if entity is not None:
                self._special_tag_names[tag_number] = entity.get("canonical_name") or ""
        self._class_data: dict[str, Any] = self._load_class_data()

    # -- bookkeeping -------------------------------------------------------
    def add(self, contribution: model.Contribution) -> model.Contribution:
        self.contributions.append(contribution)
        return contribution

    def issue(self, severity: str, title: str, detail: str, scope: str) -> None:
        self._issue_seq += 1
        self.issues.append(
            {
                "issue_id": f"issue-input-ledger-{self._issue_seq:03d}",
                "severity": severity,
                "title": title,
                "detail": detail,
                "scope": scope,
            }
        )

    # -- source helpers ----------------------------------------------------
    def _load_class_data(self) -> dict[str, Any]:
        character = self.bundle.character
        class_name = (character.get("class") or {}).get("name")
        mastery_name = (character.get("mastery") or {}).get("name")
        if not class_name:
            raise SnapshotError("calculator export has no character class")
        class_record = self.catalog.class_by_name(str(class_name))
        class_definition = (class_record.get("extensions") or {}).get("characterClass") or {}
        mastery_record = None
        if mastery_name:
            mastery_record = self.catalog.mastery_by_name(str(mastery_name))
        return {
            "class_name": class_name,
            "mastery_name": mastery_name,
            "class_entity_id": class_record["entity_id"],
            "mastery_entity_id": (mastery_record or {}).get("entity_id"),
            "class_definition": class_definition,
            "mastery_record": mastery_record,
        }

    def _item(self, instance_id: str) -> dict[str, Any]:
        item = self._items.get(instance_id)
        if item is None:
            raise SnapshotError(f"calculator export references unknown item instance {instance_id!r}")
        return item

    def _source_info(self, instance_id: str) -> dict[str, Any]:
        if instance_id.startswith("quest."):
            return {
                "source_kind": "quest",
                "source_path": "data.quests.rewards",
                "label": "Quest reward (manual override)",
                "item": {},
            }
        item = self._item(instance_id)
        role = str(item.get("role") or "")
        if instance_id.startswith("equipment."):
            slot = str(item.get("slot") or instance_id.split(".", 1)[1])
            source_kind = "idol_altar" if slot == "idol_altar" else "equipment"
            source_path = f"data.equipment.{slot}"
        elif instance_id.startswith("idols."):
            index = instance_id.split(".", 1)[1]
            source_kind = "idol"
            source_path = f"data.idols.entries.{index}"
        elif instance_id.startswith("blessings."):
            index = instance_id.split(".", 1)[1]
            source_kind = "blessing"
            source_path = f"data.blessings.entries.{index}"
        else:
            raise SnapshotError(f"unsupported item instance id {instance_id!r} (role={role!r})")
        return {
            "source_kind": source_kind,
            "source_path": source_path,
            "label": f"{item.get('display_name') or instance_id} [{instance_id}]",
            "item": item,
        }

    def _mod_evidence(self, mod: dict[str, Any], extra: list[model.Evidence] | None = None) -> list[model.Evidence]:
        position = self._mod_positions[mod["mod_id"]]
        readable = mod.get("readable_text") or ""
        rel = self.bundle.calc_input_path.resolve().relative_to(self.bundle.root).as_posix()
        evidence = [
            _ev(
                model.EVIDENCE_CATALOG,
                f"calculation-input-v1 mod {mod['mod_id']} "
                f"(source={mod.get('source')}, channel={mod.get('channel')}, "
                f"kind={mod.get('kind')}, value_status={mod.get('value_status')})",
                locator=f"{rel}#mods[{position}]",
                snippet=readable or None,
            )
        ]
        display_value = mod.get("display_value")
        if display_value is not None and display_value != readable:
            evidence.append(
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"display_value={display_value!r}, value_numeric={mod.get('value_numeric')!r}, "
                    f"modifier_type={mod.get('modifier_type')!r}",
                    locator=f"{rel}#mods[{position}].value_numeric",
                )
            )
        if extra:
            evidence.extend(extra)
        return evidence

    def _raw_mod(self, mod: dict[str, Any]) -> dict[str, Any]:
        return {
            "mod_id": mod.get("mod_id"),
            "item_instance_id": mod.get("item_instance_id"),
            "source": mod.get("source"),
            "channel": mod.get("channel"),
            "kind": mod.get("kind"),
            "stat_name": (mod.get("stat") or {}).get("name"),
            "property": mod.get("property"),
            "tags": mod.get("tags"),
            "special_tag": mod.get("special_tag"),
            "modifier_type": mod.get("modifier_type"),
            "value_numeric": mod.get("value_numeric"),
            "display_value": mod.get("display_value"),
            "value_status": mod.get("value_status"),
            "value_source": mod.get("value_source"),
        }

    def _tag_evidence(self, mod: dict[str, Any]) -> list[model.Evidence]:
        """Evidence for tag decoding + specialTag mapping where relevant."""
        evidence: list[model.Evidence] = []
        tags = model.decode_tags(mod.get("tags"))
        if tags:
            evidence.append(
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"tags bitmask {mod.get('tags')} decodes to {tags} "
                    "(phase-1 tags table, LE Tools AT enum)",
                    locator="data/releases/phase1-dataset-003/data.sqlite#tags",
                )
            )
        special_tag = mod.get("special_tag")
        if special_tag in self._special_tag_names:
            name = self._special_tag_names[special_tag]
            evidence.append(
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"specialTag={special_tag} -> {name!r} "
                    "(catalog LEAbilities.ailmentList index; Apex of Thought tooltip order cross-check)",
                    locator=f"data/releases/phase1-dataset-003/data.sqlite#entities "
                    f"ailment:letools:LEAbilities.ailmentList:{special_tag}",
                )
            )
        elif special_tag:
            evidence.append(
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"specialTag={special_tag} has no catalog ailmentList entity; "
                    "semantics not used by this row",
                    locator="data/releases/phase1-dataset-003/data.sqlite#ailments",
                )
            )
        return evidence

    # ==================================================================
    # 1. class base values
    # ==================================================================
    def build_base_character(self) -> None:
        definition = self._class_data["class_definition"]
        class_label = f"{self._class_data['class_name']} class base"
        level = int(self.bundle.character.get("level") or 0)
        if level != 100:
            self.issue(
                "warning",
                "non-standard character level",
                f"calculator export level={level!r}; v1 fixpoint expects level 100",
                "attributes",
            )
        for key in model.ATTRIBUTE_ORDER:
            camel = {
                "strength": "baseStrength",
                "vitality": "baseVitality",
                "intelligence": "baseIntelligence",
                "dexterity": "baseDexterity",
                "attunement": "baseAttunement",
            }[key]
            base_value = definition.get(camel)
            if base_value is None:
                raise SnapshotError(f"class record has no {camel}")
            self.add(
                _contribution(
                    f"base:attribute:{key}",
                    source_kind="base_character",
                    source_label=class_label,
                    source_path="data.character.class",
                    stat_name=camel,
                    stat_key=model.STAT_ATTRIBUTE,
                    operation="added",
                    value=float(base_value),
                    unit="flat",
                    target=key,
                    evidence=[
                        _ev(
                            model.EVIDENCE_CATALOG,
                            f"catalog classes.characterClass.{camel}={base_value}",
                            locator="data/releases/phase1-dataset-003/data.sqlite#classes "
                            f"entity_id={self._class_data['class_entity_id']}",
                        )
                    ],
                    notes="class base attribute (no per-level attribute gains in the class record)",
                )
            )

        base_mana = definition.get("baseMana")
        mana_per_level = definition.get("manaPerLevel")
        if base_mana is None or mana_per_level is None:
            raise SnapshotError("class record has no baseMana/manaPerLevel")
        mana_at_level = float(base_mana) + float(mana_per_level) * (level - 1)
        self.add(
            _contribution(
                "base:mana:level100",
                source_kind="base_character",
                source_label=class_label,
                source_path="data.character.class",
                stat_name="Mana",
                stat_key=model.STAT_MANA,
                operation="added",
                value=mana_at_level,
                unit="flat",
                status=model.STATUS_DERIVED,
                evidence=[
                    _ev(
                        model.EVIDENCE_CATALOG,
                        f"catalog classes.characterClass.baseMana={base_mana}, "
                        f"manaPerLevel={mana_per_level}, level={level}",
                        locator="data/releases/phase1-dataset-003/data.sqlite#classes "
                        f"entity_id={self._class_data['class_entity_id']}",
                    ),
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"mana(level {level}) = baseMana + manaPerLevel*(level-1) "
                        f"= {float(base_mana)} + {float(mana_per_level)}*{level - 1} = {mana_at_level}",
                        locator="le_calc.stats#build_base_character",
                    ),
                ],
                notes="v1 assumption: per-level gains apply to levels 2..100 (level-1). "
                f"Using level*perLevel would give {float(base_mana) + float(mana_per_level) * level:.6f}; "
                "both values stay below the 300-mana idol condition, so that condition is robust.",
            )
        )

    # ==================================================================
    # 2. calculator export mods (all emitted mod + description rows)
    # ==================================================================
    def build_equipment(self) -> None:
        self._deferred_glove_mods: list[dict[str, Any]] = []
        self._deferred_doubled_fire_mods: list[dict[str, Any]] = []
        for mod in self.mods:
            if mod.get("kind") == "description":
                self._description_row(mod)
                continue
            self._classify_mod(mod)
        expected = len(self.mods)
        if len(self.contributions) < expected:
            raise SnapshotError(
                f"only {len(self.contributions)} contributions for {expected} mods"
            )

    def _description_row(self, mod: dict[str, Any]) -> None:
        instance_id = str(mod.get("item_instance_id"))
        source = self._source_info(instance_id)
        self._description_rows += 1
        self.add(
            _contribution(
                f"mod:{mod['mod_id']}",
                source_kind=source["source_kind"],
                source_label=source["label"],
                source_path=source["source_path"],
                stat_name="Unique tooltip description",
                stat_key=model.STAT_UNSUPPORTED,
                operation="flag",
                value=1.0,
                unit="flag",
                included=False,
                exclusion_reason="unique-tooltip description line: display/evidence only; "
                "no numeric value (structured hidden mods / other rows carry the counted values)",
                value_source=str(mod.get("value_source") or "source"),
                status=model.STATUS_CONFIRMED,
                evidence=self._mod_evidence(mod),
                notes=(mod.get("readable_text") or "")[:500],
                raw=self._raw_mod(mod),
            )
        )

    def _excluded(
        self,
        mod: dict[str, Any],
        *,
        reason: str,
        stat_name: str | None = None,
        stat_key: str = model.STAT_UNSUPPORTED,
        operation: str = "flag",
        unit: str = "flat",
        value: float | None = None,
        status: str = model.STATUS_CONFIRMED,
        extra_evidence: list[model.Evidence] | None = None,
        notes: str | None = None,
    ) -> None:
        source = self._source_info(str(mod.get("item_instance_id")))
        numeric = mod.get("value_numeric")
        if value is None:
            value = float(numeric) if numeric is not None else 0.0
        self.add(
            _contribution(
                f"mod:{mod['mod_id']}",
                source_kind=source["source_kind"],
                source_label=source["label"],
                source_path=source["source_path"],
                stat_name=stat_name or ((mod.get("stat") or {}).get("name") or ""),
                stat_key=stat_key,
                operation=operation,
                value=value,
                unit=unit,
                included=False,
                exclusion_reason=reason,
                value_source=str(mod.get("value_source") or "source"),
                status=status,
                evidence=self._mod_evidence(mod, extra_evidence),
                notes=notes,
                raw=self._raw_mod(mod),
            )
        )

    def _classify_mod(self, mod: dict[str, Any]) -> None:
        mod_id = str(mod.get("mod_id"))
        stat = mod.get("stat") or {}
        name = str(stat.get("name") or "")
        tags = model.decode_tags(mod.get("tags"))
        special_tag = mod.get("special_tag")
        modifier_type = mod.get("modifier_type")
        value = mod.get("value_numeric")
        value_status = str(mod.get("value_status") or "")

        handler = _SPECIAL_MOD_HANDLERS.get(mod_id)
        if handler is not None:
            handler(self, mod)
            return

        if value is None:
            self._excluded(
                mod,
                reason="value_numeric is null: no actual value is known (must not be treated as 0)",
                status=model.STATUS_UNRESOLVED,
            )
            self.issue(
                "warning",
                "mod without numeric value",
                f"{mod_id} has value_status={value_status!r} and value_numeric=null; "
                "row kept as unresolved (not defaulted to 0)",
                "contributions",
            )
            return
        if value_status == "range":
            self.issue(
                "warning",
                "range-valued mod",
                f"{mod_id} value_status=range; using parsed value {value!r} but the roll is not actual",
                "contributions",
            )
        try:
            numeric = float(value)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            raise SnapshotError(f"non-numeric value {value!r} for {mod_id}")

        # -- attributes -----------------------------------------------------
        if name in ATTRIBUTE_BY_STAT_NAME:
            key = ATTRIBUTE_BY_STAT_NAME[name]
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_ATTRIBUTE,
                    operation="added",
                    value=numeric,
                    unit="flat",
                    target=key,
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod),
                    raw=self._raw_mod(mod),
                )
            )
            return
        if name == "All Attributes":
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_ALL_ATTRIBUTES,
                    operation="added",
                    value=numeric,
                    unit="flat",
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod),
                    raw=self._raw_mod(mod),
                )
            )
            return

        # -- resistances -----------------------------------------------------
        if name in RESISTANCE_BY_STAT_NAME:
            if "Minion" in tags:
                self._excluded(
                    mod,
                    reason="minion-scoped stat; the v1 calculator is player-only",
                    stat_key=model.STAT_RESISTANCE,
                    operation="added",
                    unit="percent",
                )
                return
            target = RESISTANCE_BY_STAT_NAME[name]
            notes = None
            if target in ("elemental", "all"):
                notes = (
                    "source pool only: aggregated into fire/cold/lightning (elemental) or all "
                    "resistances (all); the pool itself is not an applied resistance"
                )
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_RESISTANCE,
                    operation="added",
                    value=numeric,
                    unit="percent",
                    target=target,
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                    notes=notes,
                    raw=self._raw_mod(mod),
                )
            )
            return

        # -- mana -------------------------------------------------------------
        if name == "Mana":
            operation = "increased" if modifier_type == 1 else "added"
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_MANA,
                    operation=operation,
                    value=numeric,
                    unit="percent" if operation == "increased" else "flat",
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod),
                    raw=self._raw_mod(mod),
                )
            )
            return
        if name == "Mana Regen":
            self._excluded(
                mod,
                reason=EXCLUDED_STAT_REASONS["Mana Regen"],
                stat_key=model.STAT_MANA_REGEN,
                operation="increased" if modifier_type == 1 else "added",
                unit="percent" if modifier_type == 1 else "flat",
            )
            return

        # -- ward generation ---------------------------------------------------
        if name.startswith("Ward Gain") or name == "Ward" or name.startswith("Ward Generation"):
            self._excluded(
                mod,
                reason="ward generation is a defensive mechanic; it does not change Black Hole / Ignite damage in v1",
                stat_key=model.STAT_UNSUPPORTED,
                operation="added",
                unit="flat",
            )
            return

        # -- ailment duration / effect / chance ------------------------------
        is_ailment_duration_name = name == "Increased Ailment Duration" or (
            name.startswith("Increased ") and name.endswith(" Duration")
        )
        if is_ailment_duration_name:
            same_semantics = "increased duration (documented propertyList stat); additive duration pool"
            if special_tag == 1:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_AILMENT_DURATION,
                        operation="increased",
                        value=numeric,
                        unit="percent",
                        target="ignite",
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        notes=same_semantics,
                        raw=self._raw_mod(mod),
                    )
                )
                return
            if special_tag == 24:
                reason = (
                    "Spreading Flames duration: no Spreading Flames source inside the Black Hole "
                    "v1 scope (out of scope)"
                )
            elif special_tag in (3, 5):
                reason = (
                    f"{self._special_tag_names.get(special_tag, 'non-Ignite')} duration: "
                    "non-damaging ailment, no effect on Black Hole / Ignite damage in v1"
                )
            elif special_tag in (42, 74, 75):
                reason = (
                    "resistance-shred duration: shred is not modelled in v1; the Apex of Thought "
                    "conversion condition (standing on Glyph of Dominion) is also not satisfied"
                )
            else:
                reason = f"ailment duration with unmapped specialTag={special_tag!r}"
            self._excluded(
                mod,
                reason=reason,
                stat_key=model.STAT_AILMENT_DURATION,
                operation="increased",
                unit="percent",
                extra_evidence=self._tag_evidence(mod),
            )
            return
        if name == "Increased Ailment Effect":
            if special_tag == 1:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_AILMENT_EFFECT,
                        operation="increased",
                        value=numeric,
                        unit="percent",
                        target="ignite",
                        status=model.STATUS_DERIVED,
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        notes="pool attribution is an engine-side v1 assumption: in le_calc.ignite "
                        "increased ailment effect is added into the same inc pool as increased "
                        "fire/DoT damage (ruleset ignite.scaling_pools).",
                        raw=self._raw_mod(mod),
                    )
                )
                return
            self._excluded(
                mod,
                reason=f"ailment effect with unmapped specialTag={special_tag!r} (only Ignite is used)",
                stat_key=model.STAT_AILMENT_EFFECT,
                operation="increased",
                unit="percent",
                extra_evidence=self._tag_evidence(mod),
            )
            return
        is_ailment_chance_name = name == "Ailment Chance" or (
            special_tag and name.startswith("Chance to ") and "Hit" in name
        )
        if is_ailment_chance_name:
            if special_tag == 1:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_AILMENT_CHANCE,
                        operation="added",
                        value=numeric,
                        unit="percent",
                        target="ignite",
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        notes="chance to ignite from this source; the Heat Wave application model "
                        "is defined by the ruleset (execution plan B2.1 asks whether other ignite "
                        "chance sources participate — engine currently ignores this in v1).",
                        raw=self._raw_mod(mod),
                    )
                )
                return
            self._excluded(
                mod,
                reason="non-Ignite ailment chance (no damage effect in v1)",
                stat_key=model.STAT_AILMENT_CHANCE,
                operation="added",
                unit="percent",
                extra_evidence=self._tag_evidence(mod),
            )
            return

        # -- penetration ------------------------------------------------------
        if name == "Fire Penetration with Ignite":
            # Phase-1 dataset 003 derives this display name from the single-property
            # affix definitions for property 43 + specialTag 1 (Ignite).  The stat is
            # fire penetration that only applies to the Ignite damage, so it is kept
            # separate from generic body penetration.
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_PENETRATION_WITH_IGNITE,
                    operation="added",
                    value=numeric,
                    unit="percent",
                    target="ignite",
                    status=model.STATUS_CONFIRMED,
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                    notes="Fire Penetration with Ignite: fire resistance penetration applied to the "
                    "Ignite damage only (not to the Black Hole body). Display name comes from the "
                    "phase-1 dataset 003 special-tag display mapping (property 43 + specialTag 1), "
                    "derived from single-property affix definitions; the user confirmed the item line.",
                    raw=self._raw_mod(mod),
                )
            )
            return
        if name == "Penetration":
            if "Minion" in tags:
                self._excluded(
                    mod,
                    reason="minion penetration; the v1 calculator is player-only",
                    stat_key=model.STAT_PENETRATION,
                    operation="added",
                    unit="percent",
                )
                return
            damage_tags = [tag for tag in tags if tag in model.ELEMENT_TAGS]
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_PENETRATION,
                    operation="added",
                    value=numeric,
                    unit="percent",
                    damage_tags=damage_tags,
                    target=(damage_tags[0].lower() if len(damage_tags) == 1 else None),
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                    notes="resistance penetration is applied by the engine to the body component and "
                    "Ignite according to ruleset target_side.penetration_applies_to_ignite "
                    "(v3: true; Fire Penetration with Ignite is additional)",
                    raw=self._raw_mod(mod),
                )
            )
            return

        # -- cooldown recovery speed -----------------------------------------
        if name == "Increased Cooldown Recovery Speed":
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    **self._source_kwargs(mod),
                    stat_name=name,
                    stat_key=model.STAT_COOLDOWN_RECOVERY,
                    operation="increased",
                    value=numeric,
                    unit="percent",
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=self._mod_evidence(mod),
                    raw=self._raw_mod(mod),
                )
            )
            return

        # -- damage / added damage -------------------------------------------
        if name == "Damage":
            if "Minion" in tags:
                self._excluded(
                    mod,
                    reason="minion damage; the v1 calculator is player-only",
                    stat_key=model.STAT_DAMAGE,
                    operation="increased" if modifier_type else "added",
                )
                return
            if "Melee" in tags:
                self._excluded(
                    mod,
                    reason="melee damage does not apply to the Black Hole spell component "
                    "(execution plan A3: weapon melee damage is not added spell damage)",
                    stat_key=model.STAT_DAMAGE if modifier_type else model.STAT_ADDED_DAMAGE,
                    operation="increased" if modifier_type else "added",
                )
                return
            if "Hit" in tags:
                self._excluded(
                    mod,
                    reason="hit-scoped modifier; Black Hole body hit=false",
                    stat_key=model.STAT_DAMAGE,
                    operation="increased" if modifier_type else "added",
                )
                return
            if modifier_type == 1:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_DAMAGE,
                        operation="increased",
                        value=numeric,
                        unit="percent",
                        damage_tags=tags,
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        raw=self._raw_mod(mod),
                    )
                )
                return
            if modifier_type == 2:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_DAMAGE,
                        operation="more",
                        value=numeric,
                        unit="percent",
                        damage_tags=tags,
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        raw=self._raw_mod(mod),
                    )
                )
                return
            if modifier_type == 0:
                self.add(
                    _contribution(
                        f"mod:{mod_id}",
                        **self._source_kwargs(mod),
                        stat_name=name,
                        stat_key=model.STAT_ADDED_DAMAGE,
                        operation="added",
                        value=numeric,
                        unit="flat",
                        damage_tags=tags,
                        value_source=str(mod.get("value_source") or "source"),
                        evidence=self._mod_evidence(mod) + self._tag_evidence(mod),
                        notes="flat added damage: the engine scales it by the Black Hole "
                        "addedDamageScaling=2.4 per tick (ruleset black_hole.added_damage)",
                        raw=self._raw_mod(mod),
                    )
                )
                return

        # -- informational / known exclusions --------------------------------
        if name == "Level of Skills":
            self._excluded(
                mod,
                reason="informational only: skill level / allocatable points; v1 does not "
                "fabricate per-level damage (execution plan A2)",
                stat_key=model.STAT_UNSUPPORTED,
                operation="added",
                unit="flat",
                notes="value preserved in the ledger; no damage contribution is inferred",
            )
            return
        if name in EXCLUDED_STAT_REASONS:
            self._excluded(mod, reason=EXCLUDED_STAT_REASONS[name])
            return

        # -- unknown --------------------------------------------------------
        self._excluded(
            mod,
            reason=f"unmapped stat name {name!r}: no v1 damage interpretation in the input adapter",
            status=model.STATUS_UNRESOLVED,
        )
        self.issue(
            "warning",
            "unmapped calculator stat",
            f"{mod_id} stat name {name!r} could not be mapped to a v1 stat_key",
            "contributions",
        )

    def _source_kwargs(self, mod: dict[str, Any]) -> dict[str, Any]:
        if mod.get("source_kind") or mod.get("source_label") or mod.get("source_path"):
            return {
                "source_kind": str(mod.get("source_kind") or "manual_override"),
                "source_label": str(mod.get("source_label") or "Manual override"),
                "source_path": str(mod.get("source_path") or "manual:override"),
            }
        source = self._source_info(str(mod.get("item_instance_id")))
        return {
            "source_kind": source["source_kind"],
            "source_label": source["label"],
            "source_path": source["source_path"],
        }

    # -- deferred equipment rows (need other aggregates) --------------------
    def _defer_glove(self, mod: dict[str, Any]) -> None:
        self._deferred_glove_mods.append(mod)

    def _defer_doubled_fire(self, mod: dict[str, Any]) -> None:
        self._deferred_doubled_fire_mods.append(mod)

    def resolve_glove_derivations(self, overcapped_cold_percent: float) -> None:
        """Gloves: 1% CDR per 8% / 1% cast speed per 5% overcapped cold resistance."""
        for mod in self._deferred_glove_mods:
            mod_id = str(mod["mod_id"])
            stat = mod.get("stat") or {}
            name = str(stat.get("name") or "")
            source = self._source_info(str(mod.get("item_instance_id")))
            per = 8.0 if "Cooldown" in name else 5.0
            stat_key = (
                model.STAT_COOLDOWN_RECOVERY if "Cooldown" in name else model.STAT_CAST_SPEED
            )
            ratio_value = min(200.0, max(0.0, overcapped_cold_percent)) / per * 0.01
            floored_value = int(min(200.0, max(0.0, overcapped_cold_percent)) // per) * 0.01
            is_exact = abs(ratio_value - floored_value) < 1e-12
            rounding_note = (
                f"overcapped cold {overcapped_cold_percent:.6g}% is divisible by {per:g}%, "
                "so continuous-ratio and integer-chunk interpretations agree"
                if is_exact
                else f"overcapped cold {overcapped_cold_percent:.6g}% is not divisible by {per:g}%; "
                f"v1 uses the continuous ratio {ratio_value:.6g} -> {ratio_value * 100:.6g}% "
                f"(integer-chunk reading would be {floored_value * 100:.6g}%)"
            )
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    source_kind=source["source_kind"],
                    source_label=source["label"],
                    source_path=source["source_path"],
                    stat_name=name,
                    stat_key=stat_key,
                    operation="increased",
                    value=ratio_value,
                    unit="percent",
                    status=model.STATUS_DERIVED,
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=[
                        _ev(
                            model.EVIDENCE_CATALOG,
                            f"calculation-input-v1 mod {mod_id}: per-{per:g}% threshold affix "
                            "value_numeric=0.01 (1% per threshold step, cap 200% overcapped)",
                            locator=f"{self.bundle.calc_input_path.resolve().relative_to(self.bundle.root).as_posix()}"
                            f"#mods[{self._mod_positions[mod_id]}]",
                            snippet=mod.get("readable_text") or None,
                        ),
                        _ev(
                            model.EVIDENCE_DERIVATION,
                            f"1% × min(200, overcapped cold {overcapped_cold_percent:.6g}) / {per:g} "
                            f"= {ratio_value:.6g}",
                            locator="le_calc.stats#resolve_glove_derivations",
                        ),
                    ],
                    notes=(
                        f"1% × min(200, {overcapped_cold_percent:.6g})% / {per:g}% = "
                        f"{ratio_value * 100:.6g}%; " + rounding_note
                        + "; integer/chunk rounding is an explicit v1 assumption."
                    ),
                    raw=self._raw_mod(mod),
                )
            )

    def resolve_doubled_fire(self, mana_max: float) -> None:
        """Idols 2/3: fire damage is doubled when max mana >= 300."""
        satisfied = mana_max >= 300.0
        for mod in self._deferred_doubled_fire_mods:
            mod_id = str(mod["mod_id"])
            numeric = float(mod.get("value_numeric") or 0.0)
            source = self._source_info(str(mod.get("item_instance_id")))
            evidence = self._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"max mana = {mana_max:.6f} -> condition max_mana_gte_300 is "
                        f"{'satisfied' if satisfied else 'NOT satisfied'}",
                        locator="le_calc.stats#resolve_doubled_fire",
                    )
                ],
            )
            self.add(
                _contribution(
                    f"mod:{mod_id}",
                    source_kind=source["source_kind"],
                    source_label=source["label"],
                    source_path=source["source_path"],
                    stat_name="Damage",
                    stat_key=model.STAT_DAMAGE,
                    operation="increased",
                    value=numeric,
                    unit="percent",
                    damage_tags=["Fire"],
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=evidence,
                    notes="base value of the doubled-if-300-mana fire damage affix",
                    raw=self._raw_mod(mod),
                )
            )
            self.add(
                _contribution(
                    f"mod:{mod_id}:doubled",
                    source_kind=source["source_kind"],
                    source_label=source["label"],
                    source_path=source["source_path"],
                    stat_name="Damage",
                    stat_key=model.STAT_DAMAGE,
                    operation="increased",
                    value=numeric,
                    unit="percent",
                    damage_tags=["Fire"],
                    condition_code="max_mana_gte_300",
                    condition_status=(
                        model.CONDITION_SATISFIED if satisfied else model.CONDITION_NOT_SATISFIED
                    ),
                    value_source=str(mod.get("value_source") or "source"),
                    evidence=evidence,
                    notes=(
                        "the affix doubles its fire damage while max mana >= 300; "
                        f"actual max mana {mana_max:.6f} so the extra row is "
                        f"{'applied' if satisfied else 'inactive'}"
                    ),
                    raw=self._raw_mod(mod),
                )
            )

    # ==================================================================
    # 3. Mage passive tree (19 selected nodes, 113 points)
    # ==================================================================
    def build_passive_nodes(self) -> None:
        mapped_entries: set[tuple[str, int]] = set()
        for node_id in sorted(self._passive_nodes, key=int):
            node = self._passive_nodes[node_id]
            points = int(node.get("points") or 0)
            if points <= 0:
                continue
            plan = _PASSIVE_NODE_EFFECTS.get(node_id)
            if plan is None:
                self.issue(
                    "warning",
                    "unmapped passive node",
                    f"Mage passive node {node_id} ({node.get('name')!r}) has no v1 mapping",
                    "passive_tree",
                )
                self.add(
                    _contribution(
                        f"passive:mg-1:{node_id}:unmapped",
                        source_kind="passive_node",
                        source_label=self._passive_label(node_id, node),
                        source_path=f"data.passive_tree.nodes.{node_id}",
                        stat_name=str(node.get("name") or f"node {node_id}"),
                        stat_key=model.STAT_UNSUPPORTED,
                        operation="flag",
                        value=float(points),
                        unit="points",
                        included=False,
                        exclusion_reason="passive node has no v1 mapping in the input adapter",
                        status=model.STATUS_UNRESOLVED,
                        evidence=self._passive_evidence(node_id),
                    )
                )
                continue
            entries = self._passive_bonus.get(node_id) or []
            catalog_evidence = self._passive_evidence(node_id) + self._passive_prose_evidence(node_id)
            for effect in plan["effects"]:
                entry_index = int(effect["idx"])
                mapped_entries.add((node_id, entry_index))
                if effect.get("kind") == "buff":
                    self._preparation_effect(node_id, node, points, effect)
                    continue
                if entry_index >= len(entries):
                    self.issue(
                        "warning",
                        "passive entry missing",
                        f"passive_bonus mg-1 node {node_id} has no stat entry index {entry_index}",
                        "passive_tree",
                    )
                    continue
                entry = entries[entry_index]
                total, scaling_note = _scale_passive_entry(entry, points)
                entry_evidence = self._passive_entry_evidence(node_id, entry_index)
                evidence = catalog_evidence + entry_evidence + [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        f"planner.passiveBonuses['{node_id}'][{entry_index}] = {json.dumps(entry, sort_keys=True)}; "
                        f"{scaling_note}",
                        locator="data/releases/phase1-dataset-003/data.sqlite#source_records "
                        "entity_kind=passive_bonus source_scope=planner.passiveBonuses source_id=mg-1",
                    )
                ]
                excluded_reason = effect.get("excluded")
                if excluded_reason:
                    self.add(
                        _contribution(
                            f"passive:mg-1:{node_id}:e{entry_index}",
                            source_kind="passive_node",
                            source_label=self._passive_label(node_id, node),
                            source_path=f"data.passive_tree.nodes.{node_id}",
                            stat_name=effect.get("stat_name") or str(node.get("name")),
                            stat_key=model.STAT_UNSUPPORTED,
                            operation="flag",
                            value=total,
                            unit=effect.get("unit", "flat"),
                            condition_code=effect.get("condition_code"),
                            condition_status=(
                                model.CONDITION_UNKNOWN
                                if effect.get("condition_code")
                                else model.CONDITION_ALWAYS
                            ),
                            included=False,
                            exclusion_reason=excluded_reason,
                            status=(
                                model.STATUS_UNRESOLVED
                                if effect.get("unresolved")
                                else model.STATUS_CONFIRMED
                            ),
                            evidence=evidence,
                            notes=effect.get("notes"),
                            raw={"node_id": node_id, "points": points, "entry": entry},
                        )
                    )
                    continue
                condition_code = effect.get("condition_code")
                condition_status = model.CONDITION_ALWAYS
                if condition_code:
                    condition_status = model.CONDITION_UNKNOWN
                self.add(
                    _contribution(
                        f"passive:mg-1:{node_id}:e{entry_index}",
                        source_kind="passive_node",
                        source_label=self._passive_label(node_id, node),
                        source_path=f"data.passive_tree.nodes.{node_id}",
                        stat_name=effect.get("stat_name") or str(node.get("name")),
                        stat_key=effect["stat_key"],
                        operation=effect.get("operation", "added"),
                        value=total,
                        unit=effect.get("unit", "flat"),
                        damage_tags=effect.get("damage_tags", ()),
                        target=effect.get("target"),
                        condition_code=condition_code,
                        condition_status=condition_status,
                        status=model.STATUS_DERIVED
                        if not bool(entry.get("noScaling"))
                        else model.STATUS_CONFIRMED,
                        evidence=evidence,
                        notes=(effect.get("notes") or "") + (
                            "; condition unresolved in the static snapshot" if condition_code else ""
                        )
                        or None,
                        raw={"node_id": node_id, "points": points, "entry": entry},
                    )
                )
            # keep unmapped catalog effect entries visible (never drop a line)
            for entry_index in range(len(entries)):
                if (node_id, entry_index) in mapped_entries:
                    continue
                entry = entries[entry_index]
                self.add(
                    _contribution(
                        f"passive:mg-1:{node_id}:e{entry_index}",
                        source_kind="passive_node",
                        source_label=self._passive_label(node_id, node),
                        source_path=f"data.passive_tree.nodes.{node_id}",
                        stat_name=f"unmapped passive effect (property {entry.get('property')!r})",
                        stat_key=model.STAT_UNSUPPORTED,
                        operation="flag",
                        value=float(entry.get("value") or 0.0),
                        unit="unconfirmed",
                        included=False,
                        exclusion_reason="passive effect entry not interpreted by the v1 adapter",
                        value_source="source",
                        status=model.STATUS_UNRESOLVED,
                        evidence=catalog_evidence + self._passive_entry_evidence(node_id, entry_index),
                        raw={"node_id": node_id, "points": points, "entry": entry},
                    )
                )
                self.issue(
                    "warning",
                    "unmapped passive effect",
                    f"Mage passive node {node_id} entry {entry_index} not interpreted "
                    f"({entry!r})",
                    "passive_tree",
                )

    def _passive_label(self, node_id: str, node: dict[str, Any]) -> str:
        return f"Mage passive {node.get('name')!r} (mg-1 node {node_id}, {node.get('points')} pts)"

    def _passive_catalog_node(self, node_id: str) -> dict[str, Any]:
        return self.catalog.node_by_index(self._passive_tree["entity_id"], node_id)

    def _passive_evidence(self, node_id: str) -> list[model.Evidence]:
        node = self._passive_nodes[node_id]
        description = ""
        try:
            catalog_node = self._passive_catalog_node(node_id)
            description = catalog_node.get("description") or ""
        except Exception:  # pragma: no cover - description is optional evidence
            description = ""
        return [
            _ev(
                model.EVIDENCE_CATALOG,
                f"Mage passive node {node_id} {node.get('name')!r}, points={node.get('points')}, "
                f"max_points={node.get('max_points')}",
                locator=f"data/releases/phase1-dataset-003/data.sqlite#nodes "
                f"node:letools:mg-1:{node_id}",
                snippet=description or None,
            )
        ]

    def _passive_prose_evidence(self, node_id: str) -> list[model.Evidence]:
        """Node description / alt-text translations (may be generic mechanics text)."""
        record = self.catalog.source_record("passive_node", "mg-1", node_id)
        raw = (record or {}).get("raw_payload") or {}
        evidence: list[model.Evidence] = []
        for field, label in (("descriptionKey", "description"), ("altTextKey", "alt text")):
            key = raw.get(field)
            if not key:
                continue
            value = self.catalog.translation_by_key(str(key))
            if not value:
                continue
            evidence.append(
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"node {label} translation {key}: {value!r}",
                    locator=f"data/releases/phase1-dataset-003/data.sqlite#translations {key}",
                    snippet=value[:500],
                )
            )
        return evidence

    def _passive_entry_evidence(self, node_id: str, entry_index: int) -> list[model.Evidence]:
        """Catalog stat-effect evidence for one node stat entry (semantics cross-check)."""
        try:
            catalog_node = self._passive_catalog_node(node_id)
            effects = self.catalog.effects_for_owner(catalog_node["entity_id"])
        except Exception:  # pragma: no cover - defensive
            return []
        for effect in effects:
            effect_id = str(effect.get("effect_id") or "")
            if not effect_id.endswith(f"#stat#{entry_index}"):
                continue
            label = effect.get("raw_text")
            values = [
                {
                    "value_kind": value.get("value_kind"),
                    "value_numeric": value.get("value_numeric"),
                    "unit": value.get("unit"),
                    "stat_id": value.get("stat_id"),
                }
                for value in (effect.get("values") or [])
            ]
            return [
                _ev(
                    model.EVIDENCE_CATALOG,
                    f"catalog node stat effect #stat#{entry_index} label={label!r} "
                    f"values={json.dumps(values, sort_keys=True)} "
                    f"parse_status={effect.get('parse_status')!r}",
                    locator="data/releases/phase1-dataset-003/data.sqlite#effects "
                    f"{effect_id}",
                    snippet=label,
                )
            ]
        return []

    def _preparation_effect(
        self, node_id: str, node: dict[str, Any], points: int, effect: dict[str, Any]
    ) -> None:
        """Preparation (node 4): 'next elemental spell' buff, not a static stat."""
        payload = self.catalog.passive_bonus_payload("mg-1")
        buff_entries = (payload.get("buffs") or {}).get(node_id) or []
        # the buff table stores one entry per mod; keep only the damage mod (prop 0)
        damage_values = [
            float(mod["value"])
            for buff in buff_entries
            for mod in (buff.get("mods") or [])
            if mod.get("prop") == 0
        ]
        per_point = damage_values[0] if damage_values else 0.2
        total = per_point * points
        prose_evidence = self._passive_prose_evidence(node_id)
        damage_evidence = self._passive_entry_evidence(node_id, 0)
        self.add(
            _contribution(
                f"passive:mg-1:{node_id}:buff-damage",
                source_kind="passive_node",
                source_label=self._passive_label(node_id, node),
                source_path=f"data.passive_tree.nodes.{node_id}",
                stat_name="Next Spell Increased Damage",
                stat_key=model.STAT_DAMAGE,
                operation="increased",
                value=total,
                unit="percent",
                damage_tags=["Elemental", "Spell"],
                condition_code="preparation_next_elemental_spell",
                condition_status=model.CONDITION_UNKNOWN,
                status=model.STATUS_DERIVED,
                evidence=prose_evidence
                + damage_evidence
                + [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        "planner.passiveBonuses.buffs['4'] Next Spell Increased Damage buff mod "
                        f"prop=0 value={per_point}; node description: "
                        "'Every 3 seconds, your next elemental spell deals increased damage...'",
                        locator="data/releases/phase1-dataset-003/data.sqlite#source_records "
                        "entity_kind=passive_bonus source_id=mg-1#buffs.4",
                    ),
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"{per_point} per point × {points} points = {total}",
                        locator="le_calc.stats#_preparation_effect",
                    ),
                ],
                notes="state-dependent next-spell buff: readiness at t=0 / refresh during a cast "
                "loop is not resolved by the static snapshot (execution plan §2.3); the engine "
                "must not apply this unconditionally. Per-point scaling of the buff value is a "
                "v1 assumption (planner buff table has noScaling=false/pointThreshold=0).",
                raw={"node_id": node_id, "points": points, "buff_entries": buff_entries},
            )
        )
        slow_entries = [
            float(mod["value"])
            for buff in buff_entries
            for mod in (buff.get("mods") or [])
            if mod.get("prop") == 1
        ]
        self.add(
            _contribution(
                f"passive:mg-1:{node_id}:buff-slow-chance",
                source_kind="passive_node",
                source_label=self._passive_label(node_id, node),
                source_path=f"data.passive_tree.nodes.{node_id}",
                stat_name="Next Spell Slow Chance",
                stat_key=model.STAT_UNSUPPORTED,
                operation="flag",
                value=(slow_entries[0] if slow_entries else 0.2) * points,
                unit="percent",
                included=False,
                exclusion_reason="slow is a non-damaging ailment effect; not part of the v1 "
                "damage model",
                status=model.STATUS_CONFIRMED,
                evidence=prose_evidence + self._passive_entry_evidence(node_id, 1),
            )
        )

    # ==================================================================
    # 4. Black Hole skill tree (17 selected nodes, 36 points)
    # ==================================================================
    def build_skill_nodes(self) -> None:
        if not self._bh_selected:
            raise SnapshotError("calculator export has no selected Black Hole skill nodes")
        for node_id in sorted(self._bh_selected, key=int):
            points = self._bh_selected[node_id]
            plan = _SKILL_NODE_EFFECTS.get(node_id)
            if plan is None:
                self.issue(
                    "warning",
                    "unmapped skill node",
                    f"Black Hole node {node_id} has no v1 mapping",
                    "skill_tree",
                )
                self.add(
                    _contribution(
                        f"skill:bh2:{node_id}:unmapped",
                        source_kind="skill_node",
                        source_label=f"Black Hole skill node {node_id}",
                        source_path=f"data.specializations.bh2.nodes.{node_id}",
                        stat_name=f"node {node_id}",
                        stat_key=model.STAT_UNSUPPORTED,
                        operation="flag",
                        value=float(points),
                        unit="points",
                        group=f"{SKILL_GROUP_PREFIX}{node_id}",
                        included=False,
                        exclusion_reason="skill node has no v1 mapping in the input adapter",
                        status=model.STATUS_UNRESOLVED,
                        evidence=self._skill_node_evidence(node_id),
                    )
                )
                continue
            catalog_node = self._bh_nodes[node_id]
            effects = self.catalog.effects_for_owner(catalog_node["entity_id"])
            effects_by_name: dict[str, dict[str, Any]] = {}
            for effect in effects:
                name = (effect.get("extensions") or {}).get("stat_name") or effect.get("raw_text")
                if name:
                    effects_by_name[str(name)] = effect
            covered_names: set[str] = set()
            for spec in plan["effects"]:
                source_name = spec.get("catalog_stat_name") or spec.get("stat_name")
                effect = effects_by_name.get(str(source_name))
                if spec.get("kind") == "flag":
                    self._skill_flag(node_id, points, catalog_node, spec, effect)
                    covered_names.add(str(source_name))
                    continue
                covered_names.add(str(source_name))
                if effect is None or not effect.get("values"):
                    self.issue(
                        "warning",
                        "skill node stat missing from catalog",
                        f"bh2 node {node_id} stat {source_name!r} not found in catalog effects",
                        "skill_tree",
                    )
                    found = 0.0
                    no_scaling = True
                    raw_stat: dict[str, Any] = {}
                else:
                    value_row = effect["values"][0]
                    found = float(value_row.get("value_numeric") or 0.0)
                    raw_stat = (effect.get("extensions") or {}).get("stat") or {}
                    no_scaling = bool(raw_stat.get("noScaling"))
                total = found if no_scaling else found * points
                expected = spec.get("expected_total")
                if expected is not None and abs(total - float(expected)) > 1e-9:
                    self.issue(
                        "warning",
                        "skill node value differs from planning evidence",
                        f"bh2 node {node_id} {spec.get('stat_name')!r}: catalog-derived "
                        f"{total!r} vs planning-evidence/ruleset {expected!r}; using catalog value "
                        "(Lead review required)",
                        "skill_tree",
                    )
                evidence = self._skill_node_evidence(node_id) + [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        f"bh2 node {node_id} effect {str(source_name)!r} value_numeric={found!r}, "
                        f"noScaling={no_scaling}; points={points} -> total {total!r}",
                        locator=f"data/releases/phase1-dataset-003/data.sqlite#nodes "
                        f"node:letools:bh2:{node_id}",
                    )
                ]
                self.add(
                    _contribution(
                        f"skill:bh2:{node_id}:{spec['key']}",
                        source_kind="skill_node",
                        source_label=f"Black Hole skill node {node_id} {catalog_node.get('canonical_name') or ''}".strip(),
                        source_path=f"data.specializations.bh2.nodes.{node_id}",
                        stat_name=spec.get("stat_name") or str(source_name),
                        stat_key=spec["stat_key"],
                        operation=spec.get("operation", "increased"),
                        value=total,
                        unit=spec.get("unit", "percent"),
                        damage_tags=spec.get("damage_tags", ()),
                        target=spec.get("target"),
                        condition_code=spec.get("condition_code"),
                        condition_status=(
                            model.CONDITION_SATISFIED
                            if spec.get("condition_code")
                            else model.CONDITION_ALWAYS
                        ),
                        group=f"{SKILL_GROUP_PREFIX}{node_id}",
                        status=model.STATUS_CONFIRMED,
                        evidence=evidence,
                        notes=spec.get("notes"),
                        raw={
                            "node_id": node_id,
                            "points": points,
                            "value_numeric": found,
                            "no_scaling": no_scaling,
                            "total": total,
                        },
                    )
                )
            # keep every remaining catalog effect line for the ledger (stable ids)
            for leftover_index, name in enumerate(sorted(effects_by_name)):
                if name in covered_names:
                    continue
                effect = effects_by_name[name]
                stat = (effect.get("extensions") or {}).get("stat") or {}
                self.add(
                    _contribution(
                        f"skill:bh2:{node_id}:x{leftover_index:02d}",
                        source_kind="skill_node",
                        source_label=f"Black Hole skill node {node_id} {catalog_node.get('canonical_name') or ''}".strip(),
                        source_path=f"data.specializations.bh2.nodes.{node_id}",
                        stat_name=str(name),
                        stat_key=model.STAT_UNSUPPORTED,
                        operation="flag",
                        value=1.0,
                        unit="flag",
                        group=f"{SKILL_GROUP_PREFIX}{node_id}",
                        included=False,
                        exclusion_reason="skill-tree stat is not consumed by the v1 damage model "
                        "(behaviour/downside/utility line)",
                        status=model.STATUS_CONFIRMED,
                        evidence=self._skill_node_evidence(node_id),
                        raw={"node_id": node_id, "points": points, "stat": stat},
                    )
                )

    def _skill_flag(
        self,
        node_id: str,
        points: int,
        catalog_node: dict[str, Any],
        spec: dict[str, Any],
        effect: dict[str, Any] | None,
    ) -> None:
        stat = (effect.get("extensions") or {}).get("stat") if effect else {}
        self.add(
            _contribution(
                f"skill:bh2:{node_id}:{spec['key']}",
                source_kind="skill_node",
                source_label=f"Black Hole skill node {node_id} {catalog_node.get('canonical_name') or ''}".strip(),
                source_path=f"data.specializations.bh2.nodes.{node_id}",
                stat_name=spec.get("stat_name") or spec.get("key", "flag"),
                stat_key=spec.get("stat_key", model.STAT_UNSUPPORTED),
                operation=spec.get("operation", "flag"),
                value=float(points),
                unit=spec.get("unit", "points"),
                target=spec.get("target"),
                group=f"{SKILL_GROUP_PREFIX}{node_id}",
                included=bool(spec.get("included", False)),
                exclusion_reason=spec.get("exclusion_reason"),
                condition_code=spec.get("condition_code"),
                condition_status=(
                    model.CONDITION_SATISFIED if spec.get("condition_code") else model.CONDITION_ALWAYS
                ),
                status=model.STATUS_CONFIRMED,
                evidence=self._skill_node_evidence(node_id),
                notes=spec.get("notes"),
                raw={"node_id": node_id, "points": points, "stat": stat},
            )
        )

    def _skill_node_evidence(self, node_id: str) -> list[model.Evidence]:
        node = self._bh_nodes[node_id]
        return [
            _ev(
                model.EVIDENCE_GAME_DATA,
                f"Black Hole skill tree node {node_id} "
                f"({node.get('node_type')}, max_points={node.get('max_points')}); "
                f"selected points={self._bh_selected.get(node_id)}",
                locator="reports/phase-3/black-hole-planning-evidence-2026-11-03.json"
                f"#selected_nodes (bh2 node {node_id})",
                snippet=node.get("description") or None,
            )
        ]

    # ==================================================================
    # 5. derived contributions
    # ==================================================================
    def add_derived_attribute_contributions(self, attributes: dict[str, float]) -> None:
        source_path = "data.character.attributes"
        public_doc = _ev(
            model.EVIDENCE_PUBLIC_DOC,
            "official attribute benefits per point: Strength 4% armor, Dexterity 4 dodge, "
            "Intelligence 2% ward retention, Attunement 2 mana, Vitality 6 health and "
            "1% poison/necrotic resistance",
            locator=ATTRIBUTES_URL,
        )
        attunement = attributes["attunement"]
        self.add(
            _contribution(
                "derived:attribute:attunement-mana",
                source_kind="derived",
                source_label="Attunement (attribute)",
                source_path=source_path,
                stat_name="Mana",
                stat_key=model.STAT_MANA,
                operation="added",
                value=attunement * ATTRIBUTE_PER_POINT_EFFECTS["attunement"]["mana_added_per_point"],
                unit="flat",
                status=model.STATUS_DERIVED,
                evidence=[
                    public_doc,
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"attunement {attunement:.6g} × 2 mana = "
                        f"{attunement * 2:.6g} flat mana",
                        locator="le_calc.stats#add_derived_attribute_contributions",
                    ),
                ],
                notes="attunement is counted from base + all-attributes gear only (no attunement "
                "passives/affixes in this build)",
            )
        )
        vitality = attributes["vitality"]
        self.add(
            _contribution(
                "derived:attribute:vitality-poison-res",
                source_kind="derived",
                source_label="Vitality (attribute)",
                source_path=source_path,
                stat_name="Poison Resistance",
                stat_key=model.STAT_RESISTANCE,
                operation="added",
                value=vitality * ATTRIBUTE_PER_POINT_EFFECTS["vitality"]["poison_resistance_per_point"],
                unit="percent",
                target="poison",
                status=model.STATUS_DERIVED,
                evidence=[
                    public_doc,
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"vitality {vitality:.6g} × 1% poison resistance = {vitality * 0.01:.6g}",
                        locator="le_calc.stats#add_derived_attribute_contributions",
                    ),
                ],
                notes="Vitality also grants +6 health per point; health is not consumed by the "
                "v1 damage model and is recorded as an excluded row",
            )
        )
        self.add(
            _contribution(
                "derived:attribute:vitality-necrotic-res",
                source_kind="derived",
                source_label="Vitality (attribute)",
                source_path=source_path,
                stat_name="Necrotic Resistance",
                stat_key=model.STAT_RESISTANCE,
                operation="added",
                value=vitality
                * ATTRIBUTE_PER_POINT_EFFECTS["vitality"]["necrotic_resistance_per_point"],
                unit="percent",
                target="necrotic",
                status=model.STATUS_DERIVED,
                evidence=[
                    public_doc,
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        f"vitality {vitality:.6g} × 1% necrotic resistance = {vitality * 0.01:.6g}",
                        locator="le_calc.stats#add_derived_attribute_contributions",
                    ),
                ],
            )
        )
        unsupported_specs = [
            (
                "derived:attribute:strength-armor",
                "Armor (Strength)",
                "Armor",
                attributes["strength"] * ATTRIBUTE_PER_POINT_EFFECTS["strength"]["armor_increased_per_point"],
                "percent",
                "0.04 increased armor per Strength point; defensive, not used in v1 damage",
            ),
            (
                "derived:attribute:dexterity-dodge",
                "Dodge Rating (Dexterity)",
                "Dodge Rating",
                attributes["dexterity"] * ATTRIBUTE_PER_POINT_EFFECTS["dexterity"]["dodge_rating_per_point"],
                "flat",
                "4 dodge rating per Dexterity point; defensive, not used in v1 damage",
            ),
            (
                "derived:attribute:intelligence-ward-retention",
                "Ward Retention (Intelligence)",
                "Ward Retention",
                attributes["intelligence"]
                * ATTRIBUTE_PER_POINT_EFFECTS["intelligence"]["ward_retention_increased_per_point"],
                "percent",
                "2% ward retention per Intelligence point (official article; catalog alt text says "
                "4% in 1.5.2 - documentation-only conflict, no damage impact); not used in v1 damage",
            ),
            (
                "derived:attribute:vitality-health",
                "Health (Vitality)",
                "Health",
                attributes["vitality"]
                * ATTRIBUTE_PER_POINT_EFFECTS["vitality"]["health_added_per_point"],
                "flat",
                "6 health per Vitality point; not consumed by the v1 damage model",
            ),
        ]
        for cid, label, stat_name, value, unit, reason in unsupported_specs:
            self.add(
                _contribution(
                    cid,
                    source_kind="derived",
                    source_label=label,
                    source_path=source_path,
                    stat_name=stat_name,
                    stat_key=model.STAT_UNSUPPORTED,
                    operation="flag",
                    value=value,
                    unit=unit,
                    included=False,
                    exclusion_reason=reason,
                    status=model.STATUS_DERIVED,
                    evidence=[public_doc],
                )
            )

    # ==================================================================
    # 6. excluded sources
    # ==================================================================
    def build_excluded_sources(self) -> list[dict[str, Any]]:
        excluded: list[dict[str, Any]] = [
            {
                "source_id": "skill_tree:weaver",
                "label": "编织树 / 编织容器（weaver tree, weaverTreeContainers）",
                "reason": "用户 2026-11-03 范围要求排除；历史 revision 原样保留且只读",
                "category": "scope",
            },
            {
                "source_id": "blessing_non_dps:drop_rate",
                "label": "5 条 Increased Drop Rate 祝福（Grand Scales of Greed / Talon of "
                "Grandeur / Subtlety of Slaughter / Hope of the Beginning / Memory of Masters）",
                "reason": "2026-11-04 定向复审 R1：掉落率祝福为 non-DPS 来源，已在 calc-export "
                "层以 excluded_non_dps=5（propertyList 104 = Increased Drop Rate，"
                "excluded_non_dps_mod_ids 列出 5 个 id）排除；不属于伤害来源账，"
                "对 Black Hole / Ignite 数值零影响。原始来源仍归档在 BuildSpec 可读列表",
                "category": "scope",
            },
            {
                "source_id": "system:weaver_containers",
                "label": "编织容器内容（历史 revision 中的 5 个容器物品）",
                "reason": "不属于本轮穿戴属性；不把历史 BuildSpec 的容器物品计入",
                "category": "scope",
            },
        ]
        tree_labels = {
            "sb44eQ": "Enchant Weapon 专精树",
            "me27": "Meteor 专精树",
            "fw3d": "Flame Ward 专精树",
            "fr4wl": "Frost Wall 专精树",
        }
        for tree_id, label in tree_labels.items():
            spec = self.bundle.specialization_for_tree(tree_id) or {}
            nodes = [
                {
                    "node_id": node.get("node_id"),
                    "name": node.get("name"),
                    "points": node.get("points"),
                }
                for node in (spec.get("nodes") or [])
            ]
            excluded.append(
                {
                    "source_id": f"skill_tree:{tree_id}",
                    "label": label,
                    "reason": "范围外技能树：其主动/被动效果不进入 Black Hole 本体与 Ignite 计算"
                    "（执行方案 §2.2）",
                    "category": "scope",
                    "points_total": spec.get("points_total"),
                    "nodes": nodes,
                }
            )
        excluded.extend(
            [
                {
                    "source_id": "action_bar:other_skills",
                    "label": "技能栏其他技能（Meteor / Enchant Weapon / Teleport / Frost Wall）",
                    "reason": "本轮不执行其他主动技能；技能栏配置仅作构筑上下文",
                    "category": "scope",
                },
                {
                    "source_id": "active_buff:sb44eQ",
                    "label": "Enchant Weapon 保存的激活配置（activeBuffs.skills=[sb44eQ]）",
                    "reason": "范围外主动 Buff；保存的激活配置不等于生效/满层（执行方案 §2.2）",
                    "category": "scope",
                },
                {
                    "source_id": "ignite_sources:other_skills",
                    "label": "其他技能施加的 Ignite（Enchant Weapon 树 Immolating Strike / "
                    "Eternal Flames / Conflagrate、Frost Wall Pyroglass 等）",
                    "reason": "其他技能施加的 Ignite 不属于本次 Black Hole 施放（执行方案 §2.2）",
                    "category": "scope",
                },
                {
                    "source_id": "item:equipment.chest#supernova",
                    "label": "Unstable Core 的 Elemental Nova 条件词条（346% 面积 / +63 Spell Damage）",
                    "reason": "条件依赖范围外技能 Elemental Nova 的连续施放",
                    "category": "condition",
                },
                {
                    "source_id": "item:equipment.weapon1#chthonic",
                    "label": "Spine of Malatros 的 Chthonic Fissure / Flame Whip 词条",
                    "reason": "Chthonic Fissure / Flame Whip 为范围外技能",
                    "category": "scope",
                },
                {
                    "source_id": "item:equipment.feet#fire_trail",
                    "label": "Fiery Dragon Shoes 的被暴击时施放 Fire Trail 词条",
                    "reason": "触发依赖范围外事件（且本轮不计受到暴击），不进入伤害模型",
                    "category": "condition",
                },
                {
                    "source_id": "system:target_side_assumptions",
                    "label": "目标侧与空间条件（Boss/稀有标签、0 抗性、中心 1m、双星全覆盖）",
                    "reason": "v1 按用户规则 7 取最优场景（显式假设，不是真实 Boss 实测）",
                    "category": "assumption",
                },
            ]
        )
        return excluded

    # ==================================================================
    # 7. issues
    # ==================================================================
    def build_issues(self, *, mana: dict[str, Any], resistances: dict[str, Any]) -> list[dict[str, Any]]:
        # snapshot-level assumptions recorded once (kept in insertion order)
        issues = list(self.issues)
        seq = len(issues)

        def add(severity: str, title: str, detail: str, scope: str) -> None:
            nonlocal seq
            seq += 1
            issues.append(
                {
                    "issue_id": f"issue-input-ledger-{seq:03d}",
                    "severity": severity,
                    "title": title,
                    "detail": detail,
                    "scope": scope,
                }
            )

        add(
            "info",
            "v1 抗性基线假设",
            "基础抗性按 0 处理；不应用区域等级抗性惩罚（v1 无 level-based resistance "
            "penalty）；抗性上限 cap=75（百分点）。",
            "resistances",
        )
        add(
            "info",
            "抗性单位与来源池",
            "snapshot.resistances 的 total/cap/capped/overcapped 使用百分点数字（75.0 表示 75%），"
            "contribution 的 resistance value 使用小数（0.67 = +67%）。"
            "elemental / all 两条是来源池（只用于聚合），不是可被截断的实际抗性。",
            "resistances",
        )
        add(
            "info",
            "基础法力等级缩放公式",
            "base mana 采用 baseMana + manaPerLevel*(level-1) = 100.00094（v1 假设）；"
            "若改用 level*manaPerLevel 则为 100.506，mana_max 约 219.9，仍 <300，"
            "因此 300 法力条件结论不受影响。",
            "mana",
        )
        add(
            "info",
            "Intelligence 每点效果来源冲突",
            "用户提供的 support 文章写 Intelligence=2% ward retention/点（采信，仅文档用途）；"
            "catalog 中 MG 节点 altText（1.5.2 数据）写 4% ward retention/点。"
            "该值不进入 v1 伤害计算，仅记录差异。",
            "attributes",
        )
        add(
            "info",
            "Crescendoing Rage 最小规则",
            "ring2 sealed affix 1124 的值语义由 catalog buff UIBuffs:248 与翻译确认："
            "每第 3 次施放（ability cast）造成 50% more damage。静态快照记录为 "
            "condition_code=crescendoing_rage_third_cast、condition_status=unknown；"
            "单次基线（第 1 次施放）不满足，循环中每隔 3 次施放满足一次，由引擎/规则包决定。",
            "conditions",
        )
        add(
            "info",
            "Preparation 下一次施法状态未确定",
            "Mage 被动 Preparation（mg-1 node 4, 4 点）是 next elemental spell buff："
            "buffs={SpellDamage: 20%/点, SlowChance}；快照记录 condition_code="
            "preparation_next_elemental_spell、condition_status=unknown（不默认生效）。"
            "单次测试的初始就绪状态与循环刷新需引擎/规则包显式处理（执行方案 §2.3）。",
            "conditions",
        )
        add(
            "info",
            "Frozen Steel 的 property=-1 条目按 unsupported 记账",
            "Mage 被动 Frozen Steel（mg-1 node 36, 4 点）第 3 条：catalog stat 标签为 "
            "'Damage Against Chilled Or Frozen' +6%（noScaling，4 点阈值），但 "
            "planner.passiveBonuses 把它归一化为 property=-1（未映射属性命名空间），"
            "数值 6 也不是 6.0 倍小数；因此**不**把它解释为 global increased damage，"
            "记为 stat_key=unsupported/unresolved、included=False，并保留 raw 与证据。"
            "即使语义确认，它也是 chilled/frozen 条件（Black Hole 的 chill 仅有 25%/秒概率）。",
            "conditions",
        )
        add(
            "info",
            "Heat Wave 与其他 ignite chance 来源",
            "Pyromancer（mg-1 node 18）提供 +40% ignite chance（Fire skills），"
            "静态快照记为 stat_key=ailment_chance/target=ignite；Heat Wave 的每秒判定公式由"
            "规则包定义，引擎 v1 未使用其他 ignite chance 来源（执行方案 B2.1 未决项）。",
            "ignite",
        )
        add(
            "warning",
            "idol_altar 条件 CDR 词条无法判定",
            f"equipment.idol_altar implicit '10% increased cooldown recovery speed if there are no "
            f"larger idols above smaller ones in the grid'：本数据集只有神像 x/y 与"
            f"catalog 尺寸名（Grand/Minor/Adorned），没有宽高或网格翻转规则（phase-2 交接明确 "
            f"grid_validation=not_verified）。条件记 unknown、included=False，不给 10% CDR。",
            "conditions",
        )
        add(
            "info",
            "idol_altar 前后缀效果通过 manual override 计入",
            "idol_altar 的 'Effect of Prefixes/Suffixes for Idols in Refracted Slots' 原始行"
            "不直接进入贡献池；其效果已通过人工确认的折射选择折算进神像数值"
            "（见 idol-refraction-selection.json / override）。网格与槽位规则仍未验证。",
            "excluded_sources",
        )
        add(
            "info",
            "max mana 300 条件判定",
            f"mana_max = {mana.get('total'):.6f} < 300，因此两条 'increased fire damage doubled "
            f"if 300+ max mana' 词条只取基础值；额外翻倍行 condition_status=not_satisfied。",
            "conditions",
        )
        add(
            "info",
            "非法术/非法术技能来源排除",
            "weapon1 implicit '+90 Melee Damage'、minion 版本词条、Supernova/Chthonic/Flame Whip "
            "条件词条、暴击相关词条均保留在 source_ledger 并写明排除原因；Black Hole 本体 hit=false，"
            "不应用暴击。",
            "excluded_sources",
        )
        add(
            "info",
            "calc-export 定向复审：5 条 non-DPS 掉落率祝福已在上游排除",
            "2026-11-04 定向复审 R1（calc-export 148 mods / 11 description）把 5 条 propertyList "
            "104 = Increased Drop Rate 的祝福（blessings.1/2/4/6/8）以上游 "
            "excluded_non_dps / excluded_non_dps_mod_ids 排除，因此本轮 241 条贡献中没有这 5 行；"
            "它们是 non-DPS 来源，对 Black Hole / Ignite 伤害数值零影响，"
            "原始来源仍归档在 BuildSpec 可读列表，并在 snapshot.excluded_sources 记为 scope 排除。",
            "excluded_sources",
        )
        add(
            "info",
            "revision pin 以 calc-export 为准",
            "输入适配器以 calculation-input.revision_id 作为 revision pin（当前 "
            "rev-c746411ccebee9c0, seq 8），只用 build current.json 做交叉核对："
            "current 指向不同 revision 时记 info 级漂移警告而不是 fatal，"
            "保证并发工作流升级交付构筑时不会破坏本次运行的可复现性。",
            "inputs",
        )
        add(
            "info",
            "技能等级行仅信息性",
            "所有 'Level of Skills' 行只影响技能等级/可分配点，按执行方案 §A2 不臆造每级伤害；"
            "保留原值并以 informational 记录。",
            "contributions",
        )
        add(
            "info",
            "被动节点每点缩放来源",
            "Mage 被动按 LE Tools planner.passiveBonuses 的‘每点值 × 已分配点数’计算；"
            "noScaling 条目在 node 点数 >= noScalingPointThreshold 时按一次计入。"
            "总量行 status=derived（per-point 值来自 catalog，缩放为本项目算术）。",
            "passive_tree",
        )
        add(
            "info",
            "Binary System 的 +100% 由规则包按独立 more 应用",
            "bh2 node 9 的 '+100% Damage' 已由 v3 规则包按已确认的双星独立 more 组装；"
            "skill_node 来源行只记结构 flag，不重复把该数值加入贡献池。",
            "skill_tree",
        )
        add(
            "info",
            "Supermassive / Weakened Reality 条件按最优场景满足",
            "v1 场景固定 Boss/稀有标签、目标在中心 1m 内（用户规则 7），engine 会同时应用"
            "Supermassive 与 Weakened Reality 的 more；Weakened Reality 不继承给 Ignite"
            "（规则包已排除 node 27）。",
            "scenario",
        )
        derived_glove = [
            c for c in self.contributions if c.contribution_id.endswith(":doubled") is False
            and c.source_path == "data.equipment.hands"
            and c.stat_key in (model.STAT_CAST_SPEED, model.STAT_COOLDOWN_RECOVERY)
        ]
        for row in derived_glove:
            if row.status == model.STATUS_DERIVED:
                add(
                    "info",
                    "手套 overcap cold 派生词条取整假设",
                    f"{row.contribution_id}: {row.notes}",
                    "cooldown",
                )
        return issues

    # ==================================================================
    # 8. final assembly
    # ==================================================================
    def character(self) -> dict[str, Any]:
        definition = self._class_data["class_definition"]
        base_keys = (
            "baseHealth",
            "baseMana",
            "healthPerLevel",
            "manaPerLevel",
            "healthRegen",
            "manaRegen",
            "healthRegenPerLevel",
            "baseStrength",
            "baseDexterity",
            "baseIntelligence",
            "baseAttunement",
            "baseVitality",
            "baseStunAvoidance",
            "baseEndurance",
            "baseEnduranceThreshold",
            "enduranceThresholdPerLevel",
            "firstLevelForMinionScaling",
        )
        class_base = {key: definition.get(key) for key in base_keys if key in definition}
        return {
            "level": int(self.bundle.character.get("level") or 0),
            "class": self._class_data["class_name"],
            "mastery": self._class_data["mastery_name"],
            "class_entity_id": self._class_data["class_entity_id"],
            "mastery_entity_id": self._class_data["mastery_entity_id"],
            "class_base": class_base,
            "attribute_per_point_bonuses": ATTRIBUTE_PER_POINT_EFFECTS,
            "attribute_per_point_source": ATTRIBUTES_URL,
        }


# ---------------------------------------------------------------------------
# Passive node mapping (values come from catalog planner.passiveBonuses)
# ---------------------------------------------------------------------------

_PASSIVE_NODE_EFFECTS: dict[str, dict[str, Any]] = {
    "0": {  # Arcanist (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Intelligence", "stat_key": model.STAT_ATTRIBUTE,
             "target": "intelligence", "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Fire Resistance", "stat_key": model.STAT_RESISTANCE,
             "target": "fire", "operation": "added", "unit": "percent"},
            {"idx": 2, "stat_name": "Lightning Resistance", "stat_key": model.STAT_RESISTANCE,
             "target": "lightning", "operation": "added", "unit": "percent"},
        ]
    },
    "2": {  # Elementalist (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Increased Fire Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Fire"]},
            {"idx": 1, "stat_name": "Increased Cold Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Cold"]},
            {"idx": 2, "stat_name": "Increased Lightning Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Lightning"]},
        ]
    },
    "4": {  # Preparation (4 pts): buff-only node
        "effects": [
            {"idx": 0, "kind": "buff", "stat_name": "Next Spell Increased Damage"},
        ]
    },
    "12": {  # Mana Shell (5 pts)
        "effects": [
            {"idx": 0, "stat_name": "Mana", "stat_key": model.STAT_MANA,
             "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Ward per Second", "excluded":
             "ward generation; ward is not modelled in v1"},
            {"idx": 2, "stat_name": "Ward per Second per 10 Max Mana", "excluded":
             "ward generation (5-point threshold effect); ward is not modelled in v1"},
        ]
    },
    "18": {  # Pyromancer (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Increased Fire Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Fire"]},
            {"idx": 1, "stat_name": "Fire Skill Ignite Chance", "stat_key": model.STAT_AILMENT_CHANCE,
             "operation": "added", "unit": "percent", "target": "ignite",
             "notes": "catalog label 'Fire Skill Ignite Chance' +5%/point × 8 points = +40%; "
             "passiveBonuses row normalizes it to property=1 (Ailment Chance) specialTag=1 "
             "(Ignite) tags=8 (Fire). Application through Heat Wave is an open question "
             "(execution plan B2.1): the engine v1 heat_wave_application does not consume this "
             "row, and the ruleset marks other ignite-chance sources as not included."},
        ]
    },
    "20": {  # Calculated Destruction (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Intelligence", "stat_key": model.STAT_ATTRIBUTE,
             "target": "intelligence", "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Spell Critical Strike Chance per Intelligence", "excluded":
             "Black Hole body hit=false -> no critical strikes (execution plan A1)"},
        ]
    },
    "23": {  # Inferno (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Increased Fire Damage Over Time", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Fire", "DoT"]},
            {"idx": 1, "stat_name": "Spreading Flames Chance", "excluded":
             "applies to Spreading Flames, not Ignite; no Spreading Flames source inside the "
             "Black Hole v1 scope"},
            {"idx": 2, "stat_name": "More Fire Damage Over Time", "stat_key": model.STAT_DAMAGE,
             "operation": "more", "unit": "percent", "damage_tags": ["Fire", "DoT"],
             "notes": "catalog label 'More Fire Damage Over Time' +15%, noScaling with node-level "
             "threshold 5 (8 points allocated -> applies once). The passiveBonuses entry has no "
             "conditions field, i.e. it is unconditional; tags=4104 = Fire+DoT so it applies to "
             "the Black Hole fire DoT component and to the Ignite DoT component (not to the cold "
             "component), per the ruleset inheritance list. Whether 'more Fire DoT' applies to "
             "Ignite is an engine-side pool attribution recorded in the ruleset "
             "(ignite.inherit_from_tree_nodes includes node 23)."},
        ]
    },
    "25": {  # Chronomancy (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Vitality", "stat_key": model.STAT_ATTRIBUTE,
             "target": "vitality", "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Chill Duration", "excluded":
             "non-damaging ailment duration; no effect on Black Hole / Ignite damage"},
            {"idx": 2, "stat_name": "Shock Duration", "excluded":
             "non-damaging ailment duration; no effect on Black Hole / Ignite damage"},
            {"idx": 3, "stat_name": "Ignite Duration", "stat_key": model.STAT_AILMENT_DURATION,
             "operation": "increased", "unit": "percent", "target": "ignite",
             "notes": "catalog label 'Ignite Duration' +5%/point × 8 points = +40%; "
             "passiveBonuses row normalizes it to property=42 specialTag=1 (Ignite); additive "
             "duration pool consumed by engine _ignite_duration"},
        ]
    },
    "32": {  # Arcane Warden (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Ward Gained", "excluded": "ward generation; not modelled in v1"},
            {"idx": 1, "stat_name": "Intelligence", "stat_key": model.STAT_ATTRIBUTE,
             "target": "intelligence", "operation": "added", "unit": "flat"},
            {"idx": 2, "stat_name": "Ward Decay Threshold per 10 Max Health", "excluded":
             "ward stat (5-point threshold effect); not modelled in v1"},
        ]
    },
    "33": {  # Elemental Affinity (6 pts)
        "effects": [
            {"idx": 0, "stat_name": "Increased Elemental Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Elemental"]},
            {"idx": 1, "stat_name": "Elemental Resistance", "stat_key": model.STAT_RESISTANCE,
             "target": "elemental", "operation": "added", "unit": "percent"},
        ]
    },
    "35": {  # Infused Weapon (1 pt)
        "effects": [
            {"idx": 0, "stat_name": "Melee Damage", "excluded":
             "melee flat damage does not apply to the Black Hole spell"},
            {"idx": 1, "stat_name": "Adaptive Spell Damage", "stat_key": model.STAT_ADDED_DAMAGE,
             "operation": "added", "unit": "flat", "damage_tags": ["Spell"],
             "notes": "flat adaptive spell damage; scaled by addedDamageScaling=2.4 per tick"},
        ]
    },
    "36": {  # Frozen Steel (4 pts)
        "effects": [
            {"idx": 0, "stat_name": "Melee Cold Damage", "excluded":
             "melee flat damage does not apply to the Black Hole spell"},
            {"idx": 1, "stat_name": "Freeze Rate Multiplier", "excluded":
             "freeze rate is not part of the v1 damage model"},
            {"idx": 2, "stat_name": "Damage Against Chilled Or Frozen",
             "excluded":
             "planner.passiveBonuses normalizes this entry to property=-1 (unmapped property "
             "namespace); the node stat label is 'Damage Against Chilled Or Frozen' +6% "
             "(noScaling, 4-point threshold) and the effect is conditional on the enemy being "
             "chilled/frozen. The value is NOT interpreted as an unconditional damage modifier; "
             "kept as unsupported/unresolved for the ledger.",
             "unit": "unconfirmed", "unresolved": True,
             "condition_code": "enemy_chilled_or_frozen"},
        ]
    },
    "55": {  # Sphere of Protection (1 pt)
        "effects": [
            {"idx": 0, "stat_name": "Health", "excluded": "health is not consumed by the v1 damage model"},
            {"idx": 1, "stat_name": "Less Damage Taken from Ignited Shocked or Chilled Enemies",
             "excluded": "incoming damage mitigation; not part of the outgoing damage model"},
        ]
    },
    "56": {  # Unsealed Mana (6 pts)
        "effects": [
            {"idx": 0, "stat_name": "Mana", "stat_key": model.STAT_MANA,
             "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Increased Cast Speed", "stat_key": model.STAT_CAST_SPEED,
             "operation": "increased", "unit": "percent"},
            {"idx": 2, "stat_name": "Mana Burst", "excluded":
             "mana burst mechanic (5-point threshold); mana is not modelled in v1"},
            {"idx": 3, "stat_name": "Ward Burst", "excluded":
             "ward burst mechanic (5-point threshold); ward is not modelled in v1"},
        ]
    },
    "63": {  # Lavamancer (5 pts)
        "effects": [
            {"idx": 0, "stat_name": "Fire Penetration", "stat_key": model.STAT_PENETRATION,
             "operation": "added", "unit": "percent", "damage_tags": ["Fire"], "target": "fire"},
            {"idx": 1, "stat_name": "Fire Damage Leech", "excluded":
             "leech is not part of the v1 damage model"},
        ]
    },
    "65": {  # Recollection (5 pts)
        "effects": [
            {"idx": 0, "stat_name": "Increased Cooldown Recovery Speed",
             "stat_key": model.STAT_COOLDOWN_RECOVERY, "operation": "increased", "unit": "percent"},
        ]
    },
    "67": {  # Elemental Ascendance (7 pts)
        "effects": [
            {"idx": 0, "stat_name": "Elemental Resistance", "stat_key": model.STAT_RESISTANCE,
             "target": "elemental", "operation": "added", "unit": "percent"},
            {"idx": 1, "stat_name": "Increased Elemental Damage", "stat_key": model.STAT_DAMAGE,
             "operation": "increased", "unit": "percent", "damage_tags": ["Elemental"]},
            {"idx": 2, "stat_name": "Increased Stun Chance with Elemental Spells", "excluded":
             "stun chance; Black Hole body hit=false so hit-based stun does not apply"},
        ]
    },
    "69": {  # Arcane Insight (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Intelligence", "stat_key": model.STAT_ATTRIBUTE,
             "target": "intelligence", "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Ward Decay Threshold per Intelligence", "excluded":
             "ward stat (8-point threshold effect); not modelled in v1"},
        ]
    },
    "86": {  # Arcane Focus (8 pts)
        "effects": [
            {"idx": 0, "stat_name": "Intelligence", "stat_key": model.STAT_ATTRIBUTE,
             "target": "intelligence", "operation": "added", "unit": "flat"},
            {"idx": 1, "stat_name": "Ward Gain on Direct Spell Cast", "excluded":
             "ward generation; not modelled in v1"},
        ]
    },
}


def _scale_passive_entry(entry: dict[str, Any], points: int) -> tuple[float, str]:
    """Scale one ``planner.passiveBonuses`` stat entry by allocated points.

    Values in that table are per allocated point; ``noScaling`` entries apply
    once when the allocated points reach ``noScalingPointThreshold``.
    """
    value = float(entry.get("value") or 0.0)
    if bool(entry.get("noScaling")):
        threshold = int(entry.get("noScalingPointThreshold") or 0)
        if points >= threshold:
            return value, f"noScaling value applied once (points {points} >= threshold {threshold})"
        return 0.0, f"noScaling value not applied (points {points} < threshold {threshold})"
    return value * points, f"per-point value x {points} points"


# ---------------------------------------------------------------------------
# Black Hole skill-tree mapping (values read from catalog node effects)
# ---------------------------------------------------------------------------

_SKILL_NODE_EFFECTS: dict[str, dict[str, Any]] = {
    "2": {  # Tidal Force (4 pts)
        "effects": [
            {"key": "more", "stat_name": "Damage", "catalog_stat_name": "Damage",
             "stat_key": model.STAT_DAMAGE, "operation": "more", "unit": "percent",
             "damage_tags": [], "expected_total": 0.6,
             "notes": "more damage group node:bh2:2; applies to body and Ignite (ruleset)"},
        ]
    },
    "3": {  # Absolute Zero (1 pt)
        "effects": [
            {"key": "flag-chill", "kind": "flag", "stat_name": "Chill Chance",
             "catalog_stat_name": "Chill Chance", "exclusion_reason":
             "chill application only; chill is not part of the v1 damage model"},
        ]
    },
    "4": {  # Red Giant (1 pt)
        "effects": [
            {"key": "flag-convert", "kind": "flag", "stat_name": "Base Damage Converted to Fire",
             "catalog_stat_name": "Base Damage Converted to Fire", "exclusion_reason":
             "structural flag: base cold converted to fire (also applied to added damage per "
             "ruleset v1 assumption); consumed from ruleset.structural.red_giant, not as a "
             "damage modifier"},
            {"key": "duration", "stat_name": "Duration", "catalog_stat_name": "Duration",
             "stat_key": model.STAT_SKILL_DURATION_INCREASED, "operation": "increased",
             "unit": "percent", "expected_total": 0.8},
            {"key": "flag-no-pull", "kind": "flag", "stat_name": "Does Not Pull",
             "catalog_stat_name": "Does Not Pull", "exclusion_reason":
             "behaviour flag (no enemy pull); no damage effect"},
        ]
    },
    "6": {  # Heat Wave (5 pts)
        "effects": [
            {"key": "flag-ignite-chance", "kind": "flag", "stat_name":
             "Ignite Chance Per Second Per 1% Uncapped Fire Res",
             "catalog_stat_name": "Ignite Chance Per Second Per 1% Uncapped Fire Res",
             "stat_key": model.STAT_AILMENT_CHANCE, "operation": "flag", "target": "ignite",
             "unit": "points", "included": True,
             "notes": "points=5; +2% per point per 1% uncapped fire resistance per second "
             "(chance per second = 0.02 × 5 × uncapped_fire_resistance_percent). The chance "
             "is computed by the ruleset/engine; this row is the source-ledger flag."},
        ]
    },
    "7": {  # Flaming (3 pts)
        "effects": [
            {"key": "more-fire", "stat_name": "Fire Damage", "catalog_stat_name": "Fire Damage",
             "stat_key": model.STAT_DAMAGE, "operation": "more", "unit": "percent",
             "damage_tags": ["Fire"], "expected_total": 0.3,
             "notes": "more fire damage group node:bh2:7; body fire component and Ignite"},
        ]
    },
    "9": {  # Binary System (1 pt)
        "effects": [
            {"key": "flag-binary", "kind": "flag", "stat_name": "Binary System",
             "catalog_stat_name": "Binary System", "exclusion_reason":
             "structural flag: two stars (fire + cold), total structural doubling (user rule 2)"},
            {"key": "flag-damage", "kind": "flag", "stat_name": "Damage",
             "catalog_stat_name": "Damage", "exclusion_reason":
             "the node's displayed '+100% Damage' is the structural two-component doubling of "
             "the single Black Hole, not an additional independent more group in v1 "
             "(ruleset.structural.binary_system.extra_more = 0.0)"},
        ]
    },
    "10": {  # Umbral Core (3 pts)
        "effects": [
            {"key": "more", "stat_name": "Damage", "catalog_stat_name": "Damage",
             "stat_key": model.STAT_DAMAGE, "operation": "more", "unit": "percent",
             "damage_tags": [], "expected_total": 0.3,
             "notes": "more damage group node:bh2:10; body and Ignite"},
        ]
    },
    "15": {  # Time Dilation (1 pt)
        "effects": [
            {"key": "duration", "stat_name": "Duration", "catalog_stat_name": "Duration",
             "stat_key": model.STAT_SKILL_DURATION_INCREASED, "operation": "increased",
             "unit": "percent", "expected_total": 0.1},
            {"key": "flag-blind", "kind": "flag", "stat_name": "Blind Chance",
             "catalog_stat_name": "Blind Chance", "exclusion_reason":
             "blind application only; not part of the v1 damage model"},
        ]
    },
    "17": {  # Recurrence (4 pts)
        "effects": [
            {"key": "cooldown-reduction", "stat_name": "Cooldown Reduction",
             "catalog_stat_name": "Cooldown Reduction", "stat_key": model.STAT_COOLDOWN_REDUCTION,
             "operation": "increased", "unit": "percent", "expected_total": 0.6,
             "notes": "ruleset treats this as a multiplicative cooldown reduction applied before "
             "cooldown recovery speed"},
            {"key": "flag-mana-cost", "kind": "flag", "stat_name": "Mana Cost",
             "catalog_stat_name": "Mana Cost", "exclusion_reason":
             "mana cost reduction; v1 ignores the mana resource (user rule 6)"},
        ]
    },
    "20": {  # Drifting Singularity (1 pt)
        "effects": [
            {"key": "flag-seek", "kind": "flag", "stat_name": "Black Hole Seeks Enemies",
             "catalog_stat_name": "Black Hole Seeks Enemies", "exclusion_reason":
             "movement behaviour only; no damage effect"},
        ]
    },
    "21": {  # Spacial Dilation (1 pt)
        "effects": [
            {"key": "flag-projectile-speed", "kind": "flag", "stat_name": "Projectile Speed",
             "catalog_stat_name": "Projectile Speed", "exclusion_reason":
             "projectile speed only; no damage effect"},
        ]
    },
    "23": {  # Supermassive (4 pts)
        "effects": [
            {"key": "more-boss", "stat_name": "Damage To Rares And Bosses",
             "catalog_stat_name": "Damage To Rares And Bosses", "stat_key": model.STAT_DAMAGE,
             "operation": "more", "unit": "percent", "damage_tags": [],
             "condition_code": "target_is_boss_or_rare", "expected_total": 1.0,
             "notes": "v1 scenario has the boss/rare tag (user rule 7) -> condition satisfied"},
            {"key": "flag-priority", "kind": "flag", "stat_name": "Prioritises Rares And Bosses",
             "catalog_stat_name": "Prioritises Rares And Bosses", "exclusion_reason":
             "targeting behaviour; no damage effect"},
        ]
    },
    "24": {  # Density (2 pts)
        "effects": [
            {"key": "duration", "stat_name": "Duration", "catalog_stat_name": "Duration",
             "stat_key": model.STAT_SKILL_DURATION_INCREASED, "operation": "increased",
             "unit": "percent", "expected_total": 0.4},
        ]
    },
    "25": {  # Endless Maw (1 pt)
        "effects": [
            {"key": "duration", "stat_name": "Duration", "catalog_stat_name": "Duration",
             "stat_key": model.STAT_SKILL_DURATION_INCREASED, "operation": "increased",
             "unit": "percent", "expected_total": 0.3},
            {"key": "cooldown-flat", "stat_name": "Cooldown Duration (seconds)",
             "catalog_stat_name": "Cooldown Duration (seconds)",
             "stat_key": model.STAT_COOLDOWN_FLAT, "operation": "added", "unit": "seconds",
             "expected_total": 2.0,
             "notes": "ruleset applies +2s to the recharge before cooldown reduction"},
        ]
    },
    "27": {  # Weakened Reality (2 pts)
        "effects": [
            {"key": "more-center", "stat_name": "Damage to Enemies at Center",
             "catalog_stat_name": "Damage to Enemies at Center", "stat_key": model.STAT_DAMAGE,
             "operation": "more", "unit": "percent", "damage_tags": [],
             "condition_code": "target_within_center_1m", "expected_total": 0.6,
             "notes": "body-only more; does not affect ailments applied by Black Hole "
             "(ruleset excludes node 27 from Ignite)"},
        ]
    },
    "28": {  # Singularity (1 pt)
        "effects": [
            {"key": "more", "stat_name": "Damage", "catalog_stat_name": "Damage",
             "stat_key": model.STAT_DAMAGE, "operation": "more", "unit": "percent",
             "damage_tags": [], "expected_total": 1.0,
             "notes": "more damage group node:bh2:28; body and Ignite"},
            {"key": "flag-one-at-a-time", "kind": "flag", "stat_name": "Black Holes Per Cast",
             "catalog_stat_name": "Black Holes Per Cast", "exclusion_reason":
             "structural limit (1 Black Hole at a time / per cast); ruleset structural.singularity"},
        ]
    },
    "30": {  # Pocket Dimension (1 pt)
        "effects": [
            {"key": "charges", "stat_name": "Additional Charges",
             "catalog_stat_name": "Additional Charges", "stat_key": model.STAT_CHARGES,
             "operation": "added", "unit": "stack", "expected_total": 1.0,
             "notes": "extra max charge; ruleset.cooldown.extra_max_charges also carries it"},
        ]
    },
}


# ---------------------------------------------------------------------------
# Special calculator-export mod handlers (context-dependent rows)
# ---------------------------------------------------------------------------


def _handle_glyph_ward(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder.add(
        _contribution(
            f"mod:{mod['mod_id']}",
            **builder._source_kwargs(mod),
            stat_name=(mod.get("stat") or {}).get("name") or "Ward per Second",
            stat_key=model.STAT_UNSUPPORTED,
            operation="flag",
            value=float(mod.get("value_numeric") or 0.0),
            unit="flat",
            condition_code="standing_on_glyph_of_dominion",
            condition_status=model.CONDITION_NOT_SATISFIED,
            status=model.STATUS_CONFIRMED,
            evidence=builder._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        "no Glyph of Dominion source in the build/catalog scope -> the "
                        "'while standing on your Glyph of Dominion' condition is not satisfied",
                        locator="le_calc.stats#_handle_glyph_ward",
                    )
                ],
            ),
            notes="Apex of Thought 'while standing on your Glyph of Dominion' line: condition "
            "not satisfied (execution plan §2.3); value is ward generation which is also "
            "outside the v1 damage model",
            raw=builder._raw_mod(mod),
        )
    )


def _handle_glyph_conversion(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder.add(
        _contribution(
            f"mod:{mod['mod_id']}",
            **builder._source_kwargs(mod),
            stat_name=(mod.get("stat") or {}).get("name") or "Ailment chance conversion",
            stat_key=model.STAT_UNSUPPORTED,
            operation="flag",
            value=float(mod.get("value_numeric") or 0.0),
            unit="percent",
            condition_code="standing_on_glyph_of_dominion",
            condition_status=model.CONDITION_NOT_SATISFIED,
            status=model.STATUS_CONFIRMED,
            evidence=builder._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        "no Glyph of Dominion source in the build/catalog scope -> conversion "
                        "line is inactive; even if active it converts ailment chances to "
                        "resistance shred, which is not part of the v1 damage model",
                        locator="le_calc.stats#_handle_glyph_conversion",
                    )
                ],
            ),
            notes="Apex of Thought conversion line; original tooltip text kept in evidence snippet",
            raw=builder._raw_mod(mod),
        )
    )


def _handle_glove_cdr(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder._defer_glove(mod)


def _handle_glove_cast_speed(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder._defer_glove(mod)


def _handle_crescendoing_rage(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    buff = builder.catalog.buff_by_entity_id("buff:letools:UIBuffs:248")
    description = ((buff or {}).get("extensions") or {}).get("description") or ""
    more_value = 0.5
    builder.add(
        _contribution(
            f"mod:{mod['mod_id']}",
            **builder._source_kwargs(mod),
            stat_name="You have Crescendoing Rage",
            stat_key=model.STAT_DAMAGE,
            operation="more",
            value=more_value,
            unit="percent",
            condition_code="crescendoing_rage_third_cast",
            condition_status=model.CONDITION_UNKNOWN,
            status=model.STATUS_CONFIRMED,
            evidence=builder._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        "catalog buff UIBuffs:248 'Crescendoing Rage': "
                        f"{description!r}",
                        locator="data/releases/phase1-dataset-003/data.sqlite#buffs "
                        "buff:letools:UIBuffs:248",
                    ),
                    _ev(
                        model.EVIDENCE_CATALOG,
                        "affix 1124 entity extensions: specialTag=12, tags=950, property=58",
                        locator="data/releases/phase1-dataset-003/data.sqlite#entities "
                        "affix:letools:affixList.singleAffixes:1124",
                    ),
                ],
            ),
            notes="minimal rule from catalog buff text: every third ability cast deals 50% more "
            "damage. Static snapshot marks the cast-ordinal condition unknown; the engine / "
            "ruleset must decide per scenario (single baseline cast = not the third cast).",
            raw=builder._raw_mod(mod),
        )
    )


def _handle_idol_altar_cdr(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder.add(
        _contribution(
            f"mod:{mod['mod_id']}",
            **builder._source_kwargs(mod),
            stat_name=(mod.get("stat") or {}).get("name") or "Increased Cooldown Recovery Speed",
            stat_key=model.STAT_UNSUPPORTED,
            operation="flag",
            value=float(mod.get("value_numeric") or 0.0),
            unit="percent",
            condition_code="no_larger_idol_above_smaller_idol",
            condition_status=model.CONDITION_UNKNOWN,
            included=False,
            exclusion_reason="idol grid condition cannot be evaluated: catalog/build keep only "
            "idol x/y and size names (phase-2 grid_validation=not_verified); no width/height or "
            "grid-orientation rule is available",
            status=model.STATUS_UNRESOLVED,
            evidence=builder._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        "idol layout from calculator export: "
                        f"{json.dumps(_idol_layout(builder), sort_keys=True)}",
                        locator="archive://calculation-input.json#items",
                    ),
                    _ev(
                        model.EVIDENCE_DERIVATION,
                        "cannot decide 'no larger idols above smaller ones' without idol "
                        "width/height and the newGrid orientation rule; the 10% CDR is NOT applied",
                        locator="le_calc.stats#_handle_idol_altar_cdr",
                    ),
                ],
            ),
            notes="Pyramidal Altar implicit; league-1 CDR contribution intentionally withheld "
            "pending idol grid evidence (禁止猜测)",
            raw=builder._raw_mod(mod),
        )
    )


def _handle_doubled_fire(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder._defer_doubled_fire(mod)


def _handle_ambition_stack_chance(builder: _SnapshotBuilder, mod: dict[str, Any]) -> None:
    builder.add(
        _contribution(
            f"mod:{mod['mod_id']}",
            **builder._source_kwargs(mod),
            stat_name=(mod.get("stat") or {}).get("name")
            or "Chance to gain a stack of Ambition when you hit a boss or rare enemy",
            stat_key=model.STAT_UNSUPPORTED,
            operation="flag",
            value=float(mod.get("value_numeric") or 0.0),
            unit="percent",
            condition_code="ambition_stack_from_boss_hit",
            condition_status=model.CONDITION_NOT_SATISFIED,
            status=model.STATUS_CONFIRMED,
            evidence=builder._mod_evidence(
                mod,
                [
                    _ev(
                        model.EVIDENCE_CATALOG,
                        "Throne of Ambition tooltip: 'You gain a stack of Ambition when you hit "
                        "a boss or rare enemy (1 second cooldown)'; '2% more Fire Damage per "
                        "stack of Ambition'; '20 Maximum Stacks'",
                        locator="reports/phase-3/black-hole-planning-evidence-2026-11-03.json"
                        "#translation_evidence.Item_Names.Unique_Tooltip_0_211",
                    )
                ],
            ),
            notes="Black Hole body hit=false (execution plan A1); no qualified hit event in scope "
            "-> Ambition stays at 0 stacks and its per-stack more damage is not applied",
            raw=builder._raw_mod(mod),
        )
    )


_SPECIAL_MOD_HANDLERS = {
    "equipment.head#unique_mod#0": _handle_glyph_ward,
    "equipment.head#unique_mod#2": _handle_glyph_conversion,
    "equipment.hands#affix#sealed#0.0": _handle_glove_cdr,
    "equipment.hands#affix#primordial#0.0": _handle_glove_cast_speed,
    "equipment.ring2#affix#sealed#0.0": _handle_crescendoing_rage,
    "equipment.idol_altar#implicit#0": _handle_idol_altar_cdr,
    "idols.2#affix#normal#1.0": _handle_doubled_fire,
    "idols.3#affix#normal#1.0": _handle_doubled_fire,
    "idols.4#unique_mod#0": _handle_ambition_stack_chance,
}


def _idol_layout(builder: _SnapshotBuilder) -> list[dict[str, Any]]:
    layout: list[dict[str, Any]] = []
    for item in builder.bundle.items:
        instance_id = str(item.get("instance_id"))
        if not instance_id.startswith("idols."):
            continue
        position = item.get("position") or {}
        base_entity_id = item.get("base_entity_id")
        layout.append(
            {
                "instance_id": instance_id,
                "display_name": item.get("display_name"),
                "base_entity_id": base_entity_id,
                "idol_size": builder.catalog.idol_size_for_item(str(base_entity_id))
                if base_entity_id
                else None,
                "x": position.get("x"),
                "y": position.get("y"),
                "source_width": position.get("source_width"),
                "source_height": position.get("source_height"),
                "grid_validation": "not_verified",
            }
        )
    layout.sort(key=lambda row: int(str(row["instance_id"]).split(".", 1)[1]))
    return layout


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def compute_attributes(contributions: Iterable[model.Contribution]) -> dict[str, float]:
    """Sum attribute contributions (base + all-attributes + single attribute)."""
    attributes = {key: 0.0 for key in model.ATTRIBUTE_ORDER}
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.operation not in ("added",):
            continue
        if contribution.stat_key == model.STAT_ATTRIBUTE:
            key = (contribution.target or "").lower()
            if key in attributes:
                attributes[key] += float(contribution.value)
        elif contribution.stat_key == model.STAT_ALL_ATTRIBUTES:
            for key in attributes:
                attributes[key] += float(contribution.value)
    return {key: _round(value) for key, value in attributes.items()}


def compute_resistances(contributions: Iterable[model.Contribution]) -> dict[str, dict[str, Any]]:
    """Aggregate resistance contributions into applied totals in percentage points."""
    pools = {name: 0.0 for name in model.RESISTANCE_NAMES}
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.stat_key != model.STAT_RESISTANCE:
            continue
        if contribution.operation != "added":
            continue
        target = (contribution.target or "").lower()
        if target not in pools:
            continue
        pools[target] += float(contribution.value)

    def entry(total_fraction: float) -> dict[str, Any]:
        total = _round(total_fraction * 100.0)
        capped = _round(min(75.0, total))
        return {
            "total": total,
            "cap": 75.0,
            "capped": capped,
            "overcapped": _round(max(0.0, total - 75.0)),
        }

    resistances: dict[str, dict[str, Any]] = {}
    for name in ("fire", "cold", "lightning", "physical", "void", "necrotic", "poison"):
        total_fraction = pools[name]
        if name in ("fire", "cold", "lightning"):
            total_fraction += pools["elemental"] + pools["all"]
        else:
            total_fraction += pools["all"]
        row = entry(total_fraction)
        row["breakdown"] = {
            "specific": _round(pools[name] * 100.0),
            "elemental": _round(pools["elemental"] * 100.0),
            "all": _round(pools["all"] * 100.0),
        }
        resistances[name] = row
    # raw source pools (not double-applied); cap fields kept for shape consistency
    for pool_name in ("elemental", "all"):
        row = entry(pools[pool_name])
        row["breakdown"] = {"specific": _round(pools[pool_name] * 100.0)}
        row["note"] = "source pool only (not an applied resistance)"
        resistances[pool_name] = row
    return resistances


def compute_mana(contributions: Iterable[model.Contribution]) -> dict[str, Any]:
    base = 0.0
    flat = 0.0
    increased = 0.0
    rows: list[dict[str, Any]] = []
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.stat_key != model.STAT_MANA:
            continue
        if contribution.operation == "added":
            if contribution.source_kind == "base_character":
                base += float(contribution.value)
            else:
                flat += float(contribution.value)
            rows.append(
                {
                    "contribution_id": contribution.contribution_id,
                    "value": float(contribution.value),
                    "operation": "added",
                    "source_kind": contribution.source_kind,
                }
            )
        elif contribution.operation == "increased":
            increased += float(contribution.value)
            rows.append(
                {
                    "contribution_id": contribution.contribution_id,
                    "value": float(contribution.value),
                    "operation": "increased",
                    "source_kind": contribution.source_kind,
                }
            )
    total = (base + flat) * (1.0 + increased)
    return {
        "base_flat": _round(base),
        "flat_total": _round(flat),
        "increased_total": _round(increased),
        "total": _round(total),
        "rows": rows,
    }


def _sum_by(
    contributions: Iterable[model.Contribution],
    *,
    stat_key: str,
    operations: tuple[str, ...] = ("increased", "added"),
    tag_subset: Iterable[str] = (),
    target: str | None = None,
    source_kinds: Iterable[str] | None = None,
    include_skill_nodes: bool = False,
) -> tuple[float, list[model.Contribution]]:
    wanted_tags = {tag for tag in tag_subset}
    wanted_kinds = set(source_kinds) if source_kinds is not None else None
    total = 0.0
    rows: list[model.Contribution] = []
    for contribution in contributions:
        if not contribution.active:
            continue
        if contribution.stat_key != stat_key:
            continue
        if contribution.operation not in operations:
            continue
        if not include_skill_nodes and contribution.source_kind == "skill_node":
            continue
        if wanted_kinds is not None and contribution.source_kind not in wanted_kinds:
            continue
        if target is not None and (contribution.target or "").lower() != target.lower():
            continue
        if wanted_tags and not wanted_tags.intersection(contribution.damage_tags):
            continue
        total += float(contribution.value)
        rows.append(contribution)
    return total, rows


def build_derived_summary(
    builder: _SnapshotBuilder,
    *,
    contributions: list[model.Contribution],
    attributes: dict[str, float],
    resistances: dict[str, dict[str, Any]],
    mana: dict[str, Any],
) -> dict[str, Any]:
    fire = resistances["fire"]
    cold = resistances["cold"]
    cast_speed_total, cast_rows = _sum_by(
        contributions, stat_key=model.STAT_CAST_SPEED, operations=("increased", "added")
    )
    cdr_total, cdr_rows = _sum_by(
        contributions, stat_key=model.STAT_COOLDOWN_RECOVERY, operations=("increased", "added")
    )
    cd_reduction_total, cd_reduction_rows = _sum_by(
        contributions,
        stat_key=model.STAT_COOLDOWN_REDUCTION,
        operations=("increased", "added"),
        include_skill_nodes=True,
    )
    duration_total, duration_rows = _sum_by(
        contributions,
        stat_key=model.STAT_SKILL_DURATION_INCREASED,
        operations=("increased", "added"),
        include_skill_nodes=True,
    )
    ignite_duration_total, ignite_duration_rows = _sum_by(
        contributions,
        stat_key=model.STAT_AILMENT_DURATION,
        operations=("increased", "added"),
        target="ignite",
    )
    ignite_effect_total, ignite_effect_rows = _sum_by(
        contributions,
        stat_key=model.STAT_AILMENT_EFFECT,
        operations=("increased", "added"),
        target="ignite",
    )
    added_spell_total, added_spell_rows = _sum_by(
        contributions,
        stat_key=model.STAT_ADDED_DAMAGE,
        operations=("added",),
        tag_subset=["Spell"],
    )
    fire_pen_total, fire_pen_rows = _sum_by(
        contributions,
        stat_key=model.STAT_PENETRATION,
        operations=("added", "increased"),
        tag_subset=["Fire"],
        include_skill_nodes=True,
    )
    pen_with_ignite_total, pen_with_ignite_rows = _sum_by(
        contributions,
        stat_key=model.STAT_PENETRATION_WITH_IGNITE,
        operations=("added", "increased"),
        target="ignite",
    )
    cooldown_flat_total, cooldown_flat_rows = _sum_by(
        contributions,
        stat_key=model.STAT_COOLDOWN_FLAT,
        operations=("added",),
        include_skill_nodes=True,
    )
    charges_total, charges_rows = _sum_by(
        contributions,
        stat_key=model.STAT_CHARGES,
        operations=("added",),
        include_skill_nodes=True,
    )
    ailment_chance_total, ailment_chance_rows = _sum_by(
        contributions,
        stat_key=model.STAT_AILMENT_CHANCE,
        operations=("added",),
        target="ignite",
    )
    heat_wave_points = 0
    for contribution in contributions:
        if contribution.stat_key == model.STAT_AILMENT_CHANCE and contribution.operation == "flag":
            if (contribution.target or "").lower() == "ignite" and contribution.source_kind == "skill_node":
                heat_wave_points = int(round(float(contribution.value)))
    heat_wave_stacks_per_roll = 0.02 * heat_wave_points * float(fire["total"])
    return {
        # report/fixture-compatible scalar names
        "mana_max": mana["total"],
        "cast_time_s": _round(
            1.0 / (BH2_ANIMATION_SPEED * BH2_SPEED_MULTIPLIER * (1.0 + cast_speed_total))
        )
        if (1.0 + cast_speed_total)
        else None,
        "cooldown_recovery_speed_increased": _round(cdr_total),
        "skill_duration_s": _round(2.75 * (1.0 + duration_total)),
        "ignite_duration_s": _round(2.5 * (1.0 + ignite_duration_total)),
        "ignite_chance_per_second_percent": _round(heat_wave_stacks_per_roll * 100.0),
        # explicit phase-3 summary required by the input-ledger task
        "uncapped_fire_resistance_percent": float(fire["total"]),
        "overcapped_fire_resistance_percent": float(fire["overcapped"]),
        "overcapped_cold_resistance_percent": float(cold["overcapped"]),
        "mana_max_sources": mana,
        "increased_cast_speed_total": _round(cast_speed_total),
        "increased_cast_speed_rows": [c.contribution_id for c in cast_rows],
        "increased_cooldown_recovery_speed_total": _round(cdr_total),
        "increased_cooldown_recovery_speed_rows": [c.contribution_id for c in cdr_rows],
        "cooldown_reduction_total": _round(cd_reduction_total),
        "cooldown_reduction_rows": [c.contribution_id for c in cd_reduction_rows],
        "increased_bh_duration_total": _round(duration_total),
        "increased_bh_duration_rows": [c.contribution_id for c in duration_rows],
        "increased_ignite_duration_total": _round(ignite_duration_total),
        "increased_ignite_duration_rows": [c.contribution_id for c in ignite_duration_rows],
        "increased_ignite_effect_total": _round(ignite_effect_total),
        "increased_ignite_effect_rows": [c.contribution_id for c in ignite_effect_rows],
        "flat_added_spell_damage_total": _round(added_spell_total),
        "flat_added_spell_damage_rows": [c.contribution_id for c in added_spell_rows],
        "fire_penetration_total": _round(fire_pen_total),
        "fire_penetration_rows": [c.contribution_id for c in fire_pen_rows],
        "penetration_with_ignite_total": _round(pen_with_ignite_total),
        "penetration_with_ignite_rows": [c.contribution_id for c in pen_with_ignite_rows],
        "cooldown_flat_total_s": _round(cooldown_flat_total),
        "extra_max_charges": _round(charges_total),
        "ailment_chance_ignite_total": _round(ailment_chance_total),
        "heat_wave_points": heat_wave_points,
        "heat_wave_expected_stacks_per_roll": _round(heat_wave_stacks_per_roll),
        "intelligence": float(attributes["intelligence"]),
        "attunement": float(attributes["attunement"]),
        "strength": float(attributes["strength"]),
        "dexterity": float(attributes["dexterity"]),
        "vitality": float(attributes["vitality"]),
        "intelligence_increased_damage_fraction": _round(0.04 * float(attributes["intelligence"])),
        "crescendoing_rage_more_damage": 0.5,
        "idol_layout": _idol_layout(builder),
        "attribute_per_point_bonuses": ATTRIBUTE_PER_POINT_EFFECTS,
        "assumptions": [
            "resistances total/cap/capped/overcapped are percentage-point numbers (75.0 = 75%)",
            "base resistance 0; no area-level resistance penalty in v1",
            "mana(level 100) = baseMana + manaPerLevel*(level-1)",
            "per-point passive scaling: planner.passiveBonuses value × allocated points; "
            "noScaling entries apply once at >= noScalingPointThreshold",
            "gloves overcap-cold derivation uses a continuous ratio; integer-chunk rounding is "
            "an unresolved assumption",
            "skill-node rows are cross-check artifacts: the engine consumes the ruleset, not "
            "these contributions; values must agree with it",
        ],
    }


# ---------------------------------------------------------------------------
# build_snapshot
# ---------------------------------------------------------------------------


def build_snapshot(bundle: InputBundle | None = None) -> model.Snapshot:
    """Build the resolved static snapshot for the frozen inputs.

    The no-argument call loads the default frozen inputs via ``load_bundle()``;
    ``le_calc.cli`` calls ``build_snapshot(load_bundle())``.
    """
    if bundle is None:
        bundle = load_bundle()
    catalog = Catalog(bundle.database_path, verify_file_sha256=False)
    try:
        builder = _SnapshotBuilder(bundle, catalog)

        builder.build_base_character()
        builder.build_equipment()
        builder.build_passive_nodes()
        builder.build_skill_nodes()

        attributes_stage1 = compute_attributes(builder.contributions)
        builder.add_derived_attribute_contributions(attributes_stage1)
        attributes = compute_attributes(builder.contributions)
        if attributes != attributes_stage1:
            raise SnapshotError(
                "attributes changed after derived attribute contributions (circular dependency)"
            )

        resistances = compute_resistances(builder.contributions)
        mana = compute_mana(builder.contributions)

        builder.resolve_doubled_fire(mana["total"])
        builder.resolve_glove_derivations(resistances["cold"]["overcapped"])

        # the deferred rows only add damage / cast-speed / CDR rows; recompute the
        # dependent aggregates so the snapshot is internally consistent
        attributes = compute_attributes(builder.contributions)
        resistances = compute_resistances(builder.contributions)
        mana = compute_mana(builder.contributions)

        contributions = sorted(builder.contributions, key=model.contribution_sort_key)
        derived = build_derived_summary(
            builder,
            contributions=contributions,
            attributes=attributes,
            resistances=resistances,
            mana=mana,
        )
        issues = builder.build_issues(mana=mana, resistances=resistances)
        excluded_sources = builder.build_excluded_sources()

        # coverage guard: every input mod must be represented in the ledger
        covered_mods = {
            c.contribution_id[len("mod:"):].split(":")[0]
            if c.contribution_id.startswith("mod:")
            else None
            for c in contributions
        }
        covered_mods.discard(None)
        missing_mods = [
            mod["mod_id"] for mod in bundle.mods if mod["mod_id"] not in covered_mods
        ]
        if missing_mods:
            raise SnapshotError(
                f"{len(missing_mods)} calculator mod(s) have no ledger row, e.g. {missing_mods[:5]}"
            )

        snapshot = model.Snapshot(
            build_meta=bundle.build_meta(),
            character=builder.character(),
            attributes=attributes,
            resistances=resistances,
            contributions=contributions,
            issues=issues,
            derived=derived,
            excluded_sources=excluded_sources,
        )
        return snapshot
    finally:
        catalog.close()


def snapshot_json(snapshot: model.Snapshot) -> str:
    """Deterministic JSON text: sorted keys, UTF-8, trailing newline."""
    return json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dump_snapshot(snapshot: model.Snapshot, path: str | Path) -> Path:
    """Write the deterministic snapshot JSON (UTF-8, LF, sorted keys)."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(snapshot_json(snapshot).encode("utf-8"))
    return target


# ---------------------------------------------------------------------------
# Self-check CLI (``if __name__ == "__main__"``; le_calc.cli.py is Lead-owned)
# ---------------------------------------------------------------------------


def _summary_lines(snapshot: model.Snapshot) -> list[str]:
    derived = snapshot.derived
    included = [c for c in snapshot.contributions if c.included]
    active = [c for c in snapshot.contributions if c.active]
    excluded = [c for c in snapshot.contributions if not c.included]
    lines = [
        f"build: {snapshot.build_meta.get('build', {}).get('build_id')} "
        f"revision={snapshot.build_meta.get('build', {}).get('revision_id')}",
        f"character: level {snapshot.character.get('level')} "
        f"{snapshot.character.get('class')}/{snapshot.character.get('mastery')}",
        f"contributions: {len(snapshot.contributions)} total, {len(included)} included, "
        f"{len(active)} active, {len(excluded)} excluded",
        f"attributes: " + ", ".join(f"{k}={snapshot.attributes.get(k)}" for k in model.ATTRIBUTE_ORDER),
        f"fire resistance (uncapped): {derived.get('uncapped_fire_resistance_percent')}% "
        f"(overcapped {derived.get('overcapped_fire_resistance_percent')}%)",
        f"cold overcapped: {derived.get('overcapped_cold_resistance_percent')}%",
        f"mana_max: {derived.get('mana_max')}",
        f"intelligence: {derived.get('intelligence')}",
        f"increased cast speed: {derived.get('increased_cast_speed_total')}",
        f"increased cooldown recovery speed: "
        f"{derived.get('increased_cooldown_recovery_speed_total')}",
        f"cooldown reduction (skill nodes): {derived.get('cooldown_reduction_total')}",
        f"BH duration increased: {derived.get('increased_bh_duration_total')} "
        f"({derived.get('skill_duration_s')}s)",
        f"ignite duration increased: {derived.get('increased_ignite_duration_total')} "
        f"({derived.get('ignite_duration_s')}s)",
        f"ignite effect increased: {derived.get('increased_ignite_effect_total')}",
        f"added spell damage: {derived.get('flat_added_spell_damage_total')}",
        f"fire penetration: {derived.get('fire_penetration_total')}",
        f"heat wave: {derived.get('heat_wave_points')} points, "
        f"{derived.get('ignite_chance_per_second_percent')}% chance/s "
        f"({derived.get('heat_wave_expected_stacks_per_roll')} stacks/roll)",
        f"issues: {len(snapshot.issues)}",
    ]
    histogram: dict[str, int] = {}
    for contribution in excluded:
        key = (contribution.exclusion_reason or "unknown").split(";")[0][:72]
        histogram[key] = histogram.get(key, 0) + 1
    lines.append("exclusion reasons:")
    for reason, count in sorted(histogram.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"  {count:3d}  {reason}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m le_calc.stats",
        description="Build the phase-3 Black Hole input snapshot (self-check entry point).",
    )
    parser.add_argument(
        "--dump",
        metavar="PATH",
        default=None,
        help="write snapshot JSON to PATH (default: print only the summary)",
    )
    parser.add_argument(
        "--no-verify-database",
        action="store_true",
        help="skip the 263 MB data.sqlite sha256 verification (faster)",
    )
    args = parser.parse_args(argv)

    bundle = load_bundle(verify_database_file=not args.no_verify_database)
    snapshot = build_snapshot(bundle)
    text = snapshot_json(snapshot)
    if args.dump:
        target = Path(args.dump)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode("utf-8"))
        print(f"snapshot written: {target} ({len(text.encode('utf-8'))} bytes)")
    for line in _summary_lines(snapshot):
        print(line)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())


__all__ = [
    "ATTRIBUTE_PER_POINT_EFFECTS",
    "ATTRIBUTES_URL",
    "DEFAULT_DERIVED_DUMP",
    "EXCLUDED_STAT_REASONS",
    "RESISTANCE_BY_STAT_NAME",
    "SPECIAL_TAG_AILMENTS",
    "SnapshotError",
    "_contribution",
    "build_derived_summary",
    "build_snapshot",
    "compute_attributes",
    "compute_mana",
    "compute_resistances",
    "dump_snapshot",
    "main",
    "snapshot_json",
]
