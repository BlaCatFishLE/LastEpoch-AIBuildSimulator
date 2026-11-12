"""目标侧结算共享算子（P2）。

公式（两个模型共同口径）::

    实际伤害 = 模型伤害 × (1 - 有效抗性) × (1 - 怪物减伤 DR)

两个模型的分歧点被显式参数化，**不在这里强行统一**：

``penetration_mode``
    * ``fold_into_resistance``（Black Hole 口径）：穿透折进有效抗性
      ``effective = resistance - penetration``，令 ``effective`` 可负；
    * ``separate_multiplier``（Flame Reave hit 口径）：穿透是独立乘区
      ``(1 + penetration) × (1 - resistance)``。

``negative_resistance_clamp``
    * ``allow``：保留负有效抗性（BH v4 / FR 当前口径）；
    * ``clamp_at_zero``：有效抗性下限 0（BH ruleset 的
      ``negative_resistance_allowed=false``）。
    只对 ``fold_into_resistance`` 定义；``separate_multiplier`` 下该组合
    没有已确认语义，构造策略时直接拒绝，不猜。

``dr_source``
    * ``scenario_override``：从 scenario 的 ``target.monster_damage_reduction``
      读取（BH：``report.build_result`` 会把 ``monster_dr.describe(level)`` 的
      DR 注入 scenario；recompute 覆盖也走这条）；
    * ``table_by_level``：忽略 scenario 显式值，按 ``monster_level`` 查
      ``le_calc.monster_dr`` 的 1..100 级同一张表（FR 口径）。

参数全部由调用方显式传入；本模块不读规则文件、不读当前案例、无模块级可变
状态。两个生产调用点（``damage._target_multiplier`` /
``hit_damage.resolve_target`` + 组件乘区）接的就是这里的算子。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ..monster_dr import dr_for_level

SCHEMA_VERSION = "le-mechanics-target-side-v1"

#: 穿透处理方式
PENETRATION_FOLD = "fold_into_resistance"
PENETRATION_SEPARATE = "separate_multiplier"
PENETRATION_MODES = (PENETRATION_FOLD, PENETRATION_SEPARATE)

#: 负抗性策略
NEGATIVE_ALLOW = "allow"
NEGATIVE_CLAMP_AT_ZERO = "clamp_at_zero"
NEGATIVE_RESISTANCE_POLICIES = (NEGATIVE_ALLOW, NEGATIVE_CLAMP_AT_ZERO)

#: 怪物 DR 来源
DR_FROM_SCENARIO = "scenario_override"
DR_FROM_TABLE = "table_by_level"
DR_SOURCES = (DR_FROM_SCENARIO, DR_FROM_TABLE)

#: 目标抗性来源（body 读 scenario.target；ignite 历史口径读 ruleset base）
RESISTANCE_FROM_SCENARIO_TARGET = "scenario_target"
RESISTANCE_FROM_RULESET_BASE = "ruleset_base"
RESISTANCE_SOURCES = (RESISTANCE_FROM_SCENARIO_TARGET, RESISTANCE_FROM_RULESET_BASE)


class TargetSideError(ValueError):
    """目标侧结算策略无效（未定义组合或非法枚举值）。"""


@dataclass(frozen=True)
class TargetSidePolicy:
    """目标侧结算策略；字段含义见模块文档。"""

    penetration_mode: str = PENETRATION_FOLD
    negative_resistance_clamp: str = NEGATIVE_ALLOW
    dr_source: str = DR_FROM_SCENARIO

    def __post_init__(self) -> None:
        if self.penetration_mode not in PENETRATION_MODES:
            raise TargetSideError(
                f"penetration_mode={self.penetration_mode!r} 不在 {PENETRATION_MODES}"
            )
        if self.negative_resistance_clamp not in NEGATIVE_RESISTANCE_POLICIES:
            raise TargetSideError(
                "negative_resistance_clamp="
                f"{self.negative_resistance_clamp!r} 不在 {NEGATIVE_RESISTANCE_POLICIES}"
            )
        if self.dr_source not in DR_SOURCES:
            raise TargetSideError(f"dr_source={self.dr_source!r} 不在 {DR_SOURCES}")
        if (
            self.penetration_mode == PENETRATION_SEPARATE
            and self.negative_resistance_clamp == NEGATIVE_CLAMP_AT_ZERO
        ):
            raise TargetSideError(
                "separate_multiplier + clamp_at_zero 没有已确认的游戏语义；"
                "需要新证据后再实现，不能默认猜成其中一种"
            )

    def to_dict(self) -> dict[str, str]:
        return {
            "schema": SCHEMA_VERSION,
            "penetration_mode": self.penetration_mode,
            "negative_resistance_clamp": self.negative_resistance_clamp,
            "dr_source": self.dr_source,
        }


#: Black Hole 当前口径（fold + 负抗性由 ruleset 决定 + scenario 注入 DR）
BH_TARGET_SIDE_POLICY = TargetSidePolicy(
    penetration_mode=PENETRATION_FOLD,
    negative_resistance_clamp=NEGATIVE_ALLOW,
    dr_source=DR_FROM_SCENARIO,
)
#: Flame Reave hit 当前口径（独立穿透乘区 + 查表 DR）
FR_HIT_TARGET_SIDE_POLICY = TargetSidePolicy(
    penetration_mode=PENETRATION_SEPARATE,
    negative_resistance_clamp=NEGATIVE_ALLOW,
    dr_source=DR_FROM_TABLE,
)


@dataclass(frozen=True)
class MonsterDR:
    """一次目标侧结算里的怪物 DR（值、因子与来源）。"""

    monster_damage_reduction: float
    monster_dr_factor: float
    dr_source: str
    monster_level: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "monster_damage_reduction": self.monster_damage_reduction,
            "monster_dr_factor": self.monster_dr_factor,
            "dr_source": self.dr_source,
            "monster_level": self.monster_level,
        }


@dataclass(frozen=True)
class TargetSideResult:
    """一条伤害组件在目标侧结算后的分项。"""

    resistance: float
    penetration: float
    effective_resistance: float
    resistance_multiplier: float
    monster_damage_reduction: float
    monster_dr_factor: float
    multiplier: float
    penetration_multiplier: float
    penetration_applied: bool
    policy: TargetSidePolicy

    def to_dict(self) -> dict[str, Any]:
        return {
            "resistance": self.resistance,
            "penetration": self.penetration,
            "effective_resistance": self.effective_resistance,
            "resistance_multiplier": self.resistance_multiplier,
            "monster_damage_reduction": self.monster_damage_reduction,
            "monster_dr_factor": self.monster_dr_factor,
            "multiplier": self.multiplier,
            "penetration_multiplier": self.penetration_multiplier,
            "penetration_applied": self.penetration_applied,
            "policy": self.policy.to_dict(),
        }


def resolve_monster_dr(
    *,
    dr_source: str,
    monster_level: Optional[int] = None,
    scenario_monster_damage_reduction: Any = None,
) -> MonsterDR:
    """按策略取得怪物 DR；``table_by_level`` 不读 scenario 显式值（FR 口径）。"""
    if dr_source not in DR_SOURCES:
        raise TargetSideError(f"dr_source={dr_source!r} 不在 {DR_SOURCES}")
    if dr_source == DR_FROM_TABLE:
        level = None if monster_level is None else int(monster_level)
        dr = float(dr_for_level(level))
        return MonsterDR(
            monster_damage_reduction=dr,
            monster_dr_factor=1.0 - dr,
            dr_source=dr_source,
            monster_level=level,
        )
    # scenario_override: 保持既有 ``float(value or 0.0)`` 的取值语义（None/缺失->0）
    dr = float(scenario_monster_damage_reduction or 0.0)
    return MonsterDR(
        monster_damage_reduction=dr,
        monster_dr_factor=1.0 - dr,
        dr_source=dr_source,
        monster_level=None if monster_level is None else int(monster_level),
    )


def compute_target_multiplier(
    *,
    policy: TargetSidePolicy,
    resistance: float,
    monster_damage_reduction: float,
    penetration: float = 0.0,
    penetration_applied: bool = True,
) -> TargetSideResult:
    """按策略计算目标侧乘区（分项 + 总乘区）。

    浮点运算顺序与两个模型的既有实现逐位一致（见
    ``test_mechanics_target_side.py`` 的冻结基线）：接口只做参数化，不改算术。
    """
    resistance = float(resistance)
    monster_damage_reduction = float(monster_damage_reduction)
    effective_penetration = float(penetration) if penetration_applied else 0.0
    monster_dr_factor = 1.0 - monster_damage_reduction
    penetration_multiplier = 1.0 + float(penetration)

    if policy.penetration_mode == PENETRATION_FOLD:
        effective_resistance = resistance - effective_penetration
        if policy.negative_resistance_clamp == NEGATIVE_CLAMP_AT_ZERO:
            effective_resistance = max(0.0, effective_resistance)
        resistance_multiplier = 1.0 - effective_resistance
    else:  # separate_multiplier
        # 不折叠穿透：effective_resistance 只保留目标抗性，穿透在独立乘区里
        effective_resistance = resistance
        resistance_multiplier = penetration_multiplier * (1.0 - resistance)

    multiplier = resistance_multiplier * monster_dr_factor
    return TargetSideResult(
        resistance=resistance,
        penetration=effective_penetration,
        effective_resistance=effective_resistance,
        resistance_multiplier=resistance_multiplier,
        monster_damage_reduction=monster_damage_reduction,
        monster_dr_factor=monster_dr_factor,
        multiplier=multiplier,
        penetration_multiplier=penetration_multiplier,
        penetration_applied=bool(penetration_applied),
        policy=policy,
    )


# ---------------------------------------------------------------------------
# Black Hole 适配器（生产调用点：damage._target_multiplier）
# ---------------------------------------------------------------------------


def bh_policy(ruleset_target_side: Mapping[str, Any]) -> TargetSidePolicy:
    """从 BH ruleset 的 ``target_side`` 配置构造策略（显式映射，不改数值）。"""
    negative_allowed = bool(ruleset_target_side.get("negative_resistance_allowed", True))
    return TargetSidePolicy(
        penetration_mode=PENETRATION_FOLD,
        negative_resistance_clamp=NEGATIVE_ALLOW if negative_allowed else NEGATIVE_CLAMP_AT_ZERO,
        dr_source=DR_FROM_SCENARIO,
    )


def bh_target_side(
    *,
    ruleset_target_side: Mapping[str, Any],
    scenario_target: Mapping[str, Any],
    damage_type: str,
    penetration_total: float,
    component_scope: str = "body",
    resistance_source: str = RESISTANCE_FROM_SCENARIO_TARGET,
) -> dict[str, Any]:
    """BH 目标乘区块；返回字段与改造前 ``damage._target_multiplier`` 完全同形。

    ``resistance_source`` 显式区分两种已存在的口径：

    * ``scenario_target``（body 默认）：从 ``scenario.target.resistances`` 读；
    * ``ruleset_base``（BH Ignite 历史口径）：从
      ``ruleset.target_side.base_resistances`` 读，忽略 scenario 抗性覆盖。

    参数必须显式给出，避免两个调用点“碰巧相同”时把来源差异静默合并。
    """
    if resistance_source not in RESISTANCE_SOURCES:
        raise TargetSideError(
            f"resistance_source={resistance_source!r} 不在 {RESISTANCE_SOURCES}"
        )
    target = scenario_target or {}
    if resistance_source == RESISTANCE_FROM_RULESET_BASE:
        base_resistance = float(
            (ruleset_target_side.get("base_resistances") or {}).get(
                damage_type.lower(), 0.0
            )
        )
    else:
        base_resistance = float(
            (target.get("resistances") or {}).get(damage_type.lower(), 0.0)
        )
    negative_allowed = bool(ruleset_target_side.get("negative_resistance_allowed", True))
    applies_to = ruleset_target_side.get("penetration_applies_to") or [component_scope]
    penetration_applies = component_scope in applies_to
    policy = bh_policy(ruleset_target_side)
    monster_level = int(target.get("monster_level", 0) or 0)
    dr = resolve_monster_dr(
        dr_source=policy.dr_source,
        monster_level=monster_level,
        scenario_monster_damage_reduction=target.get("monster_damage_reduction", 0.0),
    )
    # Keep the caller's numeric type for ``penetration`` (historical output had
    # an int 0 when a component had no penetration rows); arithmetic is float.
    penetration_value = penetration_total if penetration_applies else 0.0
    result = compute_target_multiplier(
        policy=policy,
        resistance=base_resistance,
        monster_damage_reduction=dr.monster_damage_reduction,
        penetration=penetration_value,
        penetration_applied=penetration_applies,
    )
    return {
        "target_resistance": base_resistance,
        "penetration": penetration_value,
        "effective_resistance": result.effective_resistance,
        "resistance_multiplier": result.resistance_multiplier,
        "monster_level": monster_level,
        "monster_damage_reduction": dr.monster_damage_reduction,
        "monster_dr_factor": dr.monster_dr_factor,
        "multiplier": result.multiplier,
        "negative_resistance_allowed": negative_allowed,
        "penetration_applied": penetration_applies,
    }


# ---------------------------------------------------------------------------
# Flame Reave hit 适配器（生产调用点：hit_damage.resolve_target + 组件乘区）
# ---------------------------------------------------------------------------


def resolve_hit_target(
    merged_target: Mapping[str, Any],
    *,
    default_level: int = 100,
) -> dict[str, Any]:
    """FR ``resolve_target`` 的共享实现；返回字段与改造前完全同形。

    ``merged_target`` 必须已由调用方合并 ruleset.target 与 scenario.target
    （合并规则属于输入适配，不是目标侧数学）。
    """
    target = merged_target or {}
    monster_level = target.get("monster_level")
    if monster_level is None:
        monster_level = default_level
    fire_resistance = float(target.get("fire_resistance") or 0.0)
    dr = resolve_monster_dr(
        dr_source=DR_FROM_TABLE,
        monster_level=int(monster_level),
    )
    return {
        "boss_or_rare": bool(target.get("boss_or_rare", True)),
        "monster_level": int(monster_level),
        "monster_damage_reduction": dr.monster_damage_reduction,
        "monster_dr_factor": dr.monster_dr_factor,
        "fire_resistance": fire_resistance,
        "resistance_factor": 1.0 - fire_resistance,
        "source": "ruleset.target + scenario.target",
    }


def hit_component_target(
    *,
    resistance: float,
    penetration_total: float,
    monster_damage_reduction: float,
) -> TargetSideResult:
    """FR 单组件目标乘区（独立穿透乘区）；生产字段映射见 ``hit_damage``。"""
    return compute_target_multiplier(
        policy=FR_HIT_TARGET_SIDE_POLICY,
        resistance=resistance,
        monster_damage_reduction=monster_damage_reduction,
        penetration=penetration_total,
        penetration_applied=True,
    )


__all__ = [
    "BH_TARGET_SIDE_POLICY",
    "DR_FROM_SCENARIO",
    "DR_FROM_TABLE",
    "DR_SOURCES",
    "FR_HIT_TARGET_SIDE_POLICY",
    "MonsterDR",
    "NEGATIVE_ALLOW",
    "NEGATIVE_CLAMP_AT_ZERO",
    "NEGATIVE_RESISTANCE_POLICIES",
    "PENETRATION_FOLD",
    "PENETRATION_MODES",
    "PENETRATION_SEPARATE",
    "RESISTANCE_FROM_RULESET_BASE",
    "RESISTANCE_FROM_SCENARIO_TARGET",
    "RESISTANCE_SOURCES",
    "SCHEMA_VERSION",
    "TargetSideError",
    "TargetSidePolicy",
    "TargetSideResult",
    "bh_policy",
    "bh_target_side",
    "compute_target_multiplier",
    "hit_component_target",
    "resolve_hit_target",
    "resolve_monster_dr",
]
