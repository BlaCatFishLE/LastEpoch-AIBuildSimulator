"""increased/reduced 加算池共享算子（P2）。

两个模型的数学相同：``total = Σ values``，乘区为 ``1 + total``；BH 的属性
缩放（Intelligence）在求和之后作为一项额外加数进入同一池，FR 没有属性加数。
本模块只做求和与因子，不筛选行、不判定标签——筛选是调用方的职责。

浮点语义与既有实现一致：对入参顺序做左到右 ``sum``，属性加数在行求和之后
相加（``sum(values) + attribute_increase``），不重排、不用补偿求和。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

SCHEMA_VERSION = "le-mechanics-increased-aggregate-v1"


@dataclass(frozen=True)
class IncreasedAggregate:
    """一个加算池的汇总结果。"""

    row_count: int
    rows_total: float
    attribute_increase: float
    total: float
    factor: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "row_count": self.row_count,
            "rows_total": self.rows_total,
            "attribute_increase": self.attribute_increase,
            "total": self.total,
            "factor": self.factor,
        }


def aggregate(
    values: Iterable[float],
    *,
    attribute_increase: float = 0.0,
) -> IncreasedAggregate:
    """加算池：``total = sum(values) + attribute_increase``；``factor = 1+total``。"""
    materialized = [float(value) for value in values]
    rows_total = sum(materialized)
    attribute = float(attribute_increase)
    total = rows_total + attribute
    return IncreasedAggregate(
        row_count=len(materialized),
        rows_total=rows_total,
        attribute_increase=attribute,
        total=total,
        factor=1.0 + total,
    )


def aggregate_rows(
    rows: Iterable[Any],
    *,
    value_key: str = "value",
    attribute_increase: float = 0.0,
) -> IncreasedAggregate:
    """从行对象/字典取 ``value`` 再聚合（行内容不重写、不排序）。"""
    values = [
        float(row[value_key]) if isinstance(row, dict) else float(getattr(row, value_key))
        for row in rows
    ]
    return aggregate(values, attribute_increase=attribute_increase)


__all__ = [
    "SCHEMA_VERSION",
    "IncreasedAggregate",
    "aggregate",
    "aggregate_rows",
]
