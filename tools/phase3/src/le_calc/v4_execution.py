"""v4 native execution primitives: plan-driven FR/BH/VC computation.

Only public plan_handlers/mechanics primitives are used; no legacy v2/v3 complete
executor dispatch is imported. FR/BH orchestration is v4-owned and consumes the
plan's `execution` object.
"""
from __future__ import annotations
import copy, math
from typing import Any, Mapping, Optional

from . import calculation_plan as planmod
from . import dps_components, dps_result, model, plan_handlers
from .mechanics.dot import (
    EPS, SAMPLE_STEP_S, _active_rows, _attribute_increase, _matching, _more_product,
    _resistance_totals, _scope_allows, _sum, _target_multiplier, cohort_between as _ignite_between,
    dot_component_result as _dot_component_result, make_cast as _make_cast,
    max_concurrent as _max_concurrent, window_metrics as _window_metrics,
)
from .mechanics.applications import (
    application_cohort as _ignite_application, application_core as _ignite_core,
    single_cast as _single_cast,
)
from .mechanics.charge import (
    blocked_attempts as _blocked_attempts, charge_loop as _charge_loop,
    charge_parameters as _charge_parameters,
)
from .dps_components import (
    DPS_TAG_POLICY, attack_speed_increased, build_hit_components, component_tags,
    cooldown_recovery_increased, more_product,
)
from . import plan_handlers as _ph
REMAINDER_POLICIES = ("ceil_full_ticks", "floor_full_ticks", "exact_partial")
ACTION_POLICIES = ("eager", "patient")
ELEMENTAL_TYPES = frozenset({"Fire", "Cold", "Lightning"})
_BODY_DAMAGE_STATS = frozenset({"damage", "added_damage", "penetration"})
_BODY_DURATION_STATS = frozenset({"skill_duration"})
_IGNITE_DAMAGE_STATS = frozenset({"damage", "penetration"})
_COVERAGE_RELEVANT_STATS = frozenset(
    {"damage", "added_damage", "penetration", "penetration_with_ignite", "ailment_duration", "ailment_chance"}
)
_FREQUENCY_STATS = frozenset(
    {"cooldown_recovery_speed", "cooldown_reduction", "cooldown_flat", "charges", "cast_speed", "attack_speed"}
)

def project_coverage(
    *,
    plan: dict[str, Any],
    dot_applications: list[dict[str, Any]],
) -> dict[str, Any]:
    base_gaps = copy.deepcopy((plan.get("facts") or {}).get("coverage_base_gaps") or [])
    return dps_result.merge_runtime_gaps(base_gaps, unmodeled_chance_records=dot_applications)


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



class PlanValidationError(ValueError):
    """v4 native 执行校验错误（本地异常，不依赖旧执行模块）。"""


SCHEMA_RESULT_V4 = "le-dps-result-v4"
SCHEMA_RESULT_V5 = "le-dps-result-v5"


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
        # effect 的声明 scope（owner/actor/all）必须进入运行时行，供组件级 scope 过滤使用；
        # 未声明 scope 的旧 effects 不写该键（raw 形状保持原样），行为不变。
        if effect.get("scope") is not None:
            if not isinstance(raw_payload, dict):
                raw_payload = {}
            raw_payload.setdefault("scope", str(effect.get("scope")))
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


