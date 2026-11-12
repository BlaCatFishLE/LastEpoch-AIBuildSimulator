"""Black Hole body damage core (v1).

The engine consumes:
  * the resolved input snapshot (player-side contributions, attributes, resistances),
  * the ruleset (Black Hole components, skill-tree modifiers, v1 decisions),
  * the scenario (target and spatial conditions).

Nothing here reads the database or the build files directly.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable, Optional

from . import model
from .mechanics import increased_aggregate as shared_increased_aggregate
from .mechanics import more_groups as shared_more_groups
from .mechanics import tag_match as shared_tag_match
from .mechanics import target_side as shared_target_side
from .ruleset import Ruleset

EPS = 1e-9

#: damage type tags
DAMAGE_TYPE_TAGS = frozenset(
    {"Physical", "Lightning", "Cold", "Fire", "Void", "Necrotic", "Poison"}
)
ELEMENT_TYPE_TAGS = frozenset({"Fire", "Cold", "Lightning"})
#: category tags that restrict applicability (spell / dot / hit / melee / ...)
CATEGORY_TAGS = frozenset(
    {
        "Spell", "DoT", "Hit", "Ailment", "Melee", "Throwing", "Bow",
        "Channelling", "Minion", "Totem", "Buff",
    }
)
#: tags that are conditions/metadata and must not change applicability here
IGNORED_TAGS = frozenset(
    {
        "Elemental", "LowLife", "HighLife", "FullLife", "Curse", "Transform",
        "Potion", "PetResisted", "Crit_deprecated", "Kill_deprecated",
        "Die_deprecated",
    }
)


@dataclass(frozen=True)
class ComponentTags:
    """Applicability profile of one damage component."""

    component_id: str
    damage_type: str
    types: frozenset[str]
    categories: frozenset[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "damage_type": self.damage_type,
            "types": sorted(self.types),
            "categories": sorted(self.categories),
        }


BODY_FIRE_TAGS = ComponentTags("body-fire", "Fire", frozenset({"Fire"}), frozenset({"Spell", "DoT"}))
BODY_COLD_TAGS = ComponentTags("body-cold", "Cold", frozenset({"Cold"}), frozenset({"Spell", "DoT"}))
IGNITE_TAGS = ComponentTags("ignite", "Fire", frozenset({"Fire"}), frozenset({"DoT", "Ailment"}))


# ---------------------------------------------------------------------------
# Tag applicability
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _component_tag_closure(component: ComponentTags) -> frozenset[str]:
    """Fold a component's type/category sets for the shared tag matcher.

    Engine components keep ``types`` and ``categories`` disjoint, so membership
    in the union is equivalent to the historical per-bucket checks.
    """
    return frozenset(component.types) | frozenset(component.categories)


def tags_match(contribution_tags: Iterable[str], component: ComponentTags) -> bool:
    """Return True when a modifier with these tags applies to the component.

    Delegates to the shared ``le_calc.mechanics.tag_match`` operator with the
    BH policy from ``tag_match.bh_tag_policy()`` (metadata tags ignored, unknown
    tags ignored, known type/category tags must be present in the component).

    Semantics follow the LE Tools AT bitmask: damage-type tags require the
    component to deal that type (Elemental matches any element), category tags
    require the component to carry that category.  Unknown tags are ignored.
    """
    return shared_tag_match.tags_match(
        contribution_tags,
        _component_tag_closure(component),
        shared_tag_match.bh_tag_policy(),
    )


def player_contributions(snapshot: model.Snapshot) -> list[model.Contribution]:
    """Active player-side contributions (skill-tree nodes are handled via ruleset)."""
    return [c for c in snapshot.active_contributions() if c.source_kind != "skill_node"]


def condition_met(code: Optional[str], scenario: dict[str, Any]) -> bool:
    if not code:
        return True
    conds = scenario.get("target_conditions", {}) or {}
    if code in conds:
        return bool(conds[code])
    return False


# ---------------------------------------------------------------------------
# Modifier collection
# ---------------------------------------------------------------------------


@dataclass
class ModifierRow:
    contribution_id: str
    label: str
    value: float
    source_kind: str
    source_path: str
    status: str
    damage_tags: list[str] = field(default_factory=list)
    group: Optional[str] = None
    rule_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "contribution_id": self.contribution_id,
            "label": self.label,
            "value": self.value,
            "source_kind": self.source_kind,
            "source_path": self.source_path,
            "status": self.status,
            "damage_tags": list(self.damage_tags),
            "group": self.group,
            "rule_id": self.rule_id,
        }


def collect_modifier_rows(
    snapshot: model.Snapshot,
    stat_key: str,
    component: ComponentTags,
    operations: Iterable[str] = ("increased",),
) -> list[model.Contribution]:
    allowed = set(operations)
    rows: list[model.Contribution] = []
    for c in player_contributions(snapshot):
        if c.stat_key != stat_key or c.operation not in allowed:
            continue
        if not tags_match(c.damage_tags, component):
            continue
        rows.append(c)
    return rows


def _row(c: model.Contribution, rule_id: Optional[str] = None) -> ModifierRow:
    return ModifierRow(
        contribution_id=c.contribution_id,
        label=c.stat_name,
        value=float(c.value),
        source_kind=c.source_kind,
        source_path=c.source_path,
        status=c.status,
        damage_tags=list(c.damage_tags),
        group=c.group,
        rule_id=rule_id,
    )


def group_more_rows(rows: list[ModifierRow]) -> list[dict[str, Any]]:
    """Group ``more`` rows with the shared more-groups operator.

    Group key = explicit ``row.group`` else ``contribution_id``; values add
    inside a group, groups multiply (caller multiplies ``factor`` in returned
    order = sorted group name).  The row payloads/order are rebuilt from the
    original rows so the result JSON keeps its historical shape bit-for-bit.
    """
    aggregate = shared_more_groups.aggregate(
        shared_more_groups.rows_from_modifier_rows(rows)
    )
    return [
        {
            "group": group.group,
            "value": group.value,
            "factor": group.factor,
            "rows": [rows[index].to_dict() for index in group.row_indices],
        }
        for group in aggregate.groups
    ]


def tree_more_groups(
    ruleset: Ruleset,
    component: ComponentTags,
    applies_keys: set[str],
    scenario: dict[str, Any],
) -> list[dict[str, Any]]:
    rows: list[ModifierRow] = []
    for eff in ruleset.tree_more_effects(applies_keys):
        if not tags_match(eff.get("damage_tags", []), component):
            continue
        if not condition_met(eff.get("condition_code"), scenario):
            continue
        rows.append(
            ModifierRow(
                contribution_id=f"ruleset:node:{eff['node_id']}",
                label=eff.get("name") or f"node {eff['node_id']}",
                value=float(eff["value"]),
                source_kind="skill_node",
                source_path=f"bh2.node.{eff['node_id']}",
                status="confirmed",
                damage_tags=list(eff.get("damage_tags", [])),
                group=eff.get("group"),
                rule_id=eff.get("rule_id"),
            )
        )
    return group_more_rows(rows)


# ---------------------------------------------------------------------------
# Duration
# ---------------------------------------------------------------------------


def compute_duration(snapshot: model.Snapshot, ruleset: Ruleset) -> dict[str, Any]:
    base = float(ruleset.duration["base_duration_s"])
    rows: list[dict[str, Any]] = []
    for term in ruleset.tree_duration_terms():
        rows.append(
            {
                "source": f"ruleset:node:{term['node_id']}",
                "label": f"skill node {term['node_id']}",
                "value": term["value"],
                "source_kind": "skill_node",
                "status": "confirmed",
            }
        )
    for c in player_contributions(snapshot):
        if c.stat_key != model.STAT_SKILL_DURATION_INCREASED:
            continue
        if c.operation not in ("increased", "added"):
            continue
        rows.append(
            {
                "source": c.contribution_id,
                "label": c.stat_name,
                "value": float(c.value),
                "source_kind": c.source_kind,
                "status": c.status,
            }
        )
    increased = sum(r["value"] for r in rows)
    return {
        "base_duration_s": base,
        "increased_total": increased,
        "duration_s": base * (1.0 + increased),
        "rows": rows,
    }


# ---------------------------------------------------------------------------
# Body components
# ---------------------------------------------------------------------------


def _collect_increased(
    snapshot: model.Snapshot, component: ComponentTags, attribute_increase: float
) -> dict[str, Any]:
    rows = [_row(c) for c in collect_modifier_rows(snapshot, model.STAT_DAMAGE, component, ("increased",))]
    # Shared additive pool: sum(rows) + attribute scaling, factor = 1 + total.
    # Sum order and the trailing attribute addition match the historical code.
    pool = shared_increased_aggregate.aggregate(
        [row.value for row in rows], attribute_increase=attribute_increase
    )
    return {
        "rows": [r.to_dict() for r in rows],
        "attribute_row": {
            "contribution_id": "ruleset:attribute:intelligence",
            "label": "Intelligence attribute scaling",
            "value": attribute_increase,
            "source_kind": "derived",
            "source_path": "bh2.attributeScaling[2]",
            "status": "confirmed",
            "damage_tags": [],
        },
        "total": pool.total,
        "factor": pool.factor,
    }


def _penetration_rows(
    snapshot: model.Snapshot, component: ComponentTags
) -> list[ModifierRow]:
    rows: list[ModifierRow] = []
    for c in player_contributions(snapshot):
        if c.stat_key != model.STAT_PENETRATION:
            continue
        if not tags_match(c.damage_tags, component):
            continue
        rows.append(_row(c))
    return rows


def _target_multiplier(
    ruleset: Ruleset,
    component: ComponentTags,
    penetration_total: float,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Target-side settlement now delegates to the shared mechanics operator.

    Policy (from P2 ``le_calc.mechanics.target_side``): penetration folded into
    effective resistance, negative resistance allowed per ruleset, monster DR
    injected by the scenario (``report.build_result`` / recompute).  The returned
    mapping keeps the historical field names and values bit-for-bit.
    """
    return shared_target_side.bh_target_side(
        ruleset_target_side=ruleset.target_side,
        scenario_target=(scenario or {}).get("target", {}) or {},
        damage_type=component.damage_type,
        penetration_total=penetration_total,
        component_scope="body",
    )


