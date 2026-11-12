"""频率/冷却/窗口骨架共享算子（P2）。

**暂不合并语义**：两个模型的调度语义不同（BH 是 charge-based 冷却 + cast
time 事件流；FR 是 cooldown-limited / attack-speed-limited 两种口径 + 命中
权重 + 5/30/120s 窗口）。本模块只抽双方共用的数学骨架：

* 冷却：``effective = (base + flat) / (1 + ICR)``，BH v2 起额外的
  “Cooldown Reduction” 分支为 ``adjusted × (1 - CDR) / (1 + ICR)``；
* 攻速：``final_aps = base_aps × (1 + Σincreased) × Π(1 + more 组)``；
* 施放速率：``min(1/attack_interval, 1/effective_cooldown)``，并记录约束方；
* 窗口骨架：``casts = cps × window``、``hits = cps × hits_per_cast × window``、
  ``damage = damage_per_second × window``、``dps = damage / window``。

BH 的窗口聚合依赖逐事件伤害切片（``simulate.window_metrics``），属于事件
模拟语义，**不在本骨架内合并**；本模块只在其事件表之上提供同样的窗口数学。
本模块不读 snapshot/ruleset，输入全部显式传入。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

SCHEMA_VERSION = "le-mechanics-frequency-v1"

#: 施放速率的约束方
BINDING_COOLDOWN = "cooldown"
BINDING_ATTACK_SPEED = "attack_speed"


class FrequencyError(ValueError):
    pass


@dataclass(frozen=True)
class CooldownResult:
    base_recharge_s: float
    flat_adjustment_s: float
    adjusted_cooldown_s: float
    increased_cooldown_recovery_speed: float
    cooldown_reduction: float
    cdr_applied_separately: bool
    effective_cooldown_s: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "base_recharge_s": self.base_recharge_s,
            "flat_adjustment_s": self.flat_adjustment_s,
            "adjusted_cooldown_s": self.adjusted_cooldown_s,
            "increased_cooldown_recovery_speed": self.increased_cooldown_recovery_speed,
            "cooldown_reduction": self.cooldown_reduction,
            "cdr_applied_separately": self.cdr_applied_separately,
            "effective_cooldown_s": self.effective_cooldown_s,
        }


def effective_cooldown(
    base_recharge_s: float,
    *,
    flat_adjustment_s: float = 0.0,
    increased_cooldown_recovery_speed: float = 0.0,
    cooldown_reduction: float = 0.0,
    cdr_applied_separately: bool = False,
) -> CooldownResult:
    """冷却骨架（BH ``cooldown.compute_cooldown`` 的算术，逐位一致）。"""
    base = float(base_recharge_s)
    flat = float(flat_adjustment_s)
    icr = float(increased_cooldown_recovery_speed)
    cdr = float(cooldown_reduction)
    adjusted = base + flat
    if cdr_applied_separately:
        effective = adjusted * max(0.0, 1.0 - cdr)
        if (1.0 + icr) > 0:
            effective /= 1.0 + icr
    else:
        effective = adjusted / (1.0 + icr) if (1.0 + icr) > 0 else adjusted
    return CooldownResult(
        base_recharge_s=base,
        flat_adjustment_s=flat,
        adjusted_cooldown_s=adjusted,
        increased_cooldown_recovery_speed=icr,
        cooldown_reduction=cdr,
        cdr_applied_separately=bool(cdr_applied_separately),
        effective_cooldown_s=effective,
    )


def cast_time(base_rate_per_s: float, *, increased_cast_speed: float = 0.0) -> float:
    """施法时间 = ``1 / (base_rate × (1 + Σincreased cast speed))``；无 base 时为 0。"""
    base_rate = float(base_rate_per_s)
    increased = float(increased_cast_speed)
    if base_rate <= 0:
        return 0.0
    return 1.0 / (base_rate * (1.0 + increased))


def cooldown_limited_rate(
    cooldown_s: float,
    *,
    increased_cooldown_recovery_speed: float = 0.0,
) -> Optional[float]:
    """FR 冷却口径：``cooldown / (1 + ICR)``；``cooldown <= 0`` 返回 ``None``。

    与 BH ``effective_cooldown`` 的区别是守卫条件（FR 以技能冷却是否为 0 决定
    是否有冷却约束，BH 以 ICR 分母是否为 0 决定）——这里显式保留 FR 语义，
    不合并成同一个分支。
    """
    cooldown = float(cooldown_s)
    if cooldown <= 0:
        return None
    return cooldown / (1.0 + float(increased_cooldown_recovery_speed))


def final_attack_rate(
    base_aps: float,
    *,
    increased_attack_speed: float = 0.0,
    more_product: float = 1.0,
) -> float:
    """``final_aps = base × (1 + inc) × more``（FR attack-speed 口径）。"""
    return float(base_aps) * (1.0 + float(increased_attack_speed)) * float(more_product)


def attack_interval(final_aps: float) -> float:
    """``interval = 1 / final_aps``；``final_aps <= 0`` 时为 0。"""
    rate = float(final_aps)
    return 1.0 / rate if rate > 0 else 0.0


@dataclass(frozen=True)
class CastPolicy:
    casts_per_second: float
    binding_constraint: str
    attack_interval_s: float
    effective_cooldown_s: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "casts_per_second": self.casts_per_second,
            "binding_constraint": self.binding_constraint,
            "attack_interval_s": self.attack_interval_s,
            "effective_cooldown_s": self.effective_cooldown_s,
        }


def cast_policy(
    *,
    attack_interval_s: float,
    effective_cooldown_s: Optional[float] = None,
) -> CastPolicy:
    """施放速率 = min(攻速上限, 冷却上限)，并记录约束方（FR ``_policy_block`` 口径）。"""
    interval = float(attack_interval_s)
    casts = 1.0 / interval if interval > 0 else 0.0
    binding = BINDING_ATTACK_SPEED
    cooldown = None if effective_cooldown_s is None else float(effective_cooldown_s)
    if cooldown and cooldown > 0:
        cooldown_casts = 1.0 / cooldown
        if cooldown_casts < casts:
            casts = cooldown_casts
            binding = BINDING_COOLDOWN
        else:
            binding = BINDING_ATTACK_SPEED
    return CastPolicy(
        casts_per_second=casts,
        binding_constraint=binding,
        attack_interval_s=interval,
        effective_cooldown_s=cooldown,
    )


def damage_weights_factor(
    damage_weights: Optional[Sequence[float]],
    hits_per_cast: int,
) -> tuple[list[float], float]:
    """每次施放的有效命中当量；显式权重与命中数不匹配时退化为每次满伤害。"""
    hits = int(hits_per_cast)
    weights = [float(value) for value in (damage_weights or [])]
    if weights and len(weights) == hits:
        return weights, sum(weights)
    return [1.0] * hits, float(hits)


def window_skeleton(
    *,
    window_s: float,
    casts_per_second: float,
    hits_per_cast: int,
    single_hit_damage: float,
    damage_weights: Optional[Sequence[float]] = None,
    rotation_more_factor: float = 1.0,
) -> dict[str, Any]:
    """单个窗口的骨架数值；字段语义对齐 FR ``hit_simulate._policy_block``。"""
    window = float(window_s)
    cps = float(casts_per_second)
    hits_per_cast = int(hits_per_cast)
    weights, effective_hits = damage_weights_factor(damage_weights, hits_per_cast)
    hits_per_second = cps * hits_per_cast
    damage_per_cast = float(single_hit_damage) * effective_hits * float(rotation_more_factor)
    damage_per_second = cps * damage_per_cast
    casts = cps * window
    hits = hits_per_second * window
    total_damage = damage_per_second * window
    return {
        "window_s": window,
        "window_id": f"{window:g}s",
        "casts": casts,
        "complete_casts": int(casts),
        "hits": hits,
        "single_hit_damage": float(single_hit_damage),
        "damage_weights": weights,
        "effective_hits_per_cast": effective_hits,
        "rotation_more_factor": float(rotation_more_factor),
        "damage_per_cast": damage_per_cast,
        "total_damage": total_damage,
        "dps": total_damage / window if window > 0 else 0.0,
        "casts_per_second": cps,
        "hits_per_second": hits_per_second,
        "damage_per_second": damage_per_second,
    }


# ---------------------------------------------------------------------------
# 派生事件率（生命周期/触发链，数据声明式）
# ---------------------------------------------------------------------------
#: 派生事件率声明 schema 标识
DERIVATION_SCHEMA = "le-derived-event-rate-v1"
#: 声明 kind 白名单（新增 kind 必须升级契约）
DERIVATION_KINDS = ("summon_lifecycle",)
UPSTREAM_KINDS = ("scenario_control", "static")
GATE_KINDS = ("chance",)
GATE_AGGREGATES = ("per_point_add",)
DESTROY_POLICIES = ("replace_or_expire", "expire_only", "replace_only")
RATE_FORMULAS = ("upstream_rate * gate_probability",)
VIOLATION_POLICIES = ("warn", "error")
MAX_RATE_ASSERTIONS = ("lifecycle.limit / lifecycle.duration_s",)

_DERIVATION_ALLOWED = {
    "schema", "kind", "upstream", "gate", "lifecycle", "rate_formula", "assertions", "notes", "evidence",
}
_UPSTREAM_ALLOWED = {"kind", "control", "default", "value", "unit", "notes"}
_GATE_ALLOWED = {"kind", "per_point", "points_control", "aggregate", "source", "probability", "points", "notes", "evidence"}
_LIFECYCLE_ALLOWED = {"limit", "duration_s", "destroy_policy", "events_per_summon", "notes", "evidence"}
_ASSERTION_ALLOWED = {"max_rate", "on_violation"}


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise FrequencyError(f"{label} 必须是有限数值")
    return float(value)


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise FrequencyError(f"{label} 必须是对象")
    return value


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise FrequencyError(f"{label} 含未知字段：{unknown}")


def _resolve_upstream(
    upstream: Mapping[str, Any],
    states: Mapping[str, Any],
    *,
    validate_only: bool = False,
) -> tuple[float, dict[str, Any]]:
    kind = str(upstream.get("kind") or "")
    if kind not in UPSTREAM_KINDS:
        raise FrequencyError(f"derivation.upstream.kind 不受支持：{kind!r}")
    if kind == "scenario_control":
        control = str(upstream.get("control") or "")
        if not control:
            raise FrequencyError("derivation.upstream.control 缺失")
        if upstream.get("default") is None:
            if validate_only:
                return 0.0, {"kind": kind, "control": control, "value": None, "source": "unset"}
            raise FrequencyError(
                f"derivation.upstream 缺少 default 且 states 未提供 control {control!r}"
            )
        default = _finite_number(upstream.get("default"), "derivation.upstream.default")
        raw = states.get(control, default)
        value = _finite_number(raw, f"derivation.upstream（control {control!r}）")
        if value < 0:
            raise FrequencyError(f"derivation.upstream（control {control!r}）不能为负")
        return value, {"kind": kind, "control": control, "value": value, "source": "states" if control in states else "default"}
    value = _finite_number(upstream.get("value"), "derivation.upstream.value")
    if value < 0:
        raise FrequencyError("derivation.upstream.value 不能为负")
    return value, {"kind": kind, "value": value, "source": "static"}


def _resolve_gate(gate: Mapping[str, Any], states: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    kind = str(gate.get("kind") or "")
    if kind not in GATE_KINDS:
        raise FrequencyError(f"derivation.gate.kind 不受支持：{kind!r}")
    aggregate = str(gate.get("aggregate") or "per_point_add")
    if aggregate not in GATE_AGGREGATES:
        raise FrequencyError(f"derivation.gate.aggregate 不受支持：{aggregate!r}")
    if gate.get("probability") is not None:
        probability = _finite_number(gate.get("probability"), "derivation.gate.probability")
        if not 0.0 <= probability <= 1.0:
            raise FrequencyError("derivation.gate.probability 必须在 [0,1]")
        raw_points = gate.get("points")
        points = int(raw_points) if isinstance(raw_points, int) and not isinstance(raw_points, bool) else None
        return probability, {
            "kind": kind,
            "aggregate": aggregate,
            "probability": probability,
            "points": points,
            "per_point": gate.get("per_point"),
            "source": dict(gate.get("source") or {}),
            "mode": "compiled_probability",
        }
    per_point = _finite_number(gate.get("per_point"), "derivation.gate.per_point")
    if per_point < 0:
        raise FrequencyError("derivation.gate.per_point 不能为负")
    points_control = str(gate.get("points_control") or "")
    if not points_control:
        raise FrequencyError("derivation.gate.points_control 缺失")
    raw_points = states.get(points_control, 0)
    if isinstance(raw_points, bool) or not isinstance(raw_points, int):
        raise FrequencyError(f"derivation.gate points control {points_control!r} 必须是整数")
    if raw_points < 0:
        raise FrequencyError(f"derivation.gate points control {points_control!r} 不能为负")
    probability = min(1.0, per_point * float(raw_points))
    return probability, {
        "kind": kind,
        "aggregate": aggregate,
        "per_point": per_point,
        "points_control": points_control,
        "points": raw_points,
        "probability": probability,
    }


def derived_event_rate(
    derivation: Mapping[str, Any],
    *,
    states: Optional[Mapping[str, Any]] = None,
    validate_only: bool = False,
) -> dict[str, Any]:
    """由生命周期/触发链声明推导稳态事件率；只消费显式数据，不读技能名。

    当前唯一支持的组合是 ``kind=summon_lifecycle``：

    ``event_rate = upstream_rate × gate_probability × events_per_summon``

    - ``upstream``：外部场景参数（scenario control）或静态值；
    - ``gate``：每点概率 × 点数控制项（点数由真实装配绑定，运行期只读 control）；
    - ``lifecycle``：limit/duration 决定稳态并发与销毁方式，并给出速率上限断言
      ``max_rate = limit / duration_s``（超过时按 ``on_violation`` 报错或记录）。

    返回对象可整体写入 result-v6/execution 的 ``frequency`` 镜像；未知 kind/字段/
    策略一律拒绝，不做静默降级。
    """
    derivation = _require_mapping(derivation, "derivation")
    _reject_unknown(derivation, _DERIVATION_ALLOWED, "derivation")
    kind = str(derivation.get("kind") or "")
    if kind not in DERIVATION_KINDS:
        raise FrequencyError(f"derivation.kind 不受支持：{kind!r}")
    rate_formula = str(derivation.get("rate_formula") or "upstream_rate * gate_probability")
    if rate_formula not in RATE_FORMULAS:
        raise FrequencyError(f"derivation.rate_formula 不受支持：{rate_formula!r}")
    states = dict(states or {})
    upstream_raw = _require_mapping(derivation.get("upstream"), "derivation.upstream")
    _reject_unknown(upstream_raw, _UPSTREAM_ALLOWED, "derivation.upstream")
    gate_raw = _require_mapping(derivation.get("gate"), "derivation.gate")
    _reject_unknown(gate_raw, _GATE_ALLOWED, "derivation.gate")
    lifecycle = _require_mapping(derivation.get("lifecycle"), "derivation.lifecycle")
    _reject_unknown(lifecycle, _LIFECYCLE_ALLOWED, "derivation.lifecycle")
    limit = int(_finite_number(lifecycle.get("limit"), "derivation.lifecycle.limit"))
    if limit < 1:
        raise FrequencyError("derivation.lifecycle.limit 必须 >= 1")
    duration = _finite_number(lifecycle.get("duration_s"), "derivation.lifecycle.duration_s")
    if duration <= 0:
        raise FrequencyError("derivation.lifecycle.duration_s 必须 > 0")
    destroy_policy = str(lifecycle.get("destroy_policy") or "replace_or_expire")
    if destroy_policy not in DESTROY_POLICIES:
        raise FrequencyError(f"derivation.lifecycle.destroy_policy 不受支持：{destroy_policy!r}")
    events_per_summon = _finite_number(
        lifecycle.get("events_per_summon", 1.0), "derivation.lifecycle.events_per_summon"
    )
    if events_per_summon < 0:
        raise FrequencyError("derivation.lifecycle.events_per_summon 不能为负")
    assertions = derivation.get("assertions")
    assertion_doc: dict[str, Any] = {}
    on_violation = "warn"
    if assertions is not None:
        assertions = _require_mapping(assertions, "derivation.assertions")
        _reject_unknown(assertions, _ASSERTION_ALLOWED, "derivation.assertions")
        max_rate_assertion = assertions.get("max_rate")
        if max_rate_assertion is not None and str(max_rate_assertion) not in MAX_RATE_ASSERTIONS:
            raise FrequencyError(f"derivation.assertions.max_rate 不受支持：{max_rate_assertion!r}")
        assertion_doc["max_rate"] = str(max_rate_assertion) if max_rate_assertion is not None else None
        on_violation = str(assertions.get("on_violation") or "warn")
        if on_violation not in VIOLATION_POLICIES:
            raise FrequencyError(f"derivation.assertions.on_violation 不受支持：{on_violation!r}")
        assertion_doc["on_violation"] = on_violation

    upstream_rate, upstream_info = _resolve_upstream(
        upstream_raw, states, validate_only=validate_only
    )
    gate_probability, gate_info = _resolve_gate(gate_raw, states)
    spawn_rate = upstream_rate * gate_probability
    event_rate = spawn_rate * events_per_summon
    lifecycle_cap = limit / duration
    concurrent = min(float(limit), spawn_rate * duration)
    destroy_binding = "limit" if spawn_rate * duration >= float(limit) else "expiry"
    # ``expire_only`` 时事件只能由到期驱动，因此受 limit/duration 上限约束；
    # replace 策略下每次新召唤立即销毁一个旧实例，事件率=召唤率，不受该上限约束。
    cap_applies = destroy_policy == "expire_only"
    checks: list[dict[str, Any]] = [
        {
            "id": "gate_probability_range",
            "ok": 0.0 <= gate_probability <= 1.0,
            "detail": f"gate_probability={gate_probability!r} 必须在 [0,1]",
        },
        {
            "id": "event_rate_positive",
            "ok": event_rate > 0.0,
            "detail": f"event_rate_per_s={event_rate!r} 必须 > 0",
        },
        {
            "id": "rate_within_lifecycle_cap",
            "ok": (not cap_applies) or event_rate <= lifecycle_cap + 1e-12,
            "detail": (
                f"event_rate_per_s={event_rate!r} 超过到期驱动上限 limit/duration_s={lifecycle_cap!r}"
                if cap_applies
                else "destroy_policy 允许替换驱动，limit/duration 只决定稳态并发，不构成事件率上限"
            ),
            "applies": cap_applies,
        },
    ]
    for check in checks:
        if check["ok"] or validate_only:
            continue
        if check["id"] == "rate_within_lifecycle_cap" and on_violation == "warn":
            check["severity"] = "warning"
            continue
        raise FrequencyError(check["detail"])
    return {
        "schema": DERIVATION_SCHEMA,
        "driver": "derived_event_rate",
        "kind": kind,
        "derivation": {
            **{key: value for key, value in derivation.items() if key not in ("notes", "evidence")},
        },
        "rate_formula": rate_formula,
        "inputs": {
            "upstream": upstream_info,
            "gate": gate_info,
            "lifecycle": {
                "limit": limit,
                "duration_s": duration,
                "destroy_policy": destroy_policy,
                "events_per_summon": events_per_summon,
            },
            "assertions": assertion_doc,
        },
        "upstream_rate_per_s": upstream_rate,
        "gate_probability": gate_probability,
        "spawn_rate_per_s": spawn_rate,
        "event_rate_per_s": event_rate,
        "event_interval_s": (1.0 / event_rate) if event_rate > 0 else None,
        "steady_state_concurrent": concurrent,
        "destroy_binding": destroy_binding,
        "lifecycle_cap_per_s": lifecycle_cap,
        "checks": checks,
    }


def validate_derivation(derivation: Any) -> None:
    """结构/枚举校验（不求值）：供 execution-v1/模板契约复用。

    运行期数值约束（正事件率、到期上限）由正式执行与结果门禁负责；此处只用
    ``validate_only`` 走同一套结构解析，避免要求调用方先提供场景 state 值。
    """
    derived_event_rate(derivation, states={}, validate_only=True)


__all__ = [
    "BINDING_ATTACK_SPEED",
    "BINDING_COOLDOWN",
    "DERIVATION_SCHEMA",
    "DESTROY_POLICIES",
    "GATE_AGGREGATES",
    "GATE_KINDS",
    "MAX_RATE_ASSERTIONS",
    "RATE_FORMULAS",
    "SCHEMA_VERSION",
    "UPSTREAM_KINDS",
    "VIOLATION_POLICIES",
    "CastPolicy",
    "CooldownResult",
    "FrequencyError",
    "attack_interval",
    "cast_policy",
    "cast_time",
    "cooldown_limited_rate",
    "damage_weights_factor",
    "derived_event_rate",
    "effective_cooldown",
    "final_attack_rate",
    "validate_derivation",
    "window_skeleton",
]
