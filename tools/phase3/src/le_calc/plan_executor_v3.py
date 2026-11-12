"""plan-v3 / result-v5 执行器：纯 DoT、独立施加时钟、多实例并发、充能调度。

不 import 旧 Black Hole 计算器（``damage``/``ignite``/``cooldown``/``simulate``）；
所有公式只读取 plan-v3 的固定语义、effects 与场景值。

显式支持（未声明/未实现的语义在结构校验入口拒绝）：
  * dot 组件：逐跳伤害、tick 间隔、``ceil_full_ticks`` 余量策略、共享持续时间；
  * 独立施加时钟：每次施放的 spawn/到期；Ignite 每次 roll 独立计时并各自到期；
  * 多实例并发：充能驱动的施放队列，重叠黑洞并存（``max_concurrent`` 采样）；
  * 充能调度：eager / patient，charges / recharge / CDR / ICR / flat 调整。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Optional

from . import calculation_plan as planmod
from . import dps_components
from . import dps_result
from . import model
from . import plan_handlers
from .plan_executor import (
    PlanValidationError,
    build_contributions,
    project_coverage,
    project_ledger,
    validate_plan,
    validate_scenario,
)
from .plan_executor_v2 import resolve_evaluation_scenarios
from .mechanics.dot import (
    _active_rows,
    _attribute_increase,
    _closure,
    _matching,
    _more_product,
    _resistance_totals,
    _scope_allows,
    _sum,
    _target_multiplier,
    cohort_between as _ignite_between,
    dot_component_result as _dot_component_result,
    make_cast as _make_cast,
    max_concurrent as _max_concurrent,
    window_metrics as _window_metrics,
)
from .mechanics.applications import (
    application_cohort as _ignite_application,
    application_core as _ignite_core,
    single_cast as _single_cast,
)
from .mechanics.charge import (
    blocked_attempts as _blocked_attempts,
    charge_loop as _charge_loop,
    charge_parameters as _charge_parameters,
)

SCHEMA_RESULT_V5 = "le-dps-result-v5"
EPS = 1e-9
SAMPLE_STEP_S = 0.25
REMAINDER_POLICIES = ("ceil_full_ticks",)
ACTION_POLICIES = ("eager", "patient")
ELEMENTAL_TYPES = ("Fire", "Cold", "Lightning")


# ---------------------------------------------------------------------------
# 结构校验
# ---------------------------------------------------------------------------


def _issue(issues: list[dict[str, Any]], message: str, related: Optional[list[str]] = None) -> None:
    issues.append(
        {
            "code": "plan_v3_unsupported",
            "message": message,
            "severity": "error",
            "related_ids": list(related or []),
            "affects_coverage": False,
        }
    )


def validate_plan_v3(plan: dict[str, Any], runtime: Optional[plan_handlers.PlanRuntime] = None) -> list[dict[str, Any]]:
    issues = list(validate_plan(plan, runtime))
    if str(plan.get("schema")) != planmod.SCHEMA_V3:
        _issue(issues, f"不是 plan-v3：{plan.get('schema')!r}")
        return issues
    components = plan.get("components")
    if not isinstance(components, list) or not components:
        _issue(issues, "plan-v3 需要至少一个 dot 组件")
        return issues
    seen: set[str] = set()
    durations: set[float] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            _issue(issues, f"components[{index}] 不是对象")
            continue
        component_id = str(component.get("component_id") or "")
        if not component_id or component_id in seen:
            _issue(issues, f"components[{index}].component_id 为空或重复", [component_id])
        seen.add(component_id)
        if component.get("kind") != "dot":
            _issue(issues, f"组件 {component_id} kind 必须是 dot（plan-v3 不伪造 Hit）", [component_id])
        if component.get("scope") not in ("body",):
            _issue(issues, f"组件 {component_id} scope 必须是 body（Ignite 由 schedule.ignite 表达）", [component_id])
        if not component.get("damage_type"):
            _issue(issues, f"组件 {component_id} 缺少 damage_type", [component_id])
        if bool(component.get("crit_capable")):
            _issue(issues, f"组件 {component_id} 是纯 DoT，不得 crit_capable", [component_id])
        if not isinstance(component.get("base_damage"), (int, float)) or float(component["base_damage"]) < 0:
            _issue(issues, f"组件 {component_id} base_damage 必须是非负数", [component_id])
        if not isinstance(component.get("added_damage_scaling"), (int, float)) or float(component["added_damage_scaling"]) < 0:
            _issue(issues, f"组件 {component_id} added_damage_scaling 必须是非负数", [component_id])
        if not isinstance(component.get("tick_interval_s"), (int, float)) or float(component["tick_interval_s"]) <= 0:
            _issue(issues, f"组件 {component_id} tick_interval_s 必须为正数", [component_id])
        if not isinstance(component.get("duration_s"), (int, float)) or float(component["duration_s"]) <= 0:
            _issue(issues, f"组件 {component_id} duration_s 必须为正数", [component_id])
        else:
            durations.add(float(component["duration_s"]))
        if component.get("remainder_policy") not in REMAINDER_POLICIES:
            _issue(issues, f"组件 {component_id} remainder_policy 只支持 {REMAINDER_POLICIES}", [component_id])
        if component.get("damage_at_start") is not True:
            _issue(
                issues,
                f"组件 {component_id} damage_at_start 只支持 true（false 未实现，不得接受后忽略）",
                [component_id],
            )
    if len(durations) > 1:
        _issue(issues, "plan-v3 当前要求全部 dot 组件共享同一持续时间")

    schedule = plan.get("schedule") or {}
    if schedule.get("schema") != "le-schedule-v3":
        _issue(issues, "schedule.schema 必须是 le-schedule-v3")
    if schedule.get("driver") != "charge_dot_cycle":
        _issue(issues, "plan-v3 schedule.driver 只支持 charge_dot_cycle")
    horizon = schedule.get("horizon_s")
    if not isinstance(horizon, (int, float)) or float(horizon) <= 0:
        _issue(issues, "schedule.horizon_s 必须为正数")
    windows = schedule.get("windows_s")
    if not isinstance(windows, list) or not windows:
        _issue(issues, "schedule.windows_s 必须是非空数组")
    else:
        numbers = [float(item) for item in windows if isinstance(item, (int, float))]
        if len(numbers) != len(windows) or any(value <= 0 for value in numbers):
            _issue(issues, "schedule.windows_s 必须是正数数组")
        elif numbers != sorted(numbers) or numbers[-1] > float(horizon or 0) + EPS:
            _issue(issues, "schedule.windows_s 必须升序且不超过 horizon_s")
    cast = schedule.get("cast") or {}
    if not isinstance(cast.get("base_rate_per_s"), (int, float)) or float(cast.get("base_rate_per_s") or 0) <= 0:
        _issue(issues, "schedule.cast.base_rate_per_s 必须为正数")
    charges = schedule.get("charges") or {}
    if not isinstance(charges.get("base_max_charges"), int) or int(charges.get("base_max_charges") or 0) < 1:
        _issue(issues, "schedule.charges.base_max_charges 必须是 >=1 的整数")
    if not isinstance(charges.get("base_recharge_s"), (int, float)) or float(charges.get("base_recharge_s") or 0) <= 0:
        _issue(issues, "schedule.charges.base_recharge_s 必须为正数")
    if not isinstance(charges.get("flat_adjustment_s", 0.0), (int, float)) or float(charges.get("flat_adjustment_s", 0.0)) < 0:
        _issue(issues, "schedule.charges.flat_adjustment_s 必须是非负数")
    if not isinstance(charges.get("cooldown_reduction", 0.0), (int, float)) or not (0.0 <= float(charges.get("cooldown_reduction", 0.0)) < 1.0):
        _issue(issues, "schedule.charges.cooldown_reduction 必须在 [0,1)")
    if not isinstance(charges.get("extra_max_charges", 0), int) or int(charges.get("extra_max_charges", 0)) < 0:
        _issue(issues, "schedule.charges.extra_max_charges 必须是非负整数")
    policies = schedule.get("action_policies") or []
    policy_ids = {str(item.get("policy_id")) for item in policies if isinstance(item, dict)}
    if policy_ids != set(ACTION_POLICIES):
        _issue(issues, f"action_policies 必须恰好声明 {ACTION_POLICIES}")
    if str(schedule.get("primary_policy")) not in set(ACTION_POLICIES):
        _issue(issues, "schedule.primary_policy 不在 action_policies")
    ignite = schedule.get("ignite")
    if not isinstance(ignite, dict):
        _issue(issues, "plan-v3 需要 schedule.ignite 施加模型")
    else:
        if not isinstance(ignite.get("base_damage"), (int, float)) or float(ignite["base_damage"]) <= 0:
            _issue(issues, "schedule.ignite.base_damage 必须为正数")
        if not isinstance(ignite.get("base_duration_s"), (int, float)) or float(ignite["base_duration_s"]) <= 0:
            _issue(issues, "schedule.ignite.base_duration_s 必须为正数")
        if not isinstance(ignite.get("scaling_tags"), list) or not ignite["scaling_tags"]:
            _issue(issues, "schedule.ignite.scaling_tags 必须非空")
        if not isinstance(ignite.get("rolls_per_second"), (int, float)) or float(ignite["rolls_per_second"]) <= 0:
            _issue(issues, "schedule.ignite.rolls_per_second 必须为正数")
        if ignite.get("first_roll_at") != "spawn":
            _issue(issues, "schedule.ignite.first_roll_at 只支持 spawn")
        if not isinstance(ignite.get("chance_total"), (int, float)) or float(ignite["chance_total"]) < 0:
            _issue(issues, "schedule.ignite.chance_total 必须是非负数")

    evaluations = plan.get("evaluations") or []
    if not isinstance(evaluations, list) or not evaluations:
        _issue(issues, "plan-v3 需要 evaluations")
    else:
        if len([item for item in evaluations if item.get("primary")]) != 1:
            _issue(issues, "plan-v3 evaluations 必须恰好一个 primary")
        ids = [str(item.get("evaluation_id")) for item in evaluations]
        if len(set(ids)) != len(ids):
            _issue(issues, "evaluation_id 重复")
        used = set()
        for item in evaluations:
            policy = str(item.get("action_policy") or "")
            if policy not in ACTION_POLICIES:
                _issue(issues, f"evaluation {item.get('evaluation_id')} 的 action_policy 非法")
            if policy in used:
                _issue(issues, f"action_policy {policy} 只能出现在一个 evaluation")
            used.add(policy)
            if not item.get("scenario_sha256"):
                _issue(issues, f"evaluation {item.get('evaluation_id')} 缺少场景哈希")
        if used != set(ACTION_POLICIES):
            _issue(issues, "plan-v3 需要 eager/patient 两个 evaluation")
    return issues


# ---------------------------------------------------------------------------
# 数值核心
# ---------------------------------------------------------------------------




























def _runtime_condition_filter(
    rows: list[model.Contribution],
    *,
    states: Mapping[str, Any],
    target: Mapping[str, Any],
    runtime: plan_handlers.PlanRuntime,
) -> list[model.Contribution]:
    """按运行期场景重算 condition_code 行（编译期 fake states 不能固化条件结果）。"""
    values = {**dict(states), **dict(target)}
    evaluate = runtime.handler("condition_eval-v1")
    filtered: list[model.Contribution] = []
    for row in rows:
        if not row.condition_code:
            filtered.append(row)
            continue
        result = evaluate(str(row.condition_code), values)
        if result is True:
            row.included = True
            row.exclusion_reason = None
            row.condition_status = model.CONDITION_SATISFIED
        elif result is False:
            row.included = False
            row.exclusion_reason = f"条件未满足：{row.condition_code}"
            row.condition_status = model.CONDITION_NOT_SATISFIED
        else:
            row.included = False
            row.exclusion_reason = f"条件无法判定：{row.condition_code}"
            row.condition_status = model.CONDITION_UNKNOWN
        filtered.append(row)
    return filtered


def _cast_time(plan: Mapping[str, Any], rows: list[model.Contribution]) -> dict[str, Any]:
    base_rate = float(plan["schedule"]["cast"]["base_rate_per_s"])
    increased = _sum(
        [
            row
            for row in _active_rows(rows)
            if row.stat_key == model.STAT_CAST_SPEED and row.operation in ("increased", "added")
        ]
    )
    rate = base_rate * (1.0 + increased)
    return {
        "base_rate_per_s": base_rate,
        "animation_speed": float(plan["schedule"]["cast"].get("animation_speed") or 0.0),
        "speed_multiplier": float(plan["schedule"]["cast"].get("speed_multiplier") or 1.0),
        "increased_cast_speed": increased,
        "rate_per_s": rate,
        "cast_time_s": 1.0 / rate if rate > 0 else 0.0,
        "formula": "release_rate = base_rate * (1 + increased cast speed); cast_time = 1 / release_rate",
    }
















# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


#: 组件级应用单元的统计角色。
_BODY_DAMAGE_STATS = frozenset({"damage", "added_damage", "penetration"})
_BODY_DURATION_STATS = frozenset({"skill_duration"})
_IGNITE_DAMAGE_STATS = frozenset({"damage", "penetration"})
_COVERAGE_RELEVANT_STATS = frozenset(
    {"damage", "added_damage", "penetration", "penetration_with_ignite", "ailment_duration", "ailment_chance"}
)
_FREQUENCY_STATS = frozenset(
    {
        "cooldown_recovery_speed",
        "cooldown_reduction",
        "cooldown_flat",
        "charges",
        "cast_speed",
        "attack_speed",
    }
)


def coverage_applications(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """覆盖投影只消费真正影响伤害/施加的未解析记录（与元素/杂项属性无关）。"""
    return [
        record
        for record in applications
        if str(record.get("status")) == dps_components.UNRESOLVED
        and str(record.get("stat_key")) in _COVERAGE_RELEVANT_STATS
    ]


def _application_units(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    from .mechanics import tag_match  # noqa: PLC0415

    units: list[dict[str, Any]] = []
    for component in plan.get("components") or []:
        if component.get("kind") != "dot":
            continue
        closure = frozenset(
            {str(component.get("damage_type") or "")}
            | {str(tag) for tag in component.get("tags") or []}
        )
        units.append(
            {
                "component_id": str(component["component_id"]),
                "kind": "dot",
                "scope": "body",
                "closure": frozenset(tag for tag in closure if tag),
                "tags": sorted(tag for tag in closure if tag),
            }
        )
    ignite_tags = (plan["schedule"]["ignite"].get("scaling_tags") or [])
    units.append(
        {
            "component_id": "bh-ignite",
            "kind": "ignite",
            "scope": "ignite",
            "closure": frozenset(str(tag) for tag in ignite_tags),
            "tags": [str(tag) for tag in ignite_tags],
        }
    )
    return units


def _application_reason(row: model.Contribution) -> str:
    if not row.included:
        if row.status == model.STATUS_UNRESOLVED and row.stat_key == model.STAT_UNSUPPORTED:
            return row.exclusion_reason or "来源未解析"
        return row.exclusion_reason or f"条件状态={row.condition_status}"
    if row.condition_status not in model.ACTIVE_CONDITIONS:
        return row.exclusion_reason or f"条件状态={row.condition_status}"
    return ""


def _row_applies_to_unit(
    row: model.Contribution,
    unit: Mapping[str, Any],
    *,
    tags_match: Any,
) -> tuple[str, str, str]:
    """返回 ``(status, reason, role)``；只做应用账本判定，不改变数值。"""
    raw = row.raw or {}
    scope = str(raw.get("scope") or "")
    unit_scope = str(unit["scope"])
    if scope and scope != unit_scope:
        return (
            dps_components.NOT_APPLICABLE,
            f"scope={scope}（只作用于 {'本体' if scope == 'body' else 'Ignite'}）",
            row.stat_key,
        )
    if row.stat_key in _FREQUENCY_STATS:
        return (
            dps_components.NOT_APPLICABLE,
            "全局频率属性（作用于施放调度/持续模型，不作用于单个伤害组件）",
            "frequency",
        )
    if row.stat_key == model.STAT_ATTRIBUTE:
        if str(row.target or "") == "intelligence":
            return dps_components.APPLIED, "", "attribute_scaling"
        return dps_components.NOT_APPLICABLE, "未参与 BH 属性缩放（仅 Intelligence）", "attribute"
    if row.stat_key == model.STAT_ALL_ATTRIBUTES:
        return dps_components.APPLIED, "", "attribute_scaling"
    if row.stat_key == model.STAT_RESISTANCE:
        if unit["kind"] == "ignite":
            target = str(row.target or "").lower()
            if target in ("fire", "elemental", "all"):
                return dps_components.APPLIED, "", "ignite_application"
            return dps_components.NOT_APPLICABLE, "非火焰系抗性，不影响 Heat Wave 机会", "resistance"
        return (
            dps_components.NOT_APPLICABLE,
            "玩家抗性只用于 Heat Wave/Ignite 施加与阈值缩放；本体命中抗性来自目标场景",
            "resistance",
        )
    if unit["kind"] == "dot":
        if row.stat_key in _BODY_DAMAGE_STATS:
            closure = unit["closure"]
            if not tags_match(row.damage_tags, closure, _ACTIVE_TAG_POLICY()):
                return dps_components.NOT_APPLICABLE, "标签不匹配该组件", row.stat_key
            return dps_components.APPLIED, "", row.stat_key
        if row.stat_key in _BODY_DURATION_STATS:
            return dps_components.APPLIED, "", "duration"
        if row.stat_key in ("ailment_duration", "penetration_with_ignite", "ailment_effect"):
            return dps_components.NOT_APPLICABLE, "只作用于 Ignite", row.stat_key
        return dps_components.NOT_APPLICABLE, "非 BH 本体伤害输入", row.stat_key
    # ignite unit
    if row.stat_key in _IGNITE_DAMAGE_STATS:
        if not tags_match(row.damage_tags, unit["closure"], _ACTIVE_TAG_POLICY()):
            return dps_components.NOT_APPLICABLE, "标签不匹配 Ignite", row.stat_key
        return dps_components.APPLIED, "", row.stat_key
    if row.stat_key == "ailment_duration":
        target = str(row.target or "").lower()
        if target and target not in ("ignite", "all"):
            return dps_components.NOT_APPLICABLE, f"target={target!r} 不是 Ignite/All", row.stat_key
        return dps_components.APPLIED, "", row.stat_key
    if row.stat_key == "penetration_with_ignite":
        return dps_components.APPLIED, "", row.stat_key
    if row.stat_key == model.STAT_ATTRIBUTE:
        return dps_components.APPLIED, "", "attribute_scaling"
    if row.stat_key in _BODY_DURATION_STATS:
        return dps_components.NOT_APPLICABLE, "本体持续时间不影响 Ignite 持续时间", row.stat_key
    return dps_components.NOT_APPLICABLE, "非 Ignite 伤害/施加输入", row.stat_key


def _ACTIVE_TAG_POLICY() -> Any:
    from .mechanics import tag_match  # noqa: PLC0415

    return tag_match.bh_plan_policy()


def _application_ledger(
    plan: Mapping[str, Any], rows: list[model.Contribution]
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """每条 contribution × 每个应用单元（fire/cold/Ignite）的应用/排除记录。"""
    from .mechanics import tag_match  # noqa: PLC0415

    units = _application_units(plan)
    records: list[dict[str, Any]] = []
    for row in rows:
        base_status = None
        base_reason = ""
        if not row.active:
            base_status = (
                dps_components.UNRESOLVED
                if row.status == model.STATUS_UNRESOLVED
                else dps_components.CONDITION_NOT_SATISFIED
            )
            base_reason = _application_reason(row)
        for unit in units:
            if base_status is not None:
                records.append(
                    {
                        "contribution_id": row.contribution_id,
                        "component_id": unit["component_id"],
                        "stat_key": row.stat_key,
                        "operation": row.operation,
                        "role": row.stat_key,
                        "status": base_status,
                        "reason": base_reason,
                    }
                )
                continue
            status, reason, role = _row_applies_to_unit(row, unit, tags_match=tag_match.tags_match)
            records.append(
                {
                    "contribution_id": row.contribution_id,
                    "component_id": unit["component_id"],
                    "stat_key": row.stat_key,
                    "operation": row.operation,
                    "role": role,
                    "status": status,
                    "reason": reason or None,
                }
            )
    summary = dps_result.application_summary(records)
    applications_by_id = {row.contribution_id for row in rows}
    if applications_by_id != set(summary):
        missing = applications_by_id - set(summary)
        raise ValueError(f"应用账本缺少贡献：{sorted(missing)[:4]}")
    return records, summary, {"units": units, "record_count": len(records)}


def execute_plan_v3(
    plan: dict[str, Any],
    scenario: Any,
    *,
    runtime: Optional[plan_handlers.PlanRuntime] = None,
) -> dict[str, Any]:
    runtime = runtime or plan_handlers.PlanRuntime()
    issues = validate_plan_v3(plan, runtime)
    if issues:
        raise PlanValidationError(issues)
    planmod.verify_plan_integrity(plan)
    resolved = resolve_evaluation_scenarios(plan, scenario)
    schedule = plan["schedule"]
    horizon_s = float(schedule["horizon_s"])
    primary_policy = str(schedule.get("primary_policy"))
    duration_s = float(plan["components"][0]["duration_s"])
    evaluation_payloads: list[dict[str, Any]] = []
    scenario_hashes: list[dict[str, Any]] = []
    for meta, states, target, digest in resolved:
        rows, _, notes = build_contributions(plan, states=states, target=target, runtime=runtime)
        rows = _runtime_condition_filter(rows, states=states, target=target, runtime=runtime)
        cast_info = _cast_time(plan, rows)
        charges = _charge_parameters(plan, rows)
        policy = str(meta.get("action_policy") or "")
        casts, charge_events, max_concurrent, blocked = _charge_loop(
            policy=policy,
            horizon_s=horizon_s,
            max_charges=float(charges["max_charges"]),
            recharge_s=float(charges["effective_recharge_s"]),
            cast_time_s=float(cast_info["cast_time_s"]),
            duration_s=duration_s,
        )
        component_results = [
            _dot_component_result(plan, component, rows, target, float(target.get("monster_damage_reduction") or 0.0))
            for component in plan["components"]
        ]
        core = _ignite_core(plan, rows, target, float(target.get("monster_damage_reduction") or 0.0))
        cohorts: list[dict[str, Any]] = []
        cast_payloads: list[dict[str, Any]] = []
        for cast in casts:
            ignition = _ignite_application(plan, core, rows, float(cast["black_hole_spawn_s"]), duration_s)
            for cohort in ignition["cohorts"]:
                cohort = dict(cohort)
                cohort["cast_id"] = cast["cast_id"]
                cohorts.append(cohort)
            cast_payloads.append(
                {
                    **cast,
                    "ignite_roll_count": int(ignition["totals"]["expected_total_stacks_applied"] > 0)
                    and len(ignition["cohorts"]),
                    "ignite_expected_stacks": float(ignition["totals"]["expected_total_stacks_applied"]),
                    "ignite_damage_total": float(ignition["totals"]["damage"]),
                }
            )
        windows = [
            _window_metrics(
                casts=casts, component_results=component_results, cohorts=cohorts, window_s=float(cut)
            )
            for cut in sorted({float(item) for item in schedule["windows_s"]} | {horizon_s})
        ]
        horizon_metrics = next(item for item in windows if abs(item["window_s"] - horizon_s) < EPS)
        cast_times = [float(cast["cast_start_s"]) for cast in casts]
        cycle_s: Optional[float] = None
        if len(cast_times) >= 2:
            intervals = [b - a for a, b in zip(cast_times, cast_times[1:])]
            tail_intervals = intervals[-5:] if len(intervals) > 5 else intervals
            cycle_s = sum(tail_intervals) / len(tail_intervals)
        single = _single_cast(
            plan=plan,
            rows=rows,
            target=target,
            monster_damage_reduction=float(target.get("monster_damage_reduction") or 0.0),
            cast_time_s=float(cast_info["cast_time_s"]),
            duration_s=duration_s,
            charge_parameters=charges,
        )
        payload = {
            "evaluation_id": str(meta["evaluation_id"]),
            "label": str(meta.get("label") or meta["evaluation_id"]),
            "action_policy": policy,
            "primary": bool(meta.get("primary")),
            "scenario_sha256": digest,
            "scenario": {"states": copy.deepcopy(states), "target": copy.deepcopy(target)},
            "cast_count": len(casts),
            "max_concurrent": max_concurrent,
            "blocked_attempts": blocked,
            "charge_events": charge_events,
            "casts": cast_payloads,
            "windows": windows,
            "resource": {
                "max_charges": float(charges["max_charges"]),
                "effective_recharge_s": float(charges["effective_recharge_s"]),
                "initial_charges": float(charges["max_charges"]),
                "cast_time_s": float(cast_info["cast_time_s"]),
                "mana": "unlimited",
            },
            "single_cast": single,
            "totals": {
                "cast_count": len(casts),
                "horizon_damage": float(horizon_metrics["total_damage"]),
                "horizon_dps": float(horizon_metrics["dps"]),
                "body_damage": float(horizon_metrics["body_damage"]),
                "ignite_damage": float(horizon_metrics["ignite_damage"]),
                "unsettled_tail_damage": float(horizon_metrics["unsettled_tail_damage"]),
                "max_concurrent_black_holes": max_concurrent,
                "cycle_s": cycle_s,
                "steady_state_dps_cross_check": (
                    float(single["totals"]["single_cast_total"]) / cycle_s if cycle_s else None
                ),
            },
            "components": component_results,
            "ignite_core": core,
            "_cohorts": cohorts,
            "_rows": rows,
            "_notes": notes,
            "_states": copy.deepcopy(states),
            "_target": copy.deepcopy(target),
            "_charge_parameters": charges,
            "_cast_info": cast_info,
        }
        evaluation_payloads.append(payload)
        scenario_hashes.append({"evaluation_id": str(meta["evaluation_id"]), "scenario_sha256": digest})
    primary_payload = next((item for item in evaluation_payloads if item["primary"]), None)
    if primary_payload is None:
        raise PlanValidationError(
            [
                {
                    "code": "plan_v3_unsupported",
                    "message": "plan-v3 没有 primary evaluation",
                    "severity": "error",
                    "related_ids": [],
                    "affects_coverage": False,
                }
            ]
        )
    ordered_payloads = [primary_payload] + [
        item for item in evaluation_payloads if item is not primary_payload
    ]
    primary_rows: list[model.Contribution] = primary_payload["_rows"]
    primary_states: dict[str, Any] = primary_payload["_states"]
    primary_target: dict[str, Any] = primary_payload["_target"]
    aggregate_sha = planmod.aggregate_scenario_sha256(scenario_hashes)
    execution_fingerprint = planmod.execution_fingerprint(
        plan,
        aggregate_sha,
        plan_handlers.implementation_fingerprint()["executor_fingerprint"],
    )
    plan_paths = {
        str(item.get("evaluation_id")): item.get("scenario_path")
        for item in plan.get("evaluations") or []
    }
    primary_components = primary_payload["components"]
    primary_single = primary_payload["single_cast"]
    applications, application_summary, application_meta = _application_ledger(plan, primary_rows)
    contributions_by_id = {
        str(effect.get("effect_id")): row
        for effect, row in zip(
            [
                item
                for item in sorted(
                    plan.get("effects") or [], key=lambda item: int(item.get("sequence") or 0)
                )
                if item.get("emits_contribution")
            ],
            primary_rows,
        )
    }
    effect_ledger = project_ledger(
        plan=dict(plan),
        contributions_by_id=contributions_by_id,
        notes=primary_payload["_notes"],
        states=primary_states,
    )
    result = {
        "schema": SCHEMA_RESULT_V5,
        "case_id": plan["subject"]["case_id"],
        "model": "definitions_v3",
        "skill": {
            "ability_id": plan["subject"]["main_ability_id"],
            "name": (plan.get("parameters") or {}).get("skill", {}).get("skill_name"),
        },
        "plan_ref": planmod.plan_ref(plan),
        "plan_sha256": plan["plan_sha256"],
        "scenario_sha256": aggregate_sha,
        "primary_scenario_sha256": primary_payload["scenario_sha256"],
        "execution_fingerprint": execution_fingerprint,
        "primary_evaluation": primary_payload["evaluation_id"],
        "evaluations": [
            {
                "evaluation_id": item["evaluation_id"],
                "action_policy": item["action_policy"],
                "primary": item["primary"],
                "scenario_sha256": item["scenario_sha256"],
                "cast_count": item["cast_count"],
                "max_concurrent": item["max_concurrent"],
                "horizon_dps": item["totals"]["horizon_dps"],
                "horizon_damage": item["totals"]["horizon_damage"],
                "window_count": len(item["windows"]),
                "scenario_path": plan_paths.get(str(item["evaluation_id"])),
                "scenario": copy.deepcopy(item["scenario"]),
            }
            for item in ordered_payloads
        ],
        "evaluation_scenarios": sorted(
            scenario_hashes, key=lambda item: str(item["evaluation_id"])
        ),
        "loops": [
            {
                "policy_id": item["action_policy"],
                "label": item["label"],
                "primary": item["primary"],
                "horizon_s": horizon_s,
                "cast_count": item["cast_count"],
                "max_concurrent": item["max_concurrent"],
                "casts": item["casts"],
                "charge_events": item["charge_events"],
                "blocked_attempts": item["blocked_attempts"],
                "resource": item["resource"],
                "windows": item["windows"],
                "totals": item["totals"],
            }
            for item in ordered_payloads
        ],
        "scenario": {
            "states": copy.deepcopy(primary_states),
            "target": copy.deepcopy(primary_target),
            "primary_evaluation": primary_payload["evaluation_id"],
        },
        "states": copy.deepcopy(primary_states),
        "target": copy.deepcopy(primary_target),
        "attributes": (plan.get("facts") or {}).get("attributes") or {},
        "character": copy.deepcopy((plan.get("presentation") or {}).get("character")),
        "skill_names": copy.deepcopy((plan.get("presentation") or {}).get("skill_names") or {}),
        "equipment": copy.deepcopy((plan.get("presentation") or {}).get("equipment") or []),
        "controls": [
            {
                "key": control.get("key"),
                "label": control.get("label"),
                "source": control.get("source"),
                "type": control.get("type"),
                "default": control.get("default"),
                "min": control.get("min"),
                "max": control.get("max"),
                "readonly": control.get("readonly"),
                "effect": control.get("effect"),
                "requires_target": control.get("requires_target"),
            }
            for control in plan.get("controls") or []
            if control.get("namespace") == "state"
        ],
        "components": [
            {
                key: component[key]
                for key in (
                    "component_id",
                    "kind",
                    "damage_type",
                    "tags",
                    "scope",
                    "duration_s",
                    "tick_interval_s",
                    "tick_count",
                    "damage_at_start",
                    "remainder_policy",
                    "base_damage",
                    "added_damage_scaling",
                    "added_damage_total",
                    "per_tick_base",
                    "increased_total",
                    "increased_rows",
                    "more_product",
                    "more_groups",
                    "penetration_total",
                    "target",
                    "per_tick_damage",
                    "damage_total",
                )
            }
            for component in primary_components
        ],
        "body": {
            "duration_s": primary_components[0]["duration_s"],
            "tick_interval_s": primary_components[0]["tick_interval_s"],
            "tick_count": max(int(component["tick_count"]) for component in primary_components),
            "tick_model": {
                "remainder_policy": "ceil_full_ticks",
                "damage_at_start": bool(primary_components[0]["damage_at_start"]),
                "tick_interval_s": primary_components[0]["tick_interval_s"],
            },
            "components": primary_components,
            "totals": {
                "damage": float(primary_payload["single_cast"]["totals"]["body_total"]),
                "component_count": len(primary_components),
            },
        },
        "ignite": {
            "ailment": "Ignite",
            "base_damage": primary_payload["ignite_core"]["base_damage"],
            "duration_s": primary_payload["ignite_core"]["duration_s"],
            "duration_multiplier": primary_payload["ignite_core"]["duration_multiplier"],
            "increased_total": primary_payload["ignite_core"]["increased_total"],
            "more_product": primary_payload["ignite_core"]["more_product"],
            "per_stack_damage": primary_payload["ignite_core"]["per_stack_damage"],
            "penetration_total": primary_payload["ignite_core"]["penetration_total"],
            "target": primary_payload["ignite_core"]["target"],
            "duration": {
                "base_duration_s": primary_payload["ignite_core"]["base_duration_s"],
                "increased_total": primary_payload["ignite_core"]["duration_increased_total"],
                "duration_multiplier": primary_payload["ignite_core"]["duration_multiplier"],
                "duration_s": primary_payload["ignite_core"]["duration_s"],
                "rows": primary_payload["ignite_core"]["duration_rows"],
            },
            "application": {
                "uncapped_fire_resistance_percent": primary_payload["single_cast"]["ignition"][
                    "uncapped_fire_resistance_percent"
                ],
                "expected_total_stacks_applied": primary_payload["single_cast"]["totals"][
                    "expected_total_stacks_applied"
                ],
                "roll_count": len(primary_payload["single_cast"]["ignition"]["cohorts"]),
                "roll_interval_s": primary_payload["single_cast"]["ignition"]["roll_interval_s"],
                "stacks_per_roll": primary_payload["single_cast"]["ignition"]["stacks_per_roll"],
            },
        },
        "single_cast": primary_payload["single_cast"],
        "cooldown": copy.deepcopy(primary_payload["_charge_parameters"]),
        "cast": copy.deepcopy(primary_payload["_cast_info"]),
        "cast_detail": primary_payload["casts"][:2],
        "contributions": [
            {
                "contribution_id": row.contribution_id,
                "source_kind": row.source_kind,
                "source_label": row.source_label,
                "source_path": row.source_path,
                "stat_name": row.stat_name,
                "stat_key": row.stat_key,
                "operation": row.operation,
                "value": float(row.value),
                "unit": row.unit,
                "damage_tags": list(row.damage_tags or []),
                "target": row.target,
                "group": row.group,
                "included": bool(row.included),
                "exclusion_reason": row.exclusion_reason,
                "status": row.status,
                "notes": row.notes,
                "application": copy.deepcopy(application_summary.get(row.contribution_id)),
            }
            for row in primary_rows
        ],
        "applications": applications,
        "application_units": [
            {
                "component_id": unit["component_id"],
                "kind": unit["kind"],
                "scope": unit["scope"],
                "tags": list(unit["tags"]),
            }
            for unit in application_meta["units"]
        ],
        "effect_ledger": effect_ledger,
        "ailments": [
            {
                "ailment_id": "ignite",
                "component_id": "bh-ignite",
                "name": "Ignite",
                "damage_type": "Fire",
                "duration_s": primary_payload["ignite_core"]["duration_s"],
                "per_stack_damage": primary_payload["ignite_core"]["per_stack_damage"],
            }
        ],
        "coverage": project_coverage(
            plan=dict(plan), dot_applications=coverage_applications(applications)
        ),
        "scope": str((plan.get("scope") or {}).get("skill") or plan["subject"]["main_ability_id"]),
        "issues": [
            str(note)
            for note in (plan.get("scope") or {}).get("limitations") or []
        ]
        + [str(note) for note in (plan.get("facts") or {}).get("early_issues") or []]
        + [
            f"覆盖状态 partial：{int((project_coverage(plan=dict(plan), dot_applications=[]).get('gap_count')) or 0)} 个未建模/未验证缺口"
        ],
        "contribution_application_summary": application_summary,
        "assumptions": list(plan.get("assumptions") or []),
        "consistency": {},
    }
    result["consistency"] = {
        "single_total_equals_body_plus_ignite": abs(
            float(result["single_cast"]["totals"]["single_cast_total"])
            - float(result["single_cast"]["totals"]["body_total"])
            - float(result["single_cast"]["totals"]["ignite_total"])
        )
        < 1e-6,
        "max_concurrent_recomputed": all(
            int(loop["max_concurrent"])
            == _max_concurrent(loop["casts"], float(schedule["horizon_s"]))
            for loop in result["loops"]
        ),
        "evaluation_summary_matches_loops": all(
            next(
                (
                    loop
                    for loop in result["loops"]
                    if loop["policy_id"] == item["action_policy"]
                ),
                None,
            )
            is not None
            and abs(
                float(item["horizon_dps"]) - float(loop["totals"]["horizon_dps"])
            )
            <= 1e-6
            and abs(
                float(item["horizon_damage"]) - float(loop["totals"]["horizon_damage"])
            )
            <= 1e-6
            and int(item["cast_count"]) == int(loop["cast_count"])
            and int(item["max_concurrent"]) == int(loop["max_concurrent"])
            for item in result["evaluations"]
            for loop in [
                next(
                    (
                        candidate
                        for candidate in result["loops"]
                        if candidate["policy_id"] == item["action_policy"]
                    ),
                    {},
                )
            ]
        ),
        "body_components_sum": abs(
            sum(float(component["damage_total"]) for component in primary_components)
            - float(primary_single["totals"]["body_total"])
        )
        < 1e-6,
        "loop_horizon_matches_windows": abs(
            float(primary_payload["totals"]["horizon_dps"])
            - float(
                next(
                    item
                    for item in primary_payload["windows"]
                    if abs(float(item["window_s"]) - horizon_s) < EPS
                )["dps"]
            )
        )
        < 1e-6,
    }
    cohort_by_window: dict[float, float] = {}
    for payload in evaluation_payloads:
        for window in payload["windows"]:
            cut = float(window["window_s"])
            applied = [
                cohort
                for cohort in payload["_cohorts"]
                if float(cohort["time_s"]) <= cut + EPS
            ]
            in_window = sum(_ignite_between(cohort, 0.0, cut) for cohort in applied)
            tail = sum(
                max(0.0, float(cohort["damage_total"]) - _ignite_between(cohort, 0.0, cut))
                for cohort in applied
            )
            if not math.isclose(
                in_window, float(window["ignite_damage"]), rel_tol=1e-9, abs_tol=1e-4
            ):
                raise ValueError(
                    f"应用账本/窗口 Ignite 伤害不自洽 cut={cut} recompute={in_window!r} stored={window['ignite_damage']!r}"
                )
            if not math.isclose(
                tail, float(window["unsettled_tail_damage"]), rel_tol=1e-9, abs_tol=1e-4
            ):
                raise ValueError(
                    f"窗口尾伤与批次复算不一致 cut={cut} recompute={tail!r} stored={window['unsettled_tail_damage']!r}"
                )
        horizon_window = next(
            item for item in payload["windows"] if abs(float(item["window_s"]) - horizon_s) < EPS
        )
        if not math.isclose(
            float(payload["totals"]["horizon_damage"]),
            float(horizon_window["total_damage"]),
            rel_tol=1e-9,
            abs_tol=1e-4,
        ):
            raise ValueError("horizon_damage 与 horizon 窗口不一致")
        if not math.isclose(
            float(payload["totals"]["horizon_dps"]),
            float(horizon_window["dps"]),
            rel_tol=1e-9,
            abs_tol=1e-6,
        ):
            raise ValueError("horizon_dps 与 horizon 窗口不一致")
    result["consistency"]["window_tail_recomputed"] = True
    return result


def _v5_recompute_loop_ignite_cohorts(
    plan: Mapping[str, Any], result: Mapping[str, Any], loop: Mapping[str, Any]
) -> list[dict[str, Any]]:
    ignite = result.get("ignite") or {}
    application = ignite.get("application") or {}
    duration_s = float((result.get("body") or {}).get("duration_s") or 0.0)
    ignite_duration = float(ignite.get("duration_s") or 0.0)
    per_stack = float(ignite.get("per_stack_damage") or 0.0)
    stacks_per_roll = float(application.get("stacks_per_roll") or 0.0)
    interval = float(application.get("roll_interval_s") or 0.0)
    if interval <= 0 or ignite_duration <= 0:
        return []
    cohorts: list[dict[str, Any]] = []
    for cast in loop.get("casts") or []:
        spawn = float(cast.get("black_hole_spawn_s") or 0.0)
        offset = 0.0
        while offset < duration_s - EPS:
            time_s = spawn + offset
            cohorts.append(
                {
                    "time_s": time_s,
                    "damage_total": stacks_per_roll * per_stack,
                    "end_time_s": time_s + ignite_duration,
                    "dur": ignite_duration,
                }
            )
            offset += interval
    return cohorts


def _v5_recompute_windows(
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
    loop: Mapping[str, Any],
    cohorts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    components = result.get("components") or []
    horizon = float(plan["schedule"]["horizon_s"])
    cuts = sorted({float(item) for item in plan["schedule"]["windows_s"]} | {horizon})
    windows: list[dict[str, Any]] = []
    casts = loop.get("casts") or []
    for cut in cuts:
        body = 0.0
        for cast in casts:
            spawn = float(cast.get("black_hole_spawn_s") or 0.0)
            for component in components:
                interval = float(component.get("tick_interval_s") or 0.0)
                tick_count = int(component.get("tick_count") or 0)
                per_tick = float(component.get("per_tick_damage") or 0.0)
                for index in range(tick_count):
                    if spawn + index * interval <= cut + EPS:
                        body += per_tick
        ignite_by_window = 0.0
        tail = 0.0
        for cohort in cohorts:
            if float(cohort["time_s"]) > cut + EPS:
                continue
            in_window = _ignite_between(cohort, 0.0, cut)
            ignite_by_window += in_window
            tail += max(0.0, float(cohort["damage_total"]) - in_window)
        cast_count = sum(1 for cast in casts if float(cast.get("cast_start_s") or 0.0) <= cut + EPS)
        windows.append(
            {
                "window_s": cut,
                "body_damage": body,
                "ignite_damage": ignite_by_window,
                "total_damage": body + ignite_by_window,
                "dps": (body + ignite_by_window) / cut if cut > 0 else 0.0,
                "cast_count": cast_count,
                "unsettled_tail_damage": tail,
            }
        )
    return windows


def _v5_recompute_single_cast(
    plan: Mapping[str, Any], result: Mapping[str, Any]
) -> dict[str, Any]:
    body = result.get("body") or {}
    ignite = result.get("ignite") or {}
    application = ignite.get("application") or {}
    components = result.get("components") or []
    cast = result.get("cast") or {}
    duration_s = float(body.get("duration_s") or 0.0)
    cast_time = float(cast.get("cast_time_s") or 0.0)
    spawn = cast_time
    end = spawn + duration_s
    body_total = sum(float(component.get("damage_total") or 0.0) for component in components)
    per_stack = float(ignite.get("per_stack_damage") or 0.0)
    stacks_per_roll = float(application.get("stacks_per_roll") or 0.0)
    interval = float(application.get("roll_interval_s") or 0.0)
    ignite_duration = float(ignite.get("duration_s") or 0.0)
    if interval <= 0:
        raise ValueError("ignite roll_interval_s 无效")
    cohorts: list[dict[str, Any]] = []
    offset = 0.0
    while offset < duration_s - EPS:
        time_s = spawn + offset
        cohorts.append(
            {
                "time_s": time_s,
                "damage_total": stacks_per_roll * per_stack,
                "end_time_s": time_s + ignite_duration,
                "dur": ignite_duration,
            }
        )
        offset += interval
    ignite_total = sum(float(cohort["damage_total"]) for cohort in cohorts)
    ignite_before = sum(_ignite_between(cohort, spawn, end) for cohort in cohorts)
    last_expiry = max((float(cohort["end_time_s"]) for cohort in cohorts), default=spawn)
    return {
        "body_total": body_total,
        "ignite_total": ignite_total,
        "ignite_damage_before_black_hole_end": ignite_before,
        "ignite_tail_damage": ignite_total - ignite_before,
        "window_damage_until_black_hole_end": body_total + ignite_before,
        "single_cast_total": body_total + ignite_total,
        "observation_end_s": max(end, last_expiry),
        "last_ailment_expiry_s": last_expiry,
        "expected_total_stacks_applied": stacks_per_roll * len(cohorts),
    }


def validate_result_v5(plan: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """只读复验：身份/应用账本/顶层 primary 投影/loops/windows/尾伤/单次施放全部复算。"""
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    def check(check_id: str, ok: bool, detail: str = "") -> None:
        checks.append({"id": check_id, "status": "PASS" if ok else "FAIL", "detail": detail})
        if not ok:
            errors.append(f"{check_id}: {detail}")

    def close(a: Any, b: Any, *, rel: float = 1e-9, abs_: float = 1e-4) -> bool:
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=abs_)

    check("result.schema", result.get("schema") == SCHEMA_RESULT_V5, str(result.get("schema")))
    check("result.case_id", result.get("case_id") == plan["subject"]["case_id"])
    check("result.plan_ref", (result.get("plan_ref") or {}).get("plan_sha256") == plan.get("plan_sha256"))
    check("result.plan_sha256", result.get("plan_sha256") == plan.get("plan_sha256"))

    evaluations = plan.get("evaluations") or []
    plan_by_id = {str(item["evaluation_id"]): item for item in evaluations}
    result_evaluations = result.get("evaluations") or []
    result_by_id = {str(item.get("evaluation_id")): item for item in result_evaluations}
    primary_plan = next((item for item in evaluations if item.get("primary")), None)
    if primary_plan is None:
        check("result.primary_evaluation", False, "计划缺少 primary evaluation")
        return {"passed": False, "checks": checks, "errors": errors}
    primary_id = str(primary_plan["evaluation_id"])
    check(
        "result.evaluations",
        set(plan_by_id) == set(result_by_id) == set(result_by_id),
        f"plan={len(plan_by_id)} result={len(result_by_id)}",
    )
    check(
        "result.evaluation_policies",
        all(
            str(plan_by_id[key].get("action_policy")) == str(result_by_id[key].get("action_policy"))
            for key in plan_by_id
            if key in result_by_id
        ),
    )
    check(
        "result.primary_evaluation",
        str(result.get("primary_evaluation")) == primary_id
        and bool((result_by_id.get(primary_id) or {}).get("primary"))
        and str((result_evaluations[0] if result_evaluations else {}).get("evaluation_id")) == primary_id,
        f"primary={primary_id}",
    )

    # 场景身份：聚合/主场景按已签收 FR 规则；顶层 scenario 必须等于主评估场景
    scenario_hashes = sorted(
        (str(item["evaluation_id"]), str(item["scenario_sha256"]))
        for item in result.get("evaluation_scenarios") or []
    )
    expected_hashes = sorted(
        (str(item["evaluation_id"]), str(item["scenario_sha256"])) for item in evaluations
    )
    check("result.evaluation_scenarios", scenario_hashes == expected_hashes)
    check(
        "result.scenario_sha256",
        result.get("scenario_sha256")
        == planmod.aggregate_scenario_sha256(
            [{"evaluation_id": key, "scenario_sha256": value} for key, value in scenario_hashes]
        ),
    )
    primary_sha = next((value for key, value in scenario_hashes if key == primary_id), None)
    check("result.primary_scenario_sha256", result.get("primary_scenario_sha256") == primary_sha)
    top_scenario = result.get("scenario") or {}
    check(
        "result.primary_scenario_sha256.top_level",
        result.get("primary_scenario_sha256")
        == planmod.scenario_sha256(
            {
                "states": (top_scenario.get("states") or {}),
                "target": (top_scenario.get("target") or {}),
            }
        ),
        "顶层 scenario 必须是 primary 评估场景（不能取第一个评估）",
    )
    check(
        "result.primary_projection",
        str(top_scenario.get("primary_evaluation")) == primary_id
        and result.get("states") == top_scenario.get("states")
        and result.get("target") == top_scenario.get("target")
        and (result_by_id.get(primary_id) or {}).get("scenario", {}).get("states")
        == top_scenario.get("states")
        and (result_by_id.get(primary_id) or {}).get("scenario", {}).get("target")
        == top_scenario.get("target"),
        "scenario/states/target 必须与 primary=true 评估一致",
    )
    check(
        "result.execution_fingerprint",
        result.get("execution_fingerprint")
        == planmod.execution_fingerprint(
            plan,
            str(result.get("scenario_sha256")),
            plan_handlers.implementation_fingerprint()["executor_fingerprint"],
        ),
    )

    loops = result.get("loops") or []
    loop_by_policy = {str(item.get("policy_id")): item for item in loops}
    expected_policies = sorted(str(item["action_policy"]) for item in evaluations)
    check(
        "result.loops",
        sorted(loop_by_policy) == expected_policies,
        str([item.get("policy_id") for item in loops]),
    )
    check(
        "result.loops_primary_first",
        bool(loops) and bool(loops[0].get("primary")) and str(loops[0].get("policy_id")) == str(
            plan["schedule"].get("primary_policy")
        ),
        "loops[0] 必须是 primary 策略",
    )
    horizon = float(plan["schedule"]["horizon_s"])

    def close_win(a: Any, b: Any, *, abs_: float = 1e-4) -> bool:
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=abs_)

    totals_ok = True
    windows_ok = True
    max_concurrent_ok = True
    evaluation_summary_ok = True
    for loop in loops:
        policy = str(loop.get("policy_id"))
        casts = loop.get("casts") or []
        try:
            cohorts = _v5_recompute_loop_ignite_cohorts(plan, result, loop)
            windows = _v5_recompute_windows(plan, result, loop, cohorts)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            totals_ok = windows_ok = False
            continue
        cast_starts = [float(cast.get("cast_start_s") or 0.0) for cast in casts]
        if any(b < a - 1e-9 for a, b in zip(cast_starts, cast_starts[1:])):
            totals_ok = False
        if int(loop.get("cast_count") or 0) != len(casts):
            totals_ok = False
        for cast in casts:
            if not close_win(
                float(cast.get("black_hole_spawn_s") or 0.0)
                + float(cast.get("black_hole_duration_s") or 0.0),
                float(cast.get("black_hole_end_s") or 0.0),
            ):
                totals_ok = False
        recomputed_max = _max_concurrent(casts, horizon)
        if int(loop.get("max_concurrent") or 0) != recomputed_max:
            max_concurrent_ok = False
        if int((loop.get("totals") or {}).get("max_concurrent_black_holes") or 0) != recomputed_max:
            max_concurrent_ok = False
        horizon_window = next(
            (item for item in windows if close_win(item["window_s"], horizon, abs_=1e-9)),
            None,
        )
        if horizon_window is None:
            totals_ok = False
        else:
            totals = loop.get("totals") or {}
            for key, value in (
                ("horizon_damage", horizon_window["total_damage"]),
                ("horizon_dps", horizon_window["dps"]),
                ("body_damage", horizon_window["body_damage"]),
                ("ignite_damage", horizon_window["ignite_damage"]),
                ("unsettled_tail_damage", horizon_window["unsettled_tail_damage"]),
            ):
                if not close_win(totals.get(key), value):
                    totals_ok = False
            if int(totals.get("cast_count") or 0) != len(casts):
                totals_ok = False
        for stored, expected in zip(loop.get("windows") or [], windows):
            for key in (
                "body_damage",
                "ignite_damage",
                "total_damage",
                "dps",
                "unsettled_tail_damage",
            ):
                if not close_win(stored.get(key), expected[key]):
                    windows_ok = False
            if int(stored.get("cast_count") or 0) != int(expected["cast_count"]):
                windows_ok = False
            if not close_win(stored.get("window_s"), expected["window_s"], abs_=1e-9):
                windows_ok = False
        if len(loop.get("windows") or []) != len(windows):
            windows_ok = False
        evaluation = next(
            (
                item
                for item in result_evaluations
                if str(item.get("evaluation_id")) == str(loop.get("evaluation_id") or "")
                or str(item.get("action_policy")) == policy
            ),
            None,
        )
        if evaluation is None:
            evaluation_summary_ok = False
        else:
            totals = loop.get("totals") or {}
            for key, value in (
                ("horizon_dps", totals.get("horizon_dps")),
                ("horizon_damage", totals.get("horizon_damage")),
            ):
                if not close_win(evaluation.get(key), value):
                    evaluation_summary_ok = False
            if int(evaluation.get("cast_count") or 0) != len(casts):
                evaluation_summary_ok = False
            if int(evaluation.get("max_concurrent") or 0) != recomputed_max:
                evaluation_summary_ok = False
            if int(evaluation.get("window_count") or 0) != len(windows):
                evaluation_summary_ok = False
    check("result.loop_totals", totals_ok, "loops[].totals 必须由 casts/windows 复算一致")
    check("result.loops_windows", windows_ok, "窗口 body/ignite/total/dps/tail/cast_count 必须由批次复算")
    check("result.max_concurrent", max_concurrent_ok, "max_concurrent 必须由 casts 采样复算")
    check("result.evaluation_summary", evaluation_summary_ok, "evaluations[] 摘要必须与 loops 一致")

    # 单次施放：由 components/cast/ignite 复算（含尾伤）
    try:
        recomputed_single = _v5_recompute_single_cast(plan, result)
        single = result.get("single_cast") or {}
        stored_single = single.get("totals") or {}
        single_ok = True
        for key, value in recomputed_single.items():
            if not close_win(stored_single.get(key), value):
                single_ok = False
        cast = single.get("cast") or {}
        cast_ok = (
            close_win(cast.get("cast_time_s"), (result.get("cast") or {}).get("cast_time_s"), abs_=1e-9)
            and close_win(cast.get("black_hole_spawn_s"), cast.get("cast_time_s"), abs_=1e-9)
            and close_win(
                float(cast.get("black_hole_end_s") or 0.0),
                float(cast.get("black_hole_spawn_s") or 0.0)
                + float((result.get("body") or {}).get("duration_s") or 0.0),
            )
        )
        check("result.single_cast", single_ok and cast_ok, "single_cast 必须由组件/Ignite/cast 复算")
        check("result.single_cast_tail", single_ok, "单次施放尾伤必须由批次复算")
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        check("result.single_cast", False, f"单次施放复算失败：{exc}")
        check("result.single_cast_tail", False, f"单次施放尾伤复算失败：{exc}")

    # 应用账本：每条 contribution × 每个单元一条记录，摘要可复算
    contributions = result.get("contributions") or []
    applications = result.get("applications") or []
    units = result.get("application_units") or []
    summary = result.get("contribution_application_summary") or {}
    expected_summary = dps_result.application_summary(applications)
    by_id: dict[str, list[dict[str, Any]]] = {}
    statuses = {
        dps_components.APPLIED,
        dps_components.NOT_APPLICABLE,
        dps_components.CONDITION_NOT_SATISFIED,
        dps_components.UNRESOLVED,
        dps_components.SOURCE_ABSENT,
        dps_components.DUPLICATE,
    }
    for record in applications:
        by_id.setdefault(str(record.get("contribution_id")), []).append(record)
    contribution_ids = [str(row.get("contribution_id")) for row in contributions]
    unit_ids = [str(unit.get("component_id")) for unit in units]
    ledger_ok = bool(contributions) and bool(units) and bool(applications)
    ledger_ok = ledger_ok and set(by_id) == set(contribution_ids)
    ledger_ok = ledger_ok and all(
        set(str(record.get("component_id")) for record in by_id.get(cid, [])) == set(unit_ids)
        for cid in contribution_ids
    )
    ledger_ok = ledger_ok and all(
        str(record.get("status")) in statuses for record in applications
    )
    ledger_ok = ledger_ok and all(
        record.get("reason") for record in applications if record.get("status") != dps_components.APPLIED
    )
    ledger_ok = ledger_ok and expected_summary == summary
    ledger_ok = ledger_ok and all(
        row.get("application") == summary.get(str(row.get("contribution_id"))) for row in contributions
    )
    component_refs = {
        str(item.get("component_id")) for item in (result.get("components") or [])
    } | {
        str(item.get("component_id"))
        for item in (result.get("ailments") or [])
        if item.get("component_id")
    }
    ledger_ok = ledger_ok and set(unit_ids) <= component_refs
    check("result.applications_ledger", ledger_ok, "contributions×units 应用账本/摘要/引用必须自洽")
    check(
        "result.effect_ledger",
        isinstance(result.get("effect_ledger"), dict)
        and "node_bindings" in result["effect_ledger"]
        and "effects" in result["effect_ledger"],
    )
    check(
        "result.contributions_ids_order",
        contribution_ids
        == [
            str(effect.get("contribution_id"))
            for effect in plan.get("effects") or []
            if effect.get("emits_contribution")
        ],
        "贡献账本顺序必须与计划 effects 一致",
    )
    check(
        "result.coverage_projection",
        result.get("coverage")
        == project_coverage(plan=dict(plan), dot_applications=coverage_applications(applications)),
        "coverage 必须由计划缺口 + 应用账本未解析记录投影",
    )
    charge = result.get("cooldown") or {}
    charge_ok = (
        isinstance(charge.get("max_charges"), int)
        and charge.get("max_charges") >= 1
        and float(charge.get("effective_recharge_s") or 0.0) > 0
        and close_win(
            float(charge.get("effective_recharge_s") or 0.0),
            (float(charge.get("base_recharge_s") or 0.0) + float(charge.get("flat_adjustment_s") or 0.0))
            * (1.0 - float(charge.get("cooldown_reduction") or 0.0))
            / (1.0 + float(charge.get("icr_total") or 0.0)),
            abs_=1e-9,
        )
    )
    primary_loop = loop_by_policy.get(str(plan["schedule"].get("primary_policy"))) or {}
    charge_ok = charge_ok and close_win(
        charge.get("effective_recharge_s"),
        (primary_loop.get("resource") or {}).get("effective_recharge_s"),
        abs_=1e-9,
    )
    charge_ok = charge_ok and close_win(
        (result.get("cast") or {}).get("cast_time_s"),
        (primary_loop.get("resource") or {}).get("cast_time_s"),
        abs_=1e-9,
    )
    check("result.charge_schedule", charge_ok, "充能公式与 primary loops resource 必须一致")
    check(
        "result.consistency",
        all(bool(value) for value in (result.get("consistency") or {}).values()),
        str(result.get("consistency")),
    )
    check(
        "result.body",
        bool((result.get("body") or {}).get("components"))
        and bool((result.get("body") or {}).get("tick_count"))
        and close_win(
            sum(float(component.get("damage_total") or 0.0) for component in result.get("components") or []),
            (result.get("single_cast") or {}).get("totals", {}).get("body_total"),
        ),
    )
    check(
        "result.ignite",
        bool((result.get("ignite") or {}).get("duration_s"))
        and bool((result.get("ignite") or {}).get("per_stack_damage"))
        and float((result.get("ignite") or {}).get("application", {}).get("stacks_per_roll") or 0.0) >= 0,
    )
    return {"passed": not errors, "checks": checks, "errors": errors}


__all__ = [
    "ACTION_POLICIES",
    "REMAINDER_POLICIES",
    "SCHEMA_RESULT_V5",
    "coverage_applications",
    "execute_plan_v3",
    "validate_plan_v3",
    "validate_result_v5",
]