def _ticks_for_duration(
    duration_s: float,
    interval_s: float,
    remainder_policy: str = "merge_into_last_tick",
) -> list[dict[str, float]]:
    """Discretise the damage flow into tick events.

    Ticks occur at spawn + k*interval (damageAtStart=true).

    ``merge_into_last_tick`` (v1, user rule 1): ceil(duration/interval) events,
    each event carries the interval slice it covers, so the last event absorbs
    the fractional remainder (total = nominal rate * duration).

    ``ceil_full_ticks`` (1.5.3 RE): the game applies full damage on every tick
    while elapsed > interval and does not settle the remainder on destroy, so
    the tick count is ceil(duration/interval) and the last tick deals full
    damage (total = ceil(duration/interval) * per_tick_damage).
    """
    if duration_s <= 0:
        return []
    n_events = int(math.ceil(duration_s / interval_s - EPS))
    n_events = max(1, n_events)
    full_ticks = remainder_policy in ("ceil_full_ticks", "integer_ticks", "full_ticks")
    ticks: list[dict[str, float]] = []
    for k in range(n_events):
        offset = k * interval_s
        if full_ticks:
            coverage = interval_s
        else:
            coverage = min(interval_s, duration_s - offset)
            if coverage <= EPS and k > 0:
                break
        ticks.append(
            {
                "index": k,
                "offset_s": round(offset, 9),
                "coverage_s": coverage,
                "coverage_fraction": coverage / interval_s,
            }
        )
    return ticks


