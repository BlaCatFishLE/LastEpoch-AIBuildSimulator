"""Charge ledger / timeline 公共 mechanics（G4/M7）。"""
from __future__ import annotations

import math
from typing import Any, Mapping, Optional

from .. import model
from .dot import EPS, _active_rows, _sum, make_cast as _make_cast, max_concurrent as _max_concurrent

def blocked_attempts(casts: list[Mapping[str, Any]], charge_events: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    blocked: list[dict[str, Any]] = []
    for event in charge_events:
        if event.get("kind") != "regen":
            continue
        t = float(event["t"])
        if any(cast["black_hole_spawn_s"] <= t <= cast["black_hole_end_s"] - EPS for cast in casts):
            blocked.append(
                {
                    "t": t,
                    "reason": "active_black_hole_limit",
                    "charges": event["charges"],
                    "note": "充能已就绪，但当前黑洞存续，施放被数量上限拒绝",
                }
            )
    return blocked

def charge_parameters(plan: Mapping[str, Any], rows: list[model.Contribution]) -> dict[str, Any]:
    spec = plan["schedule"]["charges"]
    base_max = int(spec["base_max_charges"])
    extra = int(spec.get("extra_max_charges") or 0)
    base_recharge = float(spec["base_recharge_s"])
    flat = float(spec.get("flat_adjustment_s") or 0.0)
    adjusted = base_recharge + flat
    cdr = float(spec.get("cooldown_reduction") or 0.0)
    icr = _sum(
        [
            row
            for row in _active_rows(rows)
            if row.stat_key == model.STAT_COOLDOWN_RECOVERY and row.operation in ("increased", "added")
        ]
    )
    effective = adjusted * (1.0 - cdr) / (1.0 + icr)
    return {
        "charge_based": True,
        "base_max_charges": base_max,
        "extra_max_charges": extra,
        "max_charges": base_max + extra,
        "base_recharge_s": base_recharge,
        "flat_adjustment_s": flat,
        "adjusted_cooldown_s": adjusted,
        "cooldown_reduction": cdr,
        "icr_total": icr,
        "effective_recharge_s": effective,
        "charges_per_second": 1.0 / effective if effective > 0 else 0.0,
        "formula": "effective_recharge = (base_recharge + flat) * (1 - CDR) / (1 + ICR)",
    }

def charge_loop(
    *,
    policy: str,
    horizon_s: float,
    max_charges: float,
    recharge_s: float,
    cast_time_s: float,
    duration_s: float,
    condition: Optional[str] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, list[dict[str, Any]]]:
    """充能循环：condition 枚举是唯一调度语义，policy 仅兼容/身份。"""
    ALLOWED_CONDITIONS = {
        "charge_available_and_cast_action_free",
        "no_active_black_hole_and_charge_available",
        "wait_for_all_instances_expired",
    }
    if condition is not None and condition not in ALLOWED_CONDITIONS:
        raise ValueError(f"未知 charge condition：{condition!r}")
    if condition is None:
        eager = policy == "eager"
    else:
        eager = condition == "charge_available_and_cast_action_free"
    charges = max_charges
    next_charge: Optional[float] = None
    cast_ready = 0.0
    active: list[tuple[float, float]] = []
    casts: list[dict[str, Any]] = []
    charge_events: list[dict[str, Any]] = [{"t": 0.0, "charges": charges, "kind": "initial"}]

    def process_regen(now: float) -> None:
        nonlocal charges, next_charge
        while next_charge is not None and next_charge <= now + EPS:
            if charges < max_charges - EPS:
                charges += 1.0
                charge_events.append({"t": round(next_charge, 9), "charges": charges, "kind": "regen"})
                next_charge = next_charge + recharge_s if charges < max_charges - EPS else None
            else:
                next_charge = None

    t = 0.0
    while t <= horizon_s + EPS:
        process_regen(t)
        active = [(spawn, end) for spawn, end in active if end > t + EPS]
        can_cast = charges >= 1.0 - EPS and t >= cast_ready - EPS and (eager or not active)
        if can_cast:
            before = charges
            charges -= 1.0
            charge_events.append({"t": round(t, 9), "charges": charges, "kind": "cast"})
            if next_charge is None and charges < max_charges - EPS:
                next_charge = t + recharge_s
            cast = _make_cast(
                cast_id=f"cast-{len(casts) + 1}",
                cast_start_s=t,
                cast_time_s=cast_time_s,
                duration_s=duration_s,
                charges_before=before,
                charges_after=charges,
            )
            casts.append(cast)
            active.append((cast["black_hole_spawn_s"], cast["black_hole_end_s"]))
            cast_ready = t + cast_time_s
        candidates: list[float] = [cast_ready]
        if next_charge is not None:
            candidates.append(next_charge)
        if not eager:
            candidates.extend(end for _, end in active)
        future = [value for value in candidates if value > t + EPS]
        if not future:
            break
        t = min(future)
        if t > horizon_s + EPS:
            break
    process_regen(horizon_s)
    max_concurrent = _max_concurrent(casts, horizon_s)
    blocked = _blocked_attempts(casts, charge_events)
    return casts, charge_events, max_concurrent, blocked

_blocked_attempts = blocked_attempts
_charge_parameters = charge_parameters
_charge_loop = charge_loop
__all__ = ["blocked_attempts", "charge_parameters", "charge_loop"]
