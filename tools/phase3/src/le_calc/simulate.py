"""Event scheduling for the Black Hole stage-1 calculator (v1).

Two modes:
  * single cast: one cast, observed until the last Ignite stack expires.
  * cooldown loop: charge-based cast schedule for two action policies over a
    fixed horizon (5 / 30 / 120 s windows and a long-run cross-check).

Consistency contract (fix for review finding R1):
  * the event table, the cumulative time series and the window accounting all
    use the same Ignite damage slices (see `ignite.build_damage_slices`);
  * body damage is discretised into tick events and the last tick absorbs the
    fractional remainder (user rule 1);
  * windows only count damage that has occurred inside the window; Ignite
    damage from already-applied stacks that occurs after the window is
    reported separately as `unsettled_tail_damage`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

from . import model
from .cooldown import compute_cast_time, compute_cooldown
from .damage import compute_body
from .ignite import (
    IgniteCohort,
    build_cohorts,
    compute_ignite,
    cumulative_damage,
    damage_between,
    stacks_at,
    tail_damage,
)
from .ruleset import Ruleset

EPS = 1e-9
SAMPLE_STEP_S = 0.25


@dataclass
class Cast:
    cast_id: str
    cast_start_s: float
    cast_time_s: float
    spawn_s: float
    end_s: float
    charges_before: float
    charges_after: float
    body: dict[str, Any]
    ignite: dict[str, Any]
    body_ticks: list[dict[str, Any]] = field(default_factory=list)
    cohorts: list[IgniteCohort] = field(default_factory=list)
    ignite_slices: list[dict[str, Any]] = field(default_factory=list)

    @property
    def body_total(self) -> float:
        return float(self.body["totals"]["damage"])

    @property
    def ignite_total(self) -> float:
        return float(self.ignite["totals"]["damage"])

    @property
    def ignite_tail(self) -> float:
        return float(self.ignite["totals"]["tail_damage"])

    @property
    def observation_end_s(self) -> float:
        return max(self.end_s, float(self.ignite["totals"]["last_ailment_expiry_s"]))


def _make_cast(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: dict[str, Any],
    cast_id: str,
    cast_start_s: float,
    charges_before: float,
    charges_after: float,
) -> Cast:
    cast_info = compute_cast_time(snapshot, ruleset)
    cast_time = float(cast_info["cast_time_s"])
    spawn_s = cast_start_s + cast_time
    body = compute_body(snapshot, ruleset, scenario)
    duration_s = float(body["duration"]["duration_s"])
    end_s = spawn_s + duration_s
    ignite = compute_ignite(snapshot, ruleset, spawn_s, duration_s, scenario)

    body_ticks: list[dict[str, Any]] = []
    for component in body["components"]:
        damage_type = component["damage_type"]
        for tick in component["ticks"]:
            body_ticks.append(
                {
                    "t": spawn_s + tick["offset_s"],
                    "kind": "body_tick",
                    "cast_id": cast_id,
                    "component_id": component["component_id"],
                    "damage_type": damage_type,
                    "amount": tick["damage"],
                    "target_id": scenario.get("target", {}).get("target_id"),
                    "expression_ref": component["expression"]["node_id"],
                    "note": "末跳含余量合并" if tick["coverage_fraction"] < 1.0 - EPS else "",
                }
            )
    body_ticks.sort(key=lambda e: (e["t"], e["component_id"]))

    cohorts = build_cohorts(ignite, ignite["application"])
    slices = list(ignite.get("_slices") or [])
    return Cast(
        cast_id=cast_id,
        cast_start_s=cast_start_s,
        cast_time_s=cast_time,
        spawn_s=spawn_s,
        end_s=end_s,
        charges_before=charges_before,
        charges_after=charges_after,
        body=body,
        ignite=ignite,
        body_ticks=body_ticks,
        cohorts=cohorts,
        ignite_slices=slices,
    )


def _ignite_payload(ignite: dict[str, Any]) -> dict[str, Any]:
    payload = {
        k: v
        for k, v in ignite.items()
        if k not in ("cohorts",) and not k.startswith("_")
    }
    payload["cohorts"] = [c.to_dict() for c in ignite["cohorts"]]
    return payload


def _sample_times(start: float, end: float, step: float = SAMPLE_STEP_S) -> list[float]:
    if end <= start:
        return [round(start, 9)]
    n = int(math.floor((end - start) / step))
    times = [round(start + i * step, 9) for i in range(n + 1)]
    if times[-1] < end - EPS:
        times.append(round(end, 9))
    return times


# ---------------------------------------------------------------------------
# Single cast
# ---------------------------------------------------------------------------


def run_single_cast(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    scenario = scenario or ruleset.scenario
    cd = compute_cooldown(snapshot, ruleset)
    max_charges = max(1.0, float(cd["max_charges"]))
    cast_id = "cast-1"
    cast = _make_cast(
        snapshot, ruleset, scenario, cast_id, 0.0, max_charges, max_charges - 1.0
    )
    observation_end = max(cast.observation_end_s, cast.end_s)

    body_total = cast.body_total
    ignite_total = cast.ignite_total
    ignite_before = damage_between(cast.ignite_slices, 0.0, cast.end_s)
    ignite_tail = ignite_total - ignite_before
    total = body_total + ignite_total

    events: list[dict[str, Any]] = []
    seq = 0

    def add_event(t: float, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        events.append({"event_id": f"ev-{seq:05d}", "t": t, "cast_id": cast_id, **payload})

    add_event(cast.cast_start_s, kind="cast_start", component_id=None, damage_type=None, amount=0.0, target_id=None, expression_ref=None, note="开始施放")
    add_event(
        cast.spawn_s,
        kind="black_hole_spawn",
        component_id=None,
        damage_type=None,
        amount=0.0,
        target_id=None,
        expression_ref=None,
        note=f"双星={ruleset.has_binary_system} 转火={ruleset.has_red_giant}",
    )
    for tick in cast.body_ticks:
        add_event(
            tick["t"],
            kind="body_tick",
            component_id=tick["component_id"],
            damage_type=tick["damage_type"],
            amount=tick["amount"],
            target_id=tick["target_id"],
            expression_ref=tick["expression_ref"],
            note=tick["note"],
        )
    application = cast.ignite["application"]
    for roll_time in application["roll_times_s"]:
        add_event(
            roll_time,
            kind="ignite_roll",
            component_id=None,
            damage_type="Fire",
            amount=0.0,
            target_id=scenario.get("target", {}).get("target_id"),
            expression_ref=cast.ignite["expression"]["node_id"],
            note=f"期望层数 {application['expected_stacks_per_roll']:.6g}",
        )
    for sl in cast.ignite_slices:
        add_event(
            sl["t"],
            kind="ignite_tick",
            component_id=None,
            damage_type="Fire",
            amount=sl["amount"],
            target_id=scenario.get("target", {}).get("target_id"),
            expression_ref=cast.ignite["expression"]["node_id"],
            note=f"第 {sl['cohort_index'] + 1} 次判定的第 {sl['slice_index'] + 1} 个伤害切片",
        )
    add_event(cast.end_s, kind="black_hole_destroyed", component_id=None, damage_type=None, amount=0.0, target_id=None, expression_ref=None, note="黑洞消失")
    for cohort in cast.cohorts:
        add_event(
            cohort.end_time_s,
            kind="ignite_cohort_expiry",
            component_id=None,
            damage_type="Fire",
            amount=0.0,
            target_id=scenario.get("target", {}).get("target_id"),
            expression_ref=cast.ignite["expression"]["node_id"],
            note=f"第 {cohort.roll_index + 1} 次判定的层数到期（{cohort.stacks:.6g} 层，批次总量 {cohort.damage_total:.6g}）",
        )
    add_event(
        cast.ignite["totals"]["last_ailment_expiry_s"],
        kind="observation_end",
        component_id=None,
        damage_type=None,
        amount=0.0,
        target_id=None,
        expression_ref=None,
        note="最后一层 Ignite 到期",
    )
    events.sort(key=lambda e: (e["t"], e["kind"]))

    time_series = []
    for t in _sample_times(0.0, observation_end):
        body_cum = sum(e["amount"] for e in cast.body_ticks if e["t"] <= t + EPS)
        ignite_cum = cumulative_damage(cast.ignite_slices, t)
        time_series.append(
            {
                "t": t,
                "body_cum": body_cum,
                "ignite_cum": ignite_cum,
                "total_cum": body_cum + ignite_cum,
                "ignite_stacks": stacks_at(cast.cohorts, t),
                "active_black_holes": 1 if cast.spawn_s <= t <= cast.end_s + EPS else 0,
            }
        )

    return {
        "cast": {
            "cast_id": cast_id,
            "cast_start_s": cast.cast_start_s,
            "cast_time_s": cast.cast_time_s,
            "black_hole_spawn_s": cast.spawn_s,
            "black_hole_end_s": cast.end_s,
            "black_hole_duration_s": float(cast.body["duration"]["duration_s"]),
            "charges_before": cast.charges_before,
            "charges_after": cast.charges_after,
            "mana": "unlimited",
        },
        "body": cast.body,
        "ignite": _ignite_payload(cast.ignite),
        "totals": {
            "single_cast_total": total,
            "body_total": body_total,
            "ignite_total": ignite_total,
            "ignite_damage_before_black_hole_end": ignite_before,
            "ignite_tail_damage": ignite_tail,
            "window_damage_until_black_hole_end": body_total + ignite_before,
            "observation_end_s": observation_end,
            "last_ailment_expiry_s": float(cast.ignite["totals"]["last_ailment_expiry_s"]),
            "expected_total_stacks_applied": float(application["expected_stacks_applied"]),
        },
        "event_trace": events,
        "time_series": time_series,
    }


# ---------------------------------------------------------------------------
# Cooldown loop
# ---------------------------------------------------------------------------


def run_loop(
    snapshot: model.Snapshot,
    ruleset: Ruleset,
    scenario: dict[str, Any],
    policy: dict[str, Any],
    horizon_s: float = 120.0,
    windows: tuple[float, ...] = (5.0, 30.0, 120.0),
) -> dict[str, Any]:
    """Charge-based cast schedule for the two action policies.

    User decision 2026-11-04 (#6): the "only 1 Black Hole" limit applies per
    cast (a Binary System pair counts as one).  Different casts create
    independent, concurrently existing black holes, so the eager policy may
    overlap them; the patient policy waits until every active black hole has
    expired before casting again.

    Charges recharge sequentially (one recharge timer at a time); max charges
    only provide burst capacity, matching the reference model's sustained rate.
    """
    cd = compute_cooldown(snapshot, ruleset)
    cast_info = compute_cast_time(snapshot, ruleset)
    cast_time = float(cast_info["cast_time_s"])
    recharge_s = float(cd["effective_recharge_s"])
    charges_max = max(1.0, float(cd["max_charges"]))
    policy_id = policy.get("policy_id", "policy")
    eager = policy_id == "eager"

    casts: list[Cast] = []
    cast_summaries: list[dict[str, Any]] = []
    charge_events: list[dict[str, Any]] = [{"t": 0.0, "charges": charges_max, "kind": "initial"}]
    charges = charges_max
    next_charge: Optional[float] = None
    cast_ready = 0.0
    active: list[tuple[float, float]] = []

    def process_regen(now: float) -> None:
        nonlocal charges, next_charge
        while next_charge is not None and next_charge <= now + EPS:
            if charges < charges_max:
                charges += 1
                charge_events.append(
                    {"t": round(next_charge, 9), "charges": charges, "kind": "regen"}
                )
                next_charge = next_charge + recharge_s if charges < charges_max else None
            else:
                next_charge = None

    t = 0.0
    while t <= horizon_s + EPS:
        process_regen(t)
        active = [(spawn, end) for spawn, end in active if end > t + EPS]
        can_cast = (
            charges >= 1
            and t >= cast_ready - EPS
            and (eager or not active)
        )
        if can_cast:
            before = charges
            charges -= 1
            charge_events.append({"t": round(t, 9), "charges": charges, "kind": "cast"})
            if next_charge is None and charges < charges_max:
                next_charge = t + recharge_s
            cast = _make_cast(
                snapshot, ruleset, scenario, f"cast-{len(casts) + 1}", t, before, charges
            )
            casts.append(cast)
            cast_summaries.append(
                {
                    "cast_id": cast.cast_id,
                    "t": t,
                    "cast_time_s": cast.cast_time_s,
                    "spawn_s": cast.spawn_s,
                    "black_hole_end_s": cast.end_s,
                    "charges_before": before,
                    "charges_after": charges,
                    "accepted": True,
                    "blocked_reason": None,
                }
            )
            active.append((cast.spawn_s, cast.end_s))
            cast_ready = t + cast_time

        candidates: list[float] = [cast_ready]
        if next_charge is not None:
            candidates.append(next_charge)
        if not eager:
            candidates.extend(end for _, end in active)
        future = [c for c in candidates if c > t + EPS]
        if not future:
            break
        t = min(future)
        if t > horizon_s + EPS:
            break
    process_regen(horizon_s)

    all_cohorts: list[IgniteCohort] = [c for cast in casts for c in cast.cohorts]
    all_slices: list[dict[str, Any]] = [s for cast in casts for s in cast.ignite_slices]

    def window_metrics(window_s: float) -> dict[str, Any]:
        body = sum(
            tick["amount"]
            for cast in casts
            for tick in cast.body_ticks
            if tick["t"] <= window_s + EPS
        )
        ignite = sum(damage_between(cast.ignite_slices, 0.0, window_s) for cast in casts)
        tail = sum(tail_damage(cast.ignite_slices, window_s) for cast in casts)
        total = body + ignite
        cast_count = sum(1 for cast in casts if cast.cast_start_s <= window_s + EPS)
        return {
            "window_s": window_s,
            "total_damage": total,
            "body_damage": body,
            "ignite_damage": ignite,
            "dps": total / window_s if window_s > 0 else 0.0,
            "unsettled_tail_damage": tail,
            "cast_count": cast_count,
        }

    windows_payload = [window_metrics(w) for w in windows]

    max_concurrent = 0
    for sample in _sample_times(0.0, horizon_s):
        count = sum(1 for cast in casts if cast.spawn_s <= sample <= cast.end_s + EPS)
        max_concurrent = max(max_concurrent, count)

    time_series: list[dict[str, Any]] = []
    for t in _sample_times(0.0, horizon_s):
        body = sum(
            tick["amount"] for cast in casts for tick in cast.body_ticks if tick["t"] <= t + EPS
        )
        ignite = sum(cumulative_damage(cast.ignite_slices, t) for cast in casts)
        stacks = stacks_at(all_cohorts, t)
        active_count = sum(1 for cast in casts if cast.spawn_s <= t <= cast.end_s + EPS)
        time_series.append(
            {
                "t": t,
                "body_cum": body,
                "ignite_cum": ignite,
                "total_cum": body + ignite,
                "ignite_stacks": stacks,
                "active_black_holes": active_count,
                "charges": _charge_at(charge_events, t, charges_max),
            }
        )

    cast_times = [cast.cast_start_s for cast in casts]
    if len(cast_times) >= 2:
        intervals = [b - a for a, b in zip(cast_times, cast_times[1:])]
        tail_intervals = intervals[-5:] if len(intervals) > 5 else intervals
        steady_interval = sum(tail_intervals) / len(tail_intervals)
        cycle_s: Optional[float] = steady_interval
    else:
        cycle_s = None
    single = run_single_cast(snapshot, ruleset, scenario)
    single_total = float(single["totals"]["single_cast_total"])
    steady_dps = single_total / cycle_s if cycle_s and cycle_s > 0 else None

    event_trace: list[dict[str, Any]] = []
    seq = 0

    def add_loop_event(
        t: float,
        kind: str,
        cast_id: Optional[str],
        *,
        component_id: Optional[str] = None,
        damage_type: Optional[str] = None,
        amount: float = 0.0,
        note: str = "",
    ) -> None:
        nonlocal seq
        seq += 1
        event_trace.append(
            {
                "event_id": f"lev-{seq:05d}",
                "t": t,
                "kind": kind,
                "cast_id": cast_id,
                "component_id": component_id,
                "damage_type": damage_type,
                "amount": amount,
                "target_id": scenario.get("target", {}).get("target_id") if amount else None,
                "expression_ref": None,
                "note": note,
            }
        )

    for summary in cast_summaries:
        add_loop_event(
            summary["t"],
            "cast_start",
            summary["cast_id"],
            note=f"充能 {summary['charges_before']:.0f} -> {summary['charges_after']:.0f}",
        )
        add_loop_event(summary["black_hole_end_s"], "black_hole_destroyed", summary["cast_id"])
    for cast in casts[:2]:
        for tick in cast.body_ticks:
            add_loop_event(
                tick["t"],
                "body_tick",
                cast.cast_id,
                component_id=tick["component_id"],
                damage_type=tick["damage_type"],
                amount=tick["amount"],
            )
        for sl in cast.ignite_slices:
            add_loop_event(
                sl["t"],
                "ignite_tick",
                cast.cast_id,
                damage_type="Fire",
                amount=sl["amount"],
                note=f"批次 {sl['cohort_index'] + 1} 切片 {sl['slice_index'] + 1}",
            )
        for roll_time in cast.ignite["application"]["roll_times_s"]:
            add_loop_event(
                roll_time,
                "ignite_roll",
                cast.cast_id,
                damage_type="Fire",
                note=f"期望层数 {cast.ignite['application']['expected_stacks_per_roll']:.6g}",
            )
    event_trace.sort(key=lambda e: (e["t"], e["kind"]))

    horizon_metrics = window_metrics(horizon_s)
    return {
        "policy_id": policy_id,
        "label": policy.get("label", policy_id),
        "action_policy": {
            "policy_id": policy_id,
            "description": policy.get("description", ""),
            "cast_condition": policy.get("cast_condition"),
            "blocked_attempts_recorded": False,
        },
        "horizon_s": horizon_s,
        "windows": windows_payload,
        "casts": cast_summaries,
        "blocked_attempts": [],
        "resource": {
            "max_charges": charges_max,
            "effective_recharge_s": recharge_s,
            "initial_charges": charges_max,
            "mana": "unlimited",
            "cast_time_s": cast_time,
            "charge_events": charge_events,
            "note": "v1 忽略法力消耗与恢复；充能按顺序恢复（同一时间只有一个恢复计时器），max charges 仅提供起手爆发",
        },
        "totals": {
            "cast_count": len(casts),
            "horizon_damage": horizon_metrics["total_damage"],
            "horizon_dps": horizon_metrics["dps"],
            "cycle_s": cycle_s,
            "steady_state_dps_cross_check": steady_dps,
            "unsettled_tail_damage": horizon_metrics["unsettled_tail_damage"],
            "max_concurrent_black_holes": max_concurrent,
        },
        "event_trace": event_trace,
        "event_trace_limited": True,
        "time_series": time_series,
    }


def _charge_at(events: list[dict[str, Any]], t: float, default: float) -> float:
    charges = default
    for event in events:
        if event["t"] <= t + EPS:
            charges = event["charges"]
        else:
            break
    return charges


def _replay_charges(
    cast_times: list[float],
    charges_max: float,
    recharge_s: float,
    horizon_s: float,
) -> list[dict[str, Any]]:
    """Deterministic charge curve replay up to the horizon.

    v1 assumption: charges refill sequentially (one recharge timer at a time),
    with the first missing charge starting immediately after the cast.
    """
    events: list[dict[str, Any]] = [{"t": 0.0, "charges": charges_max, "kind": "initial"}]
    charges = charges_max
    next_charge: Optional[float] = None
    for cast_t in cast_times:
        while next_charge is not None and next_charge <= cast_t + EPS:
            charges = min(charges_max, charges + 1)
            events.append({"t": round(next_charge, 9), "charges": charges, "kind": "regen"})
            next_charge = next_charge + recharge_s if charges < charges_max else None
        charges -= 1
        events.append({"t": round(cast_t, 9), "charges": charges, "kind": "cast"})
        if next_charge is None and charges < charges_max:
            next_charge = cast_t + recharge_s
    while next_charge is not None and next_charge <= horizon_s + EPS:
        charges = min(charges_max, charges + 1)
        events.append({"t": round(next_charge, 9), "charges": charges, "kind": "regen"})
        next_charge = next_charge + recharge_s if charges < charges_max else None
    return events


def _derive_blocked_attempts(
    charge_events: list[dict[str, Any]], casts: list[Cast]
) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for event in charge_events:
        if event["kind"] != "regen":
            continue
        t = event["t"]
        if any(cast.spawn_s <= t <= cast.end_s - EPS for cast in casts):
            blocked.append(
                {
                    "t": t,
                    "reason": "active_black_hole_limit",
                    "charges": event["charges"],
                    "note": "充能已就绪，但当前黑洞存续，施放被数量上限拒绝",
                }
            )
    return blocked


__all__ = ["Cast", "SAMPLE_STEP_S", "run_loop", "run_single_cast"]
