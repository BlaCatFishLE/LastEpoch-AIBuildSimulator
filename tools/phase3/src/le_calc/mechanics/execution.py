"""Generic v4 execution vertical slice (S2).

Consumes only the plan's explicit execution data. This slice is deliberately
independent of FR/BH/VC rules: synthetic callers supply channel/scope/condition/
window/tail/application/coverage fields and the primitives below compute numbers,
application ledgers and computed coverage diagnostics.
"""
from __future__ import annotations

from typing import Any, Mapping
from .tag_match import damage_channel_tags, TagPolicy, UNKNOWN_RESTRICT, tags_match

_CHANNEL_POLICY = TagPolicy(frozenset(), frozenset(), UNKNOWN_RESTRICT, "execution-damage-channel")


def _damage_tags(component: Mapping[str, Any]) -> set[str]:
    dtype = str(component.get("damage_type") or component.get("damage_channel") or "")
    tags = set(damage_channel_tags(component.get("tags") or [], dtype))
    tags |= {tag.lower() for tag in tags}
    return tags


ALLOWED_UNITS = {"damage"}
ALLOWED_SCOPES = {"body", "ailment", "actor", "owner", "all"}
ALLOWED_TAIL_POLICIES = {"include_unsettled", "exclude_unsettled"}
ALLOWED_WINDOW_POLICIES = {"half_open"}
ALLOWED_COHORT_POLICIES = {"independent_cast", "merge"}


class ExecutionContractError(ValueError):
    """execution 数据不满足通用执行契约。"""


def _condition(condition: Any, states: Mapping[str, Any]) -> bool | None:
    if condition in (None, "", "always", True):
        return True
    if condition in (False, "never"):
        return False
    if condition == "unresolved":
        return None
    if isinstance(condition, Mapping):
        key = condition.get("state")
        if not isinstance(key, str) or not key:
            raise ExecutionContractError("condition.state 缺失")
        if "equals" not in condition:
            raise ExecutionContractError("condition.equals 缺失")
        return bool(states.get(key) == condition["equals"])
    raise ExecutionContractError(f"未知 condition：{condition!r}")


def _target_multiplier(execution: Mapping[str, Any], component: Mapping[str, Any]) -> float:
    target = execution.get("target") or {}
    resistances = target.get("resistances") or {}
    key = str(component.get("resistance_key") or "")
    if key not in resistances:
        raise ExecutionContractError(f"target.resistances 缺少 {key!r}")
    resistance = float(resistances[key])
    damage_reduction = float(target.get("damage_reduction") or 0.0)
    return (1.0 + float(target.get("penetration") or 0.0)) * (1.0 - resistance) * (1.0 - damage_reduction)


GAP_COVERAGE_CLASSES = {"unverified", "required_missing", "semantic_unresolved"}



def _source_gap(execution: Mapping[str, Any], effect: Mapping[str, Any]) -> dict[str, Any] | None:
    """来源级校验：required 约束来源存在/已验证，不要求作用于所有组件。"""
    source_id = str(effect.get("source_id") or "")
    if not source_id:
        return None
    source = next((item for item in execution.get("sources") or [] if str(item.get("source_id")) == source_id), None)
    if source is None or source.get("status") in ("missing", "unverified", "unresolved"):
        if effect.get("required") or source is None:
            return {"code": "source_missing", "contribution_id": source_id, "coverage_class": "required_source"}
    if effect.get("damage_related") and effect.get("mapped") is False and not effect.get("exclusion_reason"):
        return {"code": "damage_source_unresolved", "contribution_id": str(effect.get("effect_id") or ""), "coverage_class": "unmapped_damage"}
    return None


