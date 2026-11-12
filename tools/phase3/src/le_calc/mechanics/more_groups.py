"""more/less 分组乘区共享算子（P2）。

两个模型的数学相同：``product over groups (1 + Σ values in group)``，组按键名
排序后依次相乘。**组归属必须由机制/规则显式给出**（``Contribution.group`` 或
ruleset effect 的 ``group`` 字段）；没有显式分组的行退回以 ``contribution_id``
作为组键，这仍然来自数据字段，不根据文本里的 "more" 一词推定独立乘区。

本模块不筛选行、不判标签、不读规则文件：调用方把已筛好的 more 行传进来。
行内容（来源/标签/状态）只用于生成组内引用，不做重写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

SCHEMA_VERSION = "le-mechanics-more-groups-v1"


class MoreGroupError(ValueError):
    pass


@dataclass(frozen=True)
class MoreRow:
    """参与分组乘积的一行 more/less。

    ``group`` 为 ``None`` 时退回 ``contribution_id``（与两条现有实现一致）。
    """

    contribution_id: str
    value: float
    group: str | None = None

    def group_key(self) -> str:
        return self.group or self.contribution_id


@dataclass(frozen=True)
class MoreGroup:
    """一个独立 more 乘区的汇总。"""

    group: str
    value: float
    factor: float
    contribution_ids: tuple[str, ...]
    row_indices: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "value": self.value,
            "factor": self.factor,
            "row_count": len(self.contribution_ids),
            "contribution_ids": list(self.contribution_ids),
        }


@dataclass(frozen=True)
class MoreAggregate:
    groups: tuple[MoreGroup, ...]
    product: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "groups": [group.to_dict() for group in self.groups],
            "product": self.product,
        }


def rows_from_modifier_rows(rows: Iterable[Any]) -> list[MoreRow]:
    """BH ``damage.ModifierRow`` -> :class:`MoreRow`（duck typing，不 import 生产模块）。"""
    return [
        MoreRow(
            contribution_id=str(getattr(row, "contribution_id")),
            value=float(getattr(row, "value")),
            group=getattr(row, "group", None),
        )
        for row in rows
    ]


def rows_from_hit_rows(rows: Iterable[dict[str, Any]]) -> list[MoreRow]:
    """FR 池行（dict）-> :class:`MoreRow`。"""
    return [
        MoreRow(
            contribution_id=str(row.get("contribution_id")),
            value=float(row.get("value") or 0.0),
            group=row.get("group"),
        )
        for row in rows
    ]


def aggregate(rows: Sequence[MoreRow]) -> MoreAggregate:
    """按组求和后按键名排序相乘；组内求和保持输入行顺序（不改算术顺序）。

    ``MoreGroup.row_indices`` 记录每组的原始行下标（输入顺序），供调用方
    在不改变行内容的前提下重建自己的行投影。
    """
    grouped: dict[str, dict[str, Any]] = {}
    for index, row in enumerate(rows):
        key = row.group_key()
        entry = grouped.setdefault(key, {"value": 0.0, "ids": [], "indices": []})
        entry["value"] += row.value
        entry["ids"].append(row.contribution_id)
        entry["indices"].append(index)
    groups: list[MoreGroup] = []
    product = 1.0
    for key in sorted(grouped):
        value = grouped[key]["value"]
        factor = 1.0 + value
        product *= factor
        groups.append(
            MoreGroup(
                group=key,
                value=value,
                factor=factor,
                contribution_ids=tuple(grouped[key]["ids"]),
                row_indices=tuple(grouped[key]["indices"]),
            )
        )
    return MoreAggregate(groups=tuple(groups), product=product)


def product(rows: Sequence[MoreRow]) -> float:
    """:func:`aggregate` 的 product 便捷入口。"""
    return aggregate(rows).product


__all__ = [
    "MoreAggregate",
    "MoreGroup",
    "MoreGroupError",
    "MoreRow",
    "SCHEMA_VERSION",
    "aggregate",
    "product",
    "rows_from_hit_rows",
    "rows_from_modifier_rows",
]
