"""Attack rate, hit rate and DPS windows for the Flame Reave hit model.

Owner: fr-engine (task-2).  Consumers: ``hit_report``/``cli``.

Model (DESIGN §2 R2/R3 + §5.1)::

    attack interval = 1 / final_aps
    final_aps       = base_aps * (1 + sum(increased attack speed))
                      * product(1 + more attack speed groups)
    base_aps        = ruleset.attack_model.base_rate_source
                      ("weapon_average_attack_rate" default; the planner
                      use-duration formula is kept as an alternative)
    casts/second    = 1 / max(attack_interval, effective_cooldown)   (cooldown policy)
                    = final_aps                                      (attack-speed policy)
    hits/second     = casts/second * hits_per_cast
    window damage   = window_s * hits/second * single_hit_damage
    window dps      = window damage / window_s

The skill cooldown comes from the selected Flame Reave tree node 16 (Reflash,
3s).  Because it can gate the output much harder than attack speed, the result
always reports both policies: the configured primary one and the alternates.
The single-hit states come from the scenario ``single_hit`` layer, the rate
states from the ``single_hit`` + ``dps`` layers (see
:func:`le_calc.hit_damage.resolve_states`).

Entry point::

    simulate(snapshot, ruleset, scenario=None) -> dict
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import model
from .hit_damage import (
    collect_rows,
    compute_single_hit,
    resolve_states,
    resolve_target,
    tags_apply,
)
from .mechanics import frequency as shared_frequency
from .mechanics import increased_aggregate as shared_increased_aggregate
from .mechanics import more_groups as shared_more_groups

SCHEMA_VERSION = "le-hit-simulation-v1"

DEFAULT_WINDOWS_S = (5.0, 30.0, 120.0)
DEFAULT_ATTACK_SPEED_FORMULA = (
    "final_aps = base_aps * (1 + sum(increased_attack_speed)) "
    "* product(1 + more_attack_speed groups)"
)
STAT_ATTACK_SPEED = model.STAT_ATTACK_SPEED


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _skill_hit_tags(snapshot: model.Snapshot) -> list[str]:
    derived = snapshot.derived or {}
    skill = derived.get("skill") or {}
    tags = list(skill.get("tags") or [])
    if "Hit" not in tags:
        tags.append("Hit")
    if "Melee" not in tags and "Melee" in (skill.get("tags") or []):
        pass
    return tags


def _base_attack_rate(
    snapshot: model.Snapshot,
    ruleset: Any,
) -> dict[str, Any]:
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    attack_model = dict(ruleset_data.get("attack_model") or {})
    derived = snapshot.derived or {}
    skill = derived.get("skill") or {}
    weapon = derived.get("weapon") or {}
    source = str(attack_model.get("base_rate_source") or "weapon_average_attack_rate")
    weapon_rate = _num(weapon.get("average_attack_rate"))
    use_duration = _num(skill.get("use_duration_s"))
    speed_multiplier = _num(skill.get("speed_multiplier"), 1.0) or 1.0
    planner_rate = _num(skill.get("attack_speed_base_planner"))
    if not planner_rate and use_duration:
        # planner reference formula: 0.75/useDuration * speedMultiplier * 1.46667
        planner_rate = 0.75 / use_duration * speed_multiplier * 1.46667
    if source == "skill_use_duration_planner":
        base = planner_rate
        chosen_source = "planner use-duration formula"
    else:
        if weapon_rate:
            base = weapon_rate
            chosen_source = "average weapon attackRate"
        elif planner_rate:
            base = planner_rate
            chosen_source = "planner use-duration formula (fallback: no weapon rate)"
        else:
            base = 1.0
            chosen_source = "fallback 1.0 (no base rate available)"
    return {
        "base_aps": base,
        "base_rate_source": source,
        "source": chosen_source,
        "weapon_average_attack_rate": weapon_rate,
        "planner_use_duration_rate": planner_rate,
        "alternatives": [
            {"source": "weapon_average_attack_rate", "value": weapon_rate},
            {"source": "skill_use_duration_planner", "value": planner_rate},
        ],
        "formula": DEFAULT_ATTACK_SPEED_FORMULA,
    }


def _attack_speed_pools(
    rows: list[dict[str, Any]],
    hit_tags: list[str],
) -> dict[str, Any]:
    increased_rows = [
        row
        for row in rows
        if row["stat_key"] == STAT_ATTACK_SPEED
        and row["operation"] == "increased"
        and tags_apply(row["damage_tags"], hit_tags)
    ]
    more_rows = [
        row
        for row in rows
        if row["stat_key"] == STAT_ATTACK_SPEED
        and row["operation"] == "more"
        and tags_apply(row["damage_tags"], hit_tags)
    ]
    increased = shared_increased_aggregate.aggregate(
        [row["value"] for row in increased_rows]
    ).total
    aggregate = shared_more_groups.aggregate(shared_more_groups.rows_from_hit_rows(more_rows))
    more_product = aggregate.product
    ordered_groups = [
        {
            "group": group.group,
            "value": group.value,
            "rows": [more_rows[index]["contribution_id"] for index in group.row_indices],
            "multiplier": group.factor,
        }
        for group in aggregate.groups
    ]
    return {
        "increased_attack_speed": increased,
        "more_attack_speed": more_product,
        "more_groups": ordered_groups,
        "increased_rows": [
            {
                "contribution_id": row["contribution_id"],
                "source_kind": row.get("source_kind"),
                "value": row["value"],
                "damage_tags": list(row.get("damage_tags") or []),
                "origin": row.get("origin"),
                "condition_code": row.get("condition_code"),
            }
            for row in increased_rows
        ],
        "more_rows": [
            {
                "contribution_id": row["contribution_id"],
                "source_kind": row.get("source_kind"),
                "value": row["value"],
                "damage_tags": list(row.get("damage_tags") or []),
                "origin": row.get("origin"),
                "condition_code": row.get("condition_code"),
            }
            for row in more_rows
        ],
    }


def _cooldown_recovery_total(rows: list[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
    total = 0.0
    used: list[dict[str, Any]] = []
    for row in rows:
        if row["stat_key"] != model.STAT_COOLDOWN_RECOVERY:
            continue
        if row["operation"] != "increased":
            continue
        total += row["value"]
        used.append(
            {
                "contribution_id": row["contribution_id"],
                "value": row["value"],
                "condition_code": row.get("condition_code"),
            }
        )
    return total, used


def _scenario_rotation(scenario: Optional[dict[str, Any]]) -> dict[str, Any]:
    dps = (scenario or {}).get("dps") or {}
    rotation = dps.get("rotation") or {}
    return dict(rotation) if isinstance(rotation, dict) else {}


def _scenario_windows(scenario: Optional[dict[str, Any]]) -> list[float]:
    dps = (scenario or {}).get("dps") or {}
    windows = dps.get("windows_s") or []
    return [float(value) for value in windows if _num(value) > 0]


def _rotation_config(
    snapshot: model.Snapshot,
    ruleset: Any,
    scenario: Optional[dict[str, Any]],
) -> dict[str, Any]:
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    rotation = dict(ruleset_data.get("rotation") or {})
    skill = (snapshot.derived or {}).get("skill") or {}
    scenario_rotation = _scenario_rotation(scenario)
    policy = str(rotation.get("policy") or "auto")
    cooldown_s = rotation.get("cooldown_s")
    if cooldown_s is None:
        cooldown_s = skill.get("cooldown_s")
    cooldown_s = _num(cooldown_s)
    formula = str(rotation.get("cooldown_formula") or "(base + flat) / (1 + increased cooldown recovery speed)")
    return {
        "policy": policy,
        "cooldown_s": cooldown_s,
        "cooldown_formula": formula,
        "rotation_notes": rotation.get("notes"),
        "scenario_rotation": scenario_rotation,
        "other_skills_per_second": _num(scenario_rotation.get("other_skills_per_second")),
    }


def _policy_block(
    *,
    policy_id: str,
    label: str,
    attack_interval: float,
    effective_cooldown: Optional[float],
    hits_per_cast: int,
    single_hit_damage: float,
    windows: list[float],
    damage_weights: Optional[list[float]] = None,
    rotation_more: float = 1.0,
) -> dict[str, Any]:
    cast = shared_frequency.cast_policy(
        attack_interval_s=attack_interval,
        effective_cooldown_s=effective_cooldown,
    )
    casts_per_second = cast.casts_per_second
    binding = cast.binding_constraint
    hits_per_second = casts_per_second * hits_per_cast
    weights, damage_factor_per_cast = shared_frequency.damage_weights_factor(
        damage_weights, hits_per_cast
    )
    # Rhythm-of-Fire rotation average is a state-level multiplier over the cycle.
    damage_per_cast = single_hit_damage * damage_factor_per_cast * rotation_more
    damage_per_second = casts_per_second * damage_per_cast
    # Shared window skeleton (casts/hits/damage/dps); the keys/order match the
    # historical window rows bit-for-bit.
    window_rows = [
        shared_frequency.window_skeleton(
            window_s=window_s,
            casts_per_second=casts_per_second,
            hits_per_cast=hits_per_cast,
            single_hit_damage=single_hit_damage,
            damage_weights=weights,
            rotation_more_factor=rotation_more,
        )
        for window_s in windows
    ]
    return {
        "id": policy_id,
        "label": label,
        "casts_per_second": casts_per_second,
        "hits_per_second": hits_per_second,
        "damage_per_second": damage_per_second,
        "damage_weights": weights,
        "effective_hits_per_cast": damage_factor_per_cast,
        "rotation_more_factor": rotation_more,
        "damage_per_cast": damage_per_cast,
        "binding_constraint": binding,
        "effective_cooldown_s": effective_cooldown,
        "windows": window_rows,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def simulate(
    snapshot: model.Snapshot,
    ruleset: Any,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Compute attack rate, per-cast hits and 5/30/120s DPS windows."""
    ruleset_data = getattr(ruleset, "data", ruleset) or {}
    attack_model = dict(ruleset_data.get("attack_model") or {})
    hit_events = dict(ruleset_data.get("hit_events") or {})
    states = resolve_states(ruleset, scenario, section="simulate", snapshot=snapshot)
    target = resolve_target(ruleset, scenario)
    single_hit = compute_single_hit(snapshot, ruleset, scenario)
    single_hit_damage = _num((single_hit.get("totals") or {}).get("damage"))

    derived = snapshot.derived or {}
    hit_count = dict(derived.get("hit_count") or {})
    hits_per_cast = int(hit_count.get("per_cast") or hit_events.get("hits_per_cast") or 1)
    if hits_per_cast <= 0:
        hits_per_cast = 1
    hits_per_cast_block = {
        "per_cast": hits_per_cast,
        "returns": int(hit_count.get("returns") or max(0, hits_per_cast - 1)),
        "sources": list(hit_count.get("sources") or []),
        "status": hit_count.get("status") or model.STATUS_ASSUMED,
        "label": hit_count.get("label") or "每次施放对同一目标的命中次数",
        "note": hit_count.get("note"),
    }
    # Per-hit damage weights: Reflash's two additional hits have a fixed 50% less
    # damage penalty (node 16 altText); total = 2.0 first-hit equivalents per cast.
    damage_weights = [
        float(value)
        for value in (
            hit_events.get("damage_weights")
            or hit_count.get("damage_weights")
            or []
        )
    ]
    # optional scenario override: the condition-buff panel can set hits/weights.
    hit_override = dict((scenario or {}).get("hit_override") or {})
    override_applied = False
    if hit_override:
        override_hits = int(hit_override.get("hits_per_cast") or hits_per_cast)
        override_weights = [
            float(value) for value in (hit_override.get("damage_weights") or [])
        ]
        if override_hits > 0:
            hits_per_cast = override_hits
            hits_per_cast_block["per_cast"] = override_hits
            hits_per_cast_block["returns"] = max(0, override_hits - 1)
            override_applied = True
        if override_weights and len(override_weights) == hits_per_cast:
            damage_weights = override_weights
        elif override_applied:
            damage_weights = [1.0] + [0.5] * (hits_per_cast - 1)
    if len(damage_weights) != hits_per_cast or not damage_weights:
        damage_weights = [1.0] * hits_per_cast
    if override_applied:
        hits_per_cast_block["override"] = {
            "source": "scenario.hit_override",
            "hits_per_cast": hits_per_cast,
            "damage_weights": damage_weights,
        }
    hits_per_cast_block["damage_weights"] = damage_weights
    hits_per_cast_block["effective_hits_per_cast"] = sum(damage_weights)
    # Rhythm of Fire rotation average (FR-only: one consume per 4 casts).
    rhythm_cfg = dict(((ruleset_data.get("rotation") or {}).get("rhythm_of_fire") or {}))
    rotation_more = float(rhythm_cfg.get("average_more") or 1.0)
    if rotation_more <= 0:
        rotation_more = 1.0

    collected = collect_rows(snapshot, ruleset, states)
    rows = collected["rows"]
    hit_tags = _skill_hit_tags(snapshot)
    base_rate = _base_attack_rate(snapshot, ruleset)
    pools = _attack_speed_pools(rows, hit_tags)
    final_aps = shared_frequency.final_attack_rate(
        base_rate["base_aps"],
        increased_attack_speed=pools["increased_attack_speed"],
        more_product=pools["more_attack_speed"],
    )
    attack_interval = shared_frequency.attack_interval(final_aps)

    rotation = _rotation_config(snapshot, ruleset, scenario)
    icr_total, icr_rows = _cooldown_recovery_total(rows)
    cooldown_s = float(rotation["cooldown_s"] or 0.0)
    effective_cooldown = shared_frequency.cooldown_limited_rate(
        cooldown_s, increased_cooldown_recovery_speed=icr_total
    )
    windows = _scenario_windows(scenario)
    if not windows:
        windows = [_num(value) for value in (ruleset_data.get("rotation") or {}).get("windows_s") or DEFAULT_WINDOWS_S]
    windows = [value for value in windows if value > 0]
    if not windows:
        windows = list(DEFAULT_WINDOWS_S)

    policy_id = str(rotation["policy"])
    cooldown_in_play = cooldown_s > 0
    if policy_id in ("auto", "cooldown_limited", "cooldown"):
        primary_uses_cooldown = cooldown_in_play
        primary_label = (
            "cooldown-limited Flame Reave casts (Reflash cooldown gates the rate; assumed)"
            if cooldown_in_play
            else "attack-speed-limited Flame Reave casts"
        )
        primary_id = "cooldown_limited" if cooldown_in_play else "attack_speed_limited"
    elif policy_id == "attack_speed_limited":
        primary_uses_cooldown = False
        primary_label = "attack-speed-limited Flame Reave casts (cooldown ignored; assumed)"
        primary_id = "attack_speed_limited"
    else:  # pragma: no cover - guarded by ruleset validation
        raise ValueError(f"unsupported rotation policy: {policy_id!r}")

    primary = _policy_block(
        policy_id=primary_id,
        label=primary_label,
        attack_interval=attack_interval,
        effective_cooldown=effective_cooldown if primary_uses_cooldown else None,
        hits_per_cast=hits_per_cast,
        single_hit_damage=single_hit_damage,
        windows=windows,
        damage_weights=damage_weights,
        rotation_more=rotation_more,
    )
    alternate = _policy_block(
        policy_id="attack_speed_limited" if primary_uses_cooldown else "cooldown_limited",
        label=(
            "alternate: attack-speed-limited casts (cooldown ignored)"
            if primary_uses_cooldown
            else "alternate: cooldown-limited casts"
        ),
        attack_interval=attack_interval,
        effective_cooldown=None if primary_uses_cooldown else effective_cooldown,
        hits_per_cast=hits_per_cast,
        single_hit_damage=single_hit_damage,
        windows=windows,
        damage_weights=damage_weights,
        rotation_more=rotation_more,
    )

    mana = dict(derived.get("mana") or {})
    resource_note = (
        f"mana is not simulated (infinite mana assumption): Flame Reave estimated cost "
        f"{_num(mana.get('skill_mana_cost_estimate')):g}/cast "
        f"({_num(mana.get('skill_mana_cost_base')):g} base + "
        f"{_num(mana.get('skill_mana_cost_flat_added')):g} flat, "
        f"increased {_num(mana.get('skill_mana_cost_increased')):g}); "
        f"max mana estimate {_num(mana.get('mana_max_estimate')):g} "
        f"(<300 -> mana_ge_300 bonus inactive). Blade Weaver stack gain/consume is "
        "represented by scenario states only."
    )

    assumptions = _dedupe(
        list(single_hit.get("assumptions") or [])
        + [
            "attack rate model: {}".format(
                "average weapon attackRate x (1+increased) x product(1+more)"
                if base_rate["base_rate_source"] != "skill_use_duration_planner"
                else "planner use-duration base rate x speed modifiers"
            ),
            "cooldown policy: Reflash (node 16) adds a 3s cooldown; both "
            "cooldown-limited and attack-speed-limited outputs are reported and "
            "the primary one is declared in ruleset.rotation.policy (assumed).",
            "windows use the expected (fractional) number of casts; complete_casts "
            "is reported for transparency. No ramp-up, movement or other-skill "
            "fillers are simulated.",
            "damage weights: Reflash's two additional hits have a fixed 50% less "
            "damage penalty (node 16 altText); per-cast damage = first-hit damage x "
            f"{sum(damage_weights):g} (weights {damage_weights!r}).",
            "Rhythm of Fire rotation average: FR-only steady cycle spends one of "
            f"every {int(rhythm_cfg.get('cycle_casts') or 4)} casts on a consume hit "
            f"(+130% more) -> x{rotation_more:g} average more (assumed; other-skill "
            "stack acceleration and exact stack timing are not simulated).",
        ]
        + list(collected.get("assumptions") or [])
    )

    return {
        "schema": SCHEMA_VERSION,
        "hits_per_cast": hits_per_cast_block,
        "attack_rate": {
            **base_rate,
            "increased_attack_speed": pools["increased_attack_speed"],
            "more_attack_speed": pools["more_attack_speed"],
            "final_aps": final_aps,
            "attack_interval_s": attack_interval,
            "hits_per_second": final_aps * hits_per_cast,
            "more_groups": pools["more_groups"],
            "increased_rows": pools["increased_rows"],
            "more_rows": pools["more_rows"],
        },
        "policy": {
            "id": primary["id"],
            "label": primary["label"],
            "casts_per_second": primary["casts_per_second"],
            "hits_per_second": primary["hits_per_second"],
            "damage_per_second": primary["damage_per_second"],
            "damage_weights": primary["damage_weights"],
            "effective_hits_per_cast": primary["effective_hits_per_cast"],
            "rotation_more_factor": primary["rotation_more_factor"],
            "damage_per_cast": primary["damage_per_cast"],
            "binding_constraint": primary["binding_constraint"],
            "cooldown_s": cooldown_s,
            "effective_cooldown_s": effective_cooldown,
            "increased_cooldown_recovery_speed": icr_total,
            "cooldown_rows": icr_rows,
            "cooldown_formula": rotation["cooldown_formula"],
            "rotation_notes": rotation["rotation_notes"],
            "scenario_rotation": rotation["scenario_rotation"],
        },
        "effective_casts_per_second": primary["casts_per_second"],
        "effective_hits_per_second": primary["hits_per_second"],
        "damage_per_second": primary["damage_per_second"],
        "windows": primary["windows"],
        "alternate_policy": {
            "id": alternate["id"],
            "label": alternate["label"],
            "casts_per_second": alternate["casts_per_second"],
            "hits_per_second": alternate["hits_per_second"],
            "damage_per_second": alternate["damage_per_second"],
            "damage_weights": alternate["damage_weights"],
            "effective_hits_per_cast": alternate["effective_hits_per_cast"],
            "rotation_more_factor": alternate["rotation_more_factor"],
            "damage_per_cast": alternate["damage_per_cast"],
            "binding_constraint": alternate["binding_constraint"],
            "windows": alternate["windows"],
        },
        "single_hit": {
            "damage": single_hit_damage,
            "component_count": len(single_hit.get("components") or []),
            "totals": single_hit.get("totals"),
        },
        "states": states,
        "target": target,
        "resource_note": resource_note,
        "assumptions": assumptions,
        "rules_applied": list(single_hit.get("rules_applied") or []),
    }


__all__ = [
    "DEFAULT_ATTACK_SPEED_FORMULA",
    "DEFAULT_WINDOWS_S",
    "SCHEMA_VERSION",
    "simulate",
]
