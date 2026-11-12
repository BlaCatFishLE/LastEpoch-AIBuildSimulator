"""Application cohort / core 公共 mechanics（BH 显式配方兼容，G4/M7）。"""
from __future__ import annotations

from typing import Any, Mapping

from .. import model
from .dot import (
    EPS,
    _active_rows,
    _attribute_increase,
    _matching,
    _more_product,
    _resistance_totals,
    _scope_allows,
    _sum,
    _target_multiplier,
    cohort_between,
    dot_component_result,
)

def application_core(
    plan: Mapping[str, Any],
    rows: list[model.Contribution],
    target: Mapping[str, Any],
    monster_damage_reduction: float,
) -> dict[str, Any]:
    spec = plan["schedule"]["ignite"]
    closure = frozenset(str(tag) for tag in spec.get("scaling_tags") or [])
    duration_rows: list[dict[str, Any]] = []
    duration_increased = 0.0
    for row in _active_rows(rows):
        if row.stat_key != "ailment_duration" or row.operation != "increased":
            continue
        if not _scope_allows(row, "ignite"):
            continue
        target_key = str(row.target or "").lower()
        if target_key and target_key not in ("ignite", "all"):
            continue
        duration_increased += float(row.value)
        duration_rows.append(
            {"contribution_id": row.contribution_id, "value": float(row.value), "target": row.target}
        )
    duration_multiplier = 1.0 + duration_increased
    duration_s = float(spec["base_duration_s"]) * duration_multiplier
    inc_rows = _matching(rows, closure=closure, scope="ignite", stat_key="damage", operation="increased")
    attribute_increase, attribute_rows = _attribute_increase(plan)
    more_rows = _matching(rows, closure=closure, scope="ignite", stat_key="damage", operation="more")
    more_product, more_groups = _more_product(more_rows)
    penetration = _sum(_matching(rows, closure=closure, scope="ignite", stat_key="penetration", operation="added"))
    pen_with_ignite_rows = [
        row
        for row in _active_rows(rows)
        if row.stat_key == "penetration_with_ignite" and row.operation == "added" and _scope_allows(row, "ignite")
    ]
    pen_with_ignite = _sum(pen_with_ignite_rows)
    penetration += pen_with_ignite
    resistance = float(target.get("resistance") or 0.0)
    multiplier = _target_multiplier(resistance, penetration, monster_damage_reduction)
    increased = _sum(inc_rows) + attribute_increase
    per_stack = float(spec["base_damage"]) * duration_multiplier * (1.0 + increased) * more_product * multiplier
    return {
        "ailment": "Ignite",
        "aliases": ["ignite", "1"],
        "base_damage": float(spec["base_damage"]),
        "base_duration_s": float(spec["base_duration_s"]),
        "duration_increased_total": duration_increased,
        "duration_multiplier": duration_multiplier,
        "duration_s": duration_s,
        "duration_rows": duration_rows,
        "scaling_tags": sorted(closure),
        "increased_total": increased,
        "increased_rows": [
            {"contribution_id": row.contribution_id, "value": float(row.value), "damage_tags": list(row.damage_tags or [])}
            for row in inc_rows
        ]
        + attribute_rows,
        "more_product": more_product,
        "more_groups": more_groups,
        "penetration_total": penetration,
        "penetration_rows": [
            {"contribution_id": row.contribution_id, "value": float(row.value), "damage_tags": list(row.damage_tags or [])}
            for row in _matching(rows, closure=closure, scope="ignite", stat_key="penetration", operation="added")
        ]
        + [{"contribution_id": row.contribution_id, "value": float(row.value), "damage_tags": list(row.damage_tags or [])} for row in pen_with_ignite_rows],
        "penetration_with_ignite": pen_with_ignite,
        "target": {
            "resistance": resistance,
            "penetration": penetration,
            "monster_damage_reduction": monster_damage_reduction,
            "multiplier": multiplier,
        },
        "per_stack_damage": per_stack,
    }

