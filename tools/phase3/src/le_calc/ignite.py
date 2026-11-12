"""Ignite model for the Black Hole stage-1 calculator (v1).

Two independent parts (validated separately per the execution plan B1/B2):
  * single-stack damage core (base damage, duration multiplier, inc/more pools)
  * application model (Heat Wave: once-per-second roll based on uncapped fire
    resistance; expected-value stacks, no cap).

The model is analytic/expected-value.  It is only used because every roll is
independent, stacks add linearly and there is no state feedback (plan B4).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from . import model
from .damage import (
    IGNITE_TAGS,
    ModifierRow,
    _row,
    collect_modifier_rows,
    group_more_rows,
    player_contributions,
    tags_match,
)
from .mechanics import increased_aggregate as shared_increased_aggregate
from .mechanics import target_side as shared_target_side
from .ruleset import Ruleset

EPS = 1e-9


@dataclass
class IgniteCohort:
    """One Heat Wave roll: `stacks` expected stacks applied at `time`."""

    roll_index: int
    time_s: float
    stacks: float
    duration_s: float
    damage_total: float
    end_time_s: float

    @property
    def dps(self) -> float:
        return self.damage_total / self.duration_s if self.duration_s > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "roll_index": self.roll_index,
            "time_s": self.time_s,
            "stacks": self.stacks,
            "duration_s": self.duration_s,
            "dps": self.dps,
            "damage_total": self.damage_total,
            "end_time_s": self.end_time_s,
        }


def _ignite_duration(
    snapshot: model.Snapshot, ruleset: Ruleset
) -> dict[str, Any]:
    base = float(ruleset.ignite["base_duration_s"])
    rows: list[dict[str, Any]] = []
    for c in player_contributions(snapshot):
        if c.stat_key != model.STAT_AILMENT_DURATION:
            continue
        if c.operation not in ("increased", "added"):
            continue
        target = (c.target or "").lower()
        if target and target not in ("ignite", "all"):
            continue
        rows.append(
            {
                "contribution_id": c.contribution_id,
                "label": c.stat_name,
                "value": float(c.value),
                "source_kind": c.source_kind,
                "source_path": c.source_path,
                "status": c.status,
            }
        )
    increased = sum(r["value"] for r in rows)
    return {
        "base_duration_s": base,
        "increased_total": increased,
        "duration_multiplier": 1.0 + increased,
        "duration_s": base * (1.0 + increased),
        "rows": rows,
    }


def _ignite_scaling(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    inc_rows = [_row(c) for c in collect_modifier_rows(snapshot, model.STAT_DAMAGE, IGNITE_TAGS, ("increased",))]
    attribute = ruleset.black_hole.get("attribute_scaling", {})
    attribute_key = attribute.get("attribute", "intelligence")
    per_point = float(attribute.get("increased_per_point", 0.0))
    attribute_increase = per_point * snapshot.attribute(attribute_key)
    attribute_row = {
        "contribution_id": "ruleset:attribute:intelligence",
        "label": "Intelligence attribute scaling (accepted into Ignite pool per reference model)",
        "value": attribute_increase,
        "source_kind": "derived",
        "source_path": "bh2.attributeScaling[2]",
        "status": "assumed",
        "damage_tags": [],
    }
    # Shared additive pool (sum of rows + INT attribute scaling appended last).
    increased_pool = shared_increased_aggregate.aggregate(
        [row.value for row in inc_rows], attribute_increase=attribute_increase
    )
    inc_total = increased_pool.total

    player_more = [_row(c) for c in collect_modifier_rows(snapshot, model.STAT_DAMAGE, IGNITE_TAGS, ("more",))]
    inheritance = ruleset.ignite.get("inheritance", {})
    inherit_ids = {str(x) for x in inheritance.get("inherit_from_tree_nodes", [])}
    tree_rows: list[ModifierRow] = []
    for eff in ruleset.tree_more_effects({"ignite"}):
        if str(eff.get("node_id")) not in inherit_ids:
            continue
        if not tags_match(eff.get("damage_tags", []), IGNITE_TAGS):
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

    # --- penetration: fire penetration + Fire Penetration with Ignite
    pen_rows = [
        _row(c)
        for c in collect_modifier_rows(
            snapshot, model.STAT_PENETRATION, IGNITE_TAGS, ("added", "increased")
        )
    ]
    with_ignite_rows = [
        _row(c)
        for c in collect_modifier_rows(
            snapshot, model.STAT_PENETRATION_WITH_IGNITE, IGNITE_TAGS, ("added", "increased")
        )
    ]
    effect_rows = [
        _row(c)
        for c in collect_modifier_rows(
            snapshot, model.STAT_AILMENT_EFFECT, IGNITE_TAGS, ("increased", "added")
        )
    ]
    pen_total = (
        sum(r.value for r in pen_rows)
        + sum(r.value for r in with_ignite_rows)
        + sum(r.value for r in effect_rows)
    )
    target = _ignite_target(ruleset, pen_total, scenario)

    return {
        "increased_rows": [r.to_dict() for r in inc_rows],
        "attribute_row": attribute_row,
        "ailment_effect_rows": [r.to_dict() for r in effect_rows],
        "penetration_with_ignite_rows": [r.to_dict() for r in with_ignite_rows],
        "increased_total": inc_total,
        "increased_factor": increased_pool.factor,
        "more_groups": more_groups,
        "more_product": more_product,
        "inheritance": inheritance,
        "penetration": {
            "rows": [r.to_dict() for r in pen_rows],
            "penetration_with_ignite_rows": [r.to_dict() for r in with_ignite_rows],
            "ailment_effect_rows": [r.to_dict() for r in effect_rows],
            "total": pen_total,
        },
        "target": target,
    }


def _ignite_target(
    ruleset: Ruleset,
    penetration_total: float,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Ignite target side through the shared operator.

    Policy is the same fold penetration mode as the body, but the resistance
    source is explicit: the historical Ignite path reads
    ``ruleset.target_side.base_resistances`` (``resistance_source="ruleset_base"``)
    rather than the scenario's resistance override, and penetration applies via
    the ``ignite`` scope.  Monster DR still comes from the scenario.
    """
    return shared_target_side.bh_target_side(
        ruleset_target_side=ruleset.target_side,
        scenario_target=(scenario or {}).get("target", {}) or {},
        damage_type="Fire",
        penetration_total=penetration_total,
        component_scope="ignite",
        resistance_source=shared_target_side.RESISTANCE_FROM_RULESET_BASE,
    )