def resolve_evaluation_scenarios(
    plan: Mapping[str, Any], scenario: Any
) -> list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]]:
    """返回 ``[(evaluation_meta, states, target, scenario_sha256), ...]``。

    接受两种输入形态：

    * ``{"states": ..., "target": ...}``：单场景，应用于全部具名评估（重算通道）；
    * ``{"evaluations": [{"evaluation_id", "scenario": {"states", "target"}}, ...]}``：
      逐评估场景（正式 run 从独立 scenario 文件加载，每个评估独立哈希）。
    """
    evaluations = [item for item in plan.get("evaluations") or [] if isinstance(item, dict)]
    if not evaluations:
        raise ValueError("plan-v2 缺少 evaluations")
    ids = {str(item.get("evaluation_id")) for item in evaluations}
    if isinstance(scenario, Mapping) and "evaluations" in scenario:
        supplied = scenario.get("evaluations")
        if not isinstance(supplied, list) or not supplied:
            raise ValueError("evaluations 场景必须是数组")
        by_id: dict[str, Any] = {}
        for item in supplied:
            if not isinstance(item, Mapping):
                raise ValueError("evaluations 场景条目必须是对象")
            evaluation_id = str(item.get("evaluation_id") or "")
            if not evaluation_id:
                raise ValueError("evaluations 场景条目缺少 evaluation_id")
            if evaluation_id in by_id:
                raise ValueError(f"evaluation_id 重复：{evaluation_id}")
            by_id[evaluation_id] = item.get("scenario")
        if set(by_id) != ids:
            raise ValueError(
                f"场景评估与计划 evaluations 不一致：missing={sorted(ids - set(by_id))}, "
                f"extra={sorted(set(by_id) - ids)}"
            )
        resolved: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]] = []
        for meta in evaluations:
            states, target = validate_scenario(
                dict(plan), by_id[str(meta.get("evaluation_id"))]
            )
            digest = planmod.scenario_sha256({"states": states, "target": target})
            resolved.append((meta, states, target, digest))
        return resolved
    states, target = validate_scenario(dict(plan), scenario)
    digest = planmod.scenario_sha256({"states": states, "target": target})
    return [(meta, copy.deepcopy(states), copy.deepcopy(target), digest) for meta in evaluations]


