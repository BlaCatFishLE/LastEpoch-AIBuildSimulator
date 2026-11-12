"""暴击期望共享算子（P2）。

公式（FR hit 口径）::

    crit_chance     = clamp((base + Σadded) × (1 + Σincreased) × Π(1 + more 组), 0, cap)
    crit_multiplier = (base + Σadded) × (1 + Σincreased) × Π(1 + more 组)
    avg_crit        = (1 - crit_chance) + crit_chance × crit_multiplier

Black Hole 模型当前没有暴击（BH ruleset 不含 ``critical`` 块，伤害链里也没有
crit 因子），因此本算子带显式 capability 开关：``crit_enabled=False`` 时
``avg_crit`` 恒为 1.0，chance/multiplier 只作为登记信息、不进入伤害。

调用方负责从行池里筛出暴击率/暴伤行并算好 added/increased/more 三个输入；
本模块不做标签筛选、不读规则文件、无全局状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

SCHEMA_VERSION = "le-mechanics-crit-expected-v1"

DEFAULT_CHANCE_CAP = 1.0


@dataclass(frozen=True)
class CritCapability:
    """模型是否启用暴击（BH=False / FR=True）。"""

    crit_enabled: bool
    model_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"schema": SCHEMA_VERSION, "model_id": self.model_id, "crit_enabled": self.crit_enabled}


BH_CRIT_CAPABILITY = CritCapability(crit_enabled=False, model_id="black-hole")
FR_HIT_CRIT_CAPABILITY = CritCapability(crit_enabled=True, model_id="flame-reave-hit")


@dataclass(frozen=True)
class CritPools:
    """暴击率/暴伤池的输入（added 与 increased 已由调用方按标签筛好）。"""

    base_chance: float = 0.0
    chance_added: float = 0.0
    chance_increased: float = 0.0
    chance_more_product: float = 1.0
    base_multiplier: float = 1.0
    multiplier_added: float = 0.0
    multiplier_increased: float = 0.0
    multiplier_more_product: float = 1.0
    chance_cap: float = DEFAULT_CHANCE_CAP

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_chance": self.base_chance,
            "chance_added": self.chance_added,
            "chance_increased": self.chance_increased,
            "chance_more_product": self.chance_more_product,
            "base_multiplier": self.base_multiplier,
            "multiplier_added": self.multiplier_added,
            "multiplier_increased": self.multiplier_increased,
            "multiplier_more_product": self.multiplier_more_product,
            "chance_cap": self.chance_cap,
        }


@dataclass(frozen=True)
class CritResult:
    crit_enabled: bool
    crit_chance: float
    crit_multiplier: float
    avg_crit: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "crit_enabled": self.crit_enabled,
            "crit_chance": self.crit_chance,
            "crit_multiplier": self.crit_multiplier,
            "avg_crit": self.avg_crit,
        }


def avg_crit(crit_chance: float, crit_multiplier: float) -> float:
    """``avg = (1 - c) + c × m``（逐位保持既有运算顺序）。"""
    chance = float(crit_chance)
    multiplier = float(crit_multiplier)
    return (1.0 - chance) + chance * multiplier


def crit_pools(pools: CritPools) -> tuple[float, float]:
    """由池输入算 ``(crit_chance, crit_multiplier)``，顺序与既有实现一致。"""
    crit_chance = (
        (float(pools.base_chance) + float(pools.chance_added))
        * (1.0 + float(pools.chance_increased))
        * float(pools.chance_more_product)
    )
    cap = float(pools.chance_cap)
    crit_chance = max(0.0, min(crit_chance, cap))
    crit_multiplier = (
        (float(pools.base_multiplier) + float(pools.multiplier_added))
        * (1.0 + float(pools.multiplier_increased))
        * float(pools.multiplier_more_product)
    )
    return crit_chance, crit_multiplier


def expected_crit(
    *,
    capability: CritCapability,
    pools: Optional[CritPools] = None,
) -> CritResult:
    """按模型 capability 返回期望暴击乘区；BH 无暴击时 avg 恒 1.0。"""
    if not capability.crit_enabled:
        return CritResult(crit_enabled=False, crit_chance=0.0, crit_multiplier=1.0, avg_crit=1.0)
    if pools is None:
        raise ValueError("crit_enabled=True 时必须提供 CritPools")
    chance, multiplier = crit_pools(pools)
    return CritResult(
        crit_enabled=True,
        crit_chance=chance,
        crit_multiplier=multiplier,
        avg_crit=avg_crit(chance, multiplier),
    )


__all__ = [
    "BH_CRIT_CAPABILITY",
    "DEFAULT_CHANCE_CAP",
    "FR_HIT_CRIT_CAPABILITY",
    "SCHEMA_VERSION",
    "CritCapability",
    "CritPools",
    "CritResult",
    "avg_crit",
    "crit_pools",
    "expected_crit",
]
