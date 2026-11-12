"""Single-hit damage core for the Flame Reave (fr11mv) hit model.

Owner: fr-engine (task-2).  Consumers: ``hit_simulate``/``hit_buffs``/
``hit_report``/``cli``.

Formula (DESIGN §2/§5.1, reference planner runtime)::

    component_damage = (base + added * added_damage_scaling)
                       * (1 + sum(increased[tags]))
                       * product(1 + sum(more per group)[tags])
                       * avg_crit
                       * (1 + penetration)
                       * (1 - target_resistance)
                       * (1 - monster_dr)

    avg_crit = (1 - crit_chance) + crit_chance * crit_multiplier

* ``increased`` rows are additive inside one pool; every ``more`` *group*
  multiplies separately (same-node multi-point effects are one group).
* Pool membership is filtered by damage tags; a row with no damage tags is
  generic and applies to every component.  ``Elemental`` rows expand onto the
  element components.  ``Spell``/``DoT``-only rows never apply to melee hits.
* Static rows come from :class:`le_calc.model.Snapshot`; conditional rows come
  from ``ruleset.effects`` and are switched by scenario ``states``.
* The single-hit result is fully traceable: every pool row carries its
  contribution id, source and value.

Entry points
------------
``compute_single_hit(snapshot, ruleset, scenario=None) -> dict``
``resolve_states(ruleset, scenario=None, *, section=None) -> dict``
``evaluate_condition(code, states) -> Optional[bool]``
``tags_apply(damage_tags, component_tags) -> bool``
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import model
from . import condition_codes
from .mechanics import crit_expected as shared_crit_expected
from .mechanics import increased_aggregate as shared_increased_aggregate
from .mechanics import more_groups as shared_more_groups
from .mechanics import tag_match as shared_tag_match
from .mechanics import target_side as shared_target_side

SCHEMA_VERSION = "le-hit-single-hit-v1"

DAMAGE_TYPE_TAGS = frozenset(
    {"Physical", "Fire", "Cold", "Lightning", "Void", "Necrotic", "Poison"}
)
ELEMENTAL_TAGS = frozenset({"Fire", "Cold", "Lightning"})

#: 场景状态别名与条件求值已抽到共享模块 ``condition_codes``；
#: 这里保留同名入口，历史调用与测试不需要改动。
_STATE_ALIASES = condition_codes.STATE_ALIASES
_first_state = condition_codes.first_state
evaluate_condition = condition_codes.evaluate_condition
_evaluate_atom = condition_codes.evaluate_atom


def default_states() -> dict[str, Any]:
    return condition_codes.default_states()


def _merge_states(target: dict[str, Any], source: Any) -> None:
    if not isinstance(source, dict):
        return
    for key, value in source.items():
        if key in ("comment", "note", "notes"):
            continue
        target[key] = value


def _scenario_states(scenario: Optional[dict[str, Any]], section: Optional[str]) -> dict[str, Any]:
    """Merge ``le-hit-scenario-v1`` state layers (later wins)."""
    merged: dict[str, Any] = {}
    scenario = scenario or {}
    profile_id = scenario.get("baseline_profile")
    profiles = scenario.get("profiles") or {}
    if profile_id and isinstance(profiles.get(profile_id), dict):
        _merge_states(merged, profiles[profile_id].get("states"))
    _merge_states(merged, scenario.get("states"))
    single_hit = scenario.get("single_hit") or {}
    _merge_states(merged, single_hit.get("states"))
    if section in ("simulate", "dps"):
        dps = scenario.get("dps") or {}
        _merge_states(merged, dps.get("states"))
    return merged


def resolve_states(
    ruleset: Any = None,
    scenario: Optional[dict[str, Any]] = None,
    *,
    section: Optional[str] = None,
    snapshot: Optional[model.Snapshot] = None,
) -> dict[str, Any]:
    """Return the effective state map for one engine call.

    Layers: ruleset defaults -> snapshot resolved facts (``derived.applied_states``)
    -> scenario profile -> top-level states -> ``single_hit.states`` ->
    (``dps.states`` when ``section="simulate"``).

    The lead-owned scenario document uses ``baseline_profile`` +
    ``profiles[<id>].states``; plain ``{"states": {...}}`` documents also work.
    Snapshot facts (dual wield, melee mana cost, mana_ge_300) are merged as a
    base layer so they never get lost when a scenario only switches buffs.
    """
    states = default_states()
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    if snapshot is not None:
        _merge_states(states, (snapshot.derived or {}).get("applied_states"))
    if isinstance(ruleset_data, dict):
        _merge_states(states, (ruleset_data.get("states") or {}).get("defaults"))
    _merge_states(states, _scenario_states(scenario, section))
    return states


def resolve_target(ruleset: Any, scenario: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Merge ``ruleset.target`` with ``scenario.target`` (scenario wins).

    The DR lookup / factor arithmetic now lives in the shared mechanics
    operator (``le_calc.mechanics.target_side.resolve_hit_target``, policy
    ``table_by_level``: the explicit scenario DR is ignored by design).  The
    returned mapping keeps the historical fields and values bit-for-bit.
    """
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    target = dict((ruleset_data.get("target") or {}) if isinstance(ruleset_data, dict) else {})
    _merge_states(target, (scenario or {}).get("target"))
    return shared_target_side.resolve_hit_target(target)


