"""来源账本（contribution -> 行投影）薄封装（P2）。

两条数值链目前各自投影来源行：

* BH ``le_calc.ledger.build_source_ledger``：``Contribution.to_dict()`` +
  ``row_id`` / ``source_id`` / ``source_location`` / ``player_label`` /
  ``raw_value`` / ``normalized_value`` / ``active`` / ``evidence_refs``；
* FR ``hit_report.build_source_ledger_rows``：从 snapshot JSON 文档投影
  display 行（字段集合不同：没有 raw/normalized/player_label，多了 stat_key 等）。

本模块**只做薄封装，不改变行内容**：BH 直接委托既有实现；FR 委托既有
renderer 的行构造（懒导入，避免把报告模块的 import 成本带进计算包）。行顺序
也沿用各自实现（``model.contribution_sort_key``），不做统一重排。

统一的行 schema 合并属于 P3（报告层）工作，本阶段只固定“投影入口 + 等价性
测试”，不引入第二套字段。
"""

from __future__ import annotations

from typing import Any, Iterable

from .. import model
from ..ledger import build_source_ledger, contribution_row

SCHEMA_VERSION = "le-mechanics-ledger-v1"

#: BH 行投影的字段集（docstring/registry 用，不在运行时重排）
BH_LEDGER_FIELDS = (
    "row_id",
    "source_id",
    "source_location",
    "player_label",
    "raw_value",
    "normalized_value",
    "active",
    "evidence_refs",
)
#: FR 行投影的字段集
HIT_LEDGER_FIELDS = (
    "row_id",
    "contribution_id",
    "source_id",
    "source_kind",
    "source_label",
    "source_path",
    "source_location",
    "stat_key",
    "stat_name",
    "operation",
    "value",
    "unit",
    "damage_tags",
    "target",
    "condition_code",
    "condition_status",
    "included",
    "active",
    "exclusion_reason",
    "value_source",
    "status",
    "evidence_refs",
)


def project_contribution_row(contribution: model.Contribution, row_number: int) -> dict[str, Any]:
    """BH 单行投影（委托既定实现，不改字段/取值）。"""
    return contribution_row(contribution, row_number)


def project_source_ledger(snapshot: model.Snapshot) -> list[dict[str, Any]]:
    """BH 全量来源账（委托既定实现，行内容与顺序不变）。"""
    return build_source_ledger(snapshot)


def project_hit_source_ledger(snapshot_document: dict[str, Any]) -> list[dict[str, Any]]:
    """FR 来源账（委托 ``hit_report.build_source_ledger_rows``；懒导入）。"""
    from ..hit_report import build_source_ledger_rows

    return build_source_ledger_rows(snapshot_document)


def ledger_summary(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """最小共享计数（与 ``le_calc.ledger.summarize`` 口径一致，不改行）。"""
    materialized = list(rows)
    included = [row for row in materialized if row.get("included")]
    active = [row for row in materialized if row.get("active")]
    return {
        "schema": SCHEMA_VERSION,
        "total_rows": len(materialized),
        "included_rows": len(included),
        "active_rows": len(active),
        "excluded_rows": len(materialized) - len(included),
    }


def field_sets() -> dict[str, tuple[str, ...]]:
    """两套行投影的字段清单（文档/registry 用）。"""
    return {"bh_ledger": BH_LEDGER_FIELDS, "hit_report": HIT_LEDGER_FIELDS}


__all__ = [
    "BH_LEDGER_FIELDS",
    "HIT_LEDGER_FIELDS",
    "SCHEMA_VERSION",
    "field_sets",
    "ledger_summary",
    "project_contribution_row",
    "project_hit_source_ledger",
    "project_source_ledger",
]
