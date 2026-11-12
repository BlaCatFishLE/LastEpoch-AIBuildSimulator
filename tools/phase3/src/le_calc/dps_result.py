"""结果投影共享辅助：覆盖缺口聚合、组件应用摘要、范围文本。

第一步的覆盖/账本口径集中在这里，编译器（静态缺口）和执行器（运行时缺口合并）
共同调用，避免两份语义。
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from . import contribution_mapping as mapping
from . import dps_components as components

#: 绑定失败中会形成覆盖缺口的状态（可选来源缺失/条件不满足不算）
COVERAGE_FAILURE_STATUSES = (
    "source_absent",
    "identity_mismatch",
    "semantics_mismatch",
    "unresolved",
    "unverified",
)


def gap_key(code: str, detail: str) -> tuple[str, str]:
    return (code, detail)


class CoverageBuilder:
    """按 (code, detail) 聚合缺口，保持首次出现顺序，finalize 时按 code/detail 排序。"""

    def __init__(self) -> None:
        self._gaps: list[dict[str, Any]] = []
        self._by_key: dict[tuple[str, str], dict[str, Any]] = {}

    def add(self, code: str, detail: str, **extra: Any) -> None:
        key = gap_key(code, detail)
        if key in self._by_key:
            self._by_key[key]["count"] += 1
            return
        record = {"code": code, "detail": detail, "count": 1, **extra}
        self._by_key[key] = record
        self._gaps.append(record)

    def extend(self, records: Iterable[dict[str, Any]]) -> None:
        for record in records:
            self.add(
                str(record.get("code") or "extra_gap"),
                str(record.get("detail") or ""),
                **{key: value for key, value in record.items() if key not in ("code", "detail", "count")},
            )

    def finalize(self) -> dict[str, Any]:
        gaps = sorted(self._gaps, key=lambda record: (record["code"], record["detail"]))
        return {
            "status": "partial" if gaps else "complete",
            "scope_note": "complete 仅指声明的计算范围，不代表整套 BD 全部伤害齐备",
            "gap_count": len(gaps),
            "gaps": gaps,
        }


def static_coverage_gaps(
    *,
    contributions: list[Any],
    claimed_mod_ids: set[str],
    effect_statuses: list[dict[str, Any]],
    declared_inactive_checks: list[dict[str, Any]],
    skill_constants: dict[str, Any],
    per_attribute_problems: list[str],
    extra_gaps: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """编译期可确定的覆盖缺口（未含运行时异常施加记录）。"""
    builder = CoverageBuilder()
    for contribution in contributions:
        raw = contribution.raw or {}
        coverage_class = raw.get("coverage_class")
        if coverage_class not in mapping.COVERAGE_GAP_CLASSES:
            continue
        mod_id = str(raw.get("mod_id") or "")
        if mod_id and mod_id in claimed_mod_ids:
            continue
        builder.add(
            f"unmapped:{coverage_class}",
            f"{contribution.source_label}｜{contribution.stat_name}：{contribution.exclusion_reason}",
            contribution_ids=[contribution.contribution_id],
        )
    for effect in effect_statuses:
        status = str(effect.get("status") or "")
        if status in COVERAGE_FAILURE_STATUSES:
            builder.add(
                f"effect:{status}",
                f"{effect.get('label') or effect.get('effect_id')}：{effect.get('reason') or status}",
                effect_id=effect.get("effect_id"),
            )
    for check in declared_inactive_checks:
        if not check.get("satisfied", False):
            builder.add("declared_inactive_mismatch", check.get("detail") or "声明的不生效来源与当前事实不符")
    if skill_constants and skill_constants.get("status") not in (None, "verified"):
        builder.add(
            "skill_constants_mismatch",
            str(skill_constants.get("reason") or "技能常量与目录不一致"),
            differences=list(skill_constants.get("differences") or []),
        )
    for problem in per_attribute_problems:
        builder.add("per_attribute_unmapped", problem)
    for record in extra_gaps or []:
        builder.add(str(record.get("code") or "extra_gap"), str(record.get("detail") or ""))
    return builder.finalize()["gaps"]


def merge_runtime_gaps(
    base_gaps: list[dict[str, Any]],
    *,
    unmodeled_chance_records: list[dict[str, Any]],
) -> dict[str, Any]:
    """把运行时异常施加记录合并进编译期缺口，得到最终 coverage。"""
    builder = CoverageBuilder()
    builder.extend(base_gaps)
    for record in unmodeled_chance_records:
        if record.get("status") != components.UNRESOLVED:
            continue
        builder.add(
            "ailment_chance_unmodeled",
            f"{record.get('contribution_id')}：{record.get('reason')}",
            contribution_ids=[record.get("contribution_id")],
        )
    return builder.finalize()


def application_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for record in records:
        entry = summary.setdefault(
            record["contribution_id"],
            {"statuses": {}, "components": [], "reasons": []},
        )
        status = record["status"]
        entry["statuses"][status] = entry["statuses"].get(status, 0) + 1
        if status == components.APPLIED:
            entry["components"].append(record["component_id"])
        elif record.get("reason"):
            reason = str(record["reason"])
            if reason not in entry["reasons"]:
                entry["reasons"].append(reason)
    return summary


def build_scope(
    *,
    skill_name: str,
    hit_components: list[dict[str, Any]],
    dot_components: list[dict[str, Any]],
    controls: list[dict[str, Any]],
    states: dict[str, Any],
    target: dict[str, Any],
    limitations: list[str],
) -> tuple[str, dict[str, Any]]:
    hit_types = [component["damage_type"] for component in hit_components]
    ailment_names = [str(ailment.get("label_zh") or ailment.get("name")) for ailment in dot_components]
    base = f"{skill_name} 本体命中"
    if hit_types:
        base += "（" + "、".join(hit_types) + "）"
    if ailment_names:
        base += "与自然" + "、".join(ailment_names)
    scope = base + "；固定条件估算"
    fixed_conditions = {
        control["key"]: states.get(control["key"])
        for control in controls
        if states.get(control["key"]) not in (False, 0)
    }
    detail = {
        "skill": skill_name,
        "hit_component_ids": [component.get("id") for component in hit_components],
        "ailment_component_ids": [ailment.get("id") for ailment in dot_components],
        "hit_damage_types": hit_types,
        "ailments": ailment_names,
        "target": dict(target),
        "fixed_conditions": fixed_conditions,
        "limitations": list(limitations),
    }
    return scope, detail


__all__ = [
    "COVERAGE_FAILURE_STATUSES",
    "CoverageBuilder",
    "application_summary",
    "build_scope",
    "gap_key",
    "merge_runtime_gaps",
    "static_coverage_gaps",
]
