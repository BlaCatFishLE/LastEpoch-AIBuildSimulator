"""DoT tick / 窗口 / cast 公共 mechanics（第五步 G4/M7）。

从 plan_executor_v3 抽出的纯函数；executor 与 handler registry 都调用本模块，
不再存在 executor→handler→executor 私有环。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from .. import model
from .tag_match import bh_plan_policy, tags_match

EPS = 1e-9
SAMPLE_STEP_S = 0.25

def _active_rows(rows: list[model.Contribution]) -> list[model.Contribution]:
    return [row for row in rows if row.active]

def _closure(component: Mapping[str, Any]) -> frozenset[str]:
    tags = {str(tag) for tag in component.get("tags") or []}
    tags.add(str(component.get("damage_type") or ""))
    return frozenset(tag for tag in tags if tag)

def _scope_allows(row: model.Contribution, scope: str) -> bool:
    declared = str((row.raw or {}).get("scope") or "")
    return not declared or declared == scope

def _matching(
    rows: list[model.Contribution],
    *,
    closure: frozenset[str],
    scope: str,
    stat_key: str,
    operation: str,
) -> list[model.Contribution]:
    from . import tag_match  # noqa: PLC0415

    policy = tag_match.bh_plan_policy()
    return [
        row
        for row in rows
        if row.active
        and row.stat_key == stat_key
        and row.operation == operation
        and _scope_allows(row, scope)
        and tag_match.tags_match(row.damage_tags, closure, policy)
    ]

def _sum(rows: list[model.Contribution]) -> float:
    return float(sum(float(row.value) for row in rows))

def _more_product(rows: list[model.Contribution]) -> tuple[float, list[dict[str, Any]]]:
    groups: dict[str, float] = {}
    for row in rows:
        key = str(row.group or row.contribution_id)
        groups[key] = groups.get(key, 1.0) * (1.0 + float(row.value))
    product = 1.0
    for value in groups.values():
        product *= value
    return product, [{"group": key, "value": value} for key, value in sorted(groups.items())]

def _attribute_increase(plan: Mapping[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    scaling = (plan.get("parameters") or {}).get("attribute_scaling") or {}
    attribute = str(scaling.get("attribute") or "")
    per_point = float(scaling.get("increased_per_point") or 0.0)
    if not attribute or not per_point:
        return 0.0, []
    attributes = (plan.get("facts") or {}).get("attributes") or {}
    points = float(attributes.get(attribute) or 0.0)
    value = per_point * points
    return value, [
        {
            "contribution_id": f"rule:attribute_scaling:{attribute}",
            "label": f"{attribute} attribute scaling (BH)",
            "value": value,
            "source_kind": "derived",
            "points": points,
            "increased_per_point": per_point,
        }
    ]

def _target_multiplier(resistance: float, penetration: float, damage_reduction: float) -> float:
    return (1.0 - (float(resistance) - float(penetration))) * (1.0 - float(damage_reduction))

def _resistance_totals(rows: list[model.Contribution]) -> dict[str, dict[str, float]]:
    """玩家未封顶抗性（百分比点）；minion / 非玩家来源不计入。"""
    pools = {"fire": 0.0, "cold": 0.0, "lightning": 0.0, "physical": 0.0, "void": 0.0, "necrotic": 0.0, "poison": 0.0, "elemental": 0.0, "all": 0.0}
    for row in _active_rows(rows):
        if row.stat_key != model.STAT_RESISTANCE or row.operation != "added":
            continue
        if "Minion" in {str(tag) for tag in row.damage_tags or []}:
            continue
        target = str(row.target or "").lower()
        if target in pools:
            pools[target] += float(row.value)
    totals: dict[str, dict[str, float]] = {}
    for name in ("fire", "cold", "lightning", "physical", "void", "necrotic", "poison"):
        fraction = pools[name] + pools["all"] + (pools["elemental"] if name in ("fire", "cold", "lightning") else 0.0)
        totals[name] = {
            "specific_percent": round(pools[name] * 100.0, 9),
            "elemental_percent": round(pools["elemental"] * 100.0, 9),
            "all_percent": round(pools["all"] * 100.0, 9),
            "total_percent": round(fraction * 100.0, 9),
            "overcapped_percent": round(max(0.0, fraction * 100.0 - 75.0), 9),
        }
    return totals

def dot_component_result(
    plan: Mapping[str, Any],
    component: Mapping[str, Any],
    rows: list[model.Contribution],
    target: Mapping[str, Any],
    monster_damage_reduction: float,
) -> dict[str, Any]:
    closure = _closure(component)
    scope = str(component.get("scope") or "body")
    inc_rows = _matching(rows, closure=closure, scope=scope, stat_key="damage", operation="increased")
    attribute_increase, attribute_rows = _attribute_increase(plan)
    added = _sum(_matching(rows, closure=closure, scope=scope, stat_key="added_damage", operation="added"))
    more_rows = _matching(rows, closure=closure, scope=scope, stat_key="damage", operation="more")
    more_product, more_groups = _more_product(more_rows)
    penetration = _sum(_matching(rows, closure=closure, scope=scope, stat_key="penetration", operation="added"))
    base = float(component.get("base_damage") or 0.0)
    scaling = float(component.get("added_damage_scaling") or 0.0)
    duration = float(component.get("duration_s") or 0.0)
    interval = float(component.get("tick_interval_s") or 0.0)
    tick_count = int(math.ceil(duration / interval - 1e-9))
    increased = _sum(inc_rows) + attribute_increase
    per_tick = (base + added * scaling) * (1.0 + increased) * more_product
    resistance = float(target.get("resistance") or 0.0)
    multiplier = _target_multiplier(resistance, penetration, monster_damage_reduction)
    per_tick *= multiplier
    return {
        "component_id": str(component.get("component_id")),
        "kind": "dot",
        "damage_type": str(component.get("damage_type")),
        "tags": list(component.get("tags") or []),
        "scope": scope,
        "closure": sorted(closure),
        "duration_s": duration,
        "tick_interval_s": interval,
        "tick_count": tick_count,
        "damage_at_start": bool(component.get("damage_at_start")),
        "remainder_policy": str(component.get("remainder_policy")),
        "base_damage": base,
        "added_damage_scaling": scaling,
        "added_damage_total": added,
        "per_tick_base": base + added * scaling,
        "increased_total": increased,
        "increased_rows": [
            {"contribution_id": row.contribution_id, "value": float(row.value), "damage_tags": list(row.damage_tags or [])}
            for row in inc_rows
        ]
        + attribute_rows,
        "more_product": more_product,
        "more_groups": more_groups,
        "penetration_total": penetration,
        "target": {
            "damage_type": str(component.get("damage_type")),
            "resistance": resistance,
            "penetration": penetration,
            "monster_damage_reduction": monster_damage_reduction,
            "multiplier": multiplier,
        },
        "per_tick_damage": per_tick,
        "damage_total": per_tick * tick_count,
        "ticks": [
            {"offset_s": index * interval, "damage": per_tick, "coverage_fraction": 1.0}
            for index in range(tick_count)
        ],
    }

def cohort_between(cohort: Mapping[str, Any], start_s: float, end_s: float) -> float:
    cohort_start = float(cohort["time_s"])
    cohort_end = float(cohort["end_time_s"])
    overlap = min(end_s, cohort_end) - max(start_s, cohort_start)
    if overlap <= EPS:
        return 0.0
    duration = cohort_end - cohort_start
    return float(cohort["damage_total"]) * (overlap / duration) if duration > EPS else 0.0

def max_concurrent(casts: list[Mapping[str, Any]], horizon_s: float) -> int:
    maximum = 0
    samples = int(math.floor(horizon_s / SAMPLE_STEP_S)) + 1
    for index in range(samples):
        t = index * SAMPLE_STEP_S
        count = sum(
            1
            for cast in casts
            if cast["black_hole_spawn_s"] <= t + EPS and cast["black_hole_end_s"] >= t - EPS
        )
        maximum = max(maximum, count)
    return maximum

def make_cast(
    *, cast_id: str, cast_start_s: float, cast_time_s: float, duration_s: float,
    charges_before: float, charges_after: float,
) -> dict[str, Any]:
    spawn_s = cast_start_s + cast_time_s
    return {
        "cast_id": cast_id,
        "cast_start_s": cast_start_s,
        "cast_time_s": cast_time_s,
        "black_hole_spawn_s": spawn_s,
        "black_hole_end_s": spawn_s + duration_s,
        "black_hole_duration_s": duration_s,
        "charges_before": charges_before,
        "charges_after": charges_after,
        "mana": "unlimited",
    }

def window_metrics(
    *,
    casts: list[Mapping[str, Any]],
    component_results: list[Mapping[str, Any]],
    cohorts: list[Mapping[str, Any]],
    window_s: float,
) -> dict[str, Any]:
    body = 0.0
    for cast in casts:
        spawn = float(cast["black_hole_spawn_s"])
        for component in component_results:
            for tick in component["ticks"]:
                if spawn + float(tick["offset_s"]) <= window_s + EPS:
                    body += float(tick["damage"])
    ignite = 0.0
    tail = 0.0
    for cohort in cohorts:
        if float(cohort["time_s"]) > window_s + EPS:
            continue
        in_window = cohort_between(cohort, 0.0, window_s)
        ignite += in_window
        tail += max(0.0, float(cohort["damage_total"]) - in_window)
    cast_count = sum(1 for cast in casts if float(cast["cast_start_s"]) <= window_s + EPS)
    return {
        "window_s": window_s,
        "total_damage": body + ignite,
        "body_damage": body,
        "ignite_damage": ignite,
        "dps": (body + ignite) / window_s if window_s > 0 else 0.0,
        "unsettled_tail_damage": tail,
        "cast_count": cast_count,
    }


_dot_component_result = dot_component_result
_ignite_between = cohort_between
_max_concurrent = max_concurrent
_make_cast = make_cast
_window_metrics = window_metrics
__all__ = ["dot_component_result","cohort_between","max_concurrent","make_cast","window_metrics",]
