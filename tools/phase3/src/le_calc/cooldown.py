"""Cooldown / charge / cast-time rules for the Black Hole stage-1 (v1).

Reference-backed shape (LE Tools planner runtime, v1 adopted as assumption):

    effective_recharge_s = (base_recharge_s + sum(flat_adjustment_s)) / (1 + ICR_total)

where ICR_total is the additive "increased cooldown recovery speed" pool.
The generic runtime has no standalone percent cooldown-reduction pool; the
Recurrence skill node's 60% is therefore put into the ICR pool in v1 (mapping
unconfirmed because bh2's numeric node table is missing from the archive).

Cast time (reference formula, uses archived ability fields):

    base_rate_per_s = animation_speed(as) * speed_multiplier
    cast_time_s = 1 / (base_rate_per_s * (1 + increased cast speed))
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import model
from .damage import player_contributions
from .mechanics import frequency as shared_frequency
from .ruleset import Ruleset


@dataclass
class CooldownResult:
    data: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def to_dict(self) -> dict[str, Any]:
        return self.data


def compute_fixed_charges(ruleset: Ruleset) -> dict[str, Any]:
    base = float(ruleset.cooldown.get("base_max_charges", 1))
    rows = [
        {
            "source": f"ruleset:node:{t['node_id']}",
            "label": f"skill node {t['node_id']}",
            "value": t["value"],
            "unit": "charges",
        }
        for t in ruleset.tree_charge_terms()
    ]
    extra = sum(r["value"] for r in rows)
    return {"base_max_charges": base, "extra_rows": rows, "max_charges": base + extra}


def compute_cast_time(snapshot: model.Snapshot, ruleset: Ruleset) -> dict[str, Any]:
    cast = ruleset.cast
    as_value = float(cast.get("animation_speed", 0.0) or 0.0)
    speed_multiplier = float(cast.get("speed_multiplier", 1.0) or 1.0)
    base_rate = as_value * speed_multiplier
    if base_rate <= 0:
        base_rate = 0.0
        fallback_use_duration = float(cast.get("use_duration_s", 0.6))
    else:
        fallback_use_duration = 0.0
    rows = []
    for c in player_contributions(snapshot):
        if c.stat_key != model.STAT_CAST_SPEED:
            continue
        if c.operation not in ("increased", "added"):
            continue
        rows.append(
            {
                "contribution_id": c.contribution_id,
                "label": c.stat_name,
                "value": float(c.value),
                "source_kind": c.source_kind,
                "status": c.status,
            }
        )
    increased = sum(r["value"] for r in rows)
    if base_rate > 0:
        cast_time = shared_frequency.cast_time(
            base_rate, increased_cast_speed=increased
        )
    else:  # pragma: no cover - defensive fallback
        cast_time = fallback_use_duration / (1.0 + increased) if fallback_use_duration > 0 else 0.0
    return {
        "base_rate_per_s": base_rate,
        "base_use_duration_s": float(cast.get("use_duration_s", 0.6)),
        "speed_multiplier": speed_multiplier,
        "animation_speed": as_value,
        "increased_cast_speed": increased,
        "cast_time_s": cast_time,
        "rows": rows,
        "status": cast.get("status", "assumed"),
        "formula": cast.get("formula"),
    }


def compute_cooldown(snapshot: model.Snapshot, ruleset: Ruleset) -> CooldownResult:
    cd = ruleset.cooldown
    base_recharge = float(
        cd.get("base_recharge_s", 1.0 / float(cd.get("base_charges_gained_per_second", 1.0)))
    )
    separate_cdr = bool(cd.get("cdr_applied_separately", False))

    flat_rows: list[dict[str, Any]] = [
        {
            "source": f"ruleset:node:{t['node_id']}",
            "label": f"skill node {t['node_id']}",
            "value": t["value"],
            "unit": "seconds",
            "status": "confirmed",
        }
        for t in ruleset.tree_cooldown_flat_terms()
    ]
    icr_rows: list[dict[str, Any]] = []
    cdr_rows: list[dict[str, Any]] = []
    if separate_cdr:
        # v2 (1.5.3 RE + user in-game measurement): the skill-tree
        # "Cooldown Reduction" term reduces the adjusted cooldown
        # multiplicatively (1 - CDR); the player's Increased Cooldown
        # Recovery Speed remains a separate (1 + ICR) divisor.
        cdr_rows = [
            {
                "source": f"ruleset:node:{t['node_id']}",
                "label": f"skill node {t['node_id']} (cooldown reduction)",
                "value": t["value"],
                "unit": "percent",
                "status": "confirmed",
            }
            for t in ruleset.tree_cooldown_reduction_terms()
        ]
    else:
        icr_rows = [
            {
                "source": f"ruleset:node:{t['node_id']}",
                "label": f"skill node {t['node_id']} (mapped to increased cooldown recovery speed)",
                "value": t["value"],
                "unit": "percent",
                "status": "assumed",
            }
            for t in ruleset.tree_cooldown_reduction_terms()
        ]
    for c in player_contributions(snapshot):
        if c.stat_key == model.STAT_COOLDOWN_FLAT:
            flat_rows.append(
                {
                    "contribution_id": c.contribution_id,
                    "label": c.stat_name,
                    "value": float(c.value),
                    "source_kind": c.source_kind,
                    "source_path": c.source_path,
                    "status": c.status,
                    "unit": "seconds",
                }
            )
        elif c.stat_key in (model.STAT_COOLDOWN_RECOVERY, model.STAT_COOLDOWN_REDUCTION):
            icr_rows.append(
                {
                    "contribution_id": c.contribution_id,
                    "label": c.stat_name,
                    "value": float(c.value),
                    "source_kind": c.source_kind,
                    "source_path": c.source_path,
                    "status": c.status,
                    "unit": "percent",
                }
            )

    flat_total = sum(r["value"] for r in flat_rows)
    icr_total = sum(r["value"] for r in icr_rows)
    cdr_total = sum(r["value"] for r in cdr_rows)
    # Shared cooldown operator: (base + flat) / (1 + ICR), with the BH v2
    # CDR/ICR split branch.  Arithmetic order/branches match the historical code.
    cooldown_result = shared_frequency.effective_cooldown(
        base_recharge,
        flat_adjustment_s=flat_total,
        increased_cooldown_recovery_speed=icr_total,
        cooldown_reduction=cdr_total,
        cdr_applied_separately=separate_cdr,
    )
    adjusted = cooldown_result.adjusted_cooldown_s
    effective = cooldown_result.effective_cooldown_s

    charges = compute_fixed_charges(ruleset)
    charge_rows = list(charges["extra_rows"])
    for c in player_contributions(snapshot):
        if c.stat_key != model.STAT_CHARGES:
            continue
        charge_rows.append(
            {
                "contribution_id": c.contribution_id,
                "label": c.stat_name,
                "value": float(c.value),
                "source_kind": c.source_kind,
                "status": c.status,
            }
        )
    max_charges = charges["base_max_charges"] + sum(r["value"] for r in charge_rows)

    return CooldownResult(
        {
            "charge_based": bool(cd.get("charge_based", True)),
            "base_recharge_s": base_recharge,
            "base_charges_gained_per_second": cd.get("base_charges_gained_per_second"),
            "flat_rows": flat_rows,
            "flat_total_s": flat_total,
            "icr_rows": icr_rows,
            "icr_total": icr_total,
            "cdr_applied_separately": separate_cdr,
            "cdr_rows": cdr_rows,
            "cdr_total": cdr_total,
            "adjusted_cooldown_s": adjusted,
            "effective_recharge_s": effective,
            "max_charges": max_charges,
            "charge_rows": charge_rows,
            "formula": cd.get("formula"),
            "status": cd.get("status", "assumed"),
            "recurrence_mapping_note": (cd.get("icr_pool") or {}).get("note"),
        }
    )


__all__ = ["CooldownResult", "compute_cast_time", "compute_cooldown", "compute_fixed_charges"]
