"""计算计划执行器：只消费计划、场景与静态 handler registry。

执行边界：

* 不读取 case / profile / catalog / dataset / 任何输入文件；
* 不调用 ``dps.calculate`` 或旧计算器；
* 所有数值与条件能力只能经由 ``PlanRuntime`` 的注册表 handler 获得；
* 计划必须通过结构、指纹、handler 版本与场景校验后才执行。

结果 schema 保持 ``le-dps-result-v3``，并追加 plan 绑定字段（``plan_ref`` /
``plan_sha256`` / ``scenario_sha256`` / ``execution_fingerprint``）。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Optional

from . import calculation_plan as planmod
from . import dps_result
from . import model
from . import plan_handlers

SCHEMA_RESULT = "le-dps-result-v3"
SCHEMA_RESULT_V4 = "le-dps-result-v4"
TARGET_KEYS = ("monster_level", "resistance", "high_health", "boss", "blinded", "slowed")


class PlanValidationError(ValueError):
    """计划/场景/运行时 handler 不满足执行条件。"""

    def __init__(self, issues: list[dict[str, Any]]) -> None:
        self.issues = issues
        summary = "; ".join(str(issue.get("message")) for issue in issues[:5])
        super().__init__(f"计划无法执行（{len(issues)} 项）：{summary}")


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


def _plan_effect_handlers(plan: dict[str, Any]) -> list[dict[str, str]]:
    return [effect for effect in plan.get("effects") or [] if isinstance(effect, dict)]


def validate_plan(plan: dict[str, Any], runtime: Optional[plan_handlers.PlanRuntime] = None) -> list[dict[str, Any]]:
    """完整性 + 结构 + handler 版本 + runtime 能力校验；返回 issues。"""
    runtime = runtime or plan_handlers.PlanRuntime()
    issues: list[dict[str, Any]] = []
    try:
        planmod.verify_plan_integrity(plan)
    except planmod.PlanError as exc:
        issues.append(
            {
                "code": "plan_integrity",
                "message": str(exc),
                "severity": "error",
                "related_ids": ["plan_sha256"],
                "affects_coverage": False,
            }
        )
    issues.extend(planmod.validate_plan_structure(plan))
    for effect in _plan_effect_handlers(plan):
        handler_id = effect.get("handler_id")
        handler_version = effect.get("handler_version")
        handler = plan_handlers.ALL_HANDLERS.get(str(handler_id))
        if handler is None:
            issues.append(
                {
                    "code": "unknown_handler",
                    "message": f"effect {effect.get('effect_id')!r} 使用未注册 handler {handler_id!r}",
                    "severity": "error",
                    "related_ids": [effect.get("effect_id")],
                    "affects_coverage": False,
                }
            )
            continue
        if str(handler_version) != plan_handlers.VERSION:
            issues.append(
                {
                    "code": "handler_version_mismatch",
                    "message": (
                        f"effect {effect.get('effect_id')!r} 的 handler {handler_id!r} 版本 "
                        f"{handler_version!r} 与执行器 {plan_handlers.VERSION!r} 不匹配"
                    ),
                    "severity": "error",
                    "related_ids": [effect.get("effect_id")],
                    "affects_coverage": False,
                }
            )
    requirements = plan.get("executor_requirements") or {}
    required_handlers = requirements.get("handlers") or []
    for handler in required_handlers:
        handler_id = str(handler.get("handler_id"))
        if handler_id not in plan_handlers.EXECUTOR_HANDLERS:
            issues.append(
                {
                    "code": "unknown_executor_handler",
                    "message": f"executor_requirements 声明的 handler 不受支持：{handler_id!r}",
                    "severity": "error",
                    "related_ids": [handler_id],
                    "affects_coverage": False,
                }
            )
            continue
        if not runtime.has(handler_id):
            issues.append(
                {
                    "code": "missing_handler",
                    "message": f"执行器未注册必需 handler：{handler_id!r}",
                    "severity": "error",
                    "related_ids": [handler_id],
                    "affects_coverage": False,
                }
            )
        if str(handler.get("version")) != plan_handlers.VERSION:
            issues.append(
                {
                    "code": "handler_version_mismatch",
                    "message": f"handler {handler_id!r} 的声明版本与执行器版本不同",
                    "severity": "error",
                    "related_ids": [handler_id],
                    "affects_coverage": False,
                }
            )
    required_fingerprint = requirements.get("executor_fingerprint")
    if runtime.executor_fingerprint != required_fingerprint:
        issues.append(
            {
                "code": "executor_fingerprint_mismatch",
                "message": "计划要求的执行器实现指纹与当前执行器不一致；请重新编译计划",
                "severity": "error",
                "related_ids": ["executor_fingerprint"],
                "affects_coverage": False,
            }
        )
    # 覆盖缺口必须有一致的诊断背书（防止手改 facts 后静默降级覆盖状态）
    facts = plan.get("facts") or {}
    gaps = facts.get("coverage_base_gaps") or []
    backed = {
        (str(item.get("code")), str(item.get("message")), int(item.get("count") or 1))
        for item in plan.get("diagnostics") or []
        if item.get("affects_coverage")
    }
    for gap in gaps:
        key = (str(gap.get("code")), str(gap.get("detail")), int(gap.get("count") or 1))
        if key not in backed:
            issues.append(
                {
                    "code": "coverage_gap_unbacked",
                    "message": f"覆盖缺口缺少对应编译诊断：{key[0]} {key[1]}",
                    "severity": "error",
                    "related_ids": list(gap.get("contribution_ids") or []),
                    "affects_coverage": True,
                }
            )
    for code, message, count in backed:
        if not any(
            str(gap.get("code")) == code
            and str(gap.get("detail")) == message
            and int(gap.get("count") or 1) == count
            for gap in gaps
        ):
            issues.append(
                {
                    "code": "coverage_diagnostic_unbacked",
                    "message": f"覆盖诊断缺少对应缺口：{code} {message}",
                    "severity": "error",
                    "related_ids": [],
                    "affects_coverage": True,
                }
            )
    return issues


def validate_scenario(plan: dict[str, Any], scenario: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """按计划的 controls 校验并复制场景；返回 ``(states, target)``。"""
    if not isinstance(scenario, dict) or set(scenario) != {"states", "target"}:
        raise ValueError("场景格式不完整")
    controls = [control for control in plan.get("controls") or [] if isinstance(control, dict)]
    state_controls = {control["key"]: control for control in controls if control.get("namespace") == "state"}
    target_controls = {control["key"]: control for control in controls if control.get("namespace") == "target"}
    states = scenario.get("states")
    target = scenario.get("target")
    if not isinstance(states, dict) or set(states) != set(state_controls):
        raise ValueError("Buff 配置包含缺失或不支持的项目")
    if not isinstance(target, dict) or set(target) != set(target_controls):
        raise ValueError("目标配置无效")
    for key, control in state_controls.items():
        value = states[key]
        if control.get("type") == "boolean":
            if type(value) is not bool:
                raise ValueError("开关值无效：" + str(control.get("label") or key))
        elif type(value) is not int or not int(control.get("min") or 0) <= value <= int(control.get("max") or 0):
            raise ValueError("层数超出范围：" + str(control.get("label") or key))
        if control.get("readonly") and value != control.get("default"):
            raise ValueError("常驻状态不可关闭")
    for key, control in target_controls.items():
        value = target[key]
        control_type = control.get("type")
        if control_type == "boolean":
            if type(value) is not bool:
                raise ValueError("目标状态无效")
        elif control_type == "integer":
            if type(value) is not int:
                raise ValueError("目标等级应为整数")
            minimum, maximum = control.get("min"), control.get("max")
            if minimum is not None and value < int(minimum):
                raise ValueError("目标等级应为 1–100")
            if maximum is not None and value > int(maximum):
                raise ValueError("目标等级应为 1–100")
        else:
            if type(value) not in (int, float) or not math.isfinite(float(value)):
                raise ValueError("目标抗性应为数值")
            minimum, maximum = control.get("min"), control.get("max")
            if float(value) < float(minimum) or float(value) > float(maximum):
                raise ValueError("目标抗性应为 -100%–75%")
    return copy.deepcopy(states), copy.deepcopy(target)


# ---------------------------------------------------------------------------
# 条件求值与贡献重建
# ---------------------------------------------------------------------------


def evaluate_condition_program(
    program: dict[str, Any],
    *,
    states: dict[str, Any],
    target: dict[str, Any],
    runtime: plan_handlers.PlanRuntime,
) -> tuple[Optional[bool], list[str]]:
    """受限条件程序求值；未知返回 None（不得当 false 静默吞掉）。"""
    op = str(program.get("op") or "all")
    if op == "always":
        return True, []
    results: list[Optional[bool]] = []
    failed: list[str] = []
    condition_eval = runtime.handler("condition_eval-v1")
    state_values = {**states, **target}
    for term in program.get("terms") or []:
        kind = term.get("kind")
        if kind == "control_true":
            key = str(term.get("key"))
            value = bool(states.get(key))
            results.append(value)
            if not value:
                failed.append(f"状态未满足：{key}")
        elif kind == "control_equals":
            key = str(term.get("key"))
            value = states.get(key) == term.get("value")
            results.append(value)
            if not value:
                failed.append(f"状态不匹配：{key}")
        elif kind == "control_count":
            key = str(term.get("key"))
            raw = states.get(key)
            value = isinstance(raw, int) and not isinstance(raw, bool) and raw > 0
            results.append(value)
            if not value:
                failed.append(f"状态层数为 0：{key}")
        elif kind == "target_true":
            key = str(term.get("key"))
            value = bool(target.get(key))
            results.append(value)
            if not value:
                failed.append(f"目标条件未满足：{key}")
        elif kind == "code":
            code = str(term.get("code"))
            value = condition_eval(code, state_values)
            results.append(value)
            if value is not True:
                failed.append(f"条件代码未满足或未知：{code}")
    if op == "any" and any(value is True for value in results):
        return True, []
    if any(result is None for result in results):
        return None, failed
    return (any(bool(result) for result in results) if op == "any" else all(bool(result) for result in results)), failed


def _condition_reason(
    failed: list[str],
    effect: dict[str, Any],
    states: dict[str, Any],
    target: dict[str, Any],
) -> Optional[str]:
    if not failed:
        return None
    style = effect.get("condition_style") or {}
    if style:
        condition_key = style.get("condition")
        requires = [str(key) for key in style.get("requires") or []]
        if condition_key and not bool(states.get(str(condition_key))):
            return f"条件未满足：{condition_key}"
        for key in requires:
            value = target.get(key) if key in target else states.get(key)
            if not bool(value):
                return f"条件未满足：{requires}"
    state_keys = [item.split("：", 1)[-1] for item in failed if item.startswith("状态未满足")]
    if state_keys:
        return f"状态未满足：{state_keys}"
    return "；".join(failed)


def _condition_status_for_code(value: Optional[bool]) -> str:
    if value is None:
        return model.CONDITION_UNKNOWN
    return model.CONDITION_SATISFIED if value else model.CONDITION_NOT_SATISFIED


def _evidence(effect: dict[str, Any]) -> list[model.Evidence]:
    rows = []
    for item in effect.get("evidence") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            model.Evidence(
                kind=str(item.get("kind") or "catalog"),
                detail=str(item.get("detail") or ""),
                locator=item.get("locator"),
            )
        )
    return rows


def _runtime_value(effect: dict[str, Any], *, states: dict[str, Any], rows: list[model.Contribution]) -> float:
    mode = effect.get("value_mode")
    params = effect.get("value_params") or {}
    if mode == "static":
        return float(effect.get("value") or 0.0)
    if mode == "state_count":
        states_keys = [str(key) for key in params.get("states") or []]
        count = sum(1 for key in states_keys if bool(states.get(key)))
        return float(params.get("per_state") or 0.0) * count * (1.0 + float(params.get("scaling_sum") or 0.0))
    if mode == "stack_scale":
        control = str(params.get("control") or "")
        raw = states.get(control)
        count = int(raw) if isinstance(raw, int) and not isinstance(raw, bool) else 0
        value = float(params.get("base_value") or 0.0) * count
        cap = params.get("cap_per_point")
        if cap is not None:
            points = int(params.get("points") or (effect.get("raw") or {}).get("points") or 0)
            value = min(value, float(cap) * points)
        return value
    if mode == "movement_conversion":
        total = sum(
            float(row.value)
            for row in rows
            if row.active and row.stat_key == str(params.get("input_stat") or "movement_speed")
        )
        return total * float(params.get("ratio") or 0.0)
    return 0.0


def build_contributions(
    plan: dict[str, Any],
    *,
    states: dict[str, Any],
    target: dict[str, Any],
    runtime: plan_handlers.PlanRuntime,
) -> tuple[list[model.Contribution], dict[str, str], dict[str, dict[str, Any]]]:
    """按计划 effects 顺序重建运行时贡献。"""
    conditions = {
        str(condition.get("condition_id")): condition for condition in plan.get("conditions") or []
    }
    rows: list[model.Contribution] = []
    reasons: dict[str, str] = {}
    notes: dict[str, dict[str, Any]] = {}
    state_values = {**states, **target}
    for effect in sorted(plan.get("effects") or [], key=lambda item: int(item.get("sequence") or 0)):
        if not effect.get("emits_contribution"):
            continue
        included_static = bool(effect.get("included_static"))
        condition_status = str(effect.get("condition_status_static") or model.CONDITION_ALWAYS)
        exclusion_reason = effect.get("exclusion_reason")
        included = included_static
        condition_ref = effect.get("condition_ref")
        if condition_ref:
            program = conditions.get(str(condition_ref))
            if program is None:
                raise PlanValidationError(
                    [
                        {
                            "code": "unknown_condition",
                            "message": f"effect {effect.get('effect_id')!r} 引用未知条件 {condition_ref!r}",
                            "severity": "error",
                            "related_ids": [effect.get("effect_id")],
                            "affects_coverage": False,
                        }
                    ]
                )
            result, failed = evaluate_condition_program(
                program, states=states, target=target, runtime=runtime
            )
            included = included_static and bool(result)
            if result is None:
                exclusion_reason = f"条件无法判定：{failed}"
            elif not result:
                exclusion_reason = _condition_reason(failed, effect, states, target)
        if effect.get("condition_code") and effect.get("condition_code_static"):
            condition_status = str(effect.get("condition_status_static") or model.CONDITION_ALWAYS)
            included = included_static
        elif effect.get("condition_code"):
            value = runtime.handler("condition_eval-v1")(
                str(effect.get("condition_code")), state_values
            )
            condition_status = _condition_status_for_code(value)
            included = included_static
        value = _runtime_value(effect, states=states, rows=rows)
        notes[effect["effect_id"]] = {"value": value, "included": included, "exclusion_reason": exclusion_reason}
        raw_payload = copy.deepcopy(effect.get("raw"))
        if isinstance(raw_payload, dict) and raw_payload.get("state_count_runtime"):
            params = effect.get("value_params") or {}
            raw_payload["state_count"] = sum(
                1 for key in params.get("states") or [] if bool(states.get(key))
            )
            raw_payload.pop("state_count_runtime", None)
        rows.append(
            model.Contribution(
                contribution_id=str(effect.get("contribution_id") or effect["effect_id"]),
                source_kind=str(effect.get("source_kind") or "derived"),
                source_label=str(effect.get("source_label") or effect["effect_id"]),
                source_path=str(effect.get("source_path") or effect["effect_id"]),
                stat_name=str(effect.get("stat_name") or effect.get("stat_key") or ""),
                stat_key=str(effect.get("stat_key") or ""),
                operation=str(effect.get("operation") or "flag"),
                value=float(value),
                unit=str(effect.get("unit") or "flat"),
                damage_tags=list(effect.get("damage_tags") or []),
                target=effect.get("target"),
                condition_code=effect.get("condition_code"),
                condition_status=condition_status,
                group=effect.get("group"),
                included=bool(included),
                exclusion_reason=None if included else exclusion_reason,
                value_source=str(effect.get("value_source") or "source"),
                status=str(effect.get("status") or model.STATUS_ASSUMED),
                evidence=_evidence(effect),
                notes=effect.get("notes"),
                raw=raw_payload,
            )
        )
    return rows, reasons, notes


# ---------------------------------------------------------------------------
# 账本 / 覆盖投影（供 plan_handlers 包装调用）
# ---------------------------------------------------------------------------


def project_ledger(
    *,
    plan: dict[str, Any],
    contributions_by_id: dict[str, model.Contribution],
    notes: dict[str, dict[str, Any]],
    states: dict[str, Any],
) -> dict[str, Any]:
    """把编译期账本模板与运行时贡献状态合并，保持第一步账本形状。"""
    facts = plan.get("facts") or {}
    ledger = copy.deepcopy(facts.get("ledger_static") or {})
    ledger.setdefault("node_bindings", [])
    ledger.setdefault("effects", [])
    ledger.setdefault("state_scalings", [])
    ledger.setdefault("movement_conversion", {})
    ledger.setdefault("attribute_scaling", {})
    ledger.setdefault("attribute_conversion", {})
    ledger.setdefault("cooldown_reduction", {})
    ledger.setdefault("ailment_chance_sources", [])
    effects = list(plan.get("effects") or [])
    by_rule = {str(effect.get("rule_id")): effect for effect in effects if effect.get("rule_id")}
    by_effect_id = {str(effect.get("effect_id")): effect for effect in effects}

    def runtime_row(effect: Optional[dict[str, Any]]) -> Optional[model.Contribution]:
        if not effect:
            return None
        return contributions_by_id.get(str(effect.get("effect_id")))

    for entry in ledger["node_bindings"]:
        effect = by_rule.get(str(entry.get("effect_id")))
        row = runtime_row(effect)
        entry["status"] = "applied" if (row is not None and row.active) else "condition_not_satisfied"
        entry["reason"] = None if (row is not None and row.active) else (row.exclusion_reason if row is not None else entry.get("reason"))
        if row is not None:
            entry["contribution_id"] = row.contribution_id
            entry["payload"] = dict(entry.get("payload") or {})
            entry["payload"]["computed_value"] = row.value
        elif entry.get("check") and (entry.get("check") or {}).get("status") not in ("verified", "applied"):
            entry["status"] = (entry.get("check") or {}).get("status") or "unresolved"

    for entry in ledger["effects"]:
        effect = by_rule.get(str(entry.get("effect_id")))
        row = runtime_row(effect)
        if row is not None:
            entry["status"] = "applied" if row.active else "condition_not_satisfied"
            entry["reason"] = None if row.active else row.exclusion_reason
            entry["contribution_id"] = row.contribution_id
            control = str(((effect or {}).get("value_params") or {}).get("control") or "")
            if control:
                entry["payload"] = dict(entry.get("payload") or {})
                entry["payload"]["stack_count"] = int(states.get(control) or 0)

    for entry in ledger["state_scalings"]:
        effect = by_rule.get(str(entry.get("effect_id")))
        params = (effect or {}).get("value_params") or {}
        active_states = [key for key in params.get("states") or [] if bool(states.get(key))]
        entry["status"] = "applied" if active_states else "not_applicable"
        entry["reason"] = None if active_states else "相关状态未启用"
        entry["payload"] = dict(entry.get("payload") or {})
        entry["payload"]["state_count"] = len(active_states)

    movement_effect = next(
        (effect for effect in effects if effect.get("category") == "movement_conversion"), None
    )
    movement_row = runtime_row(movement_effect)
    if isinstance(ledger["movement_conversion"], dict) and ledger["movement_conversion"]:
        entry = ledger["movement_conversion"]
        if movement_row is not None:
            entry["status"] = "applied" if movement_row.active else "not_applicable"
            entry["reason"] = None if movement_row.active else movement_row.exclusion_reason
            entry["contribution_id"] = movement_row.contribution_id
            entry["payload"] = dict(entry.get("payload") or {})
            entry["payload"]["movement_total"] = movement_row.value
    scaling_effect = next(
        (effect for effect in effects if effect.get("category") == "attribute_scaling"), None
    )
    if isinstance(ledger["attribute_scaling"], dict) and ledger["attribute_scaling"]:
        entry = ledger["attribute_scaling"]
        if not entry.get("contribution_id"):
            entry["contribution_id"] = "rule:attribute_scaling"
    if isinstance(ledger["cooldown_reduction"], dict) and ledger["cooldown_reduction"]:
        entry = ledger["cooldown_reduction"]
        entry["status"] = str(
            (next((effect for effect in effects if effect.get("category") == "cooldown_reduction"), {}) or {}).get(
                "coverage_status"
            )
            or entry.get("status")
        )
    return ledger


def project_coverage(
    *,
    plan: dict[str, Any],
    dot_applications: list[dict[str, Any]],
) -> dict[str, Any]:
    base_gaps = copy.deepcopy((plan.get("facts") or {}).get("coverage_base_gaps") or [])
    return dps_result.merge_runtime_gaps(base_gaps, unmodeled_chance_records=dot_applications)


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def execute_plan(
    plan: dict[str, Any],
    scenario: dict[str, Any],
    runtime: Optional[plan_handlers.PlanRuntime] = None,
) -> dict[str, Any]:
    """执行已编译计划；输入只有 plan/scenario/runtime。

    plan-v1 与 plan-v2 共用本公开入口；v2 的组件基数/频率/结果语义在
    ``plan_executor_v2`` 中实现，不维护第二套技能公式。
    """
    if isinstance(plan, dict) and plan.get("schema") == planmod.SCHEMA_V2:
        from . import plan_executor_v2  # noqa: PLC0415

        return plan_executor_v2.execute_plan_v2(plan, scenario, runtime)
    if isinstance(plan, dict) and plan.get("schema") == planmod.SCHEMA_V3:
        from . import plan_executor_v3  # noqa: PLC0415

        return plan_executor_v3.execute_plan_v3(plan, scenario, runtime=runtime)
    if isinstance(plan, dict) and plan.get("schema") == "le-calculation-plan-v4":
        from . import plan_v4  # noqa: PLC0415

        return plan_v4.execute_plan_v4(plan, scenario, runtime=runtime)
    runtime = runtime or plan_handlers.PlanRuntime()
    issues_validation = validate_plan(plan, runtime)
    if issues_validation:
        raise PlanValidationError(issues_validation)
    states, target = validate_scenario(plan, scenario)
    schedule = plan.get("schedule") or {}
    if float(schedule.get("first_cast_s") or 0.0) < 0:
        raise PlanValidationError(
            [
                {
                    "code": "invalid_schedule",
                    "message": "schedule.first_cast_s 不能为负",
                    "severity": "error",
                    "related_ids": ["schedule"],
                    "affects_coverage": False,
                }
            ]
        )
    rows, _reasons, notes = build_contributions(
        plan, states=states, target=target, runtime=runtime
    )
    contributions_by_id = {
        str(effect["effect_id"]): row
        for effect, row in zip(
            [
                item
                for item in sorted(plan.get("effects") or [], key=lambda item: int(item.get("sequence") or 0))
                if item.get("emits_contribution")
            ],
            rows,
        )
    }
    parameters = plan.get("parameters") or {}
    skill_params = parameters.get("skill") or {}
    schedule_doc = plan.get("schedule") or {}
    component_entries = [item for item in plan.get("components") or [] if isinstance(item, dict)]
    hit_entry = next((item for item in component_entries if item.get("kind") == "hit"), {})
    dot_entry = next((item for item in component_entries if item.get("kind") == "dot"), {})
    hit_template = {
        "tags": list(hit_entry.get("tags") or []),
        "crit_capable": bool(hit_entry.get("crit_capable")),
        "adaptive_damage_type": hit_entry.get("adaptive_damage_type"),
    }
    dot_template = {
        "tags": list(dot_entry.get("tags") or ["DoT"]),
        "crit_capable": bool(dot_entry.get("crit_capable")),
        "adaptive_damage_type": dot_entry.get("adaptive_damage_type"),
    }
    rule_payload = {
        "crit": parameters.get("crit") or {},
        "every_nth_hit": schedule_doc.get("every_nth_hit"),
        "ailments": parameters.get("ailments") or [],
        "damage_to_penetration": parameters.get("damage_to_penetration"),
    }
    state_values = {**states, **target}
    speed = runtime.handler("attack_speed_pool-v1")(
        contributions=rows, component_tags=list(hit_template.get("tags") or [])
    )
    cooldown_recovery = runtime.handler("cooldown_recovery_pool-v1")(contributions=rows)
    cooldown_reduction = float(parameters.get("cooldown_reduction") or 0.0)
    timing = runtime.handler("frequency_fixed_repeat-v1")(
        action="cast_interval",
        cooldown_s=float(skill_params.get("cooldown_s") or 0.0),
        action_s=float(skill_params.get("action_s") or 0.0),
        cooldown_reduction=cooldown_reduction,
        cooldown_recovery=cooldown_recovery,
        attack_speed=speed,
    )
    target_info = runtime.handler("target_side-v1")(
        action="resolve_hit_target",
        merged_target={
            "monster_level": target["monster_level"],
            "fire_resistance": target["resistance"],
        },
    )
    hit_components, hit_applications = runtime.handler("hit_component_assembly-v1")(
        contributions=rows,
        skill={
            "base_damage": skill_params.get("base_damage") or {},
            "added_damage_scaling": skill_params.get("added_damage_scaling") or 0.0,
            "crit_chance": skill_params.get("crit_chance") or 0.0,
            "crit_multiplier": skill_params.get("crit_multiplier") or 0.0,
        },
        component=hit_template,
        crit_rules=rule_payload.get("crit") or {},
        target=target,
        states=state_values,
        target_info=target_info,
    )
    dot_components, dot_applications, chance_notes = runtime.handler("dot_component_assembly-v1")(
        contributions=rows,
        ailments=rule_payload.get("ailments") or [],
        dot_component=dot_template,
        penetration_rule=rule_payload.get("damage_to_penetration"),
        target=target,
        target_info=target_info,
        additional_chance=parameters.get("additional_chance") or {},
    )
    single = sum(float(component["damage"]) for component in hit_components)
    windows, steady = runtime.handler("frequency_fixed_repeat-v1")(
        action="hit_windows",
        single_hit=single,
        ailments=dot_components,
        interval=timing["interval_s"],
        every_nth=rule_payload.get("every_nth_hit"),
        windows_s=tuple(float(value) for value in schedule.get("windows_s") or (5, 30, 40)),
        first_cast_s=float(schedule.get("first_cast_s") or 0.0),
    )
    if not math.isfinite(steady["sustained_dps"]) or not math.isfinite(single):
        raise ValueError("计算结果无效")
    coverage = runtime.handler("coverage_projection-v1")(
        plan=plan, dot_applications=dot_applications
    )
    effect_ledger = runtime.handler("ledger_projection-v1")(
        plan=plan,
        contributions_by_id=contributions_by_id,
        notes=notes,
        states=states,
    )
    facts = plan.get("facts") or {}
    presentation = plan.get("presentation") or {}
    limitations = [str(note) for note in (plan.get("scope") or {}).get("limitations") or []]
    issues: list[str] = list(limitations)
    for note in chance_notes:
        issues.append(f"{note['ailment']} 施加概率 {note['chance']:.3f} > 100%：{note['issue']}")
    issues.extend(str(item) for item in facts.get("post_assembly_issues") or [])
    issues.extend(str(item) for item in facts.get("early_issues") or [])
    if coverage["status"] == "partial":
        issues.append(
            f"覆盖状态 partial：{coverage['gap_count']} 项未建模/未验证的伤害相关来源（见 coverage.gaps）"
        )
    issues.extend(str(item) for item in facts.get("duplicate_issues") or [])
    scope, scope_detail = dps_result.build_scope(
        skill_name=str(presentation.get("skill_name") or facts.get("skill_name") or ""),
        hit_components=hit_components,
        dot_components=[
            {**ailment, "label_zh": declared.get("label_zh")}
            for ailment, declared in zip(dot_components, rule_payload.get("ailments") or [])
        ],
        controls=[
            control for control in plan.get("controls") or [] if control.get("namespace") == "state"
        ],
        states=states,
        target=target,
        limitations=limitations,
    )
    application_summary = dps_result.application_summary(hit_applications + dot_applications)
    coverage_classes: dict[str, Any] = {}
    for effect in plan.get("effects") or []:
        contribution_id = str(effect.get("contribution_id") or effect.get("effect_id"))
        coverage_classes.setdefault(contribution_id, effect.get("coverage_class"))
    contributions_ledger = []
    for row in rows:
        payload = row.to_dict()
        summary = application_summary.get(row.contribution_id)
        if summary:
            payload["application"] = summary
        payload["coverage_class"] = coverage_classes.get(row.contribution_id)
        contributions_ledger.append(payload)
    scenario_digest = planmod.scenario_sha256({"states": states, "target": target})
    fingerprint = planmod.execution_fingerprint(
        plan, scenario_digest, runtime.executor_fingerprint
    )
    profile_ref = next(
        (ref for ref in plan.get("input_refs") or [] if ref.get("role") == "profile"), None
    )
    profile_content_ref = next(
        (ref for ref in plan.get("input_refs") or [] if ref.get("role") == "profile_content"), None
    )
    calc_ref = next(
        (ref for ref in plan.get("input_refs") or [] if ref.get("role") == "calculation_input"), None
    )
    return {
        "schema": SCHEMA_RESULT,
        "case_id": (plan.get("subject") or {}).get("case_id"),
        "model": "dps",
        "skill": presentation.get("skill_name") or facts.get("skill_name"),
        "character": copy.deepcopy(facts.get("character")),
        "skill_names": copy.deepcopy(presentation.get("skill_names") or {}),
        "scope": scope,
        "scope_detail": scope_detail,
        "status": "assumption",
        "coverage": coverage,
        "scenario": {"states": states, "target": target},
        "states": states,
        "target": target_info,
        "attributes": copy.deepcopy(facts.get("attributes")),
        "attribute_conversion": copy.deepcopy(facts.get("attribute_conversion_info")),
        "equipment_facts": copy.deepcopy(facts.get("equipment_facts")),
        "skill_constants": copy.deepcopy(facts.get("skill_constants")),
        "ailment_constants": copy.deepcopy(facts.get("ailment_constants")),
        "single_hit": single,
        "single_cast_with_tail": single + sum(float(ailment["damage_per_cast"]) for ailment in dot_components),
        "sustained_dps": steady["sustained_dps"],
        "hit_dps": steady["hit_dps"],
        "dot_dps": steady["dot_dps"],
        "interval_s": timing["interval_s"],
        "cooldown_s": timing["cooldown_s"],
        "cooldown_recovery": cooldown_recovery,
        "cooldown_reduction": cooldown_reduction,
        "attack_speed_increased": speed,
        "action_s": timing["action_s"],
        "windows": windows,
        "components": hit_components,
        "ailments": dot_components,
        "applications": hit_applications + dot_applications,
        "contribution_application_summary": application_summary,
        "duplicate_effects": copy.deepcopy(facts.get("duplicate_effects") or []),
        "issues": issues,
        "contributions": contributions_ledger,
        "effect_ledger": effect_ledger,
        "rules": {
            "crit": rule_payload.get("crit"),
            "every_nth_hit": rule_payload.get("every_nth_hit"),
            "damage_to_penetration": rule_payload.get("damage_to_penetration"),
        },
        "equipment": copy.deepcopy(presentation.get("equipment") or []),
        "controls": copy.deepcopy(presentation.get("controls") or []),
        "input_sha256": (calc_ref or {}).get("sha256"),
        "profile_sha256": (profile_ref or {}).get("sha256") or (profile_content_ref or {}).get("sha256"),
        "plan_ref": planmod.plan_ref(plan),
        "plan_sha256": plan.get("plan_sha256"),
        "scenario_sha256": scenario_digest,
        "execution_fingerprint": fingerprint,
    }


__all__ = [
    "PlanValidationError",
    "SCHEMA_RESULT",
    "SCHEMA_RESULT_V4",
    "build_contributions",
    "evaluate_condition_program",
    "execute_plan",
    "project_coverage",
    "project_ledger",
    "validate_plan",
    "validate_scenario",
]
