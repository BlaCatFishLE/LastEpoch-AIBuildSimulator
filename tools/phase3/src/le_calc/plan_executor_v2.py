"""计算计划 v2 执行器：多命中组件、具名频率策略与独立场景评估。

边界与 v1 相同：只消费计划、场景与静态 handler registry；不读 case/profile/catalog，
不调用旧 FR 专用计算器（``hit_inputs`` / ``hit_damage`` / ``hit_simulate``）。

v2 相对 v1 的显式语义扩展（破坏性变化，必须走 plan-v2/result-v4）：

* ``components`` 支持 0..N 个 hit（带 ``hits_per_cast`` 与每击 ``damage_weights``）
  和 0..N 个 dot，总数至少 1；
* ``schedule`` 使用具名频率策略（``cooldown_limited`` / ``attack_speed_limited``），
  窗口按期望施放数（可声明完整施放数）聚合，周期平均因子只作用于直接命中 DPS；
* 结果按具名评估（``evaluations``）分别绑定场景哈希；``single_hit`` 只表示首击
  伤害，逐击权重、每秒命中数与每秒施放数分开记录。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Optional

from . import calculation_plan as planmod
from . import dps_result
from . import model
from . import plan_handlers
from . import plan_executor
from .dps_components import (
    DPS_TAG_POLICY,
    attack_speed_increased,
    build_hit_components,
    cooldown_recovery_increased,
    component_tags,
    more_product,
)
from .mechanics.tag_match import tags_match

SCHEMA_RESULT_V4 = "le-dps-result-v4"


# ---------------------------------------------------------------------------
# 场景：单场景（所有评估共用）或逐评估场景
# ---------------------------------------------------------------------------


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
            states, target = plan_executor.validate_scenario(
                dict(plan), by_id[str(meta.get("evaluation_id"))]
            )
            digest = planmod.scenario_sha256({"states": states, "target": target})
            resolved.append((meta, states, target, digest))
        return resolved
    states, target = plan_executor.validate_scenario(dict(plan), scenario)
    digest = planmod.scenario_sha256({"states": states, "target": target})
    return [(meta, copy.deepcopy(states), copy.deepcopy(target), digest) for meta in evaluations]


# ---------------------------------------------------------------------------
# 组件装配
# ---------------------------------------------------------------------------


def _hit_component_entries(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in plan.get("components") or []
        if isinstance(item, dict) and item.get("kind") == "hit"
    ]


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
        # entry.damage_type 是该事件的基础类型，不限制其它类型的附加点伤。
        event_skill = dict(skill)
        if entry.get("damage_type"):
            dtype = str(entry["damage_type"])
            event_skill["base_damage"] = {dtype: skill["base_damage"].get(dtype, 0.0)}
        built, built_apps = build_hit_components(
            contributions=contributions,
            skill=event_skill,
            component=template,
            crit_rules=dict(crit_rules),
            target=dict(target),
            states=dict(states),
            target_info=dict(target_info),
            typed_added_policy="by_type",
            isolate_damage_channels=True,
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
                    if component["damage_type"] == str(entry.get("damage_type") or entry.get("adaptive_damage_type"))
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
    counted_events: set[str] = set()
    for component in components:
        damage = float(component.get("damage") or 0.0)
        weights = [float(value) for value in component.get("damage_weights") or [1.0]]
        hits = int(component.get("hits_per_cast") or 1)
        first_hit += damage * weights[0]
        per_cast += damage * sum(weights)
        # 多系分量共享同一击；伤害相加，事件次数不重复增加。
        event_id = str(component.get("declared_component_id") or component.get("component_id"))
        if event_id not in counted_events:
            counted_events.add(event_id)
            hits_per_cast += hits
            effective += sum(weights)
    return {
        "single_hit": first_hit,
        "per_cast_hit": per_cast,
        "hits_per_cast": hits_per_cast,
        "effective_hits_per_cast": effective,
    }


# ---------------------------------------------------------------------------
# 频率策略
# ---------------------------------------------------------------------------


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


def rotation_factor(plan: Mapping[str, Any]) -> tuple[float, dict[str, Any]]:
    rotation = (plan.get("schedule") or {}).get("rotation") or {}
    factor = float(rotation.get("factor") or 1.0)
    if not math.isfinite(factor) or factor <= 0:
        raise ValueError("schedule.rotation.factor 必须是正数")
    return factor, dict(rotation)


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


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------


def execute_plan_v2(
    plan: dict[str, Any],
    scenario: Any,
    runtime: Optional[plan_handlers.PlanRuntime] = None,
) -> dict[str, Any]:
    """执行 plan-v2；公开入口仍是 ``plan_executor.execute_plan``（按 schema 分派）。"""
    runtime = runtime or plan_handlers.PlanRuntime()
    issues_validation = plan_executor.validate_plan(plan, runtime)
    if issues_validation:
        raise plan_executor.PlanValidationError(issues_validation)
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
        rows, _reasons, notes = plan_executor.build_contributions(
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
        raise plan_executor.PlanValidationError(
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
    rows, _reasons, notes = plan_executor.build_contributions(
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


__all__ = [
    "SCHEMA_RESULT_V4",
    "build_hit_components_v2",
    "component_hit_totals",
    "execute_plan_v2",
    "resolve_evaluation_scenarios",
    "rotation_factor",
    "steady_state_frequency",
]