def _expression_node(
    node_id: str,
    kind: str,
    label: str,
    value: float,
    children: Optional[list[dict[str, Any]]] = None,
    **extra: Any,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "node_id": node_id,
        "kind": kind,
        "label": label,
        "value": value,
        "children": children or [],
    }
    node.update(extra)
    return node


def compute_body(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: dict[str, Any],
) -> dict[str, Any]:
    """Compute the Black Hole body (self) damage for a single cast."""
    ruleset = ruleset.with_snapshot_points(snapshot)
    bh = ruleset.black_hole
    core = ruleset.component
    interval = float(core["damage_interval_s"])
    added_scaling = float(core["added_damage_scaling"])
    base_cold = float(core["base_damage"]["cold"])
    attribute = ruleset.black_hole.get("attribute_scaling", {})
    attr_key = attribute.get("attribute", "intelligence")
    attr_per_point = float(attribute.get("increased_per_point", 0.0))
    attribute_increase = attr_per_point * snapshot.attribute(attr_key)

    duration_info = compute_duration(snapshot, ruleset)
    duration_s = duration_info["duration_s"]
    tick_model = ruleset.black_hole.get("tick_model") or {}
    remainder_policy = str(tick_model.get("remainder_policy", "merge_into_last_tick"))
    ticks = _ticks_for_duration(duration_s, interval, remainder_policy)

    specs: list[dict[str, Any]] = []
    if ruleset.has_binary_system:
        specs.append(
            {
                "component_id": "bh-binary-fire",
                "label": "火焰分量（Red Giant 转火后）",
                "damage_type": "Fire",
                "base_value": base_cold,
                "source_flags": ["red_giant", "binary_system"],
                "tags": BODY_FIRE_TAGS,
                "applies_keys": {"body", "body_fire"},
            }
        )
        specs.append(
            {
                "component_id": "bh-binary-cold",
                "label": "冰霜分量（Binary System 第二颗星）",
                "damage_type": "Cold",
                "base_value": base_cold,
                "source_flags": ["binary_system"],
                "tags": BODY_COLD_TAGS,
                "applies_keys": {"body", "body_cold"},
            }
        )
    else:
        converted = ruleset.has_red_giant
        specs.append(
            {
                "component_id": "bh-body-fire" if converted else "bh-body-cold",
                "label": "本体火焰分量" if converted else "本体冰霜分量",
                "damage_type": "Fire" if converted else "Cold",
                "base_value": base_cold,
                "source_flags": ["red_giant"] if converted else [],
                "tags": BODY_FIRE_TAGS if converted else BODY_COLD_TAGS,
                "applies_keys": {"body", "body_fire"} if converted else {"body", "body_cold"},
            }
        )

    components: list[dict[str, Any]] = []
    for spec in specs:
        tags = spec["tags"]
        added_rows = [
            _row(c)
            for c in collect_modifier_rows(snapshot, model.STAT_ADDED_DAMAGE, tags, ("added",))
        ]
        added_total = sum(r.value for r in added_rows)
        inc = _collect_increased(snapshot, tags, attribute_increase)
        player_more = [_row(c) for c in collect_modifier_rows(snapshot, model.STAT_DAMAGE, tags, ("more",))]
        tree_rows: list[ModifierRow] = []
        for eff in ruleset.tree_more_effects(spec["applies_keys"]):
            if not tags_match(eff.get("damage_tags", []), tags):
                continue
            if not condition_met(eff.get("condition_code"), scenario):
                continue
            tree_rows.append(
                ModifierRow(
                    contribution_id=f"ruleset:node:{eff['node_id']}",
                    label=eff.get("name") or f"node {eff['node_id']}",
                    value=float(eff["value"]),
                    source_kind="skill_node",
                    source_path=f"bh2.node.{eff['node_id']}",
                    status="confirmed",
                    damage_tags=list(eff.get("damage_tags", [])),
                    group=eff.get("group"),
                    rule_id=eff.get("rule_id"),
                )
            )
        more_groups = group_more_rows(player_more + tree_rows)
        more_product = 1.0
        for g in more_groups:
            more_product *= g["factor"]

        pen_rows = _penetration_rows(snapshot, tags)
        pen_total = sum(r.value for r in pen_rows)
        target = _target_multiplier(ruleset, tags, pen_total, scenario)

        per_tick_base = spec["base_value"] + added_total * added_scaling
        per_tick_damage = per_tick_base * (1.0 + inc["total"]) * more_product * target["multiplier"]

        tick_rows = []
        for t in ticks:
            amount = per_tick_damage * t["coverage_fraction"]
            tick_rows.append(
                {
                    "index": t["index"],
                    "offset_s": t["offset_s"],
                    "coverage_s": round(t["coverage_s"], 9),
                    "coverage_fraction": t["coverage_fraction"],
                    "damage": amount,
                }
            )
        total = sum(t["damage"] for t in tick_rows)

        expression = _expression_node(
            f"expr:{spec['component_id']}",
            "mul",
            f"{spec['label']} 每跳伤害",
            per_tick_damage,
            children=[
                _expression_node(
                    f"expr:{spec['component_id']}:base",
                    "add",
                    "基础值 + 点伤",
                    per_tick_base,
                    children=[
                        _expression_node(f"expr:{spec['component_id']}:base-value", "value", "基础伤害", spec["base_value"], source_refs=["bh2.prefab.baseDamageStats.damage[2]"]),
                        _expression_node(f"expr:{spec['component_id']}:added", "value", f"附加伤害 × {added_scaling}", added_total * added_scaling, source_refs=[r.contribution_id for r in added_rows]),
                    ],
                ),
                _expression_node(
                    f"expr:{spec['component_id']}:inc",
                    "factor",
                    "1 + inc 加算池",
                    inc["factor"],
                    children=[],
                    rows=inc["rows"] + [inc["attribute_row"]],
                ),
                _expression_node(
                    f"expr:{spec['component_id']}:more",
                    "factor",
                    "more 独立乘区乘积",
                    more_product,
                    children=[],
                    rows=more_groups,
                ),
                _expression_node(
                    f"expr:{spec['component_id']}:target",
                    "factor",
                    "目标侧结算",
                    target["multiplier"],
                    children=[],
                    target=target,
                ),
            ],
        )

        components.append(
            {
                "component_id": spec["component_id"],
                "label": spec["label"],
                "damage_type": spec["damage_type"],
                "source_flags": spec["source_flags"],
                "base_value": spec["base_value"],
                "added_damage_total": added_total,
                "added_damage_scaling": added_scaling,
                "per_tick_base": per_tick_base,
                "per_tick_damage": per_tick_damage,
                "increased": inc,
                "more_groups": more_groups,
                "more_product": more_product,
                "penetration": {
                    "rows": [r.to_dict() for r in pen_rows],
                    "total": pen_total,
                },
                "target": target,
                "tick_count": len(tick_rows),
                "ticks": tick_rows,
                "damage_total": total,
                "expression": expression,
            }
        )

    body_total = sum(c["damage_total"] for c in components)
    return {
        "duration": duration_info,
        "tick_interval_s": interval,
        "tick_count": len(ticks),
        "tick_model": {
            "remainder_policy": remainder_policy,
            "damage_at_start": bool(core.get("damage_at_start", True)),
            "tick_interval_s": interval,
        },
        "components": components,
        "totals": {
            "damage": body_total,
            "nominal_rate_per_s": body_total / duration_s if duration_s > 0 else 0.0,
            "component_count": len(components),
        },
    }


__all__ = [
    "BODY_COLD_TAGS",
    "BODY_FIRE_TAGS",
    "CATEGORY_TAGS",
    "ComponentTags",
    "IGNITE_TAGS",
    "ModifierRow",
    "compute_body",
    "compute_duration",
    "condition_met",
    "group_more_rows",
    "player_contributions",
    "tags_match",
]
