"""统一计算计划（``le-calculation-plan-v1``）的契约与哈希工具。

计划是**编译产物**：纯 JSON、封闭字段、无 Python 对象/callable/NaN/机器绝对路径；
``plan_sha256`` 对去掉 ``plan_id``/``plan_sha256`` 的完整载荷做 canonical JSON 哈希，
``plan_id = "cp-" + plan_sha256``。执行器只消费计划、场景与静态 handler registry。
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = "le-calculation-plan-v1"
SCHEMA_V2 = "le-calculation-plan-v2"
SCHEMA_V3 = "le-calculation-plan-v3"
SUPPORTED_SCHEMAS = (SCHEMA, SCHEMA_V2, SCHEMA_V3)
PLAN_SCHEMA_PATH = "tools/phase3/schemas/calculation-plan-v1.json"
PLAN_V2_SCHEMA_PATH = "tools/phase3/schemas/calculation-plan-v2.json"
PLAN_V3_SCHEMA_PATH = "tools/phase3/schemas/calculation-plan-v3.json"
PLAN_ID_PREFIX = "cp-"
PLAN_VERSION = "1.0.0"
PLAN_VERSION_V2 = "2.0.0"
PLAN_VERSION_V3 = "3.0.0"

TOP_LEVEL_FIELDS = (
    "schema",
    "plan_id",
    "plan_sha256",
    "subject",
    "compiler",
    "executor_requirements",
    "input_refs",
    "sources",
    "facts",
    "parameters",
    "controls",
    "conditions",
    "components",
    "effects",
    "evaluation",
    "schedule",
    "diagnostics",
    "assumptions",
    "scope",
    "presentation",
)
TOP_LEVEL_FIELDS_V2 = TOP_LEVEL_FIELDS + ("evaluations",)
REQUIRED_TOP_LEVEL = tuple(field for field in TOP_LEVEL_FIELDS if field not in ("plan_id", "plan_sha256"))
REQUIRED_TOP_LEVEL_V2 = tuple(field for field in TOP_LEVEL_FIELDS_V2 if field not in ("plan_id", "plan_sha256"))
TOP_LEVEL_FIELDS_V3 = TOP_LEVEL_FIELDS_V2
REQUIRED_TOP_LEVEL_V3 = REQUIRED_TOP_LEVEL_V2
SCHEDULE_WINDOW_SEMANTICS = ("expected_casts", "complete_casts")
SCHEDULE_POLICIES = ("cooldown_limited", "attack_speed_limited")
ROTATION_KINDS = ("fixed_more_factor",)
ROTATION_APPLIES_TO = ("direct_hit_dps",)

EFFECT_VALUE_MODES = ("static", "condition", "condition_code", "state_count", "stack_scale", "movement_conversion")
PLAN_EFFECT_CATEGORIES = (
    "base",
    "mod",
    "passive",
    "node_binding",
    "effect",
    "state_scaling",
    "movement_conversion",
    "attribute_scaling",
    "attribute_conversion",
    "cooldown_reduction",
    "row_reclassification",
)
COMPONENT_KINDS = ("hit", "dot")
CONDITION_KINDS = ("always", "all", "any")
CONDITION_TERM_KINDS = ("control_true", "control_equals", "control_count", "target_true", "code")
SCHEDULE_DRIVERS = ("fixed_repeat",)
SCHEDULE_DRIVERS_V3 = ("charge_dot_cycle",)
SCHEDULE_ACTION_POLICIES = ("eager", "patient")
DOT_REMAINDER_POLICIES = ("ceil_full_ticks",)
INPUT_REF_ROLES = (
    "case",
    "profile",
    "profile_content",
    "calculation_input",
    "build_spec",
    "dataset_pointer",
    "dataset_content",
    "attribute_conversion_rules",
    "compiler_source",
    "template",
    "scenario",
    "mechanism_library",
    "rule_file",
)


class PlanError(ValueError):
    """计划结构、指纹或语义无效。"""


def canonical_json(payload: Any) -> str:
    """canonical JSON：UTF-8、键排序、紧凑分隔、禁止非有限数。"""
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def is_sha256(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def payload_for_hash(plan: dict[str, Any]) -> dict[str, Any]:
    payload = dict(plan)
    payload.pop("plan_id", None)
    payload.pop("plan_sha256", None)
    return payload


def compute_plan_sha256(payload: dict[str, Any]) -> str:
    return sha256_text(canonical_json(payload_for_hash(payload)))


def finalize_plan(payload: dict[str, Any], *, schema: Optional[str] = None) -> dict[str, Any]:
    """写入 ``plan_sha256`` 与 ``plan_id`` 并返回同一对象。

    ``schema`` 缺省时保留载荷中受支持的 schema（默认 v1），版本升级必须显式传入，
    不能静默改变旧版本语义。
    """
    plan = dict(payload)
    resolved_schema = schema or plan.get("schema") or SCHEMA
    if resolved_schema not in SUPPORTED_SCHEMAS:
        raise PlanError(f"不支持的计划 schema：{resolved_schema!r}")
    plan["schema"] = resolved_schema
    refs = plan.get("input_refs")
    if isinstance(refs, list):
        # 同一依赖集合的顺序不得改变 plan_sha：统一按 role/path/sha 规范化
        plan["input_refs"] = sorted(
            refs,
            key=lambda item: (
                str(item.get("role") or "") if isinstance(item, dict) else str(item),
                str(item.get("path") or "") if isinstance(item, dict) else "",
                str(item.get("sha256") or "") if isinstance(item, dict) else "",
            ),
        )
    plan_sha = compute_plan_sha256(plan)
    plan["plan_sha256"] = plan_sha
    plan["plan_id"] = PLAN_ID_PREFIX + plan_sha
    return plan


def plan_ref(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": plan.get("schema"),
        "plan_id": plan.get("plan_id"),
        "plan_sha256": plan.get("plan_sha256"),
    }


def scenario_sha256(scenario: dict[str, Any]) -> str:
    return sha256_text(canonical_json(scenario))


def aggregate_scenario_sha256(evaluations: Iterable[dict[str, Any]]) -> str:
    """plan-v2 多评估执行身份：按稳定 evaluation_id 排序后聚合全部评估场景哈希。

    聚合值只依赖各评估的 ``evaluation_id`` 与 ``scenario_sha256``，与评估在文件/计划
    中的顺序无关；任一评估（含非主评估）场景变化都会改变聚合值。
    """
    rows = [
        {
            "evaluation_id": str(item.get("evaluation_id")),
            "scenario_sha256": str(item.get("scenario_sha256")),
        }
        for item in evaluations
    ]
    rows.sort(key=lambda row: row["evaluation_id"])
    return sha256_text(canonical_json({"evaluations": rows}))


def execution_fingerprint(
    plan: dict[str, Any],
    scenario_digest: str,
    executor_fingerprint: str,
) -> str:
    return sha256_text(
        canonical_json(
            {
                "plan_sha256": plan.get("plan_sha256"),
                "scenario_sha256": scenario_digest,
                "executor_fingerprint": executor_fingerprint,
            }
        )
    )


def verify_plan_integrity(plan: dict[str, Any]) -> None:
    """校验 schema、plan_sha256 与 plan_id 一致（结构校验另行进行）。"""
    if not isinstance(plan, dict):
        raise PlanError("计划必须是 JSON 对象")
    if plan.get("schema") == "le-calculation-plan-v4":
        from . import plan_v4  # noqa: PLC0415

        plan_v4.validate_plan_v4(plan)
        return
    if plan.get("schema") not in SUPPORTED_SCHEMAS:
        raise PlanError(f"不支持的计划 schema：{plan.get('schema')!r}")
    declared = plan.get("plan_sha256")
    if not is_sha256(declared):
        raise PlanError("计划缺少合法的 plan_sha256")
    actual = compute_plan_sha256(plan)
    if actual != declared:
        raise PlanError(
            "计划内容与 plan_sha256 不一致（可能被手改或序列化损坏）："
            f"declared={declared} actual={actual}"
        )
    expected_id = PLAN_ID_PREFIX + declared
    if plan.get("plan_id") != expected_id:
        raise PlanError(f"plan_id 与 plan_sha256 不一致：{plan.get('plan_id')!r} != {expected_id!r}")


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    verify_plan_integrity(plan)
    return plan


def write_plan(path: str | Path, plan: dict[str, Any]) -> None:
    Path(path).write_text(
        json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 结构/语义校验（不依赖第三方 jsonschema；JSON Schema 文件是契约文本）
# ---------------------------------------------------------------------------


def _add(issues: list[dict[str, Any]], code: str, message: str, **extra: Any) -> None:
    record = {"code": code, "message": message, "severity": "error"}
    record.update(extra)
    issues.append(record)


def _require_sha(issues: list[dict[str, Any]], value: Any, field: str) -> None:
    if not is_sha256(value):
        _add(issues, "invalid_sha256", f"{field} 必须是 64 位小写 SHA-256", field=field)


def _require_number(issues: list[dict[str, Any]], value: Any, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        _add(issues, "invalid_number", f"{field} 必须是有限数值", field=field)


def _check_conditions(issues: list[dict[str, Any]], conditions: Any, control_ids: set[str]) -> set[str]:
    ids: set[str] = set()
    if not isinstance(conditions, list):
        _add(issues, "invalid_type", "conditions 必须是数组", field="conditions")
        return ids
    for index, condition in enumerate(conditions):
        field = f"conditions[{index}]"
        if not isinstance(condition, dict):
            _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
            continue
        condition_id = condition.get("condition_id")
        if not isinstance(condition_id, str) or not condition_id:
            _add(issues, "missing_field", f"{field}.condition_id 缺失", field=field)
        elif condition_id in ids:
            _add(issues, "duplicate_id", f"condition_id 重复：{condition_id}", field=field)
        else:
            ids.add(condition_id)
        if condition.get("op") not in CONDITION_KINDS:
            _add(issues, "unsupported_condition", f"{field}.op 不受支持：{condition.get('op')!r}", field=field)
        terms = condition.get("terms")
        if not isinstance(terms, list):
            _add(issues, "invalid_type", f"{field}.terms 必须是数组", field=field)
            continue
        for term_index, term in enumerate(terms):
            term_field = f"{field}.terms[{term_index}]"
            if not isinstance(term, dict):
                _add(issues, "invalid_type", f"{term_field} 必须是对象", field=term_field)
                continue
            kind = term.get("kind")
            if kind not in CONDITION_TERM_KINDS:
                _add(issues, "unsupported_condition", f"{term_field}.kind 不受支持：{kind!r}", field=term_field)
                continue
            if kind in ("control_true", "control_equals", "control_count") and term.get("key") not in control_ids:
                _add(issues, "unknown_reference", f"{term_field} 引用未声明控制项 {term.get('key')!r}", field=term_field)
            if kind == "code" and not isinstance(term.get("code"), str):
                _add(issues, "invalid_type", f"{term_field}.code 必须是字符串", field=term_field)
    return ids


PARAMETER_SKILL_KEYS = (
    "base_damage",
    "added_damage_scaling",
    "crit_chance",
    "crit_multiplier",
    "cooldown_s",
    "action_s",
    "skill_name",
)
EFFECT_UNITS = ("flat", "percent", "seconds", "flag")


def _validate_schedule_v2(issues: list[dict[str, Any]], schedule: Any) -> None:
    """plan-v2 频率/窗口调度契约：具名策略、期望施放数窗口与周期平均因子。

    当前执行器只实现 ``steady_state`` 驱动、``first_cast_s=0``、无 ``every_nth_hit``；
    未实现语义必须在结构校验入口拒绝，不能解析后静默忽略。
    """
    if not isinstance(schedule, dict):
        _add(issues, "invalid_type", "schedule 必须是对象", field="schedule")
        return
    field = "schedule"
    if schedule.get("schema") != "le-schedule-v2":
        _add(issues, "unsupported_schedule", "schedule.schema 必须是 le-schedule-v2", field=field)
    if schedule.get("driver") != "steady_state":
        _add(
            issues,
            "unsupported_plan_v2_feature",
            "plan-v2 当前执行器只实现 steady_state 频率驱动；hit_windows 未实现，必须在入口拒绝",
            field=field,
        )
    for key in ("first_cast_s", "windows_s", "policies", "primary_policy", "window_semantics", "rotation"):
        if key not in schedule:
            _add(issues, "missing_field", f"schedule.{key} 缺失", field=field)
    _require_number(issues, schedule.get("first_cast_s"), "schedule.first_cast_s")
    if schedule.get("first_cast_s") not in (0, 0.0):
        _add(
            issues,
            "unsupported_plan_v2_feature",
            "plan-v2 当前执行器只实现首次施放 0 秒；schedule.first_cast_s 必须为 0，非零值在入口拒绝",
            field=field,
        )
    if schedule.get("every_nth_hit") is not None:
        _add(
            issues,
            "unsupported_plan_v2_feature",
            "plan-v2 当前执行器未实现 schedule.every_nth_hit；必须为 null，不得声明后被忽略",
            field=field,
        )
    windows = schedule.get("windows_s")
    if not isinstance(windows, list) or not windows or any(
        not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0 for value in windows
    ):
        _add(issues, "invalid_schedule", "schedule.windows_s 必须是正数数组", field=field)
    if schedule.get("window_semantics") not in SCHEDULE_WINDOW_SEMANTICS:
        _add(
            issues,
            "unsupported_schedule",
            f"schedule.window_semantics 只支持 {SCHEDULE_WINDOW_SEMANTICS}",
            field=field,
        )
    policies = schedule.get("policies")
    policy_ids: list[str] = []
    if not isinstance(policies, list) or not policies:
        _add(issues, "invalid_schedule", "schedule.policies 必须是非空数组", field=field)
    else:
        expected_policy_shapes = {
            "cooldown_limited": ("cooldown", "max(action_time, cooldown)"),
            "attack_speed_limited": ("attack_speed", "action_time"),
        }
        for index, policy in enumerate(policies):
            policy_field = f"schedule.policies[{index}]"
            if not isinstance(policy, dict):
                _add(issues, "invalid_type", f"{policy_field} 必须是对象", field=policy_field)
                continue
            policy_id = policy.get("policy_id")
            if policy_id not in SCHEDULE_POLICIES:
                _add(issues, "unsupported_schedule", f"{policy_field}.policy_id 不受支持：{policy_id!r}", field=policy_field)
            else:
                policy_ids.append(policy_id)
            if policy.get("binding_constraint") not in ("cooldown", "attack_speed"):
                _add(issues, "unsupported_schedule", f"{policy_field}.binding_constraint 不受支持", field=policy_field)
            if policy.get("interval_policy") not in ("max(action_time, cooldown)", "action_time"):
                _add(issues, "unsupported_schedule", f"{policy_field}.interval_policy 不受支持", field=policy_field)
            expected = expected_policy_shapes.get(str(policy_id))
            if expected is not None and (
                policy.get("binding_constraint"),
                policy.get("interval_policy"),
            ) != expected:
                _add(
                    issues,
                    "unsupported_schedule",
                    f"{policy_field} 的 binding_constraint/interval_policy 必须与 policy_id={policy_id!r} 固定对应 {expected}",
                    field=policy_field,
                )
    if len(policy_ids) != len(set(policy_ids)):
        _add(issues, "invalid_schedule", "schedule.policies 的 policy_id 不得重复", field=field)
    primary = schedule.get("primary_policy")
    if not isinstance(primary, str) or primary not in policy_ids:
        _add(issues, "invalid_schedule", "schedule.primary_policy 必须声明且存在于 policies", field=field)
    rotation = schedule.get("rotation")
    if not isinstance(rotation, dict):
        _add(issues, "invalid_type", "schedule.rotation 必须是对象", field=field)
    else:
        if rotation.get("kind") not in ROTATION_KINDS:
            _add(issues, "unsupported_schedule", "schedule.rotation.kind 只支持 fixed_more_factor", field=field)
        _require_number(issues, rotation.get("factor"), "schedule.rotation.factor")
        if isinstance(rotation.get("factor"), (int, float)) and not isinstance(rotation.get("factor"), bool) and float(rotation.get("factor")) <= 0:
            _add(issues, "invalid_schedule", "schedule.rotation.factor 必须 > 0", field=field)
        applies = rotation.get("applies_to")
        if not isinstance(applies, list) or not applies or any(item not in ROTATION_APPLIES_TO for item in applies):
            _add(
                issues,
                "unsupported_schedule",
                f"schedule.rotation.applies_to 只支持 {ROTATION_APPLIES_TO}",
                field=field,
            )
        if rotation.get("status") not in ("confirmed", "derived", "assumed", "unresolved"):
            _add(issues, "invalid_schedule", "schedule.rotation.status 不受支持", field=field)


def _validate_schedule_v3(issues: list[dict[str, Any]], schedule: Any) -> None:
    if not isinstance(schedule, dict):
        _add(issues, "invalid_type", "schedule 必须是对象", field="schedule")
        return
    if schedule.get("schema") != "le-schedule-v3":
        _add(issues, "unsupported_schedule", "plan-v3 schedule.schema 必须是 le-schedule-v3", field="schedule")
    if schedule.get("driver") not in SCHEDULE_DRIVERS_V3:
        _add(issues, "unsupported_schedule", f"plan-v3 schedule.driver 必须是 {SCHEDULE_DRIVERS_V3}", field="schedule")
    _require_number(issues, schedule.get("horizon_s"), "schedule.horizon_s")
    windows = schedule.get("windows_s")
    if not isinstance(windows, list) or not windows:
        _add(issues, "invalid_schedule", "schedule.windows_s 必须是非空数组", field="schedule")
    else:
        values = [value for value in windows if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if len(values) != len(windows) or any(float(value) <= 0 for value in values):
            _add(issues, "invalid_schedule", "schedule.windows_s 必须是正数数组", field="schedule")
        elif sorted(float(value) for value in values) != [float(value) for value in values]:
            _add(issues, "invalid_schedule", "schedule.windows_s 必须升序", field="schedule")
        elif float(values[-1]) > float(schedule.get("horizon_s") or 0) + 1e-9:
            _add(issues, "invalid_schedule", "schedule.windows_s 不得超过 horizon_s", field="schedule")
    cast = schedule.get("cast")
    if not isinstance(cast, dict):
        _add(issues, "invalid_type", "schedule.cast 必须是对象", field="schedule.cast")
    else:
        _require_number(issues, cast.get("base_rate_per_s"), "schedule.cast.base_rate_per_s")
    charges = schedule.get("charges")
    if not isinstance(charges, dict):
        _add(issues, "invalid_type", "schedule.charges 必须是对象", field="schedule.charges")
    else:
        base_max = charges.get("base_max_charges")
        if isinstance(base_max, bool) or not isinstance(base_max, int) or base_max < 1:
            _add(issues, "invalid_schedule", "schedule.charges.base_max_charges 必须是 >=1 整数", field="schedule.charges")
        _require_number(issues, charges.get("base_recharge_s"), "schedule.charges.base_recharge_s")
        _require_number(issues, charges.get("flat_adjustment_s", 0.0), "schedule.charges.flat_adjustment_s")
        _require_number(issues, charges.get("cooldown_reduction", 0.0), "schedule.charges.cooldown_reduction")
        extra = charges.get("extra_max_charges", 0)
        if isinstance(extra, bool) or not isinstance(extra, int) or extra < 0:
            _add(issues, "invalid_schedule", "schedule.charges.extra_max_charges 必须是非负整数", field="schedule.charges")
    policies = schedule.get("action_policies")
    if not isinstance(policies, list):
        _add(issues, "invalid_type", "schedule.action_policies 必须是数组", field="schedule.action_policies")
    else:
        ids = [str(item.get("policy_id")) for item in policies if isinstance(item, dict)]
        if set(ids) != set(SCHEDULE_ACTION_POLICIES) or len(ids) != len(SCHEDULE_ACTION_POLICIES):
            _add(issues, "invalid_schedule", "schedule.action_policies 必须恰好声明 eager/patient", field="schedule.action_policies")
    if schedule.get("primary_policy") not in SCHEDULE_ACTION_POLICIES:
        _add(issues, "invalid_schedule", "schedule.primary_policy 必须是 eager/patient", field="schedule")
    ignite = schedule.get("ignite")
    if not isinstance(ignite, dict):
        _add(issues, "invalid_type", "plan-v3 schedule.ignite 必须是对象", field="schedule.ignite")
        return
    _require_number(issues, ignite.get("base_damage"), "schedule.ignite.base_damage")
    _require_number(issues, ignite.get("base_duration_s"), "schedule.ignite.base_duration_s")
    _require_number(issues, ignite.get("rolls_per_second"), "schedule.ignite.rolls_per_second")
    _require_number(issues, ignite.get("chance_total"), "schedule.ignite.chance_total")
    if not isinstance(ignite.get("scaling_tags"), list) or not ignite.get("scaling_tags"):
        _add(issues, "invalid_type", "schedule.ignite.scaling_tags 必须是非空数组", field="schedule.ignite")
    if ignite.get("first_roll_at") != "spawn":
        _add(issues, "unsupported_schedule", "schedule.ignite.first_roll_at 只支持 spawn", field="schedule.ignite")
    target = schedule.get("target")
    if target is not None and not isinstance(target, dict):
        _add(issues, "invalid_type", "schedule.target 必须是对象或 null", field="schedule.target")


def _validate_evaluations_v3(issues: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    evaluations = plan.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        _add(issues, "invalid_type", "plan-v3 evaluations 必须是非空数组", field="evaluations")
        return
    ids: set[str] = set()
    policies: list[str] = []
    primary = 0
    for index, item in enumerate(evaluations):
        field = f"evaluations[{index}]"
        if not isinstance(item, dict):
            _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
            continue
        evaluation_id = item.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            _add(issues, "missing_field", f"{field}.evaluation_id 缺失", field=field)
        elif evaluation_id in ids:
            _add(issues, "duplicate_id", f"evaluation_id 重复：{evaluation_id}", field=field)
        else:
            ids.add(evaluation_id)
        policy = item.get("action_policy")
        if policy not in SCHEDULE_ACTION_POLICIES:
            _add(issues, "invalid_schedule", f"{field}.action_policy 必须是 eager/patient", field=field)
        else:
            policies.append(str(policy))
        primary += 1 if item.get("primary") else 0
        if not isinstance(item.get("scenario_path"), str) or not item.get("scenario_path"):
            _add(issues, "missing_field", f"{field}.scenario_path 缺失", field=field)
        _require_sha(issues, item.get("scenario_sha256"), f"{field}.scenario_sha256")
    if primary != 1:
        _add(issues, "invalid_evaluations", "plan-v3 evaluations 必须恰好一个 primary", field="evaluations")
    if sorted(policies) != list(SCHEDULE_ACTION_POLICIES):
        _add(issues, "invalid_evaluations", "plan-v3 evaluations 必须为 eager/patient 各一个", field="evaluations")


def _validate_evaluations_v2(issues: list[dict[str, Any]], plan: dict[str, Any]) -> None:
    """plan-v2 具名评估：每个评估必须绑定独立 scenario sha 与计划输入引用。"""
    evaluations = plan.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        _add(issues, "invalid_type", "evaluations 必须是非空数组", field="evaluations")
        return
    refs = {
        (str(ref.get("role")), str(ref.get("path")), str(ref.get("sha256")))
        for ref in plan.get("input_refs") or []
        if isinstance(ref, dict)
    }
    seen: set[str] = set()
    primaries = 0
    for index, evaluation in enumerate(evaluations):
        field = f"evaluations[{index}]"
        if not isinstance(evaluation, dict):
            _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
            continue
        evaluation_id = evaluation.get("evaluation_id")
        if not isinstance(evaluation_id, str) or not evaluation_id:
            _add(issues, "missing_field", f"{field}.evaluation_id 缺失", field=field)
        elif evaluation_id in seen:
            _add(issues, "duplicate_id", f"evaluation_id 重复：{evaluation_id}", field=field)
        else:
            seen.add(evaluation_id)
        metrics = evaluation.get("metrics")
        if not isinstance(metrics, list) or not metrics or any(
            not isinstance(item, str) or not item for item in metrics
        ):
            _add(issues, "invalid_type", f"{field}.metrics 必须是非空字符串数组", field=field)
        if bool(evaluation.get("primary")):
            primaries += 1
        _require_sha(issues, evaluation.get("scenario_sha256"), f"{field}.scenario_sha256")
        path = evaluation.get("scenario_path")
        if not isinstance(path, str) or not path:
            _add(issues, "missing_field", f"{field}.scenario_path 缺失", field=field)
        elif not any(
            str(ref.get("role")) == "scenario" and str(ref.get("path")) == path
            for ref in plan.get("input_refs") or []
            if isinstance(ref, dict)
        ):
            _add(
                issues,
                "unknown_reference",
                f"{field}.scenario_path 未出现在 input_refs（role=scenario，文件哈希另行绑定）",
                field=field,
            )
    if primaries != 1:
        _add(issues, "invalid_evaluations", "evaluations 必须恰好一个 primary=true", field="evaluations")
    else:
        primary_entries = [
            item for item in evaluations if isinstance(item, dict) and item.get("primary")
        ]
        primary_metrics = primary_entries[0].get("metrics")
        if not isinstance(primary_metrics, list) or "windows" not in primary_metrics:
            _add(
                issues,
                "unsupported_plan_v2_feature",
                "primary 评估必须声明 windows（持续输出/窗口）指标；仅 single-hit 指标作为 primary "
                "会使顶层 DPS 无定义，必须在入口拒绝",
                field="evaluations",
            )


def _validate_authority_and_semantics(
    issues: list[dict[str, Any]], plan: dict[str, Any]
) -> None:
    """校验执行器实际读取的权威字段、来源引用、所有权和固定阶段/依赖。"""
    from . import plan_handlers  # 延迟导入：契约常量集中在 handler 注册表

    # 1) 单一权威：拒绝计划内的镜像字段
    parameters = plan.get("parameters") or {}
    skill = parameters.get("skill") or {}
    if "components" in skill:
        _add(
            issues,
            "authority_conflict",
            "parameters.skill.components 是已废弃的镜像字段；组件标签/暴击能力只允许在顶层 components",
            field="parameters.skill.components",
        )
    if "every_nth_hit" in parameters:
        _add(
            issues,
            "authority_conflict",
            "parameters.every_nth_hit 是已废弃的镜像字段；命中周期只允许在 schedule.every_nth_hit",
            field="parameters.every_nth_hit",
        )
    for key in PARAMETER_SKILL_KEYS:
        if key not in skill:
            _add(issues, "missing_field", f"parameters.skill.{key} 缺失", field=f"parameters.skill.{key}")
    if plan.get("schema") == SCHEMA_V2:
        _require_number(issues, skill.get("base_attack_rate"), "parameters.skill.base_attack_rate")
        if (
            isinstance(skill.get("base_attack_rate"), (int, float))
            and not isinstance(skill.get("base_attack_rate"), bool)
            and float(skill.get("base_attack_rate")) <= 0
        ):
            _add(
                issues,
                "invalid_parameter",
                "parameters.skill.base_attack_rate 必须 > 0（plan-v2 攻击频率基数）",
                field="parameters.skill.base_attack_rate",
            )
    base_damage = skill.get("base_damage")
    if not isinstance(base_damage, dict) or not base_damage:
        _add(
            issues,
            "missing_parameter",
            "parameters.skill.base_damage 必须是非空对象（主组件基础参数缺失时不得返回 DPS）",
            field="parameters.skill.base_damage",
        )
    else:
        for damage_type, value in base_damage.items():
            if not isinstance(damage_type, str) or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)) or float(value) < 0:
                _add(
                    issues,
                    "invalid_parameter",
                    f"parameters.skill.base_damage[{damage_type!r}] 必须是非负有限数值",
                    field="parameters.skill.base_damage",
                )
        if not any(float(value) > 0 for value in base_damage.values() if isinstance(value, (int, float)) and not isinstance(value, bool)):
            _add(issues, "invalid_parameter", "parameters.skill.base_damage 至少一项必须 > 0", field="parameters.skill.base_damage")
    for key in ("added_damage_scaling", "crit_multiplier", "cooldown_s", "action_s"):
        _require_number(issues, skill.get(key), f"parameters.skill.{key}")
        if isinstance(skill.get(key), (int, float)) and not isinstance(skill.get(key), bool) and float(skill.get(key)) < 0:
            _add(issues, "invalid_parameter", f"parameters.skill.{key} 不能为负", field=f"parameters.skill.{key}")
    chance = skill.get("crit_chance")
    _require_number(issues, chance, "parameters.skill.crit_chance")
    if isinstance(chance, (int, float)) and not isinstance(chance, bool) and not 0 <= float(chance) <= 1:
        _add(issues, "invalid_parameter", "parameters.skill.crit_chance 必须在 [0,1]", field="parameters.skill.crit_chance")
    if not isinstance(skill.get("skill_name"), str) or not skill.get("skill_name"):
        _add(issues, "missing_field", "parameters.skill.skill_name 缺失", field="parameters.skill.skill_name")
    if not isinstance(parameters.get("crit"), dict):
        _add(issues, "invalid_type", "parameters.crit 必须是对象", field="parameters.crit")
    ailments = parameters.get("ailments")
    if not isinstance(ailments, list):
        _add(issues, "invalid_type", "parameters.ailments 必须是数组", field="parameters.ailments")
    else:
        for index, ailment in enumerate(ailments):
            field = f"parameters.ailments[{index}]"
            if not isinstance(ailment, dict) or not ailment.get("name"):
                _add(issues, "invalid_type", f"{field}.name 缺失", field=field)
                continue
            if ailment.get("damage_type") not in ("Physical", "Fire", "Cold", "Lightning", "Void", "Poison", "Necrotic"):
                _add(issues, "invalid_parameter", f"{field}.damage_type 不受支持", field=field)
            _require_number(issues, ailment.get("duration_s"), f"{field}.duration_s")
            _require_number(issues, ailment.get("base_damage"), f"{field}.base_damage")
    if not isinstance(parameters.get("additional_chance"), dict):
        _add(issues, "invalid_type", "parameters.additional_chance 必须是对象", field="parameters.additional_chance")
    if parameters.get("damage_to_penetration") is not None and not isinstance(parameters.get("damage_to_penetration"), dict):
        _add(issues, "invalid_type", "parameters.damage_to_penetration 必须是对象或 null", field="parameters.damage_to_penetration")
    _require_number(issues, parameters.get("cooldown_reduction"), "parameters.cooldown_reduction")

    # 2) components：v1 唯一 hit/dot；v2 当前执行器只实现 hit 组件（dot 需另行实现/升级契约）
    components = plan.get("components") or []
    kinds = [component.get("kind") for component in components if isinstance(component, dict)]
    if plan.get("schema") == SCHEMA_V3:
        if not components:
            _add(issues, "invalid_components", "plan-v3 components 不能为空（至少一个 dot 组件）", field="components")
        if any(kind != "dot" for kind in kinds):
            _add(issues, "invalid_components", "plan-v3 components.kind 必须是 dot（纯 DoT，不伪造 Hit）", field="components")
        if ailments:
            _add(
                issues,
                "unsupported_plan_v3_feature",
                "plan-v3 的 Ignite 由 schedule.ignite 施加模型表达；parameters.ailments 必须为空",
                field="parameters.ailments",
            )
    elif plan.get("schema") == SCHEMA_V2:
        if not components:
            _add(issues, "invalid_components", "plan-v2 components 不能为空（至少一个组件）", field="components")
        if any(kind not in COMPONENT_KINDS for kind in kinds):
            _add(issues, "invalid_components", "components.kind 只支持 hit/dot", field="components")
        if any(kind == "dot" for kind in kinds):
            _add(
                issues,
                "unsupported_plan_v2_feature",
                "plan-v2 当前执行器未实现 DoT 组件装配/频率积分；dot 组件必须在结构校验入口拒绝，"
                "不得声明后被忽略或按零值计入（BH 切片实现后再放宽）",
                field="components",
            )
        if ailments:
            _add(
                issues,
                "unsupported_plan_v2_feature",
                "plan-v2 当前执行器未实现 ailments 施加/窗口积分；parameters.ailments 必须为空，"
                "不得声明后让总 DPS 只算命中",
                field="parameters.ailments",
            )
    elif plan.get("schema") != SCHEMA_V3 and (kinds.count("hit") != 1 or kinds.count("dot") != 1):
        _add(
            issues,
            "invalid_components",
            "components 必须恰好包含一个 hit 与一个 dot（执行器按该权威定义装配组件）",
            field="components",
        )
    component_ids: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, dict):
            continue
        field = f"components[{index}]"
        component_id = component.get("component_id")
        if not isinstance(component_id, str) or not component_id:
            _add(issues, "missing_field", f"{field}.component_id 缺失", field=field)
        elif component_id in component_ids:
            _add(issues, "duplicate_id", f"component_id 重复：{component_id}", field=field)
        else:
            component_ids.add(component_id)
        if not isinstance(component.get("tags"), list):
            _add(issues, "invalid_type", f"{field}.tags 必须是数组", field=field)
        if not isinstance(component.get("crit_capable"), bool):
            _add(issues, "invalid_type", f"{field}.crit_capable 必须是布尔值", field=field)
        if component.get("adaptive_damage_type") is not None and not isinstance(component.get("adaptive_damage_type"), str):
            _add(issues, "invalid_type", f"{field}.adaptive_damage_type 必须是字符串或 null", field=field)
        if plan.get("schema") == SCHEMA_V3:
            if component.get("kind") != "dot":
                continue
            if component.get("damage_type") is None or component.get("scope") != "body":
                _add(issues, "invalid_components", f"{field} 的 plan-v3 dot 组件必须声明 damage_type 与 scope=body", field=field)
            for key in ("base_damage", "added_damage_scaling", "tick_interval_s", "duration_s"):
                _require_number(issues, component.get(key), f"{field}.{key}")
            if component.get("remainder_policy") not in DOT_REMAINDER_POLICIES:
                _add(issues, "unsupported_plan_v3_feature", f"{field}.remainder_policy 只支持 {DOT_REMAINDER_POLICIES}", field=field)
            if component.get("damage_at_start") is not True:
                _add(
                    issues,
                    "unsupported_plan_v3_feature",
                    f"{field}.damage_at_start 只支持 true（当前执行器从 spawn 起每 tick_interval 结算一次；false 未实现）",
                    field=field,
                )
        elif plan.get("schema") == SCHEMA_V2:
            if component.get("damage_type") is not None and not isinstance(component.get("damage_type"), str):
                _add(issues, "invalid_type", f"{field}.damage_type 必须是字符串或 null", field=field)
            if component.get("kind") == "hit":
                hits = component.get("hits_per_cast")
                weights = component.get("damage_weights")
                if isinstance(hits, bool) or not isinstance(hits, int) or hits < 1:
                    _add(
                        issues,
                        "invalid_components",
                        f"{field}.hits_per_cast 必须是 >=1 的整数（plan-v2 多命中计数）",
                        field=field,
                    )
                if not isinstance(weights, list) or not weights:
                    _add(
                        issues,
                        "invalid_components",
                        f"{field}.damage_weights 必须是非空数组（每击伤害权重）",
                        field=field,
                    )
                else:
                    valid_numbers = all(
                        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value)) and float(value) >= 0
                        for value in weights
                    )
                    if not valid_numbers:
                        _add(issues, "invalid_components", f"{field}.damage_weights 必须是非负有限数值", field=field)
                    elif isinstance(hits, int) and not isinstance(hits, bool) and len(weights) != hits:
                        _add(
                            issues,
                            "invalid_components",
                            f"{field}.damage_weights 长度必须等于 hits_per_cast（{hits}）",
                            field=field,
                        )
                    elif sum(float(value) for value in weights) <= 0:
                        _add(issues, "invalid_components", f"{field}.damage_weights 之和必须 > 0", field=field)
            elif component.get("hits_per_cast") is not None or component.get("damage_weights") is not None:
                _add(
                    issues,
                    "invalid_components",
                    f"{field} 是 dot 组件，不得声明 hits_per_cast/damage_weights",
                    field=field,
                )

    # 3) effects：执行期字段、来源引用、所有权、阶段
    effects = [item for item in plan.get("effects") or [] if isinstance(item, dict)]
    source_ids = {
        str(source.get("source_id"))
        for source in plan.get("sources") or []
        if isinstance(source, dict) and source.get("source_id")
    }
    sources = {
        str(source.get("source_id")): source
        for source in plan.get("sources") or []
        if isinstance(source, dict) and source.get("source_id")
    }
    if effects and not sources:
        _add(issues, "missing_sources", "计划含 effects 但没有 sources；来源清单不能为空", field="sources")
    ownership_seen: dict[str, str] = {}
    for index, effect in enumerate(effects):
        field = f"effects[{index}]"
        if effect.get("unit") not in EFFECT_UNITS:
            _add(issues, "invalid_unit", f"{field}.unit 不受支持：{effect.get('unit')!r}", field=field)
        emits = bool(effect.get("emits_contribution"))
        if not isinstance(effect.get("required"), bool):
            _add(issues, "invalid_type", f"{field}.required 必须是布尔值", field=field)
        if not isinstance(effect.get("stage"), str):
            _add(issues, "missing_field", f"{field}.stage 缺失", field=field)
        category = effect.get("category")
        expected_stage = plan_handlers.CATEGORY_STAGE.get(str(category))
        if expected_stage is None:
            _add(issues, "unsupported_category", f"{field}.category 无法映射固定阶段：{category!r}", field=field)
        elif effect.get("stage") != expected_stage:
            _add(
                issues,
                "stage_violation",
                f"{field} 的阶段 {effect.get('stage')!r} 与 category {category!r} 要求的 {expected_stage!r} 不一致",
                field=field,
            )
        source_refs = effect.get("source_refs")
        if emits:
            if not isinstance(source_refs, list) or not source_refs:
                _add(issues, "missing_source_ref", f"{field}.source_refs 不得为空（可执行效果必须有来源）", field=field)
            else:
                for source_id in source_refs:
                    if str(source_id) not in source_ids:
                        _add(issues, "unknown_source_ref", f"{field}.source_refs 引用未知来源 {source_id!r}", field=field)
                    else:
                        source = sources[str(source_id)]
                        if source.get("binding_status") in plan_handlers.SOURCE_FAILURE_STATUSES and (
                            effect.get("included_static") or effect.get("coverage_status") not in plan_handlers.SOURCE_FAILURE_STATUSES
                        ):
                            _add(
                                issues,
                                "source_status_conflict",
                                f"{field} 引用失败来源 {source_id!r} 却声明生效/成功状态",
                                field=field,
                            )
            ownership = effect.get("ownership_key")
            if not isinstance(ownership, dict) or not isinstance(ownership.get("source"), dict) or not ownership.get("stat_key"):
                _add(issues, "missing_ownership", f"{field}.ownership_key 不完整（来源+输出通道）", field=field)
            else:
                digest = canonical_json(ownership)
                if digest in ownership_seen:
                    _add(
                        issues,
                        "duplicate_ownership",
                        f"{field} 与 {ownership_seen[digest]} 所有权键重复（同一来源/机制/输出被重复注入）",
                        field=field,
                    )
                else:
                    ownership_seen[digest] = str(effect.get("effect_id"))
        elif source_refs not in (None, []):
            if not isinstance(source_refs, list):
                _add(issues, "invalid_type", f"{field}.source_refs 必须是数组", field=field)

    # 4) sources：kind/identity/evidence/binding_status
    for index, source in enumerate(plan.get("sources") or []):
        if not isinstance(source, dict):
            continue
        field = f"sources[{index}]"
        if source.get("kind") not in plan_handlers.SOURCE_KINDS:
            _add(issues, "unsupported_source_kind", f"{field}.kind 未在契约白名单：{source.get('kind')!r}", field=field)
        if not isinstance(source.get("identity"), dict) or not source.get("identity"):
            _add(issues, "missing_source_identity", f"{field}.identity 不得为空（来源必须有身份）", field=field)
        if not isinstance(source.get("evidence"), list):
            _add(issues, "invalid_type", f"{field}.evidence 必须是数组", field=field)
        if not isinstance(source.get("binding_status"), str):
            _add(issues, "invalid_type", f"{field}.binding_status 必须是字符串", field=field)

    # 4b) facts.coverage_decisions：已应用来源裁决必须有身份/证据/前提
    facts = plan.get("facts") or {}
    decisions = facts.get("coverage_decisions")
    if decisions is not None and not isinstance(decisions, list):
        _add(issues, "invalid_type", "facts.coverage_decisions 必须是数组", field="facts.coverage_decisions")
    elif isinstance(decisions, list):
        decision_ids: set[str] = set()
        for index, decision in enumerate(decisions):
            field = f"facts.coverage_decisions[{index}]"
            if not isinstance(decision, dict):
                _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
                continue
            decision_id = decision.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id:
                _add(issues, "missing_field", f"{field}.decision_id 缺失", field=field)
            elif decision_id in decision_ids:
                _add(issues, "duplicate_id", f"决策 ID 重复：{decision_id}", field=field)
            else:
                decision_ids.add(decision_id)
            if decision.get("action") not in ("confirmed_inactive", "not_applicable", "assumed_active"):
                _add(issues, "unsupported_decision", f"{field}.action 不受支持", field=field)
            if not decision.get("applied"):
                continue
            identity = decision.get("identity") or {}
            if not isinstance(identity, dict) or not (
                identity.get("definition_entity_id") or identity.get("base_entity_id")
            ):
                _add(
                    issues,
                    "missing_decision_identity",
                    f"{field} 已应用裁决缺少可验证定义身份",
                    field=field,
                )
            evidence = decision.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                _add(issues, "missing_decision_evidence", f"{field} 已应用裁决缺少证据", field=field)
            premise = decision.get("premise") or {}
            if not isinstance(premise, dict) or premise.get("satisfied") is not True:
                _add(issues, "invalid_decision_premise", f"{field} 已应用裁决的前提未满足/缺失", field=field)
            if decision.get("status") not in ("verified", "assumed"):
                _add(issues, "invalid_decision_status", f"{field} 已应用裁决状态必须是 verified/assumed", field=field)

    # 5) evaluation：固定阶段顺序 + 完整依赖链 + 阶段引用
    evaluation = plan.get("evaluation") or {}
    stage_order = evaluation.get("stage_order")
    if list(stage_order or []) != list(plan_handlers.STAGE_ORDER):
        _add(
            issues,
            "invalid_stage_order",
            "evaluation.stage_order 必须等于契约固定阶段顺序（不支持任意阶段/任意 DAG 调度）",
            field="evaluation.stage_order",
        )
    dependencies = evaluation.get("dependencies")
    canonical = plan_handlers.canonical_dependencies()
    if dependencies != canonical:
        _add(
            issues,
            "invalid_dependencies",
            "evaluation.dependencies 必须是契约固定依赖链（不允许任意/反向/循环依赖声明）",
            field="evaluation.dependencies",
        )
    effect_stage = evaluation.get("effect_stage")
    if not isinstance(effect_stage, dict):
        _add(issues, "invalid_type", "evaluation.effect_stage 必须是对象", field="evaluation.effect_stage")
    else:
        expected_effect_ids = {str(effect.get("effect_id")) for effect in effects}
        if set(effect_stage) != expected_effect_ids:
            _add(
                issues,
                "invalid_effect_stage",
                "evaluation.effect_stage 必须覆盖且只覆盖计划全部 effects",
                field="evaluation.effect_stage",
            )
        for effect in effects:
            effect_id = str(effect.get("effect_id"))
            declared_stage = effect_stage.get(effect_id)
            if declared_stage is not None and declared_stage != effect.get("stage"):
                _add(
                    issues,
                    "invalid_effect_stage",
                    f"{effect_id} 在 evaluation.effect_stage 中阶段与 effect.stage 不一致",
                    field="evaluation.effect_stage",
                )
            if declared_stage is not None and declared_stage not in plan_handlers.STAGE_ORDER:
                _add(issues, "invalid_effect_stage", f"{effect_id} 引用未知阶段 {declared_stage!r}", field="evaluation.effect_stage")
        # 属性转换必须先于属性缩放；任何跨阶段倒置都拒绝
        order_index = {stage: index for index, stage in enumerate(plan_handlers.STAGE_ORDER)}
        if order_index.get("attribute_conversion", 0) >= order_index.get("attribute_scaling", 0):
            _add(issues, "invalid_stage_order", "属性转换阶段必须早于属性缩放阶段", field="evaluation.stage_order")
    if plan.get("schema") == SCHEMA_V2:
        _validate_evaluations_v2(issues, plan)


def validate_plan_structure(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """结构/引用/取值校验；返回 issues（空数组表示通过）。不检查 handler 实现。"""
    issues: list[dict[str, Any]] = []
    if not isinstance(plan, dict):
        _add(issues, "invalid_type", "计划必须是对象")
        return issues
    schema = plan.get("schema")
    if schema in (SCHEMA_V2, SCHEMA_V3):
        top_level_fields = TOP_LEVEL_FIELDS_V2
        required_top_level = REQUIRED_TOP_LEVEL_V2
    else:
        top_level_fields = TOP_LEVEL_FIELDS
        required_top_level = REQUIRED_TOP_LEVEL
    unknown = set(plan) - set(top_level_fields)
    if unknown:
        _add(issues, "unknown_field", f"计划含未支持顶层字段：{sorted(unknown)}")
    missing = [field for field in required_top_level if field not in plan]
    if missing:
        _add(issues, "missing_field", f"计划缺少必需字段：{missing}")
        return issues
    if schema not in SUPPORTED_SCHEMAS:
        _add(issues, "invalid_schema", f"schema 必须为 {SUPPORTED_SCHEMAS} 之一")
    _require_sha(issues, plan.get("plan_sha256"), "plan_sha256")
    expected_plan_id = PLAN_ID_PREFIX + str(plan.get("plan_sha256"))
    if plan.get("plan_id") != expected_plan_id:
        _add(issues, "invalid_plan_id", "plan_id 必须为 cp- + plan_sha256")
    subject = plan.get("subject")
    if not isinstance(subject, dict):
        _add(issues, "invalid_type", "subject 必须是对象", field="subject")
    else:
        for field in ("case_id", "build_id", "revision_id", "main_ability_id"):
            if not isinstance(subject.get(field), str) or not subject.get(field):
                _add(issues, "missing_field", f"subject.{field} 缺失", field=f"subject.{field}")
    compiler = plan.get("compiler")
    if not isinstance(compiler, dict) or not compiler.get("version") or not isinstance(compiler.get("implementation_fingerprint"), dict):
        _add(issues, "invalid_type", "compiler 必须含 version 与 implementation_fingerprint", field="compiler")
    requirements = plan.get("executor_requirements")
    if not isinstance(requirements, dict) or not isinstance(requirements.get("handlers"), list) or not requirements.get("handlers"):
        _add(issues, "invalid_type", "executor_requirements.handlers 必须是非空数组", field="executor_requirements")
    else:
        for index, handler in enumerate(requirements["handlers"]):
            if not isinstance(handler, dict) or not handler.get("handler_id") or not handler.get("version"):
                _add(issues, "invalid_type", f"executor_requirements.handlers[{index}] 缺少 handler_id/version", field="executor_requirements")
    input_refs = plan.get("input_refs")
    if not isinstance(input_refs, list) or not input_refs:
        _add(issues, "invalid_type", "input_refs 必须是非空数组", field="input_refs")
    else:
        for index, ref in enumerate(input_refs):
            field = f"input_refs[{index}]"
            if not isinstance(ref, dict):
                _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
                continue
            if ref.get("role") not in INPUT_REF_ROLES:
                _add(issues, "unknown_input_role", f"{field}.role 未声明：{ref.get('role')!r}", field=field)
            if not isinstance(ref.get("path"), (str, type(None))):
                _add(issues, "invalid_type", f"{field}.path 必须是字符串或 null", field=field)
            _require_sha(issues, ref.get("sha256"), f"{field}.sha256")
    sources = plan.get("sources")
    if not isinstance(sources, list):
        _add(issues, "invalid_type", "sources 必须是数组", field="sources")
    else:
        source_ids: set[str] = set()
        for index, source in enumerate(sources):
            field = f"sources[{index}]"
            if not isinstance(source, dict) or not isinstance(source.get("source_id"), str):
                _add(issues, "invalid_type", f"{field}.source_id 缺失", field=field)
                continue
            if source["source_id"] in source_ids:
                _add(issues, "duplicate_id", f"source_id 重复：{source['source_id']}", field=field)
            source_ids.add(source["source_id"])
            if not isinstance(source.get("kind"), str) or not source.get("kind"):
                _add(issues, "missing_field", f"{field}.kind 缺失", field=field)
    if not isinstance(plan.get("facts"), dict):
        _add(issues, "invalid_type", "facts 必须是对象", field="facts")
    if not isinstance(plan.get("parameters"), dict):
        _add(issues, "invalid_type", "parameters 必须是对象", field="parameters")
    controls = plan.get("controls")
    control_ids: set[str] = set()
    if not isinstance(controls, list):
        _add(issues, "invalid_type", "controls 必须是数组", field="controls")
    else:
        for index, control in enumerate(controls):
            field = f"controls[{index}]"
            if not isinstance(control, dict) or not isinstance(control.get("control_id"), str):
                _add(issues, "invalid_type", f"{field}.control_id 缺失", field=field)
                continue
            if control["control_id"] in control_ids:
                _add(issues, "duplicate_id", f"control_id 重复：{control['control_id']}", field=field)
            control_ids.add(control["control_id"])
            if control.get("namespace") not in ("state", "target"):
                _add(issues, "invalid_type", f"{field}.namespace 必须是 state/target", field=field)
            if control.get("type") not in ("boolean", "integer", "number"):
                _add(issues, "invalid_type", f"{field}.type 不受支持：{control.get('type')!r}", field=field)
    condition_ids = _check_conditions(issues, plan.get("conditions"), control_ids)
    components = plan.get("components")
    component_ids: set[str] = set()
    if not isinstance(components, list) or not components:
        _add(issues, "invalid_type", "components 必须是非空数组", field="components")
    else:
        for index, component in enumerate(components):
            field = f"components[{index}]"
            if not isinstance(component, dict) or not isinstance(component.get("component_id"), str):
                _add(issues, "invalid_type", f"{field}.component_id 缺失", field=field)
                continue
            if component["component_id"] in component_ids:
                _add(issues, "duplicate_id", f"component_id 重复：{component['component_id']}", field=field)
            component_ids.add(component["component_id"])
            if component.get("kind") not in COMPONENT_KINDS:
                _add(issues, "unsupported_component", f"{field}.kind 不受支持：{component.get('kind')!r}", field=field)
            if not isinstance(component.get("tags"), list):
                _add(issues, "invalid_type", f"{field}.tags 必须是数组", field=field)
    effects = plan.get("effects")
    effect_ids: set[str] = set()
    if not isinstance(effects, list):
        _add(issues, "invalid_type", "effects 必须是数组", field="effects")
    else:
        for index, effect in enumerate(effects):
            field = f"effects[{index}]"
            if not isinstance(effect, dict):
                _add(issues, "invalid_type", f"{field} 必须是对象", field=field)
                continue
            effect_id = effect.get("effect_id")
            if not isinstance(effect_id, str) or not effect_id:
                _add(issues, "missing_field", f"{field}.effect_id 缺失", field=field)
            elif effect_id in effect_ids:
                _add(issues, "duplicate_id", f"effect_id 重复：{effect_id}", field=field)
            else:
                effect_ids.add(effect_id)
            if not effect.get("handler_id") or not effect.get("handler_version"):
                _add(issues, "missing_field", f"{field} 缺少 handler_id/handler_version", field=field)
            if effect.get("operation") not in ("added", "increased", "more", "base", "flag"):
                _add(issues, "unsupported_operation", f"{field}.operation 不受支持：{effect.get('operation')!r}", field=field)
            if effect.get("category") not in PLAN_EFFECT_CATEGORIES:
                _add(issues, "unsupported_category", f"{field}.category 不受支持：{effect.get('category')!r}", field=field)
            if effect.get("value_mode") not in EFFECT_VALUE_MODES:
                _add(issues, "unsupported_value_mode", f"{field}.value_mode 不受支持：{effect.get('value_mode')!r}", field=field)
            if effect.get("value_mode") == "static":
                _require_number(issues, effect.get("value"), f"{field}.value")
            if effect.get("condition_ref") is not None and effect.get("condition_ref") not in condition_ids:
                _add(issues, "unknown_reference", f"{field}.condition_ref 引用未知条件", field=field)
            if effect.get("condition_code") is not None and not isinstance(effect.get("condition_code"), str):
                _add(issues, "invalid_type", f"{field}.condition_code 必须是字符串或 null", field=field)
            if not isinstance(effect.get("included_static"), bool):
                _add(issues, "invalid_type", f"{field}.included_static 必须是布尔值", field=field)
            params = effect.get("value_params") or {}
            if not isinstance(params, dict):
                _add(issues, "invalid_type", f"{field}.value_params 必须是对象", field=field)
            elif effect.get("value_mode") == "state_count":
                _require_number(issues, params.get("per_state"), f"{field}.value_params.per_state")
                if not isinstance(params.get("states"), list):
                    _add(issues, "invalid_type", f"{field}.value_params.states 必须是数组", field=field)
            elif effect.get("value_mode") == "stack_scale":
                _require_number(issues, params.get("base_value"), f"{field}.value_params.base_value")
                if not isinstance(params.get("control"), str):
                    _add(issues, "missing_field", f"{field}.value_params.control 缺失", field=field)
            elif effect.get("value_mode") == "movement_conversion":
                _require_number(issues, params.get("ratio"), f"{field}.value_params.ratio")
    schedule = plan.get("schedule")
    if plan.get("schema") == SCHEMA_V3:
        _validate_schedule_v3(issues, schedule)
        _validate_evaluations_v3(issues, plan)
    elif plan.get("schema") == SCHEMA_V2:
        _validate_schedule_v2(issues, schedule)
    elif not isinstance(schedule, dict) or schedule.get("driver") not in SCHEDULE_DRIVERS:
        _add(issues, "unsupported_schedule", "schedule.driver 必须是 fixed_repeat", field="schedule")
    else:
        for field in ("interval_policy", "first_cast_s", "windows_s"):
            if field not in schedule:
                _add(issues, "missing_field", f"schedule.{field} 缺失", field="schedule")
        if schedule.get("interval_policy") != "max(action_time, cooldown)":
            _add(
                issues,
                "unsupported_schedule",
                f"schedule.interval_policy 不受支持：{schedule.get('interval_policy')!r}",
                field="schedule",
            )
        _require_number(issues, schedule.get("first_cast_s"), "schedule.first_cast_s")
        windows = schedule.get("windows_s")
        if not isinstance(windows, list) or not windows or any(
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 for value in windows
        ):
            _add(issues, "invalid_schedule", "schedule.windows_s 必须是正数数组", field="schedule")
        every_nth = schedule.get("every_nth_hit")
        if every_nth is not None:
            if not isinstance(every_nth, dict):
                _add(issues, "invalid_type", "schedule.every_nth_hit 必须是对象或 null", field="schedule")
            else:
                n = every_nth.get("n")
                if isinstance(n, bool) or not isinstance(n, int) or n < 1:
                    _add(issues, "invalid_schedule", "schedule.every_nth_hit.n 必须是 >=1 的整数", field="schedule")
                if every_nth.get("counting") != "hit":
                    _add(issues, "unsupported_schedule", "schedule.every_nth_hit.counting 只支持 hit", field="schedule")
                if every_nth.get("phase") != "from_first_hit":
                    _add(issues, "unsupported_schedule", "schedule.every_nth_hit.phase 只支持 from_first_hit", field="schedule")
                _require_number(issues, every_nth.get("extra_more", 0.0), "schedule.every_nth_hit.extra_more")
    for field in ("diagnostics", "assumptions"):
        if not isinstance(plan.get(field), list):
            _add(issues, "invalid_type", f"{field} 必须是数组", field=field)
    if not isinstance(plan.get("scope"), dict):
        _add(issues, "invalid_type", "scope 必须是对象", field="scope")
    if not isinstance(plan.get("presentation"), dict):
        _add(issues, "invalid_type", "presentation 必须是对象", field="presentation")
    if not isinstance(plan.get("evaluation"), dict):
        _add(issues, "invalid_type", "evaluation 必须是对象", field="evaluation")
    _validate_authority_and_semantics(issues, plan)
    return issues


__all__ = [
    "COMPONENT_KINDS",
    "CONDITION_TERM_KINDS",
    "EFFECT_VALUE_MODES",
    "INPUT_REF_ROLES",
    "PLAN_ID_PREFIX",
    "PLAN_SCHEMA_PATH",
    "PLAN_V2_SCHEMA_PATH",
    "PLAN_V3_SCHEMA_PATH",
    "PLAN_VERSION",
    "PLAN_VERSION_V2",
    "PLAN_VERSION_V3",
    "REQUIRED_TOP_LEVEL",
    "REQUIRED_TOP_LEVEL_V2",
    "REQUIRED_TOP_LEVEL_V3",
    "ROTATION_APPLIES_TO",
    "ROTATION_KINDS",
    "SCHEMA",
    "SCHEMA_V2",
    "SCHEMA_V3",
    "SCHEDULE_ACTION_POLICIES",
    "SCHEDULE_POLICIES",
    "SCHEDULE_WINDOW_SEMANTICS",
    "SUPPORTED_SCHEMAS",
    "SCHEDULE_DRIVERS",
    "TOP_LEVEL_FIELDS",
    "TOP_LEVEL_FIELDS_V2",
    "aggregate_scenario_sha256",
    "canonical_json",
    "compute_plan_sha256",
    "execution_fingerprint",
    "finalize_plan",
    "is_sha256",
    "load_plan",
    "plan_ref",
    "scenario_sha256",
    "sha256_file",
    "sha256_text",
    "validate_plan_structure",
    "verify_plan_integrity",
    "write_plan",
]