def application_cohort(
    plan: Mapping[str, Any],
    core: Mapping[str, Any],
    rows: list[model.Contribution],
    spawn_s: float,
    black_hole_duration_s: float,
) -> dict[str, Any]:
    spec = plan["schedule"]["ignite"]
    resistances = _resistance_totals(rows)
    uncapped_fire = float(resistances["fire"]["total_percent"])
    chance_total = float(spec.get("chance_total") or 0.0)
    rolls_per_second = float(spec["rolls_per_second"])
    interval = 1.0 / rolls_per_second
    stacks_per_second = chance_total * uncapped_fire
    stacks_per_roll = stacks_per_second * interval
    roll_times: list[float] = []
    offset = 0.0
    while offset < black_hole_duration_s - EPS:
        roll_times.append(spawn_s + offset)
        offset += interval
    duration_s = float(core["duration_s"])
    per_stack = float(core["per_stack_damage"])
    cohorts: list[dict[str, Any]] = []
    for index, roll_time in enumerate(roll_times):
        cohorts.append(
            {
                "roll_index": index,
                "time_s": roll_time,
                "roll_offset_s": roll_time - spawn_s,
                "stacks": stacks_per_roll,
                "duration_s": duration_s,
                "damage_total": stacks_per_roll * per_stack,
                "end_time_s": roll_time + duration_s,
            }
        )
    return {
        "uncapped_fire_resistance_percent": uncapped_fire,
        "chance_total": chance_total,
        "rolls_per_second": rolls_per_second,
        "roll_interval_s": interval,
        "stacks_per_second": stacks_per_second,
        "stacks_per_roll": stacks_per_roll,
        "roll_times_s": roll_times,
        "cohorts": cohorts,
        "totals": {
            "damage": sum(cohort["damage_total"] for cohort in cohorts),
            "last_ailment_expiry_s": max((cohort["end_time_s"] for cohort in cohorts), default=spawn_s),
            "expected_total_stacks_applied": stacks_per_roll * len(cohorts),
        },
        "resistance_totals": resistances,
    }


def single_cast(
    *,
    plan: Mapping[str, Any],
    rows: list[model.Contribution],
    target: Mapping[str, Any],
    monster_damage_reduction: float,
    cast_time_s: float,
    duration_s: float,
    charge_parameters: Mapping[str, Any],
) -> dict[str, Any]:
    spawn_s = cast_time_s
    end_s = spawn_s + duration_s
    component_results = [
        dot_component_result(plan, component, rows, target, monster_damage_reduction)
        for component in plan["components"]
    ]
    core = _ignite_core(plan, rows, target, monster_damage_reduction)
    ignition = _ignite_application(plan, core, rows, spawn_s, duration_s)
    body_total = sum(float(component["damage_total"]) for component in component_results)
    ignite_total = float(ignition["totals"]["damage"])
    ignite_before = sum(cohort_between(cohort, spawn_s, end_s) for cohort in ignition["cohorts"])
    last_expiry = float(ignition["totals"]["last_ailment_expiry_s"])
    observation_end = max(end_s, last_expiry)
    return {
        "cast": {
            "cast_id": "cast-1",
            "cast_start_s": 0.0,
            "cast_time_s": cast_time_s,
            "black_hole_spawn_s": spawn_s,
            "black_hole_end_s": end_s,
            "black_hole_duration_s": duration_s,
            "charges_before": float(charge_parameters["max_charges"]),
            "charges_after": float(charge_parameters["max_charges"]) - 1.0,
            "mana": "unlimited",
        },
        "components": component_results,
        "ignite": {
            "core": core,
            "application": ignition,
        },
        "ignition": ignition,
        "totals": {
            "single_cast_total": body_total + ignite_total,
            "body_total": body_total,
            "ignite_total": ignite_total,
            "ignite_damage_before_black_hole_end": ignite_before,
            "ignite_tail_damage": ignite_total - ignite_before,
            "window_damage_until_black_hole_end": body_total + ignite_before,
            "observation_end_s": observation_end,
            "last_ailment_expiry_s": last_expiry,
            "expected_total_stacks_applied": float(ignition["totals"]["expected_total_stacks_applied"]),
        },
    }
_ignite_core = application_core
_ignite_application = application_cohort
__all__ = ["application_core", "application_cohort", "single_cast"]