# ---------------------------------------------------------------------------
# Tag matching
# ---------------------------------------------------------------------------


def tags_apply(damage_tags: Iterable[str], component_tags: Iterable[str]) -> bool:
    """Return True when a pool row's damage tags apply to one component.

    Delegates to the shared ``le_calc.mechanics.tag_match`` operator with the FR
    policy from ``tag_match.hit_tag_policy()`` (``Elemental`` expansion is
    equivalent to skipping it, every other tag must be in the component tag
    closure).  Rules (same shape as the reference planner aggregator):

    * no damage tags -> generic, applies to every component;
    * all tags must be contained in the component's tag closure;
    * ``Elemental`` expands onto a component that contains at least one element.
    """
    return shared_tag_match.tags_match(
        damage_tags, component_tags, shared_tag_match.hit_tag_policy()
    )


# ---------------------------------------------------------------------------
# Pool rows
# ---------------------------------------------------------------------------


def _row(
    *,
    contribution_id: str,
    stat_key: str,
    operation: str,
    value: float,
    damage_tags: Optional[list[str]] = None,
    group: Optional[str] = None,
    source_kind: str = "derived",
    source_label: str = "",
    origin: str = "snapshot",
    status: str = model.STATUS_CONFIRMED,
    condition_code: Optional[str] = None,
    notes: Optional[str] = None,
    evidence: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    return {
        "contribution_id": contribution_id,
        "stat_key": stat_key,
        "operation": operation,
        "value": float(value),
        "damage_tags": list(damage_tags or []),
        "group": group,
        "source_kind": source_kind,
        "source_label": source_label,
        "origin": origin,
        "status": status,
        "condition_code": condition_code,
        "notes": notes,
        "evidence": list(evidence or []),
    }


def _snapshot_rows(snapshot: model.Snapshot, states: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for contribution in snapshot.contributions:
        if not contribution.included:
            continue
        if contribution.condition_code:
            satisfied = evaluate_condition(contribution.condition_code, states)
            if satisfied is None:
                excluded.append(
                    {
                        "contribution_id": contribution.contribution_id,
                        "reason": "unresolved condition",
                        "condition_code": contribution.condition_code,
                        "source_kind": contribution.source_kind,
                    }
                )
                continue
            if not satisfied:
                excluded.append(
                    {
                        "contribution_id": contribution.contribution_id,
                        "reason": "condition not satisfied",
                        "condition_code": contribution.condition_code,
                        "source_kind": contribution.source_kind,
                    }
                )
                continue
        rows.append(
            _row(
                contribution_id=contribution.contribution_id,
                stat_key=contribution.stat_key,
                operation=contribution.operation,
                value=contribution.value,
                damage_tags=list(contribution.damage_tags),
                group=contribution.group,
                source_kind=contribution.source_kind,
                source_label=contribution.source_label,
                origin="snapshot",
                status=contribution.status,
                condition_code=contribution.condition_code,
                notes=contribution.notes,
                evidence=[item.to_dict() for item in contribution.evidence],
            )
        )
    return rows, excluded


def _effect_condition_satisfied(effect: dict[str, Any], states: dict[str, Any]) -> Optional[bool]:
    condition = effect.get("condition")
    if not condition:
        return True
    if isinstance(condition, dict) and "state" in condition:
        state = condition.get("state")
        names = _STATE_ALIASES.get(str(state), (str(state),))
        value = _first_state(states, names)
        comparator = str(condition.get("op") or "==")
        expected = condition.get("value", True)
        if value is None:
            return None
        try:
            if comparator == "==":
                return bool(value == expected)
            if comparator == "!=":
                return bool(value != expected)
            if comparator == ">":
                return float(value) > float(expected)
            if comparator == ">=":
                return float(value) >= float(expected)
            if comparator == "<":
                return float(value) < float(expected)
            if comparator == "<=":
                return float(value) <= float(expected)
            if comparator == "truthy":
                return bool(value)
            if comparator == "falsy":
                return not bool(value)
        except (TypeError, ValueError):
            return None
        return None
    codes = condition.get("all_of") or condition.get("any_of") or []
    if codes:
        results = [evaluate_condition(code, states) for code in codes]
        if any(result is None for result in results):
            return None
        return all(results) if condition.get("all_of") else any(results)
    return None


def _scaled_modifier_value(
    modifier: dict[str, Any],
    effect: dict[str, Any],
    states: dict[str, Any],
) -> float:
    """Value of one effect modifier, applying per-stack scaling when declared."""
    value = float(modifier.get("value") or 0.0)
    per_stack = modifier.get("value_per_stack")
    if per_stack is not None:
        stacks_state = (
            modifier.get("stacks_state")
            or effect.get("stacks_state")
            or "firebrand_stacks"
        )
        stacks_raw = _first_state(states, _STATE_ALIASES.get(str(stacks_state), (str(stacks_state),)))
        try:
            stacks = float(stacks_raw or 0)
        except (TypeError, ValueError):
            stacks = 0.0
        max_stacks = modifier.get("max_stacks") or effect.get("max_stacks")
        if max_stacks is not None:
            stacks = min(stacks, float(max_stacks))
        value = float(per_stack) * stacks
    return value


def _effect_rows(
    ruleset: Any,
    states: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Expand ``ruleset.effects`` into pool rows for the current states.

    Returns ``(rows, applied_effects, not_modeled, unresolved, assumptions)``.
    """
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    effects = list((ruleset_data.get("effects") or []) if isinstance(ruleset_data, dict) else [])
    rows: list[dict[str, Any]] = []
    applied: list[dict[str, Any]] = []
    not_modeled: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    assumptions: list[str] = []
    for effect in effects:
        effect_id = str(effect.get("id"))
        coverage = str(effect.get("coverage") or "unresolved")
        condition_met = _effect_condition_satisfied(effect, states)
        include_in = list(effect.get("include_in") or [])
        entry = {
            "effect_id": effect_id,
            "label": effect.get("label"),
            "coverage": coverage,
            "status": effect.get("status"),
            "condition_met": condition_met,
            "include_in": include_in,
            "source_kind": effect.get("source_kind"),
            "source_path": effect.get("source_path"),
        }
        if coverage == "not_modeled":
            not_modeled.append(entry)
            continue
        if coverage == "unresolved":
            unresolved.append(entry)
            continue
        if condition_met is None:
            entry["reason"] = "condition could not be evaluated"
            unresolved.append(entry)
            continue
        if not condition_met:
            continue
        modifiers: list[dict[str, Any]] = []
        for index, modifier in enumerate(effect.get("modifiers") or []):
            value = _scaled_modifier_value(modifier, effect, states)
            if value == 0.0:
                continue
            modifier_row = _row(
                contribution_id=f"ruleset:effect:{effect_id}:{index}",
                stat_key=str(modifier.get("stat_key")),
                operation=str(modifier.get("operation") or "added"),
                value=value,
                damage_tags=list(modifier.get("damage_tags") or []),
                group=modifier.get("group") or f"effect:{effect_id}",
                source_kind=str(effect.get("source_kind") or "derived"),
                source_label=str(effect.get("label") or effect_id),
                origin="ruleset_effect",
                status=str(effect.get("status") or model.STATUS_ASSUMED),
                condition_code=effect_id,
                notes=modifier.get("note"),
                evidence=list(effect.get("evidence") or []),
            )
            rows.append(modifier_row)
            modifiers.append(modifier_row)
        entry["modifiers_applied"] = [
            {
                "stat_key": row["stat_key"],
                "operation": row["operation"],
                "value": row["value"],
                "damage_tags": row["damage_tags"],
            }
            for row in modifiers
        ]
        applied.append(entry)
        if effect.get("status") in (model.STATUS_ASSUMED, model.STATUS_UNRESOLVED):
            assumptions.append(
                f"effect {effect_id} ({effect.get('label')}) applied as {effect.get('status')}: "
                f"{effect.get('assumption') or effect.get('next_probe') or ''}".strip()
            )
    return rows, applied, not_modeled, unresolved, assumptions


# ---------------------------------------------------------------------------
# Component computation
# ---------------------------------------------------------------------------


def collect_rows(
    snapshot: model.Snapshot,
    ruleset: Any,
    states: dict[str, Any],
) -> dict[str, Any]:
    """Collect every active pool row for one state map (snapshot + ruleset effects).

    Shared by ``hit_simulate`` (attack-speed pools) and the single-hit engine so
    both see exactly the same conditional rows.
    """
    snapshot_rows, excluded_contributions = _snapshot_rows(snapshot, states)
    effect_rows, applied_effects, not_modeled_effects, unresolved_effects, assumptions = _effect_rows(
        ruleset, states
    )
    return {
        "rows": snapshot_rows + effect_rows,
        "snapshot_rows": snapshot_rows,
        "effect_rows": effect_rows,
        "excluded_contributions": excluded_contributions,
        "applied_effects": applied_effects,
        "not_modeled_effects": not_modeled_effects,
        "unresolved_effects": unresolved_effects,
        "assumptions": assumptions,
    }


def _component_tags(component: dict[str, Any]) -> list[str]:
    tags = list(component.get("damage_tags") or [])
    if not tags:
        tags = [str(component.get("damage_type"))]
    for tag in ("Melee", "Hit"):
        if tag not in tags:
            tags.append(tag)
    return tags


def _typed_added_kind(rows: Iterable[dict[str, Any]]) -> list[str]:
    types: list[str] = []
    for row in rows:
        for tag in row.get("damage_tags") or []:
            if tag in DAMAGE_TYPE_TAGS and tag not in types:
                types.append(tag)
    return types


def _sum_rows(rows: Iterable[dict[str, Any]], multiplier: float = 1.0) -> float:
    return sum(float(row.get("value") or 0.0) for row in rows) * multiplier


def _crit_section(
    rows: list[dict[str, Any]],
    component_tags: list[str],
    *,
    skill: dict[str, Any],
    critical: dict[str, Any],
) -> dict[str, Any]:
    base_chance = float(skill.get("crit_chance") or 0.0)
    base_multiplier = float(skill.get("crit_multiplier") or 1.0)
    chance_rows = [row for row in rows if row["stat_key"] == model.STAT_CRIT_CHANCE]
    multi_rows = [row for row in rows if row["stat_key"] == model.STAT_CRIT_MULTIPLIER]
    chance_added = _sum_rows(r for r in chance_rows if r["operation"] == "added" and tags_apply(r["damage_tags"], component_tags))
    chance_inc = _sum_rows(r for r in chance_rows if r["operation"] in INCREASED_OPERATIONS and tags_apply(r["damage_tags"], component_tags))
    chance_more_groups: dict[str, float] = {}
    for row in chance_rows:
        if row["operation"] not in MORE_OPERATIONS or not tags_apply(row["damage_tags"], component_tags):
            continue
        group = row.get("group") or row["contribution_id"]
        chance_more_groups[group] = chance_more_groups.get(group, 0.0) + row["value"]
    chance_more_product = 1.0
    for value in chance_more_groups.values():
        chance_more_product *= 1.0 + value
    cap = float(critical.get("chance_cap") or 1.0)

    multi_added = _sum_rows(r for r in multi_rows if r["operation"] == "added" and tags_apply(r["damage_tags"], component_tags))
    multi_inc = _sum_rows(r for r in multi_rows if r["operation"] in INCREASED_OPERATIONS and tags_apply(r["damage_tags"], component_tags))
    multi_more_groups: dict[str, float] = {}
    for row in multi_rows:
        if row["operation"] not in MORE_OPERATIONS or not tags_apply(row["damage_tags"], component_tags):
            continue
        group = row.get("group") or row["contribution_id"]
        multi_more_groups[group] = multi_more_groups.get(group, 0.0) + row["value"]
    multi_more_product = 1.0
    for value in multi_more_groups.values():
        multi_more_product *= 1.0 + value

    # Shared crit operator (FR capability = enabled).  The pool extraction above
    # stays local; the operator only owns the documented arithmetic
    # ``avg = (1-c) + c*m`` and its chance/multiplier pool formulas.
    crit_pools = shared_crit_expected.CritPools(
        base_chance=base_chance,
        chance_added=chance_added,
        chance_increased=chance_inc,
        chance_more_product=chance_more_product,
        base_multiplier=base_multiplier,
        multiplier_added=multi_added,
        multiplier_increased=multi_inc,
        multiplier_more_product=multi_more_product,
        chance_cap=cap,
    )
    expected = shared_crit_expected.expected_crit(
        capability=shared_crit_expected.FR_HIT_CRIT_CAPABILITY,
        pools=crit_pools,
    )
    crit_chance = expected.crit_chance
    crit_multiplier = expected.crit_multiplier
    avg_crit = expected.avg_crit
    return {
        "base_chance": base_chance,
        "added_chance": chance_added,
        "increased_chance": chance_inc,
        "more_chance_product": chance_more_product,
        "crit_chance": crit_chance,
        "chance_cap": cap,
        "base_multiplier": base_multiplier,
        "added_multiplier": multi_added,
        "increased_multiplier": multi_inc,
        "more_multiplier_product": multi_more_product,
        "crit_multiplier": crit_multiplier,
        "avg_crit": avg_crit,
        "chance_rows": [
            _row_summary(row)
            for row in chance_rows
            if tags_apply(row["damage_tags"], component_tags)
        ],
        "multiplier_rows": [
            _row_summary(row)
            for row in multi_rows
            if tags_apply(row["damage_tags"], component_tags)
        ],
        "formula": str(
            critical.get("formula")
            or "crit_chance = (base + added) * (1 + increased) * product(1 + more); "
               "crit_multiplier = (base + added) * (1 + increased) * product(1 + more); "
               "avg_crit = (1 - chance) + chance * multiplier"
        ),
    }


def _row_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "contribution_id": row["contribution_id"],
        "source_kind": row.get("source_kind"),
        "source_label": row.get("source_label"),
        "origin": row.get("origin"),
        "operation": row["operation"],
        "value": row["value"],
        "damage_tags": list(row.get("damage_tags") or []),
        "status": row.get("status"),
        "group": row.get("group"),
    }


def _component_added_pool(
    rows: Iterable[dict[str, Any]],
    *,
    component: dict[str, Any],
    component_tags: list[str],
    policy: str,
    assumptions: list[str],
) -> tuple[float, list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(added_total, included_rows, excluded_rows)`` for one component."""
    damage_type = str(component.get("damage_type"))
    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in rows:
        if row["stat_key"] != model.STAT_ADDED_DAMAGE or row["operation"] != "added":
            continue
        if not tags_apply(row["damage_tags"], component_tags):
            excluded.append({**_row_summary(row), "reason": "damage tags do not apply to this component"})
            continue
        typed = _typed_added_kind([row])
        if not typed:
            included.append(row)
            if row["value"]:
                assumptions.append(
                    f"adaptive/untyped added damage contribution {row['contribution_id']} "
                    f"({row['value']:g}) treated as {damage_type} (user rule 2026-11-07)"
                )
        elif damage_type in typed or (
            "Elemental" in row["damage_tags"] and damage_type in ELEMENTAL_TAGS
        ):
            included.append(row)
        else:
            if policy == "separate_components":
                excluded.append(
                    {
                        **_row_summary(row),
                        "reason": "non-skill damage type; require a separate component (policy)",
                    }
                )
            else:
                excluded.append(
                    {
                        **_row_summary(row),
                        "reason": (
                            f"typed {typed} added damage does not convert to {damage_type}; "
                            "excluded by fire_only component policy (assumed)"
                        ),
                    }
                )
                assumptions.append(
                    f"typed added damage {row['contribution_id']} ({typed}) excluded from "
                    f"the {damage_type} component (fire_only policy, assumed)"
                )
    return _sum_rows(included), included, excluded


def _more_groups(
    rows: Iterable[dict[str, Any]],
    component_tags: list[str],
) -> tuple[float, list[dict[str, Any]]]:
    """Group ``more`` rows with the shared more-groups operator.

    Keep the historical ``{"group", "value", "rows", "multiplier"}`` payload
    shape/order: groups sorted by key, rows in input order, product over the
    sorted group factors.
    """
    selected = [
        row
        for row in rows
        if row["stat_key"] == model.STAT_DAMAGE
        and row["operation"] in MORE_OPERATIONS
        and tags_apply(row["damage_tags"], component_tags)
    ]
    aggregate = shared_more_groups.aggregate(
        shared_more_groups.rows_from_hit_rows(selected)
    )
    ordered = [
        {
            "group": group.group,
            "value": group.value,
            "rows": [_row_summary(selected[index]) for index in group.row_indices],
            "multiplier": group.factor,
        }
        for group in aggregate.groups
    ]
    return aggregate.product, ordered


def _penetration(
    rows: Iterable[dict[str, Any]],
    component_tags: list[str],
    damage_type: str,
) -> tuple[float, list[dict[str, Any]]]:
    total = 0.0
    used: list[dict[str, Any]] = []
    for row in rows:
        if row["stat_key"] != model.STAT_PENETRATION:
            continue
        if not tags_apply(row["damage_tags"], component_tags):
            continue
        total += row["value"]
        used.append(_row_summary(row))
    return total, used


def compute_single_hit(
    snapshot: model.Snapshot,
    ruleset: Any,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute one Flame Reave hit against one target for the given scenario."""
    states = resolve_states(ruleset, scenario, section="single_hit", snapshot=snapshot)
    target = resolve_target(ruleset, scenario)
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    damage_model = dict(ruleset_data.get("damage_model") or {})
    critical = dict(ruleset_data.get("critical") or {})
    skill = dict((snapshot.derived or {}).get("skill") or {})
    component_defs = list(damage_model.get("components") or [])
    if not component_defs:
        component_defs = [
            {
                "component_id": "fr11mv:Fire",
                "label": "Flame Reave — Fire",
                "damage_type": "Fire",
                "damage_tags": ["Fire"],
            }
        ]
    added_policy = str(damage_model.get("extra_damage_type_policy") or "fire_only")
    added_scaling = float(
        damage_model.get("added_damage_scaling")
        if damage_model.get("added_damage_scaling") is not None
        else skill.get("added_damage_scaling") or 1.0
    )

    assumptions: list[str] = []
    snapshot_rows, excluded_contributions = _snapshot_rows(snapshot, states)
    effect_rows, applied_effects, not_modeled_effects, unresolved_effects, effect_assumptions = _effect_rows(
        ruleset, states
    )
    assumptions.extend(effect_assumptions)
    rows = snapshot_rows + effect_rows

    base_damage_map: dict[str, float] = {}
    for entry in skill.get("base_damage") or []:
        damage_type = str(entry.get("damage_type") or "")
        base_damage_map[damage_type] = base_damage_map.get(damage_type, 0.0) + float(
            entry.get("value") or 0.0
        )

    components: list[dict[str, Any]] = []
    excluded_components: list[dict[str, Any]] = []
    for index, component in enumerate(component_defs):
        damage_type = str(component.get("damage_type"))
        component_tags = _component_tags(component)
        base = float(
            component.get("base")
            if component.get("base") is not None
            else base_damage_map.get(damage_type, 0.0)
        )
        base_sources = [
            {
                "source": "skill component base damage",
                "damage_type": damage_type,
                "value": base_damage_map.get(damage_type, 0.0),
            }
        ]
        added_total, added_rows, excluded_added = _component_added_pool(
            rows,
            component=component,
            component_tags=component_tags,
            policy=added_policy,
            assumptions=assumptions,
        )
        added_scaled = added_total * added_scaling
        subtotal = base + added_scaled
        inc_rows = [
            row
            for row in rows
            if row["stat_key"] == model.STAT_DAMAGE
            and row["operation"] in INCREASED_OPERATIONS
            and tags_apply(row["damage_tags"], component_tags)
        ]
        # Shared additive pool; FR has no attribute addend (0.0 keeps sum bits).
        increased_pool = shared_increased_aggregate.aggregate(
            [row["value"] for row in inc_rows]
        )
        increased_total = increased_pool.total
        inc_multiplier = increased_pool.factor
        more_product, more_groups = _more_groups(rows, component_tags)
        crit = _crit_section(rows, component_tags, skill=skill, critical=critical)
        penetration_total, penetration_rows = _penetration(rows, component_tags, damage_type)
        # Shared target-side operator (separate penetration multiplier policy).
        target_math = shared_target_side.hit_component_target(
            resistance=float(target["fire_resistance"]),
            penetration_total=penetration_total,
            monster_damage_reduction=float(target["monster_damage_reduction"]),
        )
        penetration_multiplier = target_math.penetration_multiplier
        target_multiplier = target_math.multiplier
        damage = (
            subtotal
            * inc_multiplier
            * more_product
            * float(crit["avg_crit"])
            * target_multiplier
        )
        component_id = str(component.get("component_id") or f"component-{index:02d}")
        components.append(
            {
                "component_id": component_id,
                "label": str(component.get("label") or f"Flame Reave — {damage_type}"),
                "damage_type": damage_type,
                "damage_tags": component_tags,
                "base": base,
                "base_sources": base_sources,
                "added": added_total,
                "added_damage_scaling": added_scaling,
                "added_scaled": added_scaled,
                "subtotal": subtotal,
                "increased": increased_total,
                "increased_multiplier": inc_multiplier,
                "increased_rows": [_row_summary(row) for row in inc_rows],
                "more_product": more_product,
                "more_groups": more_groups,
                "avg_crit": crit["avg_crit"],
                "crit_chance": crit["crit_chance"],
                "crit_multiplier": crit["crit_multiplier"],
                "crit_detail": crit,
                "penetration": penetration_multiplier,
                "penetration_total": penetration_total,
                "penetration_rows": penetration_rows,
                "target_multiplier": target_multiplier,
                "target_detail": {
                    "resistance_factor": target["resistance_factor"],
                    "monster_dr_factor": target["monster_dr_factor"],
                    "fire_resistance": target["fire_resistance"],
                    "monster_level": target["monster_level"],
                },
                "damage": damage,
                "added_rows": [_row_summary(row) for row in added_rows],
                "excluded_added_rows": excluded_added,
                "formula": (
                    "(base + added*added_damage_scaling) * (1+increased) "
                    "* product(1+more) * avg_crit * (1+penetration) "
                    "* (1-resistance) * (1-monster_dr)"
                ),
            }
        )

    # components defined by the skill but absent from the ruleset are reported
    for damage_type, base_value in base_damage_map.items():
        if base_value and not any(comp["damage_type"] == damage_type for comp in components):
            excluded_components.append(
                {
                    "damage_type": damage_type,
                    "base": base_value,
                    "reason": "component not declared in ruleset.damage_model.components",
                }
            )

    total_damage = sum(float(component["damage"]) for component in components)
    pre_crit = sum(
        float(component["subtotal"])
        * float(component["increased_multiplier"])
        * float(component["more_product"])
        for component in components
    )
    weighted_crit = (
        sum(
            float(component["subtotal"])
            * float(component["increased_multiplier"])
            * float(component["more_product"])
            * float(component["avg_crit"])
            for component in components
        )
        / pre_crit
        if pre_crit
        else 1.0
    )
    totals = {
        "base": sum(float(component["base"]) for component in components),
        "added": sum(float(component["added"]) for component in components),
        "added_scaled": sum(float(component["added_scaled"]) for component in components),
        "subtotal": sum(float(component["subtotal"]) for component in components),
        "increased_total": sum(float(component["increased"]) for component in components),
        "more_product": (
            components[0]["more_product"] if len(components) == 1 else None
        ),
        "avg_crit": weighted_crit,
        "crit_chance": (
            components[0]["crit_chance"] if len(components) == 1 else None
        ),
        "crit_multiplier": (
            components[0]["crit_multiplier"] if len(components) == 1 else None
        ),
        "penetration_multiplier": (
            components[0]["penetration"] if len(components) == 1 else None
        ),
        "target_multiplier": sum(
            float(component["target_multiplier"]) for component in components
        )
        / len(components)
        if components
        else 1.0,
        "damage": total_damage,
        "single_hit_damage": total_damage,
        "component_count": len(components),
        "target": target,
    }

    # -- rule ledger for this call ----------------------------------------
    rules_applied: list[dict[str, Any]] = []
    rule_assumptions: list[str] = []
    for rule in (ruleset_data.get("rules") or []):
        rule_id = str(rule.get("id") or rule.get("rule_id"))
        rules_applied.append(
            {
                "rule_id": rule_id,
                "status": rule.get("status"),
                "statement": rule.get("statement"),
            }
        )
        if rule.get("status") in (model.STATUS_ASSUMED, model.STATUS_UNRESOLVED):
            rule_assumptions.append(
                f"rule {rule_id} ({rule.get('status')}): {rule.get('statement')}"
            )
    assumptions = _dedupe(rule_assumptions + assumptions)

    return {
        "schema": SCHEMA_VERSION,
        "label": f"{skill.get('name') or 'skill'} single hit",
        "skill": {
            "ability_id": skill.get("ability_id"),
            "name": skill.get("name"),
            "tags": list(skill.get("tags") or []),
            "base_damage": list(skill.get("base_damage") or []),
            "added_damage_scaling": added_scaling,
        },
        "states": states,
        "components": components,
        "excluded_components": excluded_components,
        "totals": totals,
        "applied_effects": applied_effects,
        "not_modeled_effects": not_modeled_effects,
        "unresolved_effects": unresolved_effects,
        "excluded_contributions": excluded_contributions,
        "assumptions": assumptions,
        "rules_applied": rules_applied,
    }


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


__all__ = [
    "DAMAGE_TYPE_TAGS",
    "ELEMENTAL_TAGS",
    "SCHEMA_VERSION",
    "compute_single_hit",
    "default_states",
    "evaluate_condition",
    "resolve_states",
    "resolve_target",
    "tags_apply",
]