def build_hit_components_v2(
    *,
    plan: Mapping[str, Any],
    skill_parameters: Mapping[str, Any],
    contributions: list[model.Contribution],
    target: Mapping[str, Any],
    states: Mapping[str, Any],
    target_info: Mapping[str, Any],
    crit_rules: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """装配 v2 命中组件：逐组件执行数值，再附加每击次数/权重元数据。"""
    skill = {
        "base_damage": dict(skill_parameters.get("base_damage") or {}),
        "added_damage_scaling": float(skill_parameters.get("added_damage_scaling") or 0.0),
        "crit_chance": float(skill_parameters.get("crit_chance") or 0.0),
        "crit_multiplier": float(skill_parameters.get("crit_multiplier") or 0.0),
    }
    components: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    entries = _hit_component_entries(plan)
    if not entries:
        return components, applications
    for entry in entries:
        template = {
            "tags": list(entry.get("tags") or []),
            "crit_capable": bool(entry.get("crit_capable")),
            "adaptive_damage_type": entry.get("adaptive_damage_type"),
        }
        built, built_apps = build_hit_components(
            contributions=contributions,
            skill=skill,
            component=template,
            crit_rules=dict(crit_rules),
            target=dict(target),
            states=dict(states),
            target_info=dict(target_info),
            damage_types=(
                [str(entry.get("damage_type"))] if entry.get("damage_type") else None
            ),
            typed_added_policy="by_type",
        )
        hits = int(entry.get("hits_per_cast") or 1)
        weights = [float(value) for value in entry.get("damage_weights") or [1.0] * hits]
        if len(weights) != hits:
            raise ValueError(f"组件 {entry.get('component_id')!r} 的 damage_weights 长度与 hits_per_cast 不一致")
        effective = sum(weights)
        declared_id = str(entry.get("component_id") or "")
        component_id_map: dict[str, str] = {}
        for component in built:
            old_id = str(component["component_id"])
            if declared_id:
                new_id = (
                    declared_id
                    if len(built) == 1
                    else f"{declared_id}:{component['damage_type']}"
                )
            else:
                new_id = old_id
            component_id_map[old_id] = new_id
            item = dict(component)
            item["component_id"] = new_id
            item["id"] = new_id
            item["hits_per_cast"] = hits
            item["damage_weights"] = list(weights)
            item["effective_hits_per_cast"] = effective
            item["declared_component_id"] = declared_id or None
            components.append(item)
        # 应用记录必须引用最终组件 ID（声明 ID 替换后同步映射，而不是保留装配内部 ID）
        for application in built_apps:
            application["component_id"] = component_id_map.get(
                str(application.get("component_id")),
                str(application.get("component_id")),
            )
        applications.extend(built_apps)
    return components, applications


def component_hit_totals(components: list[Mapping[str, Any]]) -> dict[str, Any]:
    """由组件与逐击权重计算首击/每次施放命中伤害与命中计数。"""
    first_hit = 0.0
    per_cast = 0.0
    hits_per_cast = 0
    effective = 0.0
    for component in components:
        damage = float(component.get("damage") or 0.0)
        weights = [float(value) for value in component.get("damage_weights") or [1.0]]
        hits = int(component.get("hits_per_cast") or 1)
        first_hit += damage * weights[0]
        per_cast += damage * sum(weights)
        hits_per_cast += hits
        effective += sum(weights)
    return {
        "single_hit": first_hit,
        "per_cast_hit": per_cast,
        "hits_per_cast": hits_per_cast,
        "effective_hits_per_cast": effective,
    }


def _policy_metrics(
    *,
    policy_id: str,
    interval_s: float,
    per_cast_hit: float,
    damage_per_cast: float,
    hits_per_cast: int,
    effective_hits_per_cast: float,
    rotation_more: float,
    windows_s: list[float],
    semantics: str,
) -> dict[str, Any]:
    casts_per_second = 1.0 / interval_s
    return {
        "id": policy_id,
        "binding_constraint": "cooldown" if policy_id == "cooldown_limited" else "attack_speed",
        "interval_s": interval_s,
        "casts_per_second": casts_per_second,
        "hits_per_cast": hits_per_cast,
        "effective_hits_per_cast": effective_hits_per_cast,
        "hits_per_second": hits_per_cast * casts_per_second,
        "per_cast_hit": per_cast_hit,
        "rotation_more_factor": rotation_more,
        "damage_per_cast": damage_per_cast,
        "damage_per_second": damage_per_cast * casts_per_second,
        "windows": _window_rows(
            windows_s=windows_s,
            interval_s=interval_s,
            semantics=semantics,
            per_cast_hit=per_cast_hit,
            damage_per_cast=damage_per_cast,
            hits_per_cast=hits_per_cast,
            effective_hits_per_cast=effective_hits_per_cast,
            rotation_more=rotation_more,
        ),
    }


def _window_rows(
    *,
    windows_s: list[float],
    interval_s: float,
    semantics: str,
    per_cast_hit: float,
    damage_per_cast: float,
    hits_per_cast: int,
    effective_hits_per_cast: float,
    rotation_more: float,
) -> list[dict[str, Any]]:
    if not math.isfinite(interval_s) or interval_s <= 0:
        raise ValueError("窗口聚合需要正的施放间隔")
    rows: list[dict[str, Any]] = []
    for seconds in windows_s:
        casts = float(seconds) / interval_s
        if semantics == "complete_casts":
            casts = float(math.floor(casts + 1e-12))
        complete = int(math.floor(casts + 1e-12))
        hits = casts * hits_per_cast
        total = casts * damage_per_cast
        rows.append(
            {
                "window_id": f"{float(seconds):g}s",
                "seconds": float(seconds),
                "window_s": float(seconds),
                "casts": casts,
                "casts_per_second": float(casts) / float(seconds),
                "complete_casts": complete,
                "hits": hits,
                "hits_per_second": hits / float(seconds),
                "hits_per_cast": hits_per_cast,
                "effective_hits_per_cast": effective_hits_per_cast,
                "single_hit_damage": per_cast_hit / effective_hits_per_cast if effective_hits_per_cast else 0.0,
                "damage_weights": None,
                "rotation_more_factor": rotation_more,
                "damage_per_cast": damage_per_cast,
                "hit_damage": total,
                "dot_damage": 0.0,
                "damage": total,
                "total_damage": total,
                "dps": total / float(seconds),
            }
        )
    return rows


def rotation_factor(plan: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    rotation = (plan.get("schedule") or {}).get("rotation") or {}
    factor = float(rotation.get("factor") or 1.0)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("schedule.rotation.factor 必须是正数")
    return factor, dict(rotation)


def steady_state_frequency(
    *,
    plan: Mapping[str, Any],
    contributions: list[model.Contribution],
    hit_tags: list[str],
) -> dict[str, Any]:
    """按具名策略计算施放间隔、每秒施放数、每窗口命中数；非法参数明确拒绝。"""
    parameters = plan.get("parameters") or {}
    skill = parameters.get("skill") or {}
    schedule = plan.get("schedule") or {}
    base_rate = float(skill.get("base_attack_rate") or 0.0)
    cooldown_s = float(skill.get("cooldown_s") or 0.0)
    action_s = float(skill.get("action_s") or 0.0)
    if not math.isfinite(base_rate) or base_rate <= 0:
        raise ValueError("plan-v2 需要正的 parameters.skill.base_attack_rate")
    if not math.isfinite(cooldown_s) or cooldown_s < 0:
        raise ValueError("parameters.skill.cooldown_s 必须是非负有限数值")
    increased = attack_speed_increased(contributions=contributions, component_tags=hit_tags)
    more = _more_attack_speed_multiplier(contributions, hit_tags)
    cooldown_recovery = cooldown_recovery_increased(contributions)
    cooldown_reduction = float(parameters.get("cooldown_reduction") or 0.0)
    rate = base_rate * (1.0 + increased) * more
    if not math.isfinite(rate) or rate <= 0:
        raise ValueError("攻击频率无效（base_attack_rate 与攻速修正必须产生正频率）")
    action_interval = 1.0 / rate
    effective_cooldown = cooldown_s * (1.0 - cooldown_reduction) / (1.0 + max(0.0, cooldown_recovery))
    by_id = {
        str(policy.get("policy_id")): policy
        for policy in schedule.get("policies") or []
        if isinstance(policy, dict) and policy.get("policy_id")
    }
    intervals: dict[str, float] = {}
    for policy_id, policy in by_id.items():
        interval_policy = str(policy.get("interval_policy") or "")
        if interval_policy == "action_time":
            intervals[policy_id] = action_interval
        elif interval_policy == "max(action_time, cooldown)":
            intervals[policy_id] = max(action_interval, effective_cooldown)
        else:
            raise ValueError(f"不支持的频率策略 interval_policy：{interval_policy!r}")
    primary = str(schedule.get("primary_policy") or "")
    if primary not in intervals:
        raise ValueError("schedule.primary_policy 未在 policies 中声明")
    return {
        "base_attack_rate": base_rate,
        "increased_attack_speed": increased,
        "more_attack_speed": more,
        "attack_rate": rate,
        "action_interval_s": action_interval,
        "action_s": action_s,
        "effective_cooldown_s": effective_cooldown,
        "cooldown_recovery": cooldown_recovery,
        "cooldown_reduction": cooldown_reduction,
        "intervals_s": intervals,
        "primary_policy": primary,
        "interval_s": intervals[primary],
    }


def _more_attack_speed_multiplier(
    contributions: list[model.Contribution], hit_tags: list[str]
) -> float:
    rows = [
        row
        for row in contributions
        if row.active
        and row.stat_key == model.STAT_ATTACK_SPEED
        and row.operation == "more"
        and tags_match(row.damage_tags, hit_tags, DPS_TAG_POLICY)
    ]
    return more_product(rows)[0]


def _hit_component_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in plan.get("components") or []
        if isinstance(item, dict) and item.get("kind") == "hit"
    ]


def _ACTIVE_TAG_POLICY() -> Any:
    from .mechanics import tag_match  # noqa: PLC0415

    return tag_match.bh_plan_policy()


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


def _application_reason(row: model.Contribution) -> str:
    if not row.included:
        if row.status == model.STATUS_UNRESOLVED and row.stat_key == model.STAT_UNSUPPORTED:
            return row.exclusion_reason or "来源未解析"
        return row.exclusion_reason or f"条件状态={row.condition_status}"
    if row.condition_status not in model.ACTIVE_CONDITIONS:
        return row.exclusion_reason or f"条件状态={row.condition_status}"
    return ""


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


def coverage_applications(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """覆盖投影只消费真正影响伤害/施加的未解析记录（与元素/杂项属性无关）。"""
    return [
        record
        for record in applications
        if str(record.get("status")) == dps_components.UNRESOLVED
        and str(record.get("stat_key")) in _COVERAGE_RELEVANT_STATS
    ]


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


def execute_hit_events(
    plan: dict[str, Any],
    scenario: Any,
    runtime: Optional[plan_handlers.PlanRuntime] = None,
) -> dict[str, Any]:
    """通用 hit-events execution primitive（数据驱动多命中/频率/评估）。"""
    runtime = runtime or plan_handlers.PlanRuntime()
    if str(plan.get("schema")) != "le-calculation-plan-v2":
        raise PlanValidationError(
            [{"code": "unsupported_execution_schema", "message": "FR execution 需要 v2 装配数据", "severity": "error", "related_ids": ["schema"], "affects_coverage": False}]
        )
    resolved = resolve_evaluation_scenarios(plan, scenario)
    schedule = plan.get("schedule") or {}
    windows_s = [float(value) for value in schedule.get("windows_s") or [5, 30, 120]]
    semantics = str(schedule.get("window_semantics") or "expected_casts")
    parameters = plan.get("parameters") or {}
    skill_parameters = parameters.get("skill") or {}
    rule_payload = {
        "crit": parameters.get("crit") or {},
        "every_nth_hit": schedule.get("every_nth_hit"),
        "ailments": parameters.get("ailments") or [],
        "damage_to_penetration": parameters.get("damage_to_penetration"),
    }
    hit_entries = _hit_component_entries(plan)
    hit_tags = (
        component_tags(
            {"tags": list(hit_entries[0].get("tags") or [])},
            str(hit_entries[0].get("damage_type") or "Fire"),
        )
        if hit_entries
        else []
    )
    rotation_more, rotation_doc = rotation_factor(plan)
    evaluations: list[dict[str, Any]] = []
    primary_payload: Optional[dict[str, Any]] = None
    primary_meta: Optional[dict[str, Any]] = None
    applications_all: list[dict[str, Any]] = []
    for meta, states, target, digest in resolved:
        rows, _reasons, notes = build_contributions(
            plan, states=states, target=target, runtime=runtime
        )
        state_values = {**states, **target}
        timing = steady_state_frequency(plan=plan, contributions=rows, hit_tags=hit_tags)
        target_info = runtime.handler("target_side-v1")(
            action="resolve_hit_target",
            merged_target={
                "monster_level": target["monster_level"],
                "fire_resistance": target["resistance"],
            },
        )
        hit_components, hit_applications = build_hit_components_v2(
            plan=plan,
            skill_parameters=skill_parameters,
            contributions=rows,
            target=target,
            states=state_values,
            target_info=target_info,
            crit_rules=rule_payload.get("crit") or {},
        )
        # plan-v2 结构门禁已在 calculation_plan 拒绝 dot/ailments；旧 dot 装配分支已删除。
        dot_components: list[dict[str, Any]] = []
        dot_applications: list[dict[str, Any]] = []
        totals = component_hit_totals(hit_components)
        dot_per_cast = 0.0
        policy_rows: list[dict[str, Any]] = []
        for policy_id, interval in timing["intervals_s"].items():
            policy_rows.append(
                _policy_metrics(
                    policy_id=policy_id,
                    interval_s=interval,
                    per_cast_hit=totals["per_cast_hit"],
                    damage_per_cast=totals["per_cast_hit"] * rotation_more,
                    hits_per_cast=int(totals["hits_per_cast"]),
                    effective_hits_per_cast=totals["effective_hits_per_cast"],
                    rotation_more=rotation_more,
                    windows_s=windows_s,
                    semantics=semantics,
                )
            )
        primary_policy = next(
            (row for row in policy_rows if row["id"] == timing["primary_policy"]), policy_rows[0]
        )
        applications = hit_applications + dot_applications
        applications_all.extend(applications)
        declared_metrics = set(meta.get("metrics") or [])
        dps_evaluation = bool(declared_metrics & {"windows", "policies", "rotation_more_factor"})
        evaluation = {
            "evaluation_id": str(meta.get("evaluation_id")),
            "label": meta.get("label") or meta.get("evaluation_id"),
            "primary": bool(meta.get("primary")),
            "metrics": list(meta.get("metrics") or []),
            "scenario_sha256": digest,
            "scenario": {"states": copy.deepcopy(states), "target": copy.deepcopy(target)},
            "scenario_path": meta.get("scenario_path"),
            "components": hit_components,
            "ailments": dot_components,
            "applications": applications,
            "single_hit": totals["single_hit"],
            "per_cast_hit": totals["per_cast_hit"],
            "hits_per_cast": totals["hits_per_cast"],
            "effective_hits_per_cast": totals["effective_hits_per_cast"],
            "damage_weights": [
                weight
                for component in hit_components
                for weight in component.get("damage_weights") or []
            ],
            "dot_per_cast": dot_per_cast,
            "single_cast_with_tail": totals["single_hit"] + dot_per_cast,
            "timing": timing,
        }
        if dps_evaluation:
            evaluation.update(
                {
                    "rotation_more_factor": rotation_more,
                    "rotation": rotation_doc,
                    "policies": {row["id"]: row for row in policy_rows},
                    "primary_policy": {
                        key: primary_policy[key]
                        for key in (
                            "id",
                            "binding_constraint",
                            "interval_s",
                            "casts_per_second",
                            "hits_per_cast",
                            "effective_hits_per_cast",
                            "hits_per_second",
                            "per_cast_hit",
                            "rotation_more_factor",
                            "damage_per_cast",
                            "damage_per_second",
                            "windows",
                        )
                    },
                    "windows": primary_policy["windows"],
                    "damage_per_cast": primary_policy["damage_per_cast"],
                    "damage_per_second": primary_policy["damage_per_second"],
                    "hits_per_second": primary_policy["hits_per_second"],
                    "casts_per_second": primary_policy["casts_per_second"],
                    "interval_s": primary_policy["interval_s"],
                }
            )
        evaluations.append(evaluation)
        if bool(meta.get("primary")) and primary_payload is None:
            primary_payload = evaluation
            primary_meta = meta
    if primary_payload is None:
        raise ValueError("plan-v2 没有 primary 评估")
    if "damage_per_second" not in primary_payload:
        raise PlanValidationError(
            [
                {
                    "code": "unsupported_plan_v2_feature",
                    "message": "primary 评估未声明 windows/持续输出指标；plan-v2 顶层 DPS 无定义",
                    "severity": "error",
                    "related_ids": [str(primary_meta.get("evaluation_id"))],
                    "affects_coverage": False,
                }
            ]
        )
    # 主评估的完整贡献/账本/覆盖投影（与 v1 结果字段语义一致）
    primary_states = primary_payload["scenario"]["states"]
    primary_target = primary_payload["scenario"]["target"]
    rows, _reasons, notes = build_contributions(
        plan, states=primary_states, target=primary_target, runtime=runtime
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
    coverage = runtime.handler("coverage_projection-v1")(
        plan=plan, dot_applications=primary_payload.get("ailments") or []
    )
    effect_ledger = runtime.handler("ledger_projection-v1")(
        plan=plan,
        contributions_by_id=contributions_by_id,
        notes=notes,
        states=primary_states,
    )
    facts = plan.get("facts") or {}
    presentation = plan.get("presentation") or {}
    limitations = [str(note) for note in (plan.get("scope") or {}).get("limitations") or []]
    issues: list[str] = list(limitations)
    issues.extend(str(item) for item in facts.get("post_assembly_issues") or [])
    issues.extend(str(item) for item in facts.get("early_issues") or [])
    if coverage["status"] == "partial":
        issues.append(
            f"覆盖状态 partial：{coverage['gap_count']} 项未建模/未验证的伤害相关来源（见 coverage.gaps）"
        )
    issues.extend(str(item) for item in facts.get("duplicate_issues") or [])
    hit_components = primary_payload["components"]
    dot_components = primary_payload["ailments"]
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
        states=primary_states,
        target=primary_target,
        limitations=limitations,
    )
    application_summary = dps_result.application_summary(list(primary_payload.get("applications") or []))
    coverage_classes: dict[str, Any] = {}
    for effect in plan.get("effects") or []:
        contribution_id = str(effect.get("contribution_id") or effect.get("effect_id"))
        coverage_classes.setdefault(contribution_id, effect.get("coverage_class"))
    contributions_ledger: list[dict[str, Any]] = []
    for row in rows:
        payload = row.to_dict()
        summary = application_summary.get(row.contribution_id)
        if summary:
            payload["application"] = summary
        payload["coverage_class"] = coverage_classes.get(row.contribution_id)
        contributions_ledger.append(payload)
    timing = primary_payload["timing"]
    primary_scenario_digest = str(primary_payload["scenario_sha256"])
    evaluation_digests = [
        {
            "evaluation_id": str(item.get("evaluation_id")),
            "scenario_sha256": str(item.get("scenario_sha256")),
        }
        for item in evaluations
    ]
    evaluation_digests.sort(key=lambda item: item["evaluation_id"])
    scenario_digest = planmod.aggregate_scenario_sha256(evaluation_digests)
    target_info = runtime.handler("target_side-v1")(
        action="resolve_hit_target",
        merged_target={
            "monster_level": primary_target["monster_level"],
            "fire_resistance": primary_target["resistance"],
        },
    )
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
    weights: list[float] = []
    for component in hit_components:
        weights.extend(float(value) for value in component.get("damage_weights") or [])
    return {
        "schema": SCHEMA_RESULT_V4,
        "case_id": (plan.get("subject") or {}).get("case_id"),
        "model": "dps",
        "skill": presentation.get("skill_name") or facts.get("skill_name"),
        "character": copy.deepcopy(facts.get("character")),
        "skill_names": copy.deepcopy(presentation.get("skill_names") or {}),
        "scope": scope,
        "scope_detail": scope_detail,
        "status": "assumption",
        "coverage": coverage,
        "scenario": {
            "states": copy.deepcopy(primary_states),
            "target": copy.deepcopy(primary_target),
        },
        "states": copy.deepcopy(primary_states),
        "target": target_info,
        "attributes": copy.deepcopy(facts.get("attributes")),
        "attribute_conversion": copy.deepcopy(facts.get("attribute_conversion_info")),
        "equipment_facts": copy.deepcopy(facts.get("equipment_facts")),
        "skill_constants": copy.deepcopy(facts.get("skill_constants")),
        "ailment_constants": copy.deepcopy(facts.get("ailment_constants")),
        "evaluations": evaluations,
        "primary_evaluation": str(primary_meta.get("evaluation_id")),
        "single_hit": primary_payload["single_hit"],
        "single_cast_with_tail": primary_payload["single_cast_with_tail"],
        "sustained_dps": primary_payload["damage_per_second"],
        "hit_dps": primary_payload["damage_per_second"],
        "dot_dps": 0.0,
        "interval_s": timing["interval_s"],
        "cooldown_s": timing["effective_cooldown_s"],
        "cooldown_recovery": timing["cooldown_recovery"],
        "cooldown_reduction": timing["cooldown_reduction"],
        "attack_speed_increased": timing["increased_attack_speed"],
        "more_attack_speed": timing["more_attack_speed"],
        "action_s": timing["action_s"],
        "action_interval_s": timing["action_interval_s"],
        "hits_per_cast": {
            "per_cast": primary_payload["hits_per_cast"],
            "effective_hits_per_cast": primary_payload["effective_hits_per_cast"],
            "damage_weights": weights,
            "status": "confirmed",
        },
        "casts_per_second": primary_payload["casts_per_second"],
        "hits_per_second": primary_payload["hits_per_second"],
        "damage_per_cast": primary_payload["damage_per_cast"],
        "damage_per_second": primary_payload["damage_per_second"],
        "policy": primary_payload["primary_policy"]["id"],
        "policies": primary_payload["policies"],
        "rotation_more_factor": rotation_more,
        "rotation": rotation_doc,
        "windows": primary_payload["windows"],
        "components": hit_components,
        "ailments": dot_components,
        "applications": list(primary_payload.get("applications") or []),
        "contribution_application_summary": application_summary,
        "duplicate_effects": copy.deepcopy(facts.get("duplicate_effects") or []),
        "issues": issues,
        "contributions": contributions_ledger,
        "effect_ledger": effect_ledger,
        "rules": {
            "crit": rule_payload.get("crit"),
            "every_nth_hit": rule_payload.get("every_nth_hit"),
            "damage_to_penetration": rule_payload.get("damage_to_penetration"),
            "hit_events": (plan.get("facts") or {}).get("hit_events"),
            "rotation": rotation_doc,
        },
        "equipment": copy.deepcopy(presentation.get("equipment") or []),
        "controls": copy.deepcopy(presentation.get("controls") or []),
        "input_sha256": (calc_ref or {}).get("sha256"),
        "profile_sha256": (profile_ref or {}).get("sha256") or (profile_content_ref or {}).get("sha256"),
        "plan_ref": planmod.plan_ref(plan),
        "plan_sha256": plan.get("plan_sha256"),
        "scenario_sha256": scenario_digest,
        "primary_scenario_sha256": primary_scenario_digest,
        "evaluation_scenarios": evaluation_digests,
        "execution_fingerprint": fingerprint,
    }


def execute_charge_dot(
    plan: dict[str, Any],
    scenario: Any,
    *,
    runtime: Optional[plan_handlers.PlanRuntime] = None,
) -> dict[str, Any]:
    runtime = runtime or plan_handlers.PlanRuntime()
    resolved = resolve_evaluation_scenarios(plan, scenario)
    schedule = plan["schedule"]
    horizon_s = float(schedule["horizon_s"])
    primary_policy = str(schedule.get("primary_policy"))
    duration_s = float(plan["components"][0]["duration_s"])
    evaluation_payloads: list[dict[str, Any]] = []
    scenario_hashes: list[dict[str, Any]] = []
    policy_conditions = {
        str(item.get("policy_id")): item.get("cast_condition")
        for item in (plan.get("schedule") or {}).get("action_policies") or []
        if isinstance(item, dict)
    }
    for meta, states, target, digest in resolved:
        single_rows, _, _ = build_contributions(plan, states=states, target=target, runtime=runtime)
        single_rows = _runtime_condition_filter(single_rows, states=states, target=target, runtime=runtime)
        loop_states = dict(states)
        for control in plan.get("controls") or []:
            if control.get("single_cast_only"):
                loop_states[control["key"]] = False
        rows, _, notes = build_contributions(plan, states=loop_states, target=target, runtime=runtime)
        rows = _runtime_condition_filter(rows, states=loop_states, target=target, runtime=runtime)
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
            condition=policy_conditions.get(policy),
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
            rows=single_rows,
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
                "single_cast_only": bool(control.get("single_cast_only")),
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



def execute_generic(plan: Mapping[str, Any], scenario: Any = None) -> dict[str, Any]:
    """按 execution capabilities/字段选择通用 primitive，不按 ability/legacy_schema 分派。"""
    execution = plan.get("execution") if isinstance(plan, Mapping) else None
    execution = execution or plan
    capabilities = {str(item) for item in execution.get("capabilities") or []}
    schedule = execution.get("schedule") or {}
    driver = str(schedule.get("driver") or "")
    charge = schedule.get("charges") or schedule.get("charge") or {}
    if (
        "charge_schedule" in capabilities
        or driver in ("charge_driver", "charge")
        or bool(charge)
    ):
        return execute_charge_dot(plan, scenario)
    if (
        "hit_events" in capabilities
        or "multi_hit_events" in capabilities
        or driver == "steady_state"
    ):
        return execute_hit_events(plan, scenario)
    raise ValueError("execution capabilities/fields 未声明可执行的 hit/charge primitive")
