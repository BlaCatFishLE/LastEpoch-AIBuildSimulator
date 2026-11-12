"""Shared data contracts for the phase-3 Black Hole calculator (v1).

Owner: Lead.  Consumers: inputs/stats/ledger (input-ledger), damage/cooldown/
simulate/report/cli (Lead), html (html-report).  Changes require Lead sign-off.

Design notes
------------
* One `Contribution` is one *effect line* from one source instance.  A single
  item affix that affects several stats is split into several contributions;
  a single stat line that applies to several damage tags stays one contribution
  with several `damage_tags` (it must be counted once per damage component).
* Conditions are resolved by the input adapter (source of truth for "which of
  the build's sources are active in this scoped scenario").  The damage engine
  only consumes contributions with `included=True` and
  `condition_status in {"always", "satisfied"}`.
* `stat_key` normalises game stat names into the small vocabulary below.  Any
  stat that cannot be interpreted within this phase maps to `unsupported`
  (kept for the ledger, never silently dropped).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Status / evidence vocabulary
# ---------------------------------------------------------------------------

STATUS_CONFIRMED = "confirmed"      # direct game data / user-confirmed rule
STATUS_DERIVED = "derived"          # documented arithmetic over confirmed inputs
STATUS_ASSUMED = "assumed"          # explicit working assumption (review marker)
STATUS_UNRESOLVED = "unresolved"    # unknown, must not be silently defaulted
VALID_STATUS = (STATUS_CONFIRMED, STATUS_DERIVED, STATUS_ASSUMED, STATUS_UNRESOLVED)

EVIDENCE_GAME_DATA = "game_data"                # local 1.5.3 assets or archived 1.5.2 data
EVIDENCE_USER_RULE = "user_rule"                # rule confirmed by the user 2026-11-04
EVIDENCE_CATALOG = "catalog"                    # phase-1 normalised dataset
EVIDENCE_THIRD_PARTY = "third_party_reference"  # LE Tools planner runtime (reference only)
EVIDENCE_PUBLIC_DOC = "public_doc"              # official support / community documentation
EVIDENCE_DERIVATION = "derivation"              # computed inside this project

CONDITION_ALWAYS = "always"
CONDITION_SATISFIED = "satisfied"
CONDITION_NOT_SATISFIED = "not_satisfied"
CONDITION_UNKNOWN = "unknown"
ACTIVE_CONDITIONS = (CONDITION_ALWAYS, CONDITION_SATISFIED)

# ---------------------------------------------------------------------------
# Attribute order of the LE character-class data (confirmed by official support
# article + catalog coreDB.coreAttributes rows)
# ---------------------------------------------------------------------------

#: core attribute index -> canonical key (game order: 0 Str, 1 Vit, 2 Int, 3 Dex, 4 Att)
ATTRIBUTE_ORDER = ("strength", "vitality", "intelligence", "dexterity", "attunement")

#: LE Tools AT bitmask -> tag name (from phase-1 `tags` table, enum AT)
AT_BITS: dict[str, int] = {
    "Physical": 1,
    "Lightning": 2,
    "Cold": 4,
    "Fire": 8,
    "Void": 16,
    "Necrotic": 32,
    "Poison": 64,
    "Elemental": 128,
    "Spell": 256,
    "Melee": 512,
    "Throwing": 1024,
    "Bow": 2048,
    "DoT": 4096,
    "Minion": 8192,
    "Totem": 16384,
    "PetResisted": 32768,
    "Potion": 65536,
    "Buff": 131072,
    "Channelling": 262144,
    "Transform": 524288,
    "LowLife": 1048576,
    "HighLife": 2097152,
    "FullLife": 4194304,
    "Hit": 8388608,
    "Curse": 16777216,
    "Ailment": 33554432,
    "Crit_deprecated": 67108864,
    "Kill_deprecated": 134217728,
    "Die_deprecated": 268435456,
}

#: element tags that map onto "Elemental"
ELEMENT_TAGS = frozenset({"Fire", "Cold", "Lightning"})


def decode_tags(bitmask: Optional[int]) -> list[str]:
    """Decode an LE Tools AT bitmask into a stable, sorted tag-name list."""
    if not bitmask:
        return []
    return sorted(name for name, bit in AT_BITS.items() if bitmask & bit)


# ---------------------------------------------------------------------------
# Normalised stat vocabulary
# ---------------------------------------------------------------------------

#: damage-related stats (used by the damage engine)
STAT_DAMAGE = "damage"                     # increased/more damage (scoped by damage_tags)
STAT_ADDED_DAMAGE = "added_damage"         # flat added damage (scoped by damage_tags)
STAT_PENETRATION = "penetration"           # resistance penetration (scoped by damage_tags)
STAT_PENETRATION_WITH_IGNITE = "penetration_with_ignite"  # fire penetration that applies to Ignite
STAT_AILMENT_DURATION = "ailment_duration"  # increased ailment duration (scoped by ailment tag)
STAT_AILMENT_EFFECT = "ailment_effect"     # increased ailment effect (legacy; phase1<=002 datasets)
STAT_AILMENT_CHANCE = "ailment_chance"     # added/increased ailment chance
STAT_CAST_SPEED = "cast_speed"
# --- hit model additions (fr-engine, Flame Reave stage 1) -------------------
# These keys only widen the vocabulary; the Black Hole pipeline never emits
# them, so existing semantics are unchanged.
STAT_ATTACK_SPEED = "attack_speed"          # attack speed (increased/more), scoped by damage_tags
STAT_CRIT_CHANCE = "crit_chance"            # added/increased/more critical strike chance
STAT_CRIT_MULTIPLIER = "crit_multiplier"    # added/increased/more critical strike multiplier
STAT_HIT_DAMAGE = "hit_damage"              # generic hit damage (reserved; not consumed in v1)
STAT_MANA_COST = "mana_cost"                # skill mana cost (flat/increased), target = ability id
STAT_MANA_EFFICIENCY = "mana_efficiency"    # increased mana efficiency (reserved)
STAT_COOLDOWN_RECOVERY = "cooldown_recovery_speed"
STAT_COOLDOWN_REDUCTION = "cooldown_reduction"   # literal "% reduced cooldown" (skill nodes)
STAT_COOLDOWN_FLAT = "cooldown_flat"             # flat seconds added to cooldown (unit=seconds)
STAT_CHARGES = "charges"                         # extra max charges (unit=stack)
STAT_SKILL_DURATION_INCREASED = "skill_duration"  # increased skill duration (Black Hole duration)
STAT_MANA = "mana"
STAT_MANA_REGEN = "mana_regen"
STAT_ALL_ATTRIBUTES = "all_attributes"
STAT_ATTRIBUTE = "attribute"               # single attribute; see `attribute` field
STAT_RESISTANCE = "resistance"             # single resistance; see `attribute`-like target
STAT_UNSUPPORTED = "unsupported"

#: `target` values for resistance contributions (stat=resistance) / attribute contributions
RESISTANCE_NAMES = (
    "fire", "cold", "lightning", "physical", "void", "necrotic", "poison", "elemental", "all",
)

SOURCE_KINDS = (
    "equipment", "idol", "blessing", "idol_altar", "passive_node", "skill_node",
    "base_character", "derived", "system",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    kind: str
    detail: str
    locator: Optional[str] = None
    snippet: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "detail": self.detail}
        if self.locator:
            out["locator"] = self.locator
        if self.snippet:
            out["snippet"] = self.snippet
        return out


@dataclass
class Contribution:
    """One effect line of one source instance, normalised and condition-resolved."""

    contribution_id: str
    source_kind: str
    source_label: str
    source_path: str
    stat_name: str
    stat_key: str
    operation: str
    value: float
    unit: str = "flat"
    damage_tags: list[str] = field(default_factory=list)
    target: Optional[str] = None          # resistance name / attribute key / ailment name
    condition_code: Optional[str] = None
    condition_status: str = CONDITION_ALWAYS
    group: Optional[str] = None           # more-group identifier (e.g. "node:tidal_force")
    included: bool = True
    exclusion_reason: Optional[str] = None
    value_source: str = "source"
    status: str = STATUS_CONFIRMED
    evidence: list[Evidence] = field(default_factory=list)
    notes: Optional[str] = None
    raw: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.status not in VALID_STATUS:
            raise ValueError(f"invalid status {self.status!r} for {self.contribution_id}")
        if self.operation not in ("added", "increased", "more", "base", "flag"):
            raise ValueError(f"invalid operation {self.operation!r} for {self.contribution_id}")

    @property
    def active(self) -> bool:
        return self.included and self.condition_status in ACTIVE_CONDITIONS

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Contribution":
        evidence = [
            Evidence(
                kind=e.get("kind", "derivation"),
                detail=e.get("detail", ""),
                locator=e.get("locator"),
                snippet=e.get("snippet"),
            )
            for e in (data.get("evidence") or [])
        ]
        return cls(
            contribution_id=data["contribution_id"],
            source_kind=data.get("source_kind", "system"),
            source_label=data.get("source_label", ""),
            source_path=data.get("source_path", ""),
            stat_name=data.get("stat_name", ""),
            stat_key=data.get("stat_key", STAT_UNSUPPORTED),
            operation=data.get("operation", "flag"),
            value=float(data.get("value") or 0.0),
            unit=data.get("unit", "flat"),
            damage_tags=list(data.get("damage_tags") or []),
            target=data.get("target"),
            condition_code=data.get("condition_code"),
            condition_status=data.get("condition_status", CONDITION_ALWAYS),
            group=data.get("group"),
            included=bool(data.get("included", True)),
            exclusion_reason=data.get("exclusion_reason"),
            value_source=data.get("value_source", "source"),
            status=data.get("status", STATUS_CONFIRMED),
            evidence=evidence,
            notes=data.get("notes"),
            raw=data.get("raw"),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "contribution_id": self.contribution_id,
            "source_kind": self.source_kind,
            "source_label": self.source_label,
            "source_path": self.source_path,
            "stat_name": self.stat_name,
            "stat_key": self.stat_key,
            "operation": self.operation,
            "value": self.value,
            "unit": self.unit,
            "damage_tags": list(self.damage_tags),
            "target": self.target,
            "condition_code": self.condition_code,
            "condition_status": self.condition_status,
            "group": self.group,
            "included": self.included,
            "exclusion_reason": self.exclusion_reason,
            "value_source": self.value_source,
            "status": self.status,
            "evidence": [e.to_dict() for e in self.evidence],
            "notes": self.notes,
        }
        if self.raw is not None:
            out["raw"] = self.raw
        return out


@dataclass
class Snapshot:
    """Resolved static character snapshot for the scoped scenario."""

    build_meta: dict[str, Any]
    character: dict[str, Any]
    attributes: dict[str, float]
    resistances: dict[str, dict[str, Any]]
    contributions: list[Contribution]
    issues: list[dict[str, Any]] = field(default_factory=list)
    derived: dict[str, Any] = field(default_factory=dict)
    excluded_sources: list[dict[str, Any]] = field(default_factory=list)

    # -- helpers used by the damage engine ---------------------------------
    def active_contributions(self) -> list[Contribution]:
        return [c for c in self.contributions if c.active]

    def of(self, stat_key: str) -> list[Contribution]:
        return [c for c in self.active_contributions() if c.stat_key == stat_key]

    def attribute(self, key: str) -> float:
        return float(self.attributes.get(key, 0.0))

    def resistance(self, key: str) -> dict[str, Any]:
        return dict(self.resistances.get(key, {}))

    def uncapped_resistance(self, key: str) -> float:
        return float(self.resistances.get(key, {}).get("total", 0.0))

    def to_dict(self) -> dict[str, Any]:
        return {
            "build_meta": self.build_meta,
            "character": self.character,
            "attributes": self.attributes,
            "resistances": self.resistances,
            "derived": self.derived,
            "issues": self.issues,
            "excluded_sources": self.excluded_sources,
            "contributions": [c.to_dict() for c in self.contributions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Snapshot":
        return cls(
            build_meta=dict(data.get("build_meta") or {}),
            character=dict(data.get("character") or {}),
            attributes={k: float(v) for k, v in (data.get("attributes") or {}).items()},
            resistances={k: dict(v) for k, v in (data.get("resistances") or {}).items()},
            contributions=[Contribution.from_dict(c) for c in (data.get("contributions") or [])],
            issues=list(data.get("issues") or []),
            derived=dict(data.get("derived") or {}),
            excluded_sources=list(data.get("excluded_sources") or []),
        )


def contribution_sort_key(c: Contribution) -> tuple:
    """Stable ordering for deterministic ledgers/JSON."""
    return (c.source_kind, c.source_path, c.contribution_id)


def format_seconds(value: float, digits: int = 6) -> float:
    """Round a time value for stable JSON output (does not round internally)."""
    return round(float(value), digits)


def format_damage(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


__all__ = [
    "ACTIVE_CONDITIONS", "AT_BITS", "ATTRIBUTE_ORDER", "CONDITION_ALWAYS",
    "CONDITION_NOT_SATISFIED", "CONDITION_SATISFIED", "CONDITION_UNKNOWN",
    "Contribution", "ELEMENT_TAGS", "EVIDENCE_CATALOG", "EVIDENCE_DERIVATION",
    "EVIDENCE_GAME_DATA", "EVIDENCE_PUBLIC_DOC", "EVIDENCE_THIRD_PARTY",
    "EVIDENCE_USER_RULE", "Evidence", "RESISTANCE_NAMES", "SOURCE_KINDS",
    "STATUS_ASSUMED", "STATUS_CONFIRMED", "STATUS_DERIVED", "STATUS_UNRESOLVED",
    "STAT_ADDED_DAMAGE", "STAT_AILMENT_CHANCE", "STAT_AILMENT_DURATION",
    "STAT_AILMENT_EFFECT", "STAT_ALL_ATTRIBUTES", "STAT_ATTRIBUTE",
    "STAT_CAST_SPEED", "STAT_CHARGES", "STAT_COOLDOWN_FLAT",
    "STAT_COOLDOWN_RECOVERY", "STAT_COOLDOWN_REDUCTION",
    "STAT_DAMAGE", "STAT_MANA", "STAT_MANA_REGEN", "STAT_PENETRATION",
    "STAT_PENETRATION_WITH_IGNITE",
    "STAT_RESISTANCE", "STAT_SKILL_DURATION_INCREASED", "STAT_UNSUPPORTED",
    "Snapshot", "VALID_STATUS", "contribution_sort_key", "decode_tags",
    "format_damage", "format_seconds",
]