def _component_effects(
    execution: Mapping[str, Any],
    component: Mapping[str, Any],
    states: Mapping[str, Any],
    coverage_gaps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], float, float, list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    applications: list[dict[str, Any]] = []
    increased = 0.0
    more = 1.0
    for effect in execution.get("effects") or []:
        effect_id = str(effect.get("effect_id") or "")
        if not effect_id:
            raise ExecutionContractError("effect.effect_id 缺失")
        scope = str(effect.get("scope") or "body")
        if scope not in ALLOWED_SCOPES:
            raise ExecutionContractError(f"未知 effect scope：{scope!r}")
        if scope not in ("all", str(component.get("scope"))):
            gap = _source_gap(execution, effect)
            if gap:
                coverage_gaps.append(gap)
            applications.append({"effect_id": effect_id, "component_id": component["component_id"], "status": "not_applicable", "reason": "scope", "coverage_class": effect.get("coverage_class")})
            continue
        channels = {str(item) for item in effect.get("channel_tags") or []}
        if channels and not tags_match(channels, _damage_tags(component), _CHANNEL_POLICY):
            gap = _source_gap(execution, effect)
            if gap:
                coverage_gaps.append(gap)
            applications.append({"effect_id": effect_id, "component_id": component["component_id"], "status": "not_applicable", "reason": "channel", "coverage_class": effect.get("coverage_class")})
            continue
        try:
            passed = _condition(effect.get("condition"), states)
        except ExecutionContractError:
            raise
        if passed is None or passed is False:
            status = "unresolved" if passed is None else "condition_not_satisfied"
            if status == "unresolved":
                coverage_gaps.append({"code": "condition_unresolved", "contribution_id": effect_id, "detail": "condition 无法判定"})
            applications.append({"effect_id": effect_id, "component_id": component["component_id"], "status": status, "reason": "condition", "coverage_class": effect.get("coverage_class")})
            continue
        operation = str(effect.get("operation") or "")
        value = float(effect.get("value") or 0.0)
        if operation == "increased":
            increased += value
        elif operation == "more":
            more *= 1.0 + value
        elif operation in ("added", "base"):
            pass
        else:
            raise ExecutionContractError(f"未知 effect operation：{operation!r}")
        applied.append(effect)
        applications.append({"effect_id": effect_id, "component_id": component["component_id"], "status": "applied", "reason": "", "coverage_class": effect.get("coverage_class")})
    return applied, increased, more, applications


def _application_damage(
    execution: Mapping[str, Any],
    component: Mapping[str, Any],
    applications: list[Mapping[str, Any]],
    states: Mapping[str, Any],
    coverage_gaps: list[dict[str, Any]],
) -> tuple[float, list[dict[str, Any]]]:
    total = 0.0
    ledger: list[dict[str, Any]] = []
    for application in execution.get("applications") or []:
        if str(application.get("source_component")) != str(component.get("component_id")):
            continue
        policy = str(application.get("cohort_policy") or "")
        if policy not in ALLOWED_COHORT_POLICIES:
            raise ExecutionContractError(f"未知 cohort_policy：{policy!r}")
        window_policy = str(application.get("window_policy") or "")
        if window_policy not in ALLOWED_WINDOW_POLICIES:
            raise ExecutionContractError(f"未知 window_policy：{window_policy!r}")
        tail_policy = str(application.get("tail_policy") or "")
        if tail_policy not in ALLOWED_TAIL_POLICIES:
            raise ExecutionContractError(f"未知 tail_policy：{tail_policy!r}")
        try:
            passed = _condition(application.get("condition"), states)
        except ExecutionContractError:
            raise
        if passed is False:
            ledger.append({"application_id": application["application_id"], "component_id": component["component_id"], "status": "condition_not_satisfied", "damage_total": 0.0})
            continue
        if passed is None:
            coverage_gaps.append({"code": "application_condition_unresolved", "contribution_id": str(application.get("application_id")), "detail": "application condition 无法判定"})
            ledger.append({"application_id": application["application_id"], "component_id": component["component_id"], "status": "unresolved", "damage_total": 0.0})
            continue
        ticks = 1 if float(application.get("duration_s") or 0.0) <= 0 else max(1, int(float(application.get("duration_s")) / max(float(component.get("tick_interval_s") or 0.0), 1e-9)))
        damage = float(application.get("damage_per_tick") or 0.0) * ticks
        ledger.append({"application_id": application["application_id"], "component_id": component["component_id"], "status": "applied", "damage_total": damage})
        total += damage
    return total, ledger