def compute_ignite_core(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Single-stack damage core (independent of application)."""
    ruleset = ruleset.with_snapshot_points(snapshot)
    base_damage = float(ruleset.ignite["base_damage"])
    duration_info = _ignite_duration(snapshot, ruleset)
    scaling = _ignite_scaling(snapshot, ruleset, scenario)
    duration_mult = duration_info["duration_multiplier"]
    per_stack_total = (
        base_damage
        * duration_mult
        * scaling["increased_factor"]
        * scaling["more_product"]
        * scaling["target"]["multiplier"]
    )
    per_stack_dps = per_stack_total / duration_info["duration_s"]
    return {
        "ailment": ruleset.ignite.get("name", "Ignite"),
        "damage_type": ruleset.ignite.get("damage_type", "Fire"),
        "base_damage": base_damage,
        "base_duration_s": duration_info["base_duration_s"],
        "duration": duration_info,
        "duration_multiplier": duration_mult,
        "effective_duration_s": duration_info["duration_s"],
        "scaling": scaling,
        "increased_total": scaling["increased_total"],
        "increased_factor": scaling["increased_factor"],
        "more_groups": scaling["more_groups"],
        "more_product": scaling["more_product"],
        "penetration": scaling["penetration"],
        "target": scaling["target"],
        "per_stack_total": per_stack_total,
        "per_stack_dps": per_stack_dps,
    }


def heat_wave_application(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    spawn_time_s: float,
    black_hole_duration_s: float,
) -> dict[str, Any]:
    hw = ruleset.ignite.get("heat_wave", {})
    points = int(hw.get("points", 0))
    per_point = float(hw.get("chance_per_point_per_uncapped_fire_res", 0.02))
    rolls_per_second = float(hw.get("rolls_per_second", 1))
    uncapped_fire = snapshot.uncapped_resistance("fire")
    interval = 1.0 / rolls_per_second
    # The node grants "chance per second per 1% uncapped fire res": per-second
    # expected stacks stay constant, so a shorter roll interval splits the same
    # expectation across more rolls (1.5.3 aura uses applicationInterval=0.5s).
    expected_stacks_per_second = per_point * points * uncapped_fire
    stacks_per_roll = expected_stacks_per_second * interval

    max_k = int(math.ceil(black_hole_duration_s / interval - EPS))
    roll_offsets = [k * interval for k in range(max_k) if k * interval < black_hole_duration_s - EPS]
    roll_times = [spawn_time_s + off for off in roll_offsets]
    return {
        "rule": "heat_wave",
        "node_id": hw.get("node_id"),
        "points": points,
        "chance_per_point_per_uncapped_fire_res": per_point,
        "uncapped_fire_resistance": uncapped_fire,
        "chance_per_second_percent": expected_stacks_per_second * 100.0,
        "expected_stacks_per_second": expected_stacks_per_second,
        "expected_stacks_per_roll": stacks_per_roll,
        "rolls_per_second": rolls_per_second,
        "roll_interval_s": interval,
        "roll_offsets_s": roll_offsets,
        "roll_times_s": roll_times,
        "roll_count": len(roll_times),
        "expected_stacks_applied": stacks_per_roll * len(roll_times),
        "expected_value_model": True,
        "first_roll_at": hw.get("first_roll_at", "spawn"),
    }


def build_cohorts(
    core: dict[str, Any],
    application: dict[str, Any],
) -> list[IgniteCohort]:
    cohorts: list[IgniteCohort] = []
    duration = core["effective_duration_s"]
    for idx, roll_time in enumerate(application["roll_times_s"]):
        stacks = application["expected_stacks_per_roll"]
        if stacks <= 0:
            continue  # no Ignite applied: no cohort, no ailment lifetime extensions
        damage = stacks * core["per_stack_total"]
        cohorts.append(
            IgniteCohort(
                roll_index=idx,
                time_s=roll_time,
                stacks=stacks,
                duration_s=duration,
                damage_total=damage,
                end_time_s=roll_time + duration,
            )
        )
    return cohorts


def build_damage_slices(
    cohorts: list[IgniteCohort], slice_s: float = 1.0
) -> list[dict[str, Any]]:
    """Discretise each cohort's constant damage flow into fixed slices.

    Slices carry their continuous coverage interval [start_s, end_s] and the
    damage accrued in that interval.  The event table lists them as point
    events attributed to their end time (sum of amounts == cohort total), while
    window / cumulative / tail metrics integrate the *continuous* flow over the
    slice intervals (exact proration), so no timing bias is introduced.
    """
    slices: list[dict[str, Any]] = []
    for cohort in cohorts:
        if cohort.damage_total <= 0 or cohort.duration_s <= 0:
            continue
        n_slices = max(1, int(math.ceil(cohort.duration_s / slice_s - EPS)))
        for k in range(n_slices):
            start_offset = k * slice_s
            coverage = min(slice_s, cohort.duration_s - start_offset)
            if coverage <= EPS:
                continue
            amount = cohort.damage_total * (coverage / cohort.duration_s)
            slices.append(
                {
                    "cohort_index": cohort.roll_index,
                    "cohort_time_s": cohort.time_s,
                    "slice_index": k,
                    "start_s": cohort.time_s + start_offset,
                    "end_s": cohort.time_s + start_offset + coverage,
                    "t": cohort.time_s + start_offset + coverage,
                    "amount": amount,
                }
            )
    slices.sort(key=lambda s: (s["t"], s["cohort_index"], s["slice_index"]))
    return slices


def damage_between(
    slices: list[dict[str, Any]], start_s: float, end_s: float
) -> float:
    """Continuous-flow damage accrued in [start_s, end_s] (exact proration)."""
    total = 0.0
    for sl in slices:
        sl_start = float(sl["start_s"])
        sl_end = float(sl["end_s"])
        overlap = min(sl_end, end_s) - max(sl_start, start_s)
        if overlap <= EPS:
            continue
        length = sl_end - sl_start
        if length <= EPS:
            continue
        total += float(sl["amount"]) * (overlap / length)
    return total


def cumulative_damage(slices: list[dict[str, Any]], t_s: float) -> float:
    return damage_between(slices, 0.0, t_s)


def tail_damage(
    slices: list[dict[str, Any]], cut_s: float
) -> float:
    """Damage occurring after `cut_s` from batches already applied by then."""
    total = 0.0
    for sl in slices:
        if float(sl["cohort_time_s"]) > cut_s + EPS:
            continue
        sl_start = float(sl["start_s"])
        sl_end = float(sl["end_s"])
        overlap = sl_end - max(sl_start, cut_s)
        if overlap <= EPS:
            continue
        length = sl_end - sl_start
        total += float(sl["amount"]) * (overlap / length)
    return total


def split_cohort_damage(
    cohorts: list[IgniteCohort], window_end_s: float
) -> dict[str, float]:
    """Damage that occurs before / after a cut time."""
    before = 0.0
    for c in cohorts:
        if c.duration_s <= 0:
            continue
        overlap = min(c.end_time_s, window_end_s) - c.time_s
        overlap = max(0.0, min(overlap, c.duration_s))
        before += c.damage_total * (overlap / c.duration_s)
    total = sum(c.damage_total for c in cohorts)
    return {"before": before, "after": total - before, "total": total}


def damage_in_window(
    cohorts: list[IgniteCohort], start_s: float, end_s: float
) -> float:
    total = 0.0
    for c in cohorts:
        if c.duration_s <= 0:
            continue
        overlap = min(c.end_time_s, end_s) - max(c.time_s, start_s)
        overlap = max(0.0, min(overlap, c.duration_s))
        total += c.damage_total * (overlap / c.duration_s)
    return total


def stacks_at(cohorts: list[IgniteCohort], t: float) -> float:
    return sum(c.stacks for c in cohorts if c.time_s <= t < c.end_time_s)


def expression_tree(core: dict[str, Any]) -> dict[str, Any]:
    duration = core["duration"]
    scaling = core["scaling"]
    return {
        "node_id": "expr:ignite",
        "kind": "mul",
        "label": "Ignite 单层总伤害",
        "value": core["per_stack_total"],
        "children": [
            {
                "node_id": "expr:ignite:base",
                "kind": "value",
                "label": "Ignite 基础伤害",
                "value": core["base_damage"],
                "children": [],
                "source_refs": ["ailments.1"],
            },
            {
                "node_id": "expr:ignite:duration",
                "kind": "factor",
                "label": "持续时间乘区",
                "value": core["duration_multiplier"],
                "children": [],
                "rows": duration["rows"],
            },
            {
                "node_id": "expr:ignite:inc",
                "kind": "factor",
                "label": "1 + inc 加算池（含 Intelligence 属性缩放）",
                "value": scaling["increased_factor"],
                "children": [],
                "rows": scaling["increased_rows"] + [scaling["attribute_row"]],
            },
            {
                "node_id": "expr:ignite:more",
                "kind": "factor",
                "label": "more 独立乘区乘积（含技能树继承）",
                "value": scaling["more_product"],
                "children": [],
                "rows": scaling["more_groups"],
            },
            {
                "node_id": "expr:ignite:target",
                "kind": "factor",
                "label": "目标侧结算（火焰穿透 + Fire Penetration with Ignite）",
                "value": scaling["target"]["multiplier"],
                "children": [],
                "target": scaling["target"],
                "rows": scaling["penetration"]["rows"]
                + scaling["penetration"]["penetration_with_ignite_rows"]
                + scaling["penetration"]["ailment_effect_rows"],
            },
        ],
    }


def compute_ignite(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    spawn_time_s: float,
    black_hole_duration_s: float,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    core = compute_ignite_core(snapshot, ruleset, scenario)
    application = heat_wave_application(snapshot, ruleset, spawn_time_s, black_hole_duration_s)
    cohorts = build_cohorts(core, application)
    bh_end = spawn_time_s + black_hole_duration_s
    slices = build_damage_slices(cohorts)
    total = sum(s["amount"] for s in slices)
    before = damage_between(slices, spawn_time_s, bh_end)
    last_expiry = max((c.end_time_s for c in cohorts), default=spawn_time_s)
    return {
        **core,
        "application": application,
        "cohorts": cohorts,
        "_slices": slices,
        "totals": {
            "damage": total,
            "damage_before_black_hole_end": before,
            "tail_damage": total - before,
            "last_ailment_expiry_s": last_expiry,
            "expected_total_stacks_applied": application["expected_stacks_applied"],
        },
        "expression": expression_tree(core),
    }


__all__ = [
    "IgniteCohort",
    "build_cohorts",
    "build_damage_slices",
    "compute_ignite",
    "compute_ignite_core",
    "cumulative_damage",
    "damage_between",
    "damage_in_window",
    "expression_tree",
    "heat_wave_application",
    "split_cohort_damage",
    "stacks_at",
    "tail_damage",
]