def execute_execution_slice(
    execution: Mapping[str, Any],
    scenario: Mapping[str, Any] | None = None,
    *,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """执行一个通用 execution 纵向切片；未知字段/组合明确拒绝。"""
    scenario = dict(scenario or {})
    states = {**(execution.get("states") or {}), **dict(scenario.get("states") or {})}
    capabilities = dict(capabilities or execution.get("capabilities_map") or {})
    if capabilities.get("frequency") not in (None, "fixed_repeat"):
        raise ExecutionContractError("仅支持 fixed_repeat 频率；charge 由 S3+ 扩展")
    components = execution.get("components") or []
    if not components:
        raise ExecutionContractError("execution.components 不能为空")
    gaps: list[dict[str, Any]] = []
    component_rows = []
    hit_total = 0.0
    application_total = 0.0
    applications_ledger: list[dict[str, Any]] = []
    for component in components:
        if component.get("unit") not in ALLOWED_UNITS:
            raise ExecutionContractError(f"未知 unit：{component.get('unit')!r}")
        if component.get("kind") not in ("hit", "dot"):
            raise ExecutionContractError(f"未知 component kind：{component.get('kind')!r}")
        if component.get("scope") not in ALLOWED_SCOPES:
            raise ExecutionContractError(f"未知 component scope：{component.get('scope')!r}")
        applied, increased, more, applications = _component_effects(execution, component, states, gaps)
        base = float(component.get("base_damage") or 0.0)
        added = sum(float(effect.get("value") or 0.0) for effect in applied if effect.get("operation") == "added")
        crit = 1.0 if not component.get("crit_capable") else 1.0 + float(component.get("crit_chance") or 0.0) * (float(component.get("crit_multiplier") or 1.0) - 1.0)
        damage = (base + added * float(component.get("added_damage_scaling") or 0.0)) * (1.0 + increased) * more * crit * _target_multiplier(execution, component)
        application_damage, application_ledger = _application_damage(execution, component, applications, states, gaps)
        component_rows.append({"component_id": component["component_id"], "kind": component["kind"], "damage": damage, "application_damage": application_damage})
        applications_ledger.extend(application_ledger)
        if component["kind"] == "hit":
            hit_total += damage
        else:
            application_total += application_damage
    frequency = execution.get("frequency") or {}
    horizon = float(frequency.get("horizon_s") or 0.0)
    interval = float(frequency.get("cast_interval_s") or 0.0)
    if horizon <= 0 or interval <= 0:
        raise ExecutionContractError("frequency.horizon_s/cast_interval_s 必须 > 0")
    casts = int(horizon / interval) + 1
    tail_policy = str(frequency.get("tail_policy") or "")
    if tail_policy not in ALLOWED_TAIL_POLICIES:
        raise ExecutionContractError(f"未知 tail_policy：{tail_policy!r}")
    tail = application_total if tail_policy == "include_unsettled" else 0.0
    total_damage = hit_total * casts + application_total * casts + tail
    windows = []
    for cut in frequency.get("windows_s") or [horizon]:
        window_casts = int(float(cut) / interval) + 1
        windows.append({"window_s": float(cut), "hit_damage": hit_total * window_casts, "application_damage": application_total * window_casts, "total_damage": (hit_total + application_total) * window_casts, "dps": total_damage / horizon})
    unique_gaps = []
    seen_gaps = set()
    for gap in gaps:
        key = (gap.get("code"), gap.get("contribution_id"))
        if key not in seen_gaps:
            seen_gaps.add(key)
            unique_gaps.append(gap)
    gaps = unique_gaps
    coverage_status = "partial" if gaps else "complete"
    declared = execution.get("coverage") or {}
    return {
        "schema": "le-dps-result-v6-slice",
        "scenario": {"states": states},
        "components": component_rows,
        "applications_ledger": applications_ledger,
        "windows": windows,
        "totals": {"hit_damage": hit_total, "application_damage": application_total, "horizon_damage": total_damage, "horizon_dps": total_damage / horizon, "cast_count": casts, "tail_damage": tail},
        "coverage": {"status": coverage_status, "gap_count": len(gaps), "gaps": gaps, "declared_status": declared.get("status")},
    }

def crit_factor(component: Mapping[str, Any]) -> float:
    if not component.get("crit_capable"):
        return 1.0
    return 1.0 + float(component.get("crit_chance") or 0.0) * (
        float(component.get("crit_multiplier") or 1.0) - 1.0
    )


def weighted_hit_damage(component: Mapping[str, Any], *, increased: float = 0.0, more: float = 1.0) -> float:
    """VC 通用每击伤害：base + added*scaling，乘 increased/more/crit/weights。"""
    base = float(component.get("base_damage") or 0.0)
    added = float(component.get("added_damage") or 0.0) * float(component.get("added_damage_scaling") or 0.0)
    weights = component.get("damage_weights")
    weight_sum = sum(float(item) for item in weights) if isinstance(weights, list) and weights else float(component.get("hit_count") or 1)
    return (base + added) * (1.0 + float(increased)) * float(more) * crit_factor(component) * weight_sum


def ailment_expected_damage(application: Mapping[str, Any], *, effect_multiplier: float = 1.0) -> float:
    """VC 通用 ailment：chance × expected_stacks × 每 tick × 持续/间隔。"""
    duration = float(application.get("duration_s") or 0.0)
    interval = float(application.get("tick_interval_s") or 0.0)
    if duration < 0 or interval < 0:
        raise ExecutionContractError("ailment duration/tick_interval_s 不能为负")
    ticks = 1.0 if interval <= 0 else duration / interval
    chance = float(application.get("chance", 1.0))
    if chance < 0:
        raise ExecutionContractError("ailment chance 不能为负")
    return float(application.get("damage_per_tick") or 0.0) * ticks * chance * float(application.get("expected_stacks") or 1.0) * effect_multiplier


def project_computed_coverage(ledger: list[Mapping[str, Any]], coverage_input: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """由实际 application/ledger 排除记录计算 coverage；不复制声明状态。"""
    gaps: list[dict[str, Any]] = []
    for row in ledger:
        status = str(row.get("status") or "")
        coverage_class = row.get("coverage_class")
        if status in ("unresolved", "condition_not_satisfied") or (coverage_class and status == "not_applicable"):
            gaps.append(
                {
                    "code": "application_" + (status or "unknown"),
                    "contribution_id": str(row.get("effect_id") or row.get("application_id") or ""),
                    "coverage_class": coverage_class,
                }
            )
    for item in (coverage_input or {}).get("unmapped") or []:
        gaps.append({"code": "declared_unmapped", "contribution_id": str(item), "coverage_class": "unmapped"})
    return {"status": "partial" if gaps else "complete", "gap_count": len(gaps), "gaps": gaps}


def window_tail_total(window_damage: float, tail_damage: float, tail_policy: str) -> float:
    if tail_policy == "include_unsettled":
        return float(window_damage) + float(tail_damage)
    if tail_policy == "exclude_unsettled":
        return float(window_damage)
    raise ExecutionContractError(f"未知 tail_policy：{tail_policy!r}")


EFFECT_OPERATIONS = {"added", "increased", "more", "base", "flag", "override", "static"}
COVERAGE_STATUSES = {"mapped", "unresolved", "unknown", "required", "not_applicable", "excluded", "applied", "included", "verified"}


def project_effect_contributions(effects: list[Mapping[str, Any]]) -> dict[str, Any]:
    """通用 effect → contribution/effect_ledger 投影；消费真实 VC effects 元数据。"""
    rows: list[dict[str, Any]] = []
    refs: dict[str, list[str]] = {}
    for effect in effects:
        if not effect.get("emits_contribution", True):
            continue
        effect_id = str(effect.get("effect_id") or "")
        if not effect_id:
            raise ExecutionContractError("effect.effect_id 缺失")
        operation = str(effect.get("operation") or "")
        if operation not in EFFECT_OPERATIONS:
            raise ExecutionContractError(f"未知 effect operation：{operation!r}")
        coverage_status = str(effect.get("coverage_status") or "mapped")
        if coverage_status not in COVERAGE_STATUSES:
            raise ExecutionContractError(f"未知 coverage_status：{coverage_status!r}")
        included = bool(effect.get("included_static", True))
        exclusion_reason = effect.get("exclusion_reason")
        ledger_ref = str(
            effect.get("ledger_ref")
            or effect.get("ownership_key")
            or effect.get("ledger_kind")
            or effect.get("source_ref")
            or effect.get("handler_id")
            or "unassigned"
        )
        contribution_id = str(effect.get("contribution_id") or effect_id)
        ownership = {
            "owner": effect.get("owner"),
            "ownership_key": effect.get("ownership_key"),
            "source_refs": list(effect.get("source_refs") or []),
            "source_kind": effect.get("source_kind"),
        }
        rows.append(
            {
                "effect_id": effect_id,
                "contribution_id": contribution_id,
                "handler_id": str(effect.get("handler_id") or ""),
                "condition_code": effect.get("condition_code"),
                "included": included,
                "exclusion_reason": exclusion_reason,
                "operation": operation,
                "value": float(effect.get("value") or 0.0),
                "ledger_ref": ledger_ref,
                "coverage_status": coverage_status,
                "coverage_class": effect.get("coverage_class"),
                "ownership": ownership,
            }
        )
        refs.setdefault(ledger_ref, []).append(contribution_id)
    return {
        "schema": "le-effect-ledger-v1",
        "rows": rows,
        "refs": refs,
        "row_count": len(rows),
    }


def project_ownership(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    owners: dict[str, list[str]] = {}
    for row in rows:
        owner = row.get("ownership") or {}
        key = str(owner.get("ownership_key") or owner.get("owner") or "unassigned")
        owners.setdefault(key, []).append(str(row.get("contribution_id")))
    return {"schema": "le-effect-ownership-v1", "owners": owners, "owner_count": len(owners)}


def project_effect_coverage(
    rows: list[Mapping[str, Any]], coverage_input: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """正常不适用与真实缺口分区；不照抄 declared status。"""
    normal_exclusions = []
    gaps = []
    for row in rows:
        if row.get("included"):
            continue
        if row.get("coverage_status") in ("unresolved", "unknown", "required"):
            gaps.append({"code": "effect_coverage_unresolved", "contribution_id": row.get("contribution_id"), "coverage_class": row.get("coverage_class")})
        else:
            normal_exclusions.append({"contribution_id": row.get("contribution_id"), "reason": row.get("exclusion_reason")})
    for item in (coverage_input or {}).get("unmapped") or []:
        gaps.append({"code": "coverage_input_unmapped", "contribution_id": str(item), "coverage_class": "unmapped"})
    return {
        "status": "partial" if gaps else "complete",
        "gap_count": len(gaps),
        "gaps": gaps,
        "normal_exclusions": normal_exclusions,
        "declared_status": (coverage_input or {}).get("declared_status"),
    }


def application_window_damage(
    application: Mapping[str, Any],
    *,
    casts: int,
    windows_s: list[float],
    cast_interval_s: float,
) -> dict[str, Any]:
    """通用 application/ailment：chance/expected_stacks/duration/tick/cohort/window/tail。"""
    if casts < 1 or cast_interval_s <= 0:
        raise ExecutionContractError("application casts/cast_interval_s 非法")
    cooldown = str(application.get("cohort_policy") or "")
    if cooldown not in ALLOWED_COHORT_POLICIES:
        raise ExecutionContractError(f"未知 cohort_policy：{cooldown!r}")
    tail_policy = str(application.get("tail_policy") or "")
    if tail_policy not in ALLOWED_TAIL_POLICIES:
        raise ExecutionContractError(f"未知 tail_policy：{tail_policy!r}")
    per_cast = ailment_expected_damage(application)
    windows = []
    for cut in windows_s:
        window_casts = int(float(cut) / cast_interval_s) + 1
        windows.append({"window_s": float(cut), "application_damage": per_cast * window_casts})
    tail = per_cast if tail_policy == "include_unsettled" else 0.0
    return {"casts": casts, "windows": windows, "tail_damage": tail, "total_damage": per_cast * casts + tail, "cohort_policy": cooldown}


def evaluate_execution_condition_program(
    program: Mapping[str, Any],
    *,
    states: Mapping[str, Any],
    target: Mapping[str, Any],
    runtime: Any,
) -> tuple[bool | None, list[str]]:
    """通用 condition program 求值；未知返回 None，不静默当 false。"""
    op = str(program.get("op") or "all")
    if op == "always":
        return True, []
    failed: list[str] = []
    results: list[bool | None] = []
    condition_eval = runtime.handler("condition_eval-v1")
    state_values = {**dict(states), **dict(target)}
    for term in program.get("terms") or []:
        kind = term.get("kind")
        if kind == "control_true":
            key = str(term.get("key")); passed = bool(states.get(key)); results.append(passed)
            if not passed: failed.append(f"状态未满足：{key}")
        elif kind == "control_equals":
            key = str(term.get("key")); passed = states.get(key) == term.get("value"); results.append(passed)
            if not passed: failed.append(f"状态不匹配：{key}")
        elif kind == "control_count":
            key = str(term.get("key")); raw = states.get(key)
            passed = isinstance(raw, int) and not isinstance(raw, bool) and raw > 0
            results.append(passed)
            if not passed: failed.append(f"状态层数为 0：{key}")
        elif kind == "target_true":
            key = str(term.get("key")); passed = bool(target.get(key)); results.append(passed)
            if not passed: failed.append(f"目标条件未满足：{key}")
        elif kind == "code":
            code_value = str(term.get("code"))
            passed = condition_eval(code_value, state_values)
            results.append(passed)
            if passed is not True: failed.append(f"条件代码未满足或未知：{code_value}")
        else:
            raise ExecutionContractError(f"未知 condition term kind：{kind!r}")
    if op == "any" and any(value is True for value in results):
        return True, []
    if any(item is None for item in results):
        return None, failed
    return (any(bool(item) for item in results) if op == "any" else all(bool(item) for item in results)), failed


def settle_execution_effects(
    effects: list[Mapping[str, Any]],
    *,
    conditions: Mapping[str, Any] | None = None,
    states: Mapping[str, Any] | None = None,
    target: Mapping[str, Any] | None = None,
    runtime: Any,
    components: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """真实 effects → 每 contribution included/excluded/value/reason；按 operation 结算组件。"""
    conditions = dict(conditions or {})
    states = dict(states or {})
    target = dict(target or {})
    components = list(components or [])
    rows: list[dict[str, Any]] = []
    component_totals: dict[str, dict[str, float]] = {}
    for effect in sorted(effects, key=lambda item: int(item.get("sequence") or 0)):
        if not effect.get("emits_contribution"):
            continue
        effect_id = str(effect.get("effect_id") or "")
        included_static = bool(effect.get("included_static", True))
        reason = effect.get("exclusion_reason")
        included = included_static
        status = "included"
        condition_ref = effect.get("condition_ref")
        if condition_ref:
            program = conditions.get(str(condition_ref))
            if program is None:
                raise ExecutionContractError(f"未知 condition_ref：{condition_ref!r}")
            passed, failed = evaluate_execution_condition_program(
                program, states=states, target=target, runtime=runtime
            )
            included = included_static and bool(passed)
            if passed is None:
                status = "unresolved"; reason = f"条件无法判定：{failed}"
            elif not passed:
                status = "condition_not_satisfied"; reason = "；".join(failed) or "condition"
        if effect.get("condition_code") and not effect.get("condition_code_static"):
            code_passed = runtime.handler("condition_eval-v1")(str(effect["condition_code"]), {**states, **target})
            if code_passed is None:
                status = "unresolved"
            elif not code_passed:
                status = "condition_not_satisfied"
            included = included and code_passed is not False
        if not included and status == "included":
            status = "excluded_static"
        operation = str(effect.get("operation") or "")
        if operation not in EFFECT_OPERATIONS:
            raise ExecutionContractError(f"未知 effect operation：{operation!r}")
        value = float(effect.get("value") or 0.0)
        row = {
            "effect_id": effect_id,
            "contribution_id": str(effect.get("contribution_id") or effect_id),
            "component_ids": [],
            "included": included,
            "status": status if not included else ("unresolved" if status == "unresolved" else "applied"),
            "reason": None if included and status == "applied" else reason,
            "operation": operation,
            "value": value,
            "damage_tags": list(effect.get("damage_tags") or []),
            "ownership": {"owner": effect.get("owner"), "ownership_key": effect.get("ownership_key"), "source_refs": list(effect.get("source_refs") or [])},
            "handler_id": str(effect.get("handler_id") or ""),
            "coverage_status": str(effect.get("coverage_status") or "mapped"),
            "ledger_ref": str(effect.get("ownership_key") or effect.get("handler_id") or "unassigned"),
        }
        for component in components:
            tags = _damage_tags(component)
            if not tags_match(row["damage_tags"], tags, _CHANNEL_POLICY):
                continue
            row["component_ids"].append(str(component.get("component_id")))
            if not included:
                continue
            totals = component_totals.setdefault(str(component.get("component_id")), {"added": 0.0, "increased": 0.0, "more": 1.0})
            if operation in ("added", "base"):
                totals["added"] += value
            elif operation == "increased":
                totals["increased"] += value
            elif operation == "more":
                totals["more"] *= 1.0 + value
        rows.append(row)
    resistances = (target or {}).get("resistances") or {}
    damage_reduction = float((target or {}).get("damage_reduction") or 0.0)
    for component in components:
        cid = str(component.get("component_id"))
        totals = component_totals.setdefault(cid, {"added": 0.0, "increased": 0.0, "more": 1.0})
        resistance = float(resistances.get(str(component.get("resistance_key")), 0.0))
        target_factor = (1.0 - resistance) * (1.0 - damage_reduction)
        # ``weighted_hit_damage`` 的 increased/more 是 keyword-only 参数：必须显式传入，
        # 不能只塞进 component 字典（否则被静默忽略，与 execute_execution_slice 内联公式不一致）。
        component_payload = {**component, "added_damage": totals["added"]}
        totals["damage"] = weighted_hit_damage(
            component_payload, increased=totals["increased"], more=totals["more"]
        ) * target_factor
    return {"rows": rows, "row_count": len(rows), "component_totals": component_totals}


def validate_application_capabilities(
    application: Mapping[str, Any],
    component_damage_types: list[str] | set[str],
) -> dict[str, Any]:
    """通用 application 能力矩阵：消费 ailment/type/scope/channel/duration/tick/chance。"""
    ailment_id = str(application.get("ailment_id") or "")
    if not ailment_id:
        raise ExecutionContractError("application.ailment_id 缺失")
    scope = str(application.get("scope") or "")
    if scope != "ailment":
        raise ExecutionContractError(f"application scope 超出能力矩阵：{scope!r}")
    damage_type = str(application.get("damage_type") or "")
    allowed_types = {str(item).lower() for item in component_damage_types}
    if damage_type.lower() not in allowed_types:
        raise ExecutionContractError(f"application damage_type 超出能力矩阵：{damage_type!r}")
    if str(application.get("cohort_policy") or "") not in ALLOWED_COHORT_POLICIES:
        raise ExecutionContractError("application cohort_policy 未知")
    channel = str(application.get("damage_channel") or "")
    resistance_key = str(application.get("resistance_key") or "")
    if channel and channel.lower() not in allowed_types:
        raise ExecutionContractError(f"application damage_channel 超出能力矩阵：{channel!r}")
    if resistance_key and resistance_key.lower() not in allowed_types:
        raise ExecutionContractError(f"application resistance_key 超出能力矩阵：{resistance_key!r}")
    for key in ("duration_s", "tick_interval_s", "chance"):
        if application.get(key) is not None and float(application[key]) < 0:
            raise ExecutionContractError(f"application.{key} 不能为负")
    return {
        "ailment_id": ailment_id,
        "damage_type": damage_type,
        "damage_channel": application.get("damage_channel"),
        "resistance_key": application.get("resistance_key"),
        "scope": scope,
        "duration_s": float(application.get("duration_s") or 0.0),
        "tick_interval_s": float(application.get("tick_interval_s") or 0.0),
        "chance": float(application.get("chance") or 0.0),
        "expected_stacks": float(application.get("expected_stacks") or 1.0),
        "cohort_policy": str(application.get("cohort_policy") or ""),
    }


def apply_application_identity(
    ledger: list[dict[str, Any]],
    applications: list[Mapping[str, Any]],
    body_component_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """只把声明 application 身份写入 app 输出单元；本体组件行保持 body 语义。"""
    if not applications:
        return ledger
    body_ids = {str(item) for item in body_component_ids or set()}
    primary = applications[0]
    identity = {
        "application_id": str(primary.get("application_id") or ""),
        "ailment_id": str(primary.get("ailment_id") or ""),
        "damage_type": primary.get("damage_type"),
        "damage_channel": primary.get("damage_channel"),
        "resistance_key": primary.get("resistance_key"),
        "scope": primary.get("scope"),
        "presentation_label": str(primary.get("application_id") or primary.get("ailment_id") or ""),
    }
    for row in ledger:
        component_id = str(row.get("component_id") or "")
        if component_id in body_ids:
            continue
        row.update({key: value for key, value in identity.items() if value not in (None, "")})
    return ledger
