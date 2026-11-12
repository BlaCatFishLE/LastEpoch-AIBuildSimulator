"""plan-v4 / result-v6：受限通用能力外壳（G3/G4）与数据化报告（G5）。

本模块只处理**显式数据定义**的能力组合：hit 组件、dot 组件、具名 application
批次、charge 调度。VC/FR/BH 的既有 v1/v2/v3 计划仍走各自执行器，不被本模块
静默改写；v4 是新增版本，不向旧计划插字段。

设计边界见 docs/contracts/calculation-plan-v4.md。
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
from typing import Any, Mapping

from . import calculation_plan as planmod
from . import dps_result
from . import plan_handlers
from . import v4_execution
from .mechanics import execution as mechanics_execution
from .mechanics import target_side as targetside

SCHEMA_PLAN = "le-calculation-plan-v4"
SCHEMA_RESULT = "le-dps-result-v6"
PLAN_VERSION = "4.0.0"

STRATEGIES = (targetside.PENETRATION_SEPARATE, targetside.PENETRATION_FOLD)
NEGATIVE_POLICIES = (targetside.NEGATIVE_ALLOW, targetside.NEGATIVE_CLAMP_AT_ZERO)
DAMAGE_CHANNELS = {
    "Physical": "physical",
    "Fire": "fire",
    "Cold": "cold",
    "Lightning": "lightning",
    "Void": "void",
    "Necrotic": "necrotic",
    "Poison": "poison",
}
SOURCE_SUCCESS_STATUSES = {"ok", "verified", "bound", "mapped", "applied"}

CAPABILITIES = (
    "hit_components",
    "dot_components",
    "applications",
    "charge_schedule",
    "presentation",
    # 通用 actor/派生频率扩展（数据声明式；v1/v2/v3 不声明即行为不变）
    "minion_actor",
    "explicit_stat_transfer",
    "derived_event_rate",
)
#: 能力矩阵：调度/组件组合 -> 需要声明的能力；超矩阵在编译期 capability_gap。
CAPABILITY_MATRIX = {
    "hit_components": ("hit_components",),
    "dot_components": ("dot_components",),
    "applications": ("applications",),
    "charge_schedule": ("charge_schedule",),
    "single_cast": ("hit_components",),
    "minion_actor": ("minion_actor",),
    "explicit_stat_transfer": ("explicit_stat_transfer",),
    "derived_event_rate": ("derived_event_rate",),
}
#: 旧计划版本的命名兼容目标侧策略（只登记口径，不向旧计划插字段）。
COMPATIBILITY_TARGET_POLICIES = {
    "le-calculation-plan-v1": {"default": "legacy_profile_fold_negative_allow"},
    "le-calculation-plan-v2": {"fr": "fr_separate_multiplier_table_dr"},
    "le-calculation-plan-v3": {"bh": "bh_fold_scenario_dr"},
}


class PlanV4Error(ValueError):
    pass


def _finite(value: Any, *, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PlanV4Error(f"{label} 必须是有限数值")
    result = float(value)
    if not math.isfinite(result):
        raise PlanV4Error(f"{label} 不能是 NaN/Inf")
    return result


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlanV4Error(f"{label} 必须是对象")
    return value


def documentation_for_plan_v4() -> dict[str, Any]:
    return {
        "schema": SCHEMA_PLAN,
        "plan_version": PLAN_VERSION,
        "capabilities": list(CAPABILITIES),
        "capability_matrix": {key: list(value) for key, value in CAPABILITY_MATRIX.items()},
        "compatibility_target_policies": copy.deepcopy(COMPATIBILITY_TARGET_POLICIES),
    }


def compatibility_target_policy(plan_schema: str, ability_id: str | None = None) -> str | None:
    mapping = COMPATIBILITY_TARGET_POLICIES.get(str(plan_schema))
    if not mapping:
        return None
    if len(mapping) == 1:
        return next(iter(mapping.values()))
    return mapping.get(str(ability_id or ""))


def _normalize_components(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = definition.get("components")
    if not isinstance(raw, list) or not raw:
        raise PlanV4Error("v4 定义必须声明非空 components")
    components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item = _require_object(item, f"components[{index}]")
        component_id = str(item.get("component_id") or "").strip()
        if not component_id or component_id in seen:
            raise PlanV4Error(f"components[{index}].component_id 缺失或重复")
        seen.add(component_id)
        kind = str(item.get("kind") or "")
        if kind not in ("hit", "dot"):
            raise PlanV4Error(f"components[{index}].kind 只支持 hit/dot")
        damage_type = str(item.get("damage_type") or "")
        if damage_type not in DAMAGE_CHANNELS:
            raise PlanV4Error(f"components[{index}].damage_type 不受支持：{damage_type!r}")
        base = _finite(item.get("base_damage"), label=f"components[{index}].base_damage")
        added = _finite(item.get("added_damage", 0.0), label=f"components[{index}].added_damage")
        added_scaling = _finite(
            item.get("added_damage_scaling", 0.0), label=f"components[{index}].added_damage_scaling"
        )
        increased = _finite(item.get("increased", 0.0), label=f"components[{index}].increased")
        more = _finite(item.get("more", 1.0), label=f"components[{index}].more")
        crit_chance = _finite(item.get("crit_chance", 0.0), label=f"components[{index}].crit_chance")
        crit_multiplier = _finite(
            item.get("crit_multiplier", 1.0), label=f"components[{index}].crit_multiplier"
        )
        if not 0.0 <= crit_chance <= 1.0:
            raise PlanV4Error(f"components[{index}].crit_chance 必须在 [0,1]")
        if crit_multiplier < 1.0:
            raise PlanV4Error(f"components[{index}].crit_multiplier 必须 >=1")
        component = {
            "component_id": component_id,
            "kind": kind,
            "damage_type": damage_type,
            "tags": [str(tag) for tag in item.get("tags") or []],
            "base_damage": base,
            "added_damage": added,
            "added_damage_scaling": added_scaling,
            "increased": increased,
            "more": more,
            "crit_chance": crit_chance,
            "crit_multiplier": crit_multiplier,
            "crit_capable": bool(item.get("crit_capable", kind == "hit")),
            "target_ref": str(item.get("target_ref") or component_id),
            "presentation_label": str(item.get("presentation_label") or component_id),
        }
        if kind == "dot":
            interval = _finite(
                item.get("tick_interval_s"), label=f"components[{index}].tick_interval_s"
            )
            count = int(_finite(item.get("tick_count"), label=f"components[{index}].tick_count"))
            duration = _finite(item.get("duration_s"), label=f"components[{index}].duration_s")
            if interval <= 0 or count < 1 or duration <= 0:
                raise PlanV4Error(f"components[{index}] DoT tick 参数非法")
            if abs(interval * count - duration) > 1e-9:
                raise PlanV4Error(f"components[{index}] tick_interval_s×tick_count 必须等于 duration_s")
            component.update({"tick_interval_s": interval, "tick_count": count, "duration_s": duration})
        components.append(component)
    return components


def _normalize_applications(definition: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = definition.get("applications") or []
    if not isinstance(raw, list):
        raise PlanV4Error("applications 必须是数组")
    applications: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        item = _require_object(item, f"applications[{index}]")
        application_id = str(item.get("application_id") or "").strip()
        if not application_id or application_id in seen:
            raise PlanV4Error(f"applications[{index}].application_id 缺失或重复")
        seen.add(application_id)
        ailment_id = str(item.get("ailment_id") or "").strip()
        if not ailment_id:
            raise PlanV4Error(f"applications[{index}].ailment_id 缺失")
        damage_type = str(item.get("damage_type") or "")
        if damage_type not in DAMAGE_CHANNELS:
            raise PlanV4Error(f"applications[{index}].damage_type 不受支持")
        duration = _finite(item.get("duration_s"), label=f"applications[{index}].duration_s")
        if duration <= 0:
            raise PlanV4Error(f"applications[{index}].duration_s 必须 >0")
        clock = _require_object(item.get("clock"), f"applications[{index}].clock")
        first_at = _finite(clock.get("first_at_s", 0.0), label=f"applications[{index}].clock.first_at_s")
        interval = _finite(
            clock.get("interval_s"), label=f"applications[{index}].clock.interval_s"
        )
        count = int(_finite(clock.get("count"), label=f"applications[{index}].clock.count"))
        if interval <= 0 or count < 1 or first_at < 0:
            raise PlanV4Error(f"applications[{index}].clock 参数非法")
        amount = _require_object(item.get("amount"), f"applications[{index}].amount")
        if amount.get("rule") != "fixed_expected_stacks":
            raise PlanV4Error(f"applications[{index}].amount.rule 只支持 fixed_expected_stacks")
        stacks = _finite(amount.get("stacks_per_roll"), label=f"applications[{index}].stacks_per_roll")
        if stacks < 0:
            raise PlanV4Error(f"applications[{index}].stacks_per_roll 不能为负")
        base = _finite(item.get("base_damage"), label=f"applications[{index}].base_damage")
        tick_interval = _finite(
            item.get("tick_interval_s", duration / 4.0),
            label=f"applications[{index}].tick_interval_s",
        )
        if tick_interval <= 0:
            raise PlanV4Error(f"applications[{index}].tick_interval_s 必须 >0")
        tick_count = int(round(duration / tick_interval))
        if tick_count < 1:
            raise PlanV4Error(f"applications[{index}] tick 数非法")
        applications.append(
            {
                "application_id": application_id,
                "ailment_id": ailment_id,
                "damage_type": damage_type,
                "scaling_tags": [str(tag) for tag in item.get("scaling_tags") or []],
                "base_damage": base,
                "duration_s": duration,
                "duration_policy": "full_ticks",
                "clock": {"kind": "periodic", "first_at_s": first_at, "interval_s": interval, "count": count},
                "amount": {"rule": "fixed_expected_stacks", "stacks_per_roll": stacks},
                "target_ref": str(item.get("target_ref") or application_id),
                "presentation_label": str(item.get("presentation_label") or ailment_id),
                "source": copy.deepcopy(item.get("source") or {}),
                "tick_interval_s": tick_interval,
                "tick_count": tick_count,
            }
        )
    return applications


def _normalize_schedule(definition: Mapping[str, Any]) -> dict[str, Any]:
    schedule = _require_object(definition.get("schedule"), "schedule")
    kind = str(schedule.get("kind") or "")
    if kind not in ("single_cast", "charge"):
        raise PlanV4Error("schedule.kind 只支持 single_cast/charge")
    horizon = _finite(schedule.get("horizon_s"), label="schedule.horizon_s")
    if horizon <= 0:
        raise PlanV4Error("schedule.horizon_s 必须 >0")
    windows = [_finite(item, label="schedule.windows_s[]") for item in schedule.get("windows_s") or []]
    if not windows or sorted(windows) != windows or any(value <= 0 for value in windows):
        raise PlanV4Error("schedule.windows_s 必须是非空升序正数数组")
    if not any(abs(value - horizon) < 1e-9 for value in windows):
        raise PlanV4Error("schedule.windows_s 必须包含 horizon_s")
    evaluations = definition.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        raise PlanV4Error("v4 定义必须声明 evaluations")
    primary = sum(1 for item in evaluations if isinstance(item, dict) and item.get("primary"))
    if primary != 1:
        raise PlanV4Error("v4 evaluations 必须恰好一个 primary=true")
    result = {
        "kind": kind,
        "horizon_s": horizon,
        "windows_s": windows,
        "cast_time_s": _finite(schedule.get("cast_time_s", 0.0), label="schedule.cast_time_s"),
        "evaluations": [
            {
                "evaluation_id": str(item.get("evaluation_id")),
                "primary": bool(item.get("primary")),
                "action_policy": item.get("action_policy"),
                "label": item.get("label") or item.get("evaluation_id"),
            }
            for item in evaluations
        ],
    }
    if kind == "charge":
        charges = _require_object(schedule.get("charges"), "schedule.charges")
        result["charges"] = {
            "max_charges": int(_finite(charges.get("max_charges"), label="schedule.charges.max_charges")),
            "recharge_s": _finite(charges.get("recharge_s"), label="schedule.charges.recharge_s"),
            "cast_time_s": _finite(
                charges.get("cast_time_s", schedule.get("cast_time_s", 0.0)),
                label="schedule.charges.cast_time_s",
            ),
            "instance_duration_s": _finite(
                charges.get("instance_duration_s"), label="schedule.charges.instance_duration_s"
            ),
        }
        if result["charges"]["max_charges"] < 1 or result["charges"]["recharge_s"] <= 0:
            raise PlanV4Error("schedule.charges 参数非法")
        policies = schedule.get("action_policies")
        if not isinstance(policies, list) or not policies:
            raise PlanV4Error("charge 调度必须声明 action_policies")
        normalized = []
        seen = set()
        for index, item in enumerate(policies):
            item = _require_object(item, f"schedule.action_policies[{index}]")
            policy_id = str(item.get("policy_id") or "")
            condition = str(item.get("condition") or "")
            if not policy_id or policy_id in seen:
                raise PlanV4Error("action_policies.policy_id 缺失或重复")
            seen.add(policy_id)
            if condition not in (
                "charge_available_and_action_free",
                "wait_for_all_instances_expired",
            ):
                raise PlanV4Error(f"action_policies[{index}].condition 不受支持：{condition!r}")
            normalized.append({"policy_id": policy_id, "condition": condition})
        result["action_policies"] = normalized
    return result


def _target_side_for_component(
    definition: Mapping[str, Any],
    scenario: Mapping[str, Any],
    component: Mapping[str, Any],
) -> dict[str, Any]:
    target = _require_object(scenario.get("target") or {}, "scenario.target")
    channels = target.get("resistances") if isinstance(target.get("resistances"), dict) else {}
    channel = DAMAGE_CHANNELS[str(component.get("damage_type"))]
    configured = ((definition.get("target_side") or {}).get("components") or {}).get(
        str(component.get("component_id"))
    ) or {}
    resistance = _finite(
        configured.get("resistance", channels.get(channel, target.get("resistance", 0.0))),
        label=f"target_side.{component.get('component_id')}.resistance",
    )
    penetration = _finite(
        configured.get("penetration", target.get("penetration", 0.0)),
        label=f"target_side.{component.get('component_id')}.penetration",
    )
    damage_reduction = _finite(
        configured.get(
            "damage_reduction",
            target.get("damage_reduction", target.get("monster_damage_reduction", 0.0)),
        ),
        label=f"target_side.{component.get('component_id')}.damage_reduction",
    )
    if not 0.0 <= damage_reduction <= 1.0:
        raise PlanV4Error("target_side.damage_reduction 必须在 [0,1]")
    strategy = str((definition.get("target_side") or {}).get("strategy") or configured.get("strategy") or "")
    if strategy not in STRATEGIES:
        raise PlanV4Error(f"target_side.strategy 不受支持：{strategy!r}")
    negative_policy = str(
        configured.get("negative_resistance_policy")
        or (definition.get("target_side") or {}).get("negative_resistance_policy")
        or targetside.NEGATIVE_ALLOW
    )
    if negative_policy not in NEGATIVE_POLICIES:
        raise PlanV4Error(f"target_side.negative_resistance_policy 不受支持：{negative_policy!r}")
    try:
        policy = targetside.TargetSidePolicy(
            penetration_mode=strategy,
            negative_resistance_clamp=negative_policy,
            dr_source=targetside.DR_FROM_SCENARIO,
        )
        resolved = targetside.compute_target_multiplier(
            policy=policy,
            resistance=resistance,
            monster_damage_reduction=damage_reduction,
            penetration=penetration,
        )
    except targetside.TargetSideError as exc:
        raise PlanV4Error(str(exc)) from exc
    return {
        "ref": str(component.get("target_ref") or component.get("component_id")),
        "damage_channel": channel,
        "resistance": resistance,
        "penetration": penetration,
        "damage_reduction": damage_reduction,
        "strategy": strategy,
        "negative_resistance_policy": negative_policy,
        "unit": "fraction",
        **resolved.to_dict(),
    }


def _presentation(definition: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    raw = definition.get("presentation")
    if not isinstance(raw, dict):
        raise PlanV4Error("v4 定义必须声明 presentation（数据化报告标签）")
    title = str(raw.get("title") or "").strip()
    if not title:
        raise PlanV4Error("presentation.title 缺失")
    return {
        "title": title,
        "ability_id": str(case.get("main_skill", {}).get("ability_id") or definition.get("ability_id") or ""),
        "labels": copy.deepcopy(raw.get("labels") or {}),
        "notes": [str(item) for item in raw.get("notes") or []],
    }


def compile_plan_v4(
    template: Mapping[str, Any],
    case: Mapping[str, Any],
    scenario: Mapping[str, Any],
    *,
    input_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """从数据定义编译 v4 计划；不读取历史案例参数，不做技能名分支。"""
    definition = _require_object(template.get("definition"), "template.definition")
    plan_version = str(definition.get("plan_version") or "")
    if plan_version != SCHEMA_PLAN:
        raise PlanV4Error(f"definition.plan_version 必须是 {SCHEMA_PLAN}")
    allowed_definition_keys = {
        "plan_version", "skill", "controls", "state_sources", "effects", "node_bindings",
        "rules", "attribute_conversion", "ailments", "coverage", "limitations", "evidence",
        "components", "applications", "schedule", "evaluations", "capabilities",
        "presentation", "target_side",
    }
    unknown_definition = sorted(set(definition) - allowed_definition_keys)
    if unknown_definition:
        raise PlanV4Error(f"v4 定义含未支持字段：{unknown_definition}")
    if definition.get("rules"):
        raise PlanV4Error("v4 生产规则/来源绑定尚未实现；rules 必须为空（受控 helper 不得绕过）")
    if definition.get("effects") or definition.get("node_bindings"):
        raise PlanV4Error("v4 生产来源/selector 绑定尚未实现；effects/node_bindings 必须为空")
    components = _normalize_components(definition)
    applications = _normalize_applications(definition)
    schedule = _normalize_schedule(definition)
    declared = definition.get("capabilities")
    if not isinstance(declared, list) or not declared:
        raise PlanV4Error("v4 定义必须声明 capabilities")
    unknown = sorted(set(str(item) for item in declared) - set(CAPABILITIES))
    if unknown:
        raise PlanV4Error(f"capability_gap: 未知能力 {unknown}")
    needed: set[str] = set()
    if any(component["kind"] == "hit" for component in components):
        needed.add("hit_components")
    if any(component["kind"] == "dot" for component in components):
        needed.add("dot_components")
    if applications:
        needed.add("applications")
    if schedule["kind"] == "charge":
        needed.add("charge_schedule")
    if not needed <= {str(item) for item in declared}:
        raise PlanV4Error(f"capability_gap: 能力矩阵缺少 {sorted(needed - {str(item) for item in declared})}")
    target_entries: dict[str, dict[str, Any]] = {}
    for component in [*components, *applications]:
        target = _target_side_for_component(definition, scenario, component)
        if target["ref"] in target_entries:
            raise PlanV4Error(f"target_side 组件引用重复：{target['ref']}")
        target_entries[target["ref"]] = target
    coverage = definition.get("coverage") if isinstance(definition.get("coverage"), dict) else {}
    coverage_document = {
        "status": str(coverage.get("status") or ("partial" if coverage.get("gaps") else "complete")),
        "gap_count": len(coverage.get("gaps") or []),
        "gaps": copy.deepcopy(coverage.get("gaps") or []),
    }
    target_document = {
        "unit": "fraction",
        "strategy": str((definition.get("target_side") or {}).get("strategy")),
        "components": target_entries,
    }
    scenario_identity = {
        "states": copy.deepcopy(scenario.get("states") or {}),
        "target": copy.deepcopy(scenario.get("target") or {}),
    }
    plan: dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "version": PLAN_VERSION,
        "subject": {
            "case_id": str(case.get("case_id")),
            "main_ability_id": str((case.get("main_skill") or {}).get("ability_id")),
            "build_id": (case.get("build") or {}).get("build_id"),
            "revision_id": (case.get("build") or {}).get("revision_id"),
        },
        "case_ref": str(case.get("_path") or case.get("case_ref") or ""),
        "scenario": scenario_identity,
        "primary_evaluation": next(
            item["evaluation_id"] for item in schedule["evaluations"] if item["primary"]
        ),
        "capabilities": [str(item) for item in declared],
        "components": components,
        "applications": applications,
        "schedule": schedule,
        "target_side": target_document,
        "presentation": _presentation(definition, case),
        "coverage": coverage_document,
        "issues": copy.deepcopy(definition.get("issues") or []),
        "assumptions": [str(item) for item in definition.get("assumptions") or []],
        "input_refs": list(input_refs or []),
        "limitations": [str(item) for item in definition.get("limitations") or []],
        "controlled_helper": not bool(input_refs),
    }
    plan["scenario_sha256"] = planmod.scenario_sha256(plan["scenario"])
    plan["execution_fingerprint"] = _sha256_text(
        _canonical(
            {
                "plan": {k: v for k, v in plan.items() if k not in ("plan_sha256", "execution_fingerprint")},
                "scenario_sha256": plan["scenario_sha256"],
            }
        )
    )
    plan["plan_sha256"] = _sha256_text(_canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))
    validate_plan_v4(plan)
    return plan


def _legacy_plan_version(model: Any) -> str:
    definition = getattr(model, "definition", {}) or {}
    version = str(definition.get("assembly_plan_version") or "le-calculation-plan-v1")
    if version == "v4-actor":
        # v4-actor 复用 legacy v1 装配形状（hit+dot/effects/sources），actor 差异由
        # plan_compiler 内的原生装配阶段产生；计划 schema 仍是 v1 以复用既有执行器。
        return "le-calculation-plan-v1"
    if version not in ("le-calculation-plan-v1", "le-calculation-plan-v2", "le-calculation-plan-v3"):
        raise PlanV4Error(f"v4 assembly_plan_version 不支持：{version!r}")
    return version


def _scenario_inputs_from_plan(legacy_plan: Mapping[str, Any], root: Path) -> dict[str, dict[str, Any]]:
    controls = [item for item in legacy_plan.get("controls") or [] if isinstance(item, dict)]
    target_defaults = {
        str(item.get("key")): item.get("default")
        for item in controls
        if item.get("namespace") == "target"
    }
    inputs: dict[str, dict[str, Any]] = {}
    for evaluation in legacy_plan.get("evaluations") or []:
        path = str(evaluation.get("scenario_path") or "")
        if not path:
            continue
        document = json.loads((root / path).read_text(encoding="utf-8"))
        states = {str(c["key"]): c.get("default") for c in controls if c.get("namespace") == "state"}
        states.update(document.get("controls") or {})
        target = dict(target_defaults)
        if document.get("monster_level") is not None:
            target["monster_level"] = document["monster_level"]
        if document.get("resistance") is not None:
            target["resistance"] = document["resistance"]
        for key, value in (document.get("target") or {}).items():
            target[key] = value
        digest = planmod.scenario_sha256({"states": states, "target": target})
        inputs[str(evaluation.get("evaluation_id"))] = {
            "evaluation_id": str(evaluation.get("evaluation_id")),
            "scenario": {"states": states, "target": target},
            "scenario_path": path,
            "scenario_sha256": digest,
        }
    return inputs


def compile_from_resolved_model(context: Any, resolved_model: Any) -> Any:
    """真实装配的 v4 封装：按模板声明的 assembly_plan_version 复用 v1/v2/v3 装配器。"""
    from . import plan_compiler  # noqa: PLC0415

    legacy_version = _legacy_plan_version(resolved_model)
    legacy_model = copy.deepcopy(resolved_model)
    legacy_model.definition["plan_version"] = legacy_version
    legacy = plan_compiler.compile_from_resolved_model(context, legacy_model)
    if not legacy.ok or legacy.plan is None:
        raise PlanV4Error(
            "v4 assembly 装配失败：" + "；".join(str(item.get("message")) for item in legacy.diagnostics[:3])
        )
    legacy_plan = legacy.plan
    legacy_schema = str(legacy_plan.get("schema"))
    evaluations_meta = list(legacy_plan.get("evaluations") or [])
    if not evaluations_meta:
        evaluations_meta = [{"evaluation_id": "single_cast", "primary": True, "label": "single cast"}]
    primary = next((item for item in evaluations_meta if item.get("primary")), evaluations_meta[0])
    primary_id = str(primary.get("evaluation_id"))
    controls = list(legacy_plan.get("controls") or [])
    states = {
        str(control.get("key")): control.get("default")
        for control in controls
        if control.get("namespace") == "state"
    }
    target_defaults = {
        str(control.get("key")): control.get("default")
        for control in controls
        if control.get("namespace") == "target"
    }
    windows = [float(item) for item in (legacy_plan.get("schedule") or {}).get("windows_s") or []]
    legacy_scenarios = _scenario_inputs_from_plan(legacy_plan, context.root)
    primary_scenario = legacy_scenarios.get(primary_id, {}).get("scenario") or {
        "states": states, "target": target_defaults
    }
    components = []
    for component in legacy_plan.get("components") or []:
        components.append(
            {
                "component_id": str(component.get("component_id")),
                "kind": str(component.get("kind") or ""),
                "damage_type": component.get("adaptive_damage_type"),
                "tags": list(component.get("tags") or []),
                "base_damage": 0.0,
                "added_damage": 0.0,
                "added_damage_scaling": 0.0,
                "increased": 0.0,
                "more": 1.0,
                "crit_chance": 0.0,
                "crit_multiplier": 1.0,
                "crit_capable": bool(component.get("crit_capable")),
                "target_ref": str(component.get("component_id")),
                "presentation_label": component.get("adaptive_damage_type"),
            }
        )
    target_entries = {}
    for component in components:
        channel = str(component.get("damage_type") or "physical").lower()
        resistance = float((primary_scenario.get("target") or {}).get("resistance") or 0.0)
        damage_reduction = float((primary_scenario.get("target") or {}).get("monster_damage_reduction") or 0.0)
        policy = targetside.TargetSidePolicy(
            penetration_mode="separate_multiplier",
            negative_resistance_clamp="allow",
            dr_source=targetside.DR_FROM_SCENARIO,
        )
        resolved = targetside.compute_target_multiplier(
            policy=policy, resistance=resistance, monster_damage_reduction=damage_reduction, penetration=0.0
        )
        target_entries[str(component["component_id"])] = {
            "ref": str(component["component_id"]),
            "damage_channel": channel,
            "resistance": resistance,
            "penetration": 0.0,
            "damage_reduction": damage_reduction,
            "strategy": "separate_multiplier",
            "negative_resistance_policy": "allow",
            "unit": "fraction",
            "effective_resistance": resolved.effective_resistance,
            "resistance_multiplier": resolved.resistance_multiplier,
            "monster_damage_reduction": resolved.monster_damage_reduction,
            "monster_dr_factor": resolved.monster_dr_factor,
            "multiplier": resolved.multiplier,
            "penetration_multiplier": resolved.penetration_multiplier,
            "penetration_applied": resolved.penetration_applied,
            "policy": resolved.policy.to_dict(),
        }
    legacy_facts = legacy_plan.get("facts") or {}
    actor_model = copy.deepcopy(legacy_facts.get("actor_model") or {})
    if actor_model:
        actor_id = str(actor_model.get("actor_id") or "")
        actor_scope = str(actor_model.get("scope") or "actor")
        for component in components:
            component["actor_id"] = actor_id
            component["scope"] = actor_scope
        for ref, entry in target_entries.items():
            entry["actor_id"] = actor_id
            entry["scope"] = actor_scope
    if legacy_schema == "le-calculation-plan-v2":
        capabilities = ["hit_components", "applications", "presentation"]
    elif legacy_schema == "le-calculation-plan-v3":
        capabilities = ["hit_components", "dot_components", "applications", "charge_schedule", "presentation"]
    else:
        capabilities = ["hit_components", "presentation"]
        if any(component["kind"] == "dot" for component in components):
            capabilities.append("dot_components")
        if legacy_plan.get("ailments"):
            capabilities.append("applications")
    for declared_capability in legacy_facts.get("declared_capabilities") or []:
        name = str(declared_capability)
        if name in CAPABILITIES and name not in capabilities:
            capabilities.append(name)
    skill_name = str((context.case.get("main_skill") or {}).get("name") or "")
    plan: dict[str, Any] = {
        "schema": SCHEMA_PLAN,
        "version": PLAN_VERSION,
        "subject": copy.deepcopy(legacy_plan.get("subject") or {}),
        "case_ref": str((context.case or {}).get("_path") or ""),
        "scenario": {"states": states, "target": target_defaults},
        "primary_evaluation": primary_id,
        "capabilities": capabilities,
        "components": components,
        "applications": [],
        "schedule": {
            "kind": "assembly",
            "horizon_s": max(windows) if windows else 0.0,
            "windows_s": windows,
            "cast_time_s": float((legacy_plan.get("schedule") or {}).get("action_s") or 0.0),
            "evaluations": [
                {
                    "evaluation_id": str(item.get("evaluation_id")),
                    "primary": bool(item.get("primary")),
                    "action_policy": item.get("action_policy"),
                    "label": str(item.get("label") or item.get("evaluation_id")),
                }
                for item in evaluations_meta
            ],
        },
        "target_side": {"unit": "fraction", "strategy": "separate_multiplier", "components": target_entries},
        "presentation": {
            "title": skill_name or str(legacy_plan.get("subject", {}).get("main_ability_id") or "DPS"),
            "subtitle": "v4 真实装配（assembly-backed）",
        },
        "coverage": {"status": "from_execution", "gap_count": 0, "gaps": []},
        "issues": copy.deepcopy(legacy_plan.get("assumptions") or []),
        "assumptions": copy.deepcopy(legacy_plan.get("assumptions") or []),
        "input_refs": copy.deepcopy(context.input_refs),
        "limitations": copy.deepcopy(legacy_plan.get("limitations") or []),
        "assembly": {
            "kind": "definitions_v3_legacy_assembly",
            "legacy_schema": legacy_schema,
            "legacy_plan": legacy_plan,
            "legacy_scenarios": legacy_scenarios,
        },
        "execution": (
            {
                **copy.deepcopy(legacy_plan),
                "engine": "mechanics_static_v1",
                "source_contract": "definitions_v3.real_binding",
                "strategies": {
                    "frequency": (legacy_plan.get("schedule") or {}).get("driver"),
                    "crit": (legacy_plan.get("parameters") or {}).get("crit"),
                    "target": "legacy_ruleset_public_handlers",
                },
            }
            if legacy_schema in (
                "le-calculation-plan-v1", "le-calculation-plan-v2", "le-calculation-plan-v3"
            )
            else None
        ),
        "controlled_helper": False,
    }
    if plan.get("execution"):
        if legacy_schema == "le-calculation-plan-v1":
            plan["execution"]["execution_v1"] = _build_execution_v1(
                legacy_plan, components, target_entries, windows, evaluations_meta
            )
            plan["execution"]["execution_v1"]["capabilities"] = list(capabilities)
        elif legacy_schema == "le-calculation-plan-v2":
            plan["execution"]["execution_v1"] = _build_execution_v1_fr(legacy_plan, components, windows)
            plan["execution"]["execution_v1"]["capabilities"] = list(capabilities)
        elif legacy_schema == "le-calculation-plan-v3":
            plan["execution"]["execution_v1"] = _build_execution_v1_bh(legacy_plan, components, windows)
            plan["execution"]["execution_v1"]["capabilities"] = list(capabilities)
        plan["execution_sha256"] = _sha256_text(_canonical(plan["execution"]))
    plan["scenario_sha256"] = planmod.scenario_sha256(plan["scenario"])
    plan["execution_fingerprint"] = _sha256_text(
        _canonical({"plan": plan, "scenario_sha256": plan["scenario_sha256"]})
    )
    plan["plan_sha256"] = _sha256_text(_canonical({key: value for key, value in plan.items() if key != "plan_sha256"}))
    validate_plan_v4(plan)
    return plan_compiler.CompileResult("completed", "ready", plan, [])


def _apply_scenario_overrides(
    legacy_scenarios: Mapping[str, Any], scenario: Mapping[str, Any]
) -> list[dict[str, Any]]:
    if "evaluations" in scenario:
        rows = []
        for item in scenario["evaluations"]:
            rows.append({"evaluation_id": str(item["evaluation_id"]), "scenario": item.get("scenario") or item})
        return rows
    rows = []
    for evaluation_id, entry in legacy_scenarios.items():
        base = copy.deepcopy(entry.get("scenario") or {"states": {}, "target": {}})
        base["states"] = {**(base.get("states") or {}), **dict(scenario.get("states") or {})}
        base["target"] = {**(base.get("target") or {}), **dict(scenario.get("target") or {})}
        rows.append({"evaluation_id": evaluation_id, "scenario": base})
    return rows


def _assembly_target_side(plan: Mapping[str, Any], legacy_result: Mapping[str, Any]) -> dict[str, Any]:
    target = legacy_result.get("target") or {}
    resistance = float(target.get("fire_resistance") or target.get("resistance") or 0.0)
    damage_reduction = float(target.get("monster_damage_reduction") or 0.0)
    base = copy.deepcopy(plan.get("target_side") or {"unit": "fraction", "components": {}})
    for ref, entry in (base.get("components") or {}).items():
        policy = targetside.TargetSidePolicy(
            penetration_mode=str(entry["strategy"]),
            negative_resistance_clamp=str(entry["negative_resistance_policy"]),
            dr_source=targetside.DR_FROM_SCENARIO,
        )
        resolved = targetside.compute_target_multiplier(
            policy=policy,
            resistance=resistance,
            monster_damage_reduction=damage_reduction,
            penetration=float(entry.get("penetration") or 0.0),
        )
        entry.update(
            {
                "resistance": resistance,
                "damage_reduction": damage_reduction,
                "effective_resistance": resolved.effective_resistance,
                "resistance_multiplier": resolved.resistance_multiplier,
                "monster_damage_reduction": resolved.monster_damage_reduction,
                "monster_dr_factor": resolved.monster_dr_factor,
                "multiplier": resolved.multiplier,
                "penetration_multiplier": resolved.penetration_multiplier,
                "penetration_applied": resolved.penetration_applied,
                "policy": resolved.policy.to_dict(),
            }
        )
    return base


def _map_legacy_result_to_v6(
    plan: Mapping[str, Any], legacy_result: Mapping[str, Any], legacy_schema: str
) -> dict[str, Any]:
    scenario = copy.deepcopy(legacy_result.get("scenario") or {})
    scenario_sha = planmod.scenario_sha256(
        {"states": scenario.get("states") or {}, "target": scenario.get("target") or {}}
    )
    execution = _sha256_text(_canonical({"plan": plan.get("execution_fingerprint"), "scenario_sha256": scenario_sha}))
    contributions = copy.deepcopy(legacy_result.get("contributions") or [])
    applications_raw = copy.deepcopy(legacy_result.get("applications") or [])
    applications_ledger = []
    for index, application in enumerate(applications_raw):
        applications_ledger.append(
            {
                "application_id": f"legacy:{index}",
                "component_id": str(application.get("component_id") or ""),
                "contribution_id": str(application.get("contribution_id") or ""),
                "operation": application.get("operation"),
                "status": application.get("status"),
                "role": application.get("role"),
                "reason": application.get("reason"),
                "stat_key": application.get("stat_key"),
                "damage_total": 0.0,
                "presentation_label": "legacy application",
                "cohorts": [],
            }
        )
    policies: list[dict[str, Any]] = []
    evaluations: list[dict[str, Any]] = []
    windows: list[dict[str, Any]] = []
    components: list[dict[str, Any]] = []
    if legacy_schema == "le-calculation-plan-v2":
        legacy_policies = legacy_result.get("policies") or {}
        for policy_id, policy in legacy_policies.items():
            policy_windows = [
                {
                    "window_s": float(item.get("window_s") or item.get("seconds") or 0.0),
                    "hit_damage": float(item.get("hit_damage") or 0.0),
                    "dot_damage": float(item.get("dot_damage") or 0.0),
                    "application_damage": 0.0,
                    "total_damage": float(item.get("total_damage") or item.get("damage") or 0.0),
                    "dps": float(item.get("dps") or 0.0),
                    "cast_count": int(item.get("complete_casts") or item.get("casts") or 0),
                    "unsettled_tail_damage": 0.0,
                }
                for item in policy.get("windows") or []
            ]
            last = policy_windows[-1] if policy_windows else {}
            totals = {
                "horizon_damage": last.get("total_damage", 0.0),
                "horizon_dps": float(policy.get("damage_per_second") or 0.0),
                "hit_damage": float(policy.get("damage_per_cast") or 0.0),
                "dot_damage": 0.0,
                "application_damage": 0.0,
                "unsettled_tail_damage": 0.0,
                "cast_count": last.get("cast_count", 0),
                "max_concurrent": 1,
            }
            policies.append({"policy_id": str(policy_id), "totals": totals, "primary": True})
            evaluations.append(
                {
                    "evaluation_id": str(policy_id),
                    "policy_id": str(policy_id),
                    "label": str(policy.get("binding_constraint") or policy_id),
                    "scenario_sha256": scenario_sha,
                    **{key: totals[key] for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent")},
                }
            )
        primary_policy = str((legacy_result.get("evaluations") or [{}])[-1].get("primary_policy") or policies[0]["policy_id"] if policies else "assembly")
        selected = next((item for item in policies if item["policy_id"] == primary_policy), policies[0] if policies else None)
        windows = [
            {
                "window_s": float(item.get("window_s") or item.get("seconds") or 0.0),
                "hit_damage": float(item.get("hit_damage") or 0.0),
                "dot_damage": float(item.get("dot_damage") or 0.0),
                "application_damage": 0.0,
                "total_damage": float(item.get("total_damage") or item.get("damage") or 0.0),
                "dps": float(item.get("dps") or 0.0),
                "cast_count": int(item.get("complete_casts") or item.get("casts") or 0),
                "unsettled_tail_damage": 0.0,
            }
            for item in (legacy_result.get("windows") or [])
        ]
        components = [
            {
                "component_id": str(item.get("id") or item.get("component_id")),
                "kind": str(item.get("kind")),
                "damage_type": item.get("damage_type"),
                "damage_total": float(item.get("damage") or 0.0),
                "tags": [],
                "presentation_label": item.get("damage_type"),
            }
            for item in legacy_result.get("components") or []
        ]
        totals = dict(selected["totals"]) if selected else {}
        primary_id = str(plan.get("primary_evaluation") or selected["policy_id"] if selected else "assembly")
        primary_policy_id = str(selected["policy_id"]) if selected else primary_id
        old_evaluations = legacy_result.get("evaluations") or []
        evaluations = [
            {
                "evaluation_id": str(item.get("evaluation_id")),
                "policy_id": primary_policy_id,
                "label": item.get("label"),
                "scenario_sha256": scenario_sha,
                **{
                    "horizon_damage": float(item.get("single_hit") if item.get("evaluation_id") == "single_hit" else totals.get("horizon_damage", 0.0)),
                    "horizon_dps": float(item.get("single_hit") if item.get("evaluation_id") == "single_hit" else totals.get("horizon_dps", 0.0)),
                    "cast_count": totals.get("cast_count", 0),
                    "max_concurrent": 1,
                },
            }
            for item in old_evaluations
        ]
    elif legacy_schema == "le-calculation-plan-v3":
        loops = legacy_result.get("loops") or []
        for loop in loops:
            loop_windows = [
                {
                    "window_s": float(item.get("seconds") or item.get("window_s") or 0.0),
                    "hit_damage": float(item.get("hit_damage") or item.get("body_damage") or 0.0),
                    "dot_damage": float(item.get("dot_damage") or item.get("ignite_damage") or 0.0),
                    "application_damage": 0.0,
                    "total_damage": float(item.get("total_damage") or item.get("damage") or 0.0),
                    "dps": float(item.get("dps") or 0.0),
                    "cast_count": int(item.get("casts") or item.get("cast_count") or 0),
                    "unsettled_tail_damage": float(item.get("unsettled_tail_damage") or 0.0),
                }
                for item in loop.get("windows") or []
            ]
            totals = {
                "horizon_damage": float(loop["totals"].get("horizon_damage") or 0.0),
                "horizon_dps": float(loop["totals"].get("horizon_dps") or 0.0),
                "hit_damage": float(loop["totals"].get("body_damage") or 0.0),
                "dot_damage": float(loop["totals"].get("ignite_damage") or 0.0),
                "application_damage": 0.0,
                "unsettled_tail_damage": float(loop["totals"].get("unsettled_tail_damage") or 0.0),
                "cast_count": int(loop["totals"].get("cast_count") or 0),
                "max_concurrent": int(loop["totals"].get("max_concurrent_black_holes") or loop.get("max_concurrent") or 1),
            }
            policies.append({"policy_id": str(loop.get("policy_id")), "totals": totals, "primary": bool(loop.get("primary")), "windows": loop_windows})
            evaluations.append(
                {
                    "evaluation_id": str(loop.get("policy_id")),
                    "policy_id": str(loop.get("policy_id")),
                    "label": loop.get("label"),
                    "scenario_sha256": scenario_sha,
                    **{key: totals[key] for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent")},
                }
            )
        primary_policy = next((item for item in policies if item["primary"]), policies[0] if policies else None)
        windows = list(primary_policy.get("windows") or []) if primary_policy else []
        components = [
            {
                "component_id": str(item.get("component_id") or item.get("id")),
                "kind": "dot" if "ignite" in str(item.get("component_id")) or item.get("damage_type") else "hit",
                "damage_type": item.get("damage_type"),
                "damage_total": float(item.get("damage_total") or 0.0),
                "tags": [],
                "presentation_label": item.get("damage_type"),
            }
            for item in legacy_result.get("components") or []
        ]
        totals = dict(primary_policy["totals"]) if primary_policy else {}
        primary_id = str(primary_policy["policy_id"]) if primary_policy else "assembly"
    else:
        legacy = plan.get("assembly", {}).get("legacy_plan") or {}
        controls = [item for item in legacy.get("controls") or [] if isinstance(item, dict)]
        default_states = {str(item.get("key")): item.get("default") for item in controls if item.get("namespace") == "state"}
        default_target = {str(item.get("key")): item.get("default") for item in controls if item.get("namespace") == "target"}
        legacy_result = legacy_result
        windows = []
        for item in legacy_result.get("windows") or []:
            hit = float(item.get("hit_damage") or 0.0)
            dot = float(item.get("dot_damage") or 0.0)
            windows.append(
                {
                    "window_s": float(item.get("seconds") or item.get("window_s") or 0.0),
                    "hit_damage": hit,
                    "dot_damage": dot,
                    "application_damage": 0.0,
                    "total_damage": float(item.get("damage") or item.get("total_damage") or hit + dot),
                    "dps": float(item.get("dps") or 0.0),
                    "cast_count": int(item.get("casts") or item.get("cast_count") or 0),
                    "unsettled_tail_damage": 0.0,
                }
            )
        last = windows[-1] if windows else {}
        totals = {
            "horizon_damage": last.get("total_damage", 0.0),
            "horizon_dps": float(legacy_result.get("sustained_dps") or 0.0),
            "hit_damage": float(legacy_result.get("single_hit") or 0.0),
            "dot_damage": float(legacy_result.get("dot_dps") or 0.0),
            "application_damage": 0.0,
            "unsettled_tail_damage": 0.0,
            "cast_count": last.get("cast_count", 0),
            "max_concurrent": 1,
        }
        components = [
            {
                "component_id": str(item.get("id") or item.get("component_id")),
                "kind": str(item.get("kind")),
                "damage_type": item.get("damage_type"),
                "damage_total": float(item.get("damage") or 0.0),
                "tags": [],
                "presentation_label": item.get("damage_type"),
            }
            for item in legacy_result.get("components") or []
        ]
        primary_id = str(plan.get("primary_evaluation") or "assembly")
        policies = [{"policy_id": primary_id, "totals": totals, "primary": True, "windows": windows}]
        evaluations = [
            {
                "evaluation_id": primary_id,
                "policy_id": primary_id,
                "label": "single cast",
                "scenario_sha256": scenario_sha,
                **{key: totals[key] for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent")},
            }
        ]
    if not policies:
        raise PlanV4Error("assembly legacy result 没有 policy/loop")
    if not evaluations:
        evaluations = [
            {
                "evaluation_id": primary_id,
                "policy_id": primary_id,
                "label": "primary",
                "scenario_sha256": scenario_sha,
                **{key: totals[key] for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent")},
            }
        ]
    primary_policy_id = str(locals().get("primary_policy_id") or primary_id)
    plan_eval_meta = {
        str(item.get("evaluation_id")): item
        for item in ((plan.get("schedule") or {}).get("evaluations") or [])
    }
    for policy in policies:
        policy.setdefault("condition", policy.get("condition") or policy["policy_id"])
        policy.setdefault("casts", policy["totals"].get("cast_count", 0))
        policy.setdefault("windows", windows)
        policy.setdefault("primary", policy["policy_id"] == primary_policy_id)
    for evaluation in evaluations:
        evaluation.setdefault("primary", evaluation["evaluation_id"] == plan.get("primary_evaluation"))
        evaluation.setdefault("window_count", len(windows))
        if evaluation.get("policy_id") not in {item["policy_id"] for item in policies}:
            evaluation["policy_id"] = primary_policy_id
    for application in applications_ledger:
        application.setdefault("ailment_id", application.get("component_id"))
        application.setdefault("damage_type", None)
    result_payload: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "version": "6.1.0",
        "plan_ref": {"schema": SCHEMA_PLAN, "plan_sha256": plan.get("plan_sha256")},
        "plan_sha256": plan.get("plan_sha256"),
        "subject": copy.deepcopy(plan.get("subject") or {}),
        "scenario": scenario,
        "scenario_sha256": scenario_sha,
        "execution_fingerprint": execution,
        "primary_evaluation": primary_id,
        "evaluations": evaluations,
        "primary_policy_id": locals().get("primary_policy_id", primary_id),
        "target_side": _assembly_target_side(plan, legacy_result),
        "presentation": copy.deepcopy(plan.get("presentation") or {}),
        "capabilities": copy.deepcopy(plan.get("capabilities") or []),
        "policy_results": policies,
        "windows": windows,
        "totals": totals,
        "components": components,
        "applications": applications_ledger,
        "application_units": copy.deepcopy(plan.get("components") or []),
        "applications_ledger": applications_ledger,
        "contribution_application_summary": copy.deepcopy(legacy_result.get("contribution_application_summary") or {}),
        "coverage": copy.deepcopy(legacy_result.get("coverage") or plan.get("coverage") or {}),
        "issues": copy.deepcopy(legacy_result.get("issues") or []),
        "assumptions": copy.deepcopy(plan.get("assumptions") or []),
        "limitations": copy.deepcopy(plan.get("limitations") or []),
        "report_details": {
            key: copy.deepcopy(legacy_result[key])
            for key in ("components", "single_cast", "loops", "ignite", "ailments",
                        "equipment", "controls", "cast", "cooldown", "attributes",
                        "hits_per_cast", "interval_s", "policies")
            if key in legacy_result
        },
        "contributions": contributions,
        "effect_ledger": copy.deepcopy(legacy_result.get("effect_ledger") or {}),
        "consistency": {"assembly": True, "legacy_schema": legacy_schema},
        "implementation_fingerprints": {
            "engine": {"id": "le_calc.plan_v4.assembly", "version": "1.0.0"},
            "legacy_execution": legacy_result.get("execution_fingerprint"),
            "plan_sha256": plan.get("plan_sha256"),
        },
    }
    if legacy_result.get("frequency") is not None:
        result_payload["frequency"] = copy.deepcopy(legacy_result["frequency"])
    if legacy_result.get("actors"):
        result_payload["actors"] = copy.deepcopy(legacy_result["actors"])
    return result_payload


def validate_plan_v4(plan: Mapping[str, Any]) -> None:
    if plan.get("schema") != SCHEMA_PLAN:
        raise PlanV4Error(f"plan schema 不是 {SCHEMA_PLAN}")
    if plan.get("version") != PLAN_VERSION:
        raise PlanV4Error(f"plan version 不是 {PLAN_VERSION}")
    expected = _sha256_text(_canonical({k: v for k, v in plan.items() if k != "plan_sha256"}))
    if plan.get("plan_sha256") != expected:
        raise PlanV4Error("plan_sha256 与计划内容不一致")
    components = plan.get("components")
    if not isinstance(components, list) or not components:
        raise PlanV4Error("plan.components 不能为空")
    component_ids = {str(item.get("component_id")) for item in components if isinstance(item, dict)}
    if len(component_ids) != len(components):
        raise PlanV4Error("plan.components component_id 重复或缺失")
    applications = plan.get("applications") or []
    if not isinstance(applications, list):
        raise PlanV4Error("plan.applications 必须是数组")
    target = _require_object(plan.get("target_side"), "target_side")
    if target.get("unit") != "fraction":
        raise PlanV4Error("target_side.unit 必须是 fraction")
    entries = target.get("components")
    if not isinstance(entries, dict):
        raise PlanV4Error("target_side.components 必须是对象")
    expected_refs = component_ids | {str(item.get("application_id")) for item in applications}
    if set(entries) != expected_refs:
        raise PlanV4Error("target_side.components 必须与组件/application 引用一一对应")
    for ref, entry in entries.items():
        if entry.get("strategy") not in STRATEGIES:
            raise PlanV4Error(f"target_side.{ref}.strategy 不受支持")
        if entry.get("negative_resistance_policy") not in NEGATIVE_POLICIES:
            raise PlanV4Error(f"target_side.{ref}.negative_resistance_policy 不受支持")
        for key in ("resistance", "penetration", "damage_reduction", "multiplier", "resistance_multiplier", "penetration_multiplier"):
            _finite(entry.get(key), label=f"target_side.{ref}.{key}")
    schedule = _require_object(plan.get("schedule"), "schedule")
    if schedule.get("kind") not in ("single_cast", "charge", "assembly"):
        raise PlanV4Error("schedule.kind 不受支持")
    evaluations = schedule.get("evaluations")
    if not isinstance(evaluations, list) or not evaluations:
        raise PlanV4Error("schedule.evaluations 缺失")
    if sum(1 for item in evaluations if item.get("primary")) != 1:
        raise PlanV4Error("schedule.evaluations 必须恰好一个 primary")
    presentation = _require_object(plan.get("presentation"), "presentation")
    if not presentation.get("title"):
        raise PlanV4Error("presentation.title 缺失")
    capabilities = [str(item) for item in plan.get("capabilities") or []]
    if not capabilities:
        raise PlanV4Error("plan.capabilities 不能为空")
    if set(capabilities) - set(CAPABILITIES):
        raise PlanV4Error(f"capability_gap: 未知能力 {sorted(set(capabilities) - set(CAPABILITIES))}")
    subject = _require_object(plan.get("subject"), "subject")
    for key in ("case_id", "main_ability_id", "build_id", "revision_id"):
        if not subject.get(key):
            raise PlanV4Error(f"plan.subject.{key} 缺失")
    coverage = _require_object(plan.get("coverage"), "coverage")
    if coverage.get("status") not in ("complete", "partial", "from_execution"):
        raise PlanV4Error("plan.coverage.status 非法")
    gap_count = coverage.get("gap_count")
    if not isinstance(gap_count, int) or gap_count != len(coverage.get("gaps") or []):
        raise PlanV4Error("plan.coverage gap_count 与 gaps 不一致")
    if not isinstance(plan.get("issues"), list) or not isinstance(plan.get("assumptions"), list):
        raise PlanV4Error("plan.issues/assumptions 必须是数组")
    if not plan.get("execution_fingerprint") or not plan.get("scenario_sha256"):
        raise PlanV4Error("plan 缺少 execution_fingerprint/scenario_sha256")
    assembly = plan.get("assembly")
    if assembly is not None:
        assembly = _require_object(assembly, "assembly")
        legacy_plan = assembly.get("legacy_plan")
        if not isinstance(legacy_plan, dict) or legacy_plan.get("schema") not in (
            "le-calculation-plan-v1", "le-calculation-plan-v2", "le-calculation-plan-v3"
        ):
            raise PlanV4Error("assembly.legacy_plan schema 不支持")
        planmod.verify_plan_integrity(legacy_plan)
        execution = plan.get("execution")
        if execution is not None:
            execution = _require_object(execution, "execution")
            if not plan.get("execution_sha256") or plan["execution_sha256"] != _sha256_text(_canonical(execution)):
                raise PlanV4Error("execution_sha256 与 execution 内容不一致")
            if execution.get("execution_v1"):
                validate_execution_contract(execution["execution_v1"])
            else:
                for key in ("effects", "sources", "parameters", "schedule", "strategies"):
                    if key not in execution:
                        raise PlanV4Error(f"execution.{key} 缺失")
                if not execution["effects"]:
                    raise PlanV4Error("execution.effects 不能为空")
            source_components = (
                (execution.get("execution_v1") or {}).get("components")
                if execution.get("execution_v1")
                else execution.get("components")
            ) or []
            execution_component_ids = {str(item.get("component_id")) for item in source_components}
            plan_component_ids = {str(item.get("component_id")) for item in plan.get("components") or [] if isinstance(item, dict)}
            if execution_component_ids != plan_component_ids:
                raise PlanV4Error("plan.components 与 execution.components 引用不一致")


def _coverage_gap_counts(coverage: Mapping[str, Any]) -> dict[str, int]:
    """按 ``coverage.gaps[].code`` 聚合缺口数量（供 issue 文案与门禁共用）。"""
    counts: dict[str, int] = {}
    for gap in coverage.get("gaps") or []:
        if not isinstance(gap, Mapping):
            continue
        code = str(gap.get("code") or "unknown")
        counts[code] = counts.get(code, 0) + 1
    return counts


def _coverage_issue_text(coverage: Mapping[str, Any]) -> str:
    """coverage partial 的 issue 文案：按缺口 code 分类，不把所有 gap 都说成伤害缺口。

    统计口径与 :func:`_coverage_gap_counts` 相同；``unmapped:damage_relevant_unknown``
    单独标为“未解析伤害相关”，``unmapped:out_of_scope`` 保持 out_of_scope，
    其余 code 保留其 unmapped 后缀（如 idol_refraction）。
    """
    counts = _coverage_gap_counts(coverage)
    gap_count = int(coverage.get("gap_count") if coverage.get("gap_count") is not None else len(coverage.get("gaps") or []))
    parts: list[str] = []
    damage_related = 0
    for code in sorted(counts):
        count = counts[code]
        if "damage_relevant" in code:
            damage_related += count
            continue
        if code == "unmapped:out_of_scope":
            label = "out_of_scope"
        elif code.startswith("unmapped:"):
            label = code[len("unmapped:"):]
        else:
            label = code
        parts.append(f"{label} {count}")
    if damage_related:
        parts.append(f"未解析伤害相关 {damage_related}")
    detail = " / ".join(parts) if parts else "见 coverage.gaps"
    return f"覆盖状态 partial：{gap_count} 项未建模/未验证（{detail}，见 coverage.gaps）"


def _execute_native_v4(plan: Mapping[str, Any], scenario: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """VC v4 native execution: plan.execution drives public handlers/mechanics."""
    execution = _require_object(plan.get("execution"), "execution")
    generic = execution.get("execution_v1") or {}
    runtime = plan_handlers.PlanRuntime()
    scenario = dict(scenario or {})
    control_source = (generic.get("target") or {}).get("controls") if generic else execution.get("controls")
    controls = [item for item in control_source or [] if isinstance(item, dict)]
    default_states = {str(item.get("key")): item.get("default") for item in controls if item.get("namespace") == "state"}
    default_target = {str(item.get("key")): item.get("default") for item in controls if item.get("namespace") == "target"}
    states = {**default_states, **dict(scenario.get("states") or {})}
    target = {**default_target, **dict(scenario.get("target") or {})}
    target.setdefault("monster_level", 100)
    target.setdefault("resistance", 0.0)
    generic_target = (generic.get("target") or {}) if generic else {}
    generic_hit_components = [item for item in generic.get("components") or [] if item.get("kind") == "hit"] if generic else []
    resistance_key = str(generic_hit_components[0].get("resistance_key") or "physical") if generic_hit_components else "physical"
    if generic_target:
        resistance_map = generic_target.get("resistances") or {}
        if resistance_key in resistance_map and resistance_map[resistance_key] is not None:
            target["resistance"] = float(resistance_map[resistance_key])
        if generic_target.get("damage_reduction") is not None:
            target["monster_damage_reduction"] = float(generic_target["damage_reduction"])
        if float(generic_target.get("penetration") or 0.0) != 0.0:
            target["penetration"] = float(generic_target["penetration"])
    scenario_target = {key: value for key, value in target.items() if key != "monster_damage_reduction"}
    if generic:
        validate_execution_contract(generic)
    if generic:
        execution_plan = {
            "effects": copy.deepcopy(generic.get("effects") or []),
            "conditions": [copy.deepcopy(item) for item in (generic.get("condition_programs") or {}).values()],
            "components": [
                {
                    "component_id": item.get("component_id"),
                    "kind": item.get("kind"),
                    "tags": list(item.get("tags") or []),
                    "crit_capable": bool(item.get("crit_capable")),
                    "adaptive_damage_type": item.get("adaptive_damage_type"),
                }
                for item in generic.get("components") or []
            ],
            "facts": copy.deepcopy(generic.get("facts") or {}),
            "scope": copy.deepcopy(generic.get("scope") or {}),
            "coverage": copy.deepcopy(generic.get("coverage") or {}),
            "parameters": {},
            "schedule": {"windows_s": list((generic.get("frequency") or {}).get("windows_s") or [])},
            "assumptions": [],
            "limitations": [],
        }
    else:
        execution_plan = {
            key: copy.deepcopy(value)
            for key, value in execution.items()
            if key not in ("engine", "source_contract", "strategies", "execution_v1")
        }
    rows, _reasons, notes = v4_execution.build_contributions(execution_plan, states=states, target=target, runtime=runtime)
    contributions_by_id = {
        str(effect["effect_id"]): row
        for effect, row in zip(
            [item for item in sorted(execution_plan.get("effects") or [], key=lambda item: int(item.get("sequence") or 0)) if item.get("emits_contribution")],
            rows,
        )
    }
    parameters = {}
    skill_params = {}
    schedule_doc = {}
    generic_frequency = (generic.get("frequency") or {}) if generic else {}
    hit_generic = generic_hit_components[0] if generic_hit_components else {}
    component_entries = [
        item for item in (generic.get("components") if generic else execution_plan.get("components")) or []
        if isinstance(item, dict)
    ]
    hit_entry = next((item for item in component_entries if item.get("kind") == "hit"), {})
    dot_entry = next((item for item in component_entries if item.get("kind") == "dot"), {})
    hit_template = {
        "tags": list(hit_entry.get("tags") or []),
        "crit_capable": bool(hit_entry.get("crit_capable")),
        "adaptive_damage_type": hit_entry.get("adaptive_damage_type"),
        "scope": str(hit_entry.get("scope") or "body"),
        "actor_id": hit_entry.get("actor_id"),
    }
    dot_template = {"tags": list(dot_entry.get("tags") or ["DoT"]), "crit_capable": bool(dot_entry.get("crit_capable")), "adaptive_damage_type": dot_entry.get("adaptive_damage_type")}
    rule_payload = {
        "crit": (generic.get("crit_rules") or {}) if generic else (parameters.get("crit") or {}),
        "every_nth_hit": generic_frequency.get("every_nth_hit") if generic else schedule_doc.get("every_nth_hit"),
        "ailments": (generic.get("applications") or []) if generic else (parameters.get("ailments") or []),
        "damage_to_penetration": generic.get("damage_to_penetration") if generic else parameters.get("damage_to_penetration"),
    }
    state_values = {**states, **target}
    frequency_projection: dict[str, Any] | None = None
    if generic:
        driver = str(generic_frequency.get("driver") or "")
        if driver == "derived_event_rate":
            frequency_projection = runtime.handler("frequency_derived_events-v1")(
                derivation=generic_frequency.get("derivation") or {},
                states=state_values,
            )
            interval = frequency_projection.get("event_interval_s")
            if interval is None or float(interval) <= 0:
                raise PlanV4Error("execution.frequency 派生事件率必须产生正事件间隔")
            timing = {"interval_s": float(interval)}
        else:
            if float(generic_frequency.get("cast_interval_s") or 0.0) <= 0:
                raise PlanV4Error("execution.frequency.cast_interval_s 必须 > 0")
            timing = {"interval_s": float(generic_frequency["cast_interval_s"])}
    else:
        speed = runtime.handler("attack_speed_pool-v1")(contributions=rows, component_tags=list(hit_template.get("tags") or []))
        cooldown_recovery = runtime.handler("cooldown_recovery_pool-v1")(contributions=rows)
        cooldown_reduction = float(parameters.get("cooldown_reduction") or 0.0)
        timing = runtime.handler("frequency_fixed_repeat-v1")(action="cast_interval", cooldown_s=float(skill_params.get("cooldown_s") or 0.0), action_s=float(skill_params.get("action_s") or 0.0), cooldown_reduction=cooldown_reduction, cooldown_recovery=cooldown_recovery, attack_speed=speed)
    target_info = runtime.handler("target_side-v1")(action="resolve_hit_target", merged_target={"monster_level": target["monster_level"], f"{resistance_key}_resistance": target["resistance"]})
    hit_skill = (
        {
            "base_damage": {str(hit_generic.get("damage_type") or "Physical"): float(hit_generic.get("base_damage") or 0.0)},
            "added_damage_scaling": float(hit_generic.get("added_damage_scaling") or 0.0),
            "crit_chance": float(hit_generic.get("crit_chance") or 0.0),
            "crit_multiplier": float(hit_generic.get("crit_multiplier") or 1.0),
            "crit_chance_unclamped": hit_generic.get("crit_chance_unclamped"),
        }
        if generic
        else {
            "base_damage": skill_params.get("base_damage") or {},
            "added_damage_scaling": skill_params.get("added_damage_scaling") or 0.0,
            "crit_chance": skill_params.get("crit_chance") or 0.0,
            "crit_multiplier": skill_params.get("crit_multiplier") or 1.0,
        }
    )
    # v4-actor 装配（hit 组件 scope=="actor"）：行为标签与伤害通道必须隔离，
    # 且显式带类型的 added 只进入对应类型通道。非 actor 路径保持历史默认
    # （typed_added_policy="legacy"、不隔离），保证 VC/FR/BH 既有输出不变。
    hit_assembly_kwargs: dict[str, Any] = {}
    if hit_template["scope"] == "actor":
        hit_assembly_kwargs = {"typed_added_policy": "by_type", "isolate_damage_channels": True}
    hit_components, hit_applications = runtime.handler("hit_component_assembly-v1")(contributions=rows, skill=hit_skill, component=hit_template, crit_rules=rule_payload.get("crit") or {}, target=target, states=state_values, target_info=target_info, unit_scope=hit_template["scope"], **hit_assembly_kwargs)
    dot_components, dot_applications, chance_notes = runtime.handler("dot_component_assembly-v1")(contributions=rows, ailments=rule_payload.get("ailments") or [], dot_component=dot_template, penetration_rule=rule_payload.get("damage_to_penetration"), target=target, target_info=target_info, additional_chance=(generic.get("additional_chance") or {}) if generic else (parameters.get("additional_chance") or {}))
    if generic_target.get("damage_reduction") is not None:
        override_dr = float(generic_target["damage_reduction"])
        target_components = target_info.get("components") if isinstance(target_info, dict) else None
        base_dr = 0.0
        if isinstance(target_components, dict):
            for item in target_components.values():
                base_dr = max(base_dr, float(item.get("damage_reduction") or 0.0))
        if base_dr == 0.0 and isinstance(target_info, dict):
            base_dr = float(target_info.get("monster_damage_reduction") or 0.0)
        if base_dr < 1.0:
            ratio = (1.0 - override_dr) / (1.0 - base_dr)
            for component in [*hit_components, *dot_components]:
                component["damage"] = float(component.get("damage") or 0.0) * ratio
            if isinstance(target_components, dict):
                for item in target_components.values():
                    item["damage_reduction"] = override_dr
                    item["monster_damage_reduction"] = override_dr
                    item["monster_dr_factor"] = 1.0 - override_dr
                    item["multiplier"] = float(item.get("resistance_multiplier") or 1.0) * (1.0 - override_dr) * float(item.get("penetration_multiplier") or 1.0)
            if isinstance(target_info, dict):
                target_info["monster_damage_reduction"] = override_dr
                target_info["monster_dr_factor"] = 1.0 - override_dr
    single = sum(float(component["damage"]) for component in hit_components)
    windows, steady = runtime.handler("frequency_fixed_repeat-v1")(action="hit_windows", single_hit=single, ailments=dot_components, interval=timing["interval_s"], every_nth=rule_payload.get("every_nth_hit"), windows_s=tuple(float(value) for value in ((generic.get("frequency") or {}).get("windows_s") if generic else None) or schedule_doc.get("windows_s") or (5, 30, 40)), first_cast_s=float(schedule_doc.get("first_cast_s") or 0.0))
    coverage = runtime.handler("coverage_projection-v1")(plan=execution_plan, dot_applications=dot_applications)
    effect_ledger = runtime.handler("ledger_projection-v1")(plan=execution_plan, contributions_by_id=contributions_by_id, notes=notes, states=states)
    facts = execution_plan.get("facts") or {}
    presentation = execution_plan.get("presentation") or {}
    limitations = [str(note) for note in (execution_plan.get("scope") or {}).get("limitations") or []]
    issues: list[str] = list(limitations)
    for note in chance_notes:
        issues.append(f"{note['ailment']} 施加概率 {note['chance']:.3f} > 100%：{note['issue']}")
    issues.extend(str(item) for item in facts.get("post_assembly_issues") or [])
    issues.extend(str(item) for item in facts.get("early_issues") or [])
    if coverage["status"] == "partial":
        issues.append(_coverage_issue_text(coverage))
    issues.extend(str(item) for item in facts.get("duplicate_issues") or [])
    application_summary = dps_result.application_summary(hit_applications + dot_applications)
    coverage_classes: dict[str, Any] = {}
    for effect in execution_plan.get("effects") or []:
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
    native = {
        "schema": "le-dps-result-v1",
        "case_id": (plan.get("subject") or {}).get("case_id"),
        "scenario": {"states": states, "target": scenario_target},
        "target": target_info,
        "single_hit": single,
        "sustained_dps": steady["sustained_dps"],
        "hit_dps": steady["hit_dps"],
        "dot_dps": steady["dot_dps"],
        "windows": windows,
        "components": hit_components,
        "applications": hit_applications + dot_applications,
        "contribution_application_summary": application_summary,
        "issues": issues,
        "contributions": contributions_ledger,
        "effect_ledger": effect_ledger,
        "coverage": coverage,
        "scenario_sha256": planmod.scenario_sha256({"states": states, "target": scenario_target}),
    }
    if frequency_projection is not None:
        native["frequency"] = copy.deepcopy(frequency_projection)
    generic_actors = copy.deepcopy((generic or {}).get("actors") or [])
    if generic_actors:
        native["actors"] = generic_actors
    result = _map_legacy_result_to_v6(plan, native, "le-calculation-plan-v1")
    validate_result_v6(plan, result)
    return result


def _build_execution_v1(
    legacy_plan: Mapping[str, Any],
    plan_components: list[Mapping[str, Any]],
    target_entries: Mapping[str, Any],
    windows: list[float],
    evaluations_meta: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Map real v1 assembly data into explicit execution-v1 fields (no runtime legacy reads)."""
    parameters = legacy_plan.get("parameters") or {}
    schedule = legacy_plan.get("schedule") or {}
    legacy_components = {
        str(item.get("component_id")): item for item in legacy_plan.get("components") or [] if isinstance(item, dict)
    }
    components: list[dict[str, Any]] = []
    dot_components: list[str] = []
    for component in plan_components:
        kind = str(component.get("kind"))
        legacy_component = legacy_components.get(str(component.get("component_id"))) or {}
        adaptive_damage_type = legacy_component.get("adaptive_damage_type") or component.get("damage_type")
        damage_type = str(adaptive_damage_type or "Physical")
        cid = str(component.get("component_id"))
        if kind == "dot":
            dot_components.append(cid)
        components.append({
            "component_id": cid,
            "kind": kind,
            "unit": "damage",
            "damage_channel": damage_type.lower(),
            "damage_type": damage_type,
            "adaptive_damage_type": adaptive_damage_type,
            "resistance_key": damage_type.lower(),
            "scope": "body" if kind == "hit" else "ailment",
            "duration_s": float((parameters.get("skill") or {}).get("duration_s") or 0.0) if kind != "dot" else float((legacy_plan.get("components") or [{}])[0].get("duration_s") or 0.0),
            "tick_interval_s": 0.0 if kind == "hit" else float((parameters.get("skill") or {}).get("tick_interval_s") or 0.0),
            "cast_ref": "cast",
            "base_damage": float((parameters.get("skill") or {}).get("base_damage", {}).get(damage_type, 0.0) or 0.0),
            "added_damage_scaling": float((parameters.get("skill") or {}).get("added_damage_scaling") or 0.0),
            "crit_capable": bool(component.get("crit_capable")),
            "crit_chance": float((parameters.get("skill") or {}).get("crit_chance") or 0.0),
            "crit_multiplier": float((parameters.get("skill") or {}).get("crit_multiplier") or 1.0),
            "crit_chance_unclamped": (
                float(parameters["skill"]["crit_chance_unclamped"])
                if (parameters.get("skill") or {}).get("crit_chance_unclamped") is not None
                else None
            ),
            "tags": list(component.get("tags") or []),
            "damage_weights": list((parameters.get("skill") or {}).get("hit_weights") or []),
        })
    actor_model = copy.deepcopy((legacy_plan.get("facts") or {}).get("actor_model") or {})
    actor_id = str(actor_model.get("actor_id") or "")
    if actor_id:
        actor_scope = str(actor_model.get("scope") or "actor")
        for component in components:
            component["actor_id"] = actor_id
            component["scope"] = actor_scope
    applications: list[dict[str, Any]] = []
    for ailment in parameters.get("ailments") or []:
        if not isinstance(ailment, dict):
            continue
        applications.append({
            "application_id": f"ailment:{ailment.get('ailment_id')}",
            "source_component": dot_components[0] if dot_components else (components[0]["component_id"] if components else ""),
            "ailment_id": ailment.get("ailment_id"),
            "damage_channel": str(ailment.get("damage_type") or "").lower(),
            "damage_type": str(ailment.get("damage_type") or ""),
            "resistance_key": str(ailment.get("damage_type") or "").lower(),
            "scope": "ailment",
            "duration_s": float(ailment.get("duration_s") or 0.0),
            "max_stacks": 1,
            "condition": "",
            "cohort_policy": "independent_cast",
            "window_policy": "half_open",
            "tail_policy": "include_unsettled",
            "chance": ailment.get("chance"),
            "expected_stacks": 1.0,
            "tick_interval_s": float(ailment.get("tick_interval_s") or 0.0),
            "damage_per_tick": float(ailment.get("base_damage") or 0.0),
            "ownership": {"owner": "ailment", "ownership_key": f"ailment:{ailment.get('ailment_id')}"},
            "ledger_ref": f"ailment:{ailment.get('ailment_id')}",
            "base_damage": float(ailment.get("base_damage") or 0.0),
            "name": ailment.get("name"),
            "label_zh": ailment.get("label_zh"),
            "evidence": list(ailment.get("evidence") or []),
        })
    conditions = {
        str(item.get("condition_id")): copy.deepcopy(item)
        for item in legacy_plan.get("conditions") or []
        if isinstance(item, dict)
    }
    effects = copy.deepcopy(legacy_plan.get("effects") or [])
    resistances = {}
    penetration = 0.0
    damage_reduction = 0.0
    for ref, entry in (target_entries or {}).items():
        resistances[str(ref)] = float(entry.get("resistance") or 0.0)
    damage_reduction = None
    control_states = {
        str(item.get("key")): item.get("default")
        for item in legacy_plan.get("controls") or []
        if item.get("namespace") == "state"
    }
    control_target = {
        str(item.get("key")): item.get("default")
        for item in legacy_plan.get("controls") or []
        if item.get("namespace") == "target"
    }
    runtime = plan_handlers.PlanRuntime()
    contribution_rows, _reasons, _notes = v4_execution.build_contributions(
        legacy_plan, states=control_states, target=control_target, runtime=runtime
    )
    driver = str(schedule.get("driver") or "fixed_repeat")
    frequency_payload: dict[str, Any] = {
        "driver": driver,
        "horizon_s": float(max(windows) if windows else 0.0),
        "windows_s": [float(value) for value in windows],
        "action_s": float(schedule.get("action_s") or 0.0),
        "cooldown_s": float((parameters.get("skill") or {}).get("cooldown_s") or 0.0),
        "cooldown_reduction": float(parameters.get("cooldown_reduction") or 0.0),
        "first_cast_s": float(schedule.get("first_cast_s") or 0.0),
        "every_nth_hit": copy.deepcopy(schedule.get("every_nth_hit")),
        "policies": [
            {"policy_id": str(item.get("evaluation_id")), "condition": "", "interval_policy": "max(action_time, cooldown)"}
            for item in evaluations_meta
        ],
        "rotation": {},
        "tail_policy": "include_unsettled",
    }
    if driver == "derived_event_rate":
        derivation = copy.deepcopy(schedule.get("derivation") or {})
        if not isinstance(derivation, dict) or not derivation:
            raise PlanV4Error("derived_event_rate 装配必须声明 schedule.derivation")
        frequency_payload["derivation"] = derivation
        for policy in frequency_payload["policies"]:
            policy["interval_policy"] = "derived_event_rate"
    else:
        hit_tags = [str(tag) for item in legacy_components.values() if item.get("kind") == "hit" for tag in item.get("tags") or []]
        speed = runtime.handler("attack_speed_pool-v1")(contributions=contribution_rows, component_tags=hit_tags)
        cooldown_recovery = runtime.handler("cooldown_recovery_pool-v1")(contributions=contribution_rows)
        timing = runtime.handler("frequency_fixed_repeat-v1")(
            action="cast_interval",
            cooldown_s=float((parameters.get("skill") or {}).get("cooldown_s") or 0.0),
            action_s=float(schedule.get("action_s") or 0.0),
            cooldown_reduction=float(parameters.get("cooldown_reduction") or 0.0),
            cooldown_recovery=cooldown_recovery,
            attack_speed=speed,
        )
        frequency_payload["cast_interval_s"] = float(timing["interval_s"])
    payload: dict[str, Any] = {
        "schema": "le-plan-execution-v1",
        "source_contract": "definitions_v3.real_binding",
        "capabilities": list((legacy_plan.get("capabilities") or [])),
        "components": components,
        "applications": applications,
        "effects": effects,
        # execution-v1 只保留绑定成功的来源：失败/不适用来源仍保留在 assembly legacy plan
        # 与 result issues 中，但不能支撑已生效 effect（契约要求 sources 状态明确成功）。
        "sources": [
            copy.deepcopy(source)
            for source in legacy_plan.get("sources") or []
            if str(source.get("binding_status") or "") in SOURCE_SUCCESS_STATUSES
        ],
        "condition_programs": conditions,
        "frequency": frequency_payload,
        "crit_rules": copy.deepcopy(parameters.get("crit") or {}),
        "additional_chance": copy.deepcopy(parameters.get("additional_chance") or {}),
        "damage_to_penetration": copy.deepcopy(parameters.get("damage_to_penetration")),
        "facts": copy.deepcopy(legacy_plan.get("facts") or {}),
        "scope": copy.deepcopy(legacy_plan.get("scope") or {}),
        "charge": {
            "enabled": False, "max_charges": 1, "recharge_s": 0.0,
            "cast_time_s": float(schedule.get("action_s") or 0.0),
            "instance_duration_s": 0.0, "condition": "",
        },
        "target": {
            "resistances": {str(value): float(entry.get("resistance") or 0.0) for value, entry in (target_entries or {}).items()},
            "damage_reduction": damage_reduction,
            "penetration": penetration,
            "controls": copy.deepcopy(legacy_plan.get("controls") or []),
        },
        "coverage": {"status": "from_execution", "gap_count": 0, "gaps": []},
        "coverage_input": {
            "declared_status": str((legacy_plan.get("coverage") or {}).get("status") or "unknown"),
            "declared_gaps": copy.deepcopy((legacy_plan.get("coverage") or {}).get("gaps") or []),
        },
        "ownership": [],
        "ledger_refs": [],
    }
    if actor_id:
        payload["actors"] = copy.deepcopy(actor_model.get("actors") or [])
        payload["stat_transfers"] = copy.deepcopy((legacy_plan.get("facts") or {}).get("stat_transfers") or [])
    return payload


def _build_execution_v1_fr(legacy_plan: Mapping[str, Any], plan_components: list[Mapping[str, Any]], windows: list[float]) -> dict[str, Any]:
    """Map real FR v2 assembly into execution-v1; runtime uses this object only."""
    parameters = copy.deepcopy(legacy_plan.get("parameters") or {})
    schedule = copy.deepcopy(legacy_plan.get("schedule") or {})
    legacy_components = [copy.deepcopy(item) for item in legacy_plan.get("components") or [] if isinstance(item, dict)]
    components: list[dict[str, Any]] = []
    for component in legacy_components:
        kind = str(component.get("kind") or "hit")
        damage_type = str(component.get("adaptive_damage_type") or component.get("damage_type") or "Physical")
        components.append({
            **component,
            "unit": "damage",
            "damage_channel": damage_type.lower(),
            "damage_type": damage_type,
            "resistance_key": damage_type.lower(),
            "scope": "body",
            "duration_s": 0.0,
            "tick_interval_s": 0.0,
            "cast_ref": "cast",
            "base_damage": float((parameters.get("skill") or {}).get("base_damage", {}).get(damage_type, 0.0) or 0.0),
        })
    if isinstance(parameters.get("skill"), dict):
        parameters["skill"].pop("base_damage", None)
    success = {"ok", "verified", "bound", "mapped", "applied"}
    sources = [copy.deepcopy(item) for item in legacy_plan.get("sources") or [] if str(item.get("binding_status") or "") in success]
    effects = copy.deepcopy(legacy_plan.get("effects") or [])
    conditions = {
        str(item.get("condition_id")): copy.deepcopy(item)
        for item in legacy_plan.get("conditions") or []
        if isinstance(item, dict)
    }
    applications: list[dict[str, Any]] = []
    for ailment in parameters.get("ailments") or []:
        if isinstance(ailment, dict):
            applications.append({
                "application_id": f"ailment:{ailment.get('ailment_id')}",
                "source_component": components[0]["component_id"] if components else "",
                "ailment_id": ailment.get("ailment_id"),
                "damage_channel": str(ailment.get("damage_type") or "").lower(),
                "damage_type": str(ailment.get("damage_type") or ""),
                "resistance_key": str(ailment.get("damage_type") or "").lower(),
                "scope": "ailment",
                "duration_s": float(ailment.get("duration_s") or 0.0),
                "max_stacks": 1,
                "condition": "",
                "cohort_policy": "independent_cast",
                "window_policy": "half_open",
                "tail_policy": "include_unsettled",
                "chance": ailment.get("chance"),
                "expected_stacks": 1.0,
                "tick_interval_s": float(ailment.get("tick_interval_s") or 0.0),
                "damage_per_tick": float(ailment.get("base_damage") or 0.0),
                "ownership": {"owner": "ailment", "ownership_key": f"ailment:{ailment.get('ailment_id')}"},
                "ledger_ref": f"ailment:{ailment.get('ailment_id')}",
            })
    frequency = {
        **schedule,
        "horizon_s": float(max(windows) if windows else 0.0),
        "windows_s": [float(value) for value in windows],
        "tail_policy": "include_unsettled",
    }
    controls = copy.deepcopy(legacy_plan.get("controls") or [])
    target = {
        "controls": controls,
        "resistances": {str(item.get("component_id")): float((item.get("target") or {}).get("resistance") or 0.0) for item in components},
        "damage_reduction": None,
        "penetration": None,
    }
    return {
        "schema": "le-plan-execution-v1",
        "source_contract": "definitions_v3.real_binding",
        "capabilities": list((legacy_plan.get("capabilities") or [])),
        "components": components,
        "applications": applications,
        "effects": effects,
        "sources": sources,
        "condition_programs": conditions,
        "frequency": frequency,
        "evaluations": copy.deepcopy(legacy_plan.get("evaluations") or []),
        "parameters": parameters,
        "controls": controls,
        "target": target,
        "target_side": copy.deepcopy(legacy_plan.get("target_side") or {}),
        "facts": copy.deepcopy(legacy_plan.get("facts") or {}),
        "scope": copy.deepcopy(legacy_plan.get("scope") or {}),
        "coverage": copy.deepcopy(legacy_plan.get("coverage") or {"status": "from_execution", "gap_count": 0, "gaps": []}),
        "presentation": copy.deepcopy(legacy_plan.get("presentation") or {}),
        "assumptions": copy.deepcopy(legacy_plan.get("assumptions") or []),
        "limitations": copy.deepcopy(legacy_plan.get("limitations") or []),
        "diagnostics": copy.deepcopy(legacy_plan.get("diagnostics") or []),
        "primary_evaluation": legacy_plan.get("primary_evaluation"),
        "plan_id": legacy_plan.get("plan_id"),
        "charge": {"enabled": False, "max_charges": 1, "recharge_s": 0.0, "cast_time_s": float(schedule.get("action_s") or 0.0), "instance_duration_s": 0.0, "condition": ""},
    }


def _fr_runtime_plan(plan: Mapping[str, Any], execution_v1: Mapping[str, Any]) -> dict[str, Any]:
    """Build the hit-events primitive input from execution_v1 fields only."""
    parameters = copy.deepcopy(execution_v1.get("parameters") or {})
    components = execution_v1.get("components") or []
    if components:
        component = components[0]
        damage_type = str(component.get("damage_type") or "Physical")
        skill = copy.deepcopy(parameters.get("skill") or {})
        mirror_base = (skill.get("base_damage") or {}).get(damage_type) if isinstance(skill.get("base_damage"), dict) else None
        component_base = float(component.get("base_damage") or 0.0)
        if mirror_base is not None and float(mirror_base) != component_base:
            raise PlanV4Error("FR parameters.skill.base_damage 镜像与 execution_v1.components[0].base_damage 冲突")
        skill["base_damage"] = {damage_type: component_base}
        if component.get("added_damage_scaling") is not None:
            skill["added_damage_scaling"] = float(component["added_damage_scaling"])
        if component.get("crit_chance") is not None:
            skill["crit_chance"] = float(component["crit_chance"])
        if component.get("crit_multiplier") is not None:
            skill["crit_multiplier"] = float(component["crit_multiplier"])
        parameters["skill"] = skill
    return {
        "schema": "le-calculation-plan-v2",
        "plan_id": execution_v1.get("plan_id"),
        "plan_sha256": plan.get("plan_sha256"),
        "subject": copy.deepcopy(plan.get("subject") or {}),
        "effects": copy.deepcopy(execution_v1.get("effects") or []),
        "conditions": [copy.deepcopy(item) for item in (execution_v1.get("condition_programs") or {}).values()],
        "components": copy.deepcopy(execution_v1.get("components") or []),
        "schedule": copy.deepcopy(execution_v1.get("frequency") or {}),
        "evaluations": copy.deepcopy(execution_v1.get("evaluations") or []),
        "parameters": parameters,
        "controls": copy.deepcopy(execution_v1.get("controls") or []),
        "target_side": copy.deepcopy(execution_v1.get("target_side") or {}),
        "facts": copy.deepcopy(execution_v1.get("facts") or {}),
        "scope": copy.deepcopy(execution_v1.get("scope") or {}),
        "coverage": copy.deepcopy(execution_v1.get("coverage") or {}),
        "presentation": copy.deepcopy(execution_v1.get("presentation") or {}),
        "assumptions": copy.deepcopy(execution_v1.get("assumptions") or []),
        "limitations": copy.deepcopy(execution_v1.get("limitations") or []),
        "diagnostics": copy.deepcopy(execution_v1.get("diagnostics") or []),
        "sources": copy.deepcopy(execution_v1.get("sources") or []),
    }


def _build_execution_v1_bh(legacy_plan: Mapping[str, Any], plan_components: list[Mapping[str, Any]], windows: list[float]) -> dict[str, Any]:
    """Map real BH v3 assembly into execution-v1; runtime uses this object only."""
    parameters = copy.deepcopy(legacy_plan.get("parameters") or {})
    schedule = copy.deepcopy(legacy_plan.get("schedule") or {})
    legacy_components = [copy.deepcopy(item) for item in legacy_plan.get("components") or [] if isinstance(item, dict)]
    components: list[dict[str, Any]] = []
    for component in legacy_components:
        damage_type = str(component.get("damage_type") or "Physical")
        components.append({
            **component,
            "unit": "damage",
            "damage_channel": damage_type.lower(),
            "damage_type": damage_type,
            "resistance_key": damage_type.lower(),
            "scope": str(component.get("scope") or "body"),
            "duration_s": float(component.get("duration_s") or 0.0),
            "tick_interval_s": float(component.get("tick_interval_s") or 0.0),
            "cast_ref": "cast",
        })
    success = {"ok", "verified", "bound", "mapped", "applied"}
    sources = [copy.deepcopy(item) for item in legacy_plan.get("sources") or [] if str(item.get("binding_status") or "") in success]
    effects = copy.deepcopy(legacy_plan.get("effects") or [])
    conditions = {
        str(item.get("condition_id")): copy.deepcopy(item)
        for item in legacy_plan.get("conditions") or []
        if isinstance(item, dict)
    }
    ignite = copy.deepcopy(schedule.get("ignite") or {})
    charges = schedule.get("charges") or {}
    applications: list[dict[str, Any]] = []
    if ignite:
        applications.append({
            "application_id": f"application:{ignite.get('ailment') or 'dot'}",
            "source_component": components[0]["component_id"] if components else "",
            "ailment_id": ignite.get("ailment"),
            "damage_channel": str(ignite.get("damage_type") or "").lower(),
            "damage_type": str(ignite.get("damage_type") or ""),
            "resistance_key": str(ignite.get("damage_type") or "").lower(),
            "scope": "ailment",
            "duration_s": float(ignite.get("base_duration_s") or 0.0),
            "max_stacks": 1,
            "condition": "",
            "cohort_policy": "independent_cast",
            "window_policy": "half_open",
            "tail_policy": "include_unsettled",
            "chance": float(ignite.get("chance_total") or 0.0),
            "expected_stacks": 1.0,
            "tick_interval_s": 1.0 / float(ignite.get("rolls_per_second") or 1.0),
            "damage_per_tick": float(ignite.get("base_damage") or 0.0),
            "ownership": {"owner": "ailment", "ownership_key": f"ailment:{ignite.get('ailment')}"},
            "ledger_ref": f"ailment:{ignite.get('ailment')}",
        })
    frequency = {
        **schedule,
        "horizon_s": float(schedule.get("horizon_s") or max(windows) if windows else 0.0),
        "windows_s": [float(value) for value in (schedule.get("windows_s") or windows or [])],
        "policies": copy.deepcopy(schedule.get("policies") or schedule.get("action_policies") or []),
        "rotation": copy.deepcopy(schedule.get("rotation") or {}),
        "tail_policy": "include_unsettled",
    }
    controls = copy.deepcopy(legacy_plan.get("controls") or [])
    coverage = legacy_plan.get("coverage")
    if not isinstance(coverage, dict) or "status" not in coverage:
        coverage = {"status": "from_execution", "gap_count": 0, "gaps": []}
    return {
        "schema": "le-plan-execution-v1",
        "source_contract": "definitions_v3.real_binding",
        "capabilities": list((legacy_plan.get("capabilities") or [])),
        "components": components,
        "applications": applications,
        "effects": effects,
        "sources": sources,
        "condition_programs": conditions,
        "frequency": frequency,
        "evaluations": copy.deepcopy(legacy_plan.get("evaluations") or []),
        "parameters": parameters,
        "controls": controls,
        "target": {"controls": controls, "resistances": {}, "damage_reduction": None, "penetration": None},
        "target_side": copy.deepcopy(legacy_plan.get("target_side") or {}),
        "facts": copy.deepcopy(legacy_plan.get("facts") or {}),
        "scope": copy.deepcopy(legacy_plan.get("scope") or {}),
        "coverage": coverage,
        "presentation": copy.deepcopy(legacy_plan.get("presentation") or {}),
        "assumptions": copy.deepcopy(legacy_plan.get("assumptions") or []),
        "limitations": copy.deepcopy(legacy_plan.get("limitations") or []),
        "diagnostics": copy.deepcopy(legacy_plan.get("diagnostics") or []),
        "primary_evaluation": legacy_plan.get("primary_evaluation"),
        "plan_id": legacy_plan.get("plan_id"),
        "charge": {
            "enabled": True,
            "max_charges": int(charges.get("base_max_charges") or 1) + int(charges.get("extra_max_charges") or 0),
            "recharge_s": float(charges.get("base_recharge_s") or 0.0),
            "cast_time_s": 0.0,
            "instance_duration_s": float((components[0].get("duration_s") if components else 0.0) or 0.0),
            "condition": "",
        },
    }


def _bh_runtime_plan(plan: Mapping[str, Any], execution_v1: Mapping[str, Any]) -> dict[str, Any]:
    """Build the charge/dot primitive input from execution_v1 fields only."""
    schedule = copy.deepcopy(execution_v1.get("frequency") or {})
    applications = execution_v1.get("applications") or []
    allowed_charge_conditions = {
        "charge_available_and_cast_action_free",
        "no_active_black_hole_and_charge_available",
        "wait_for_all_instances_expired",
    }
    policy_conditions = [
        str(item.get("cast_condition") or "")
        for item in schedule.get("action_policies") or []
        if isinstance(item, dict)
    ]
    for condition in policy_conditions:
        if condition not in allowed_charge_conditions:
            raise PlanV4Error(f"未知 charge condition：{condition!r}")
    aggregate_condition = str((execution_v1.get("charge") or {}).get("condition") or "")
    if aggregate_condition and any(condition != aggregate_condition for condition in policy_conditions):
        raise PlanV4Error("execution_v1.charge.condition 与 action_policies.cast_condition 冲突")
    if applications:
        application = applications[0]
        component_types = {str(item.get("damage_type") or "").lower() for item in execution_v1.get("components") or []}
        try:
            mechanics_execution.validate_application_capabilities(application, component_types)
        except mechanics_execution.ExecutionContractError as exc:
            raise PlanV4Error(str(exc)) from exc
        source_component = next(
            (item for item in execution_v1.get("components") or [] if item.get("component_id") == application.get("source_component")),
            None,
        )
        if source_component is not None:
            source_type = str(source_component.get("damage_type") or "").lower()
            if str(application.get("damage_type") or "").lower() != source_type:
                raise PlanV4Error("BH application damage_type 与 source_component damage_type 不匹配")
            if str(application.get("damage_channel") or "").lower() != source_type:
                raise PlanV4Error("BH application damage_channel 与 source_component 不匹配")
            if str(application.get("resistance_key") or "").lower() != source_type:
                raise PlanV4Error("BH application resistance_key 与 source_component 不匹配")
        ignite = schedule.setdefault("ignite", {})
        if application.get("ailment_id"):
            ignite["ailment"] = str(application["ailment_id"])
        if application.get("damage_per_tick") is not None:
            ignite["base_damage"] = float(application["damage_per_tick"])
        if application.get("duration_s") is not None:
            ignite["base_duration_s"] = float(application["duration_s"])
        if application.get("chance") is not None:
            ignite["chance_total"] = float(application["chance"])
        if application.get("damage_type"):
            ignite["damage_type"] = str(application["damage_type"])
        if float(application.get("tick_interval_s") or 0.0) > 0:
            ignite["rolls_per_second"] = 1.0 / float(application["tick_interval_s"])
    charge = execution_v1.get("charge") or {}
    if charge:
        charges = schedule.setdefault("charges", {})
        if charge.get("max_charges") is not None:
            charges["base_max_charges"] = int(charge["max_charges"])
            charges["extra_max_charges"] = 0
        if charge.get("recharge_s") is not None:
            charges["base_recharge_s"] = float(charge["recharge_s"])
        policies = schedule.get("action_policies") or []
        if policies and charge.get("condition"):
            policies[0]["cast_condition"] = str(charge["condition"])
    return {
        "schema": "le-calculation-plan-v3",
        "plan_id": execution_v1.get("plan_id"),
        "plan_sha256": plan.get("plan_sha256"),
        "subject": copy.deepcopy(plan.get("subject") or {}),
        "effects": copy.deepcopy(execution_v1.get("effects") or []),
        "conditions": [copy.deepcopy(item) for item in (execution_v1.get("condition_programs") or {}).values()],
        "components": copy.deepcopy(execution_v1.get("components") or []),
        "schedule": schedule,
        "evaluations": copy.deepcopy(execution_v1.get("evaluations") or []),
        "parameters": copy.deepcopy(execution_v1.get("parameters") or {}),
        "controls": copy.deepcopy(execution_v1.get("controls") or []),
        "target_side": copy.deepcopy(execution_v1.get("target_side") or {}),
        "facts": copy.deepcopy(execution_v1.get("facts") or {}),
        "scope": copy.deepcopy(execution_v1.get("scope") or {}),
        "coverage": copy.deepcopy(execution_v1.get("coverage") or {}),
        "presentation": copy.deepcopy(execution_v1.get("presentation") or {}),
        "assumptions": copy.deepcopy(execution_v1.get("assumptions") or []),
        "limitations": copy.deepcopy(execution_v1.get("limitations") or []),
        "diagnostics": copy.deepcopy(execution_v1.get("diagnostics") or []),
        "sources": copy.deepcopy(execution_v1.get("sources") or []),
    }


def _native_from_execution(plan: Mapping[str, Any], scenario: Mapping[str, Any] | None = None) -> dict[str, Any]:
    execution = {
        key: copy.deepcopy(value)
        for key, value in plan["execution"].items()
        if key not in ("engine", "source_contract", "strategies")
    }
    execution["capabilities"] = list(plan.get("capabilities") or [])
    legacy_scenarios = (plan.get("assembly") or {}).get("legacy_scenarios") or {}
    if scenario is None:
        rows = list(legacy_scenarios.values())
    else:
        rows = _apply_scenario_overrides(legacy_scenarios, scenario)
    generic = execution.get("execution_v1") or {}
    driver = str((generic.get("frequency") or {}).get("driver") or "") if generic else ""
    if driver == "steady_state":
        native = v4_execution.execute_hit_events(_fr_runtime_plan(plan, generic), {"evaluations": rows})
    elif driver in ("charge_dot_cycle", "charge_driver"):
        native = v4_execution.execute_charge_dot(_bh_runtime_plan(plan, generic), {"evaluations": rows})
    else:
        native = v4_execution.execute_generic(execution, {"evaluations": rows})
    native_schema = str(native.get("schema") or "")
    if native_schema == "le-dps-result-v4":
        normalized = "le-calculation-plan-v2"
    elif native_schema == "le-dps-result-v5":
        normalized = "le-calculation-plan-v3"
    elif native_schema == "le-dps-result-v1":
        normalized = "le-calculation-plan-v1"
    else:
        raise PlanV4Error(f"不支持的 native result schema：{native_schema!r}")
    result = _map_legacy_result_to_v6(plan, native, normalized)
    generic = execution.get("execution_v1") or {}
    if generic and generic.get("applications"):
        body_ids = {str(item.get("component_id")) for item in generic.get("components") or []}
        ledger = mechanics_execution.apply_application_identity(
            result.get("applications_ledger") or [], generic["applications"], body_ids
        )
        result["applications"] = ledger
        result["applications_ledger"] = ledger
        application = generic["applications"][0]
        output_ids = {
            str(row.get("component_id"))
            for row in ledger
            if row.get("component_id") and str(row.get("component_id")) not in body_ids
        }
        if not output_ids:
            raise PlanV4Error("BH application 输出单元缺少 component_id")
        if len(output_ids) != 1:
            raise PlanV4Error(f"BH application 输出单元 component_id 不唯一：{sorted(output_ids)}")
        output_component_id = next(iter(output_ids))
        application_component = {
            "component_id": output_component_id,
            "kind": "application",
            "application_id": str(application.get("application_id") or ""),
            "ailment_id": str(application.get("ailment_id") or ""),
            "damage_type": application.get("damage_type"),
            "damage_channel": application.get("damage_channel"),
            "resistance_key": application.get("resistance_key"),
            "scope": application.get("scope"),
        }
        # application output unit is exposed via application_units; result.components
        # keeps body/dot components for the report renderer contract.
        units = list(result.get("application_units") or [])
        units = [item for item in units if str(item.get("component_id")) != output_component_id]
        units.append(application_component)
        result["application_units"] = units
    validate_result_v6(plan, result)
    return result


def _crit_factor(component: Mapping[str, Any]) -> float:
    if not component.get("crit_capable"):
        return 1.0
    return 1.0 + float(component.get("crit_chance") or 0.0) * (
        float(component.get("crit_multiplier") or 1.0) - 1.0
    )


def _component_hit_damage(component: Mapping[str, Any], target_multiplier: float) -> float:
    base = float(component.get("base_damage") or 0.0) + float(component.get("added_damage") or 0.0) * float(
        component.get("added_damage_scaling") or 0.0
    )
    scaled = base * (1.0 + float(component.get("increased") or 0.0)) * float(component.get("more") or 1.0)
    return scaled * _crit_factor(component) * float(target_multiplier)


def _target_multiplier(plan: Mapping[str, Any], ref: str) -> float:
    entry = ((plan.get("target_side") or {}).get("components") or {})[str(ref)]
    policy = targetside.TargetSidePolicy(
        penetration_mode=str(entry["strategy"]),
        negative_resistance_clamp=str(entry["negative_resistance_policy"]),
        dr_source=targetside.DR_FROM_SCENARIO,
    )
    resolved = targetside.compute_target_multiplier(
        policy=policy,
        resistance=float(entry["resistance"]),
        monster_damage_reduction=float(entry["damage_reduction"]),
        penetration=float(entry["penetration"]),
    )
    return float(resolved.multiplier)


def _charge_timeline(
    *,
    condition: str,
    horizon_s: float,
    max_charges: int,
    recharge_s: float,
    cast_time_s: float,
    instance_duration_s: float,
) -> dict[str, Any]:
    if condition not in ("charge_available_and_action_free", "wait_for_all_instances_expired"):
        raise PlanV4Error(f"未知 charge condition：{condition!r}")
    eager = condition == "charge_available_and_action_free"
    charges = float(max_charges)
    next_charge = recharge_s
    casts: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = [{"t": 0.0, "charges": charges, "kind": "initial"}]
    active: list[float] = []
    t = 0.0
    while t <= horizon_s + 1e-9:
        while next_charge is not None and next_charge <= t + 1e-9:
            if charges < max_charges - 1e-9:
                charges += 1.0
                events.append({"t": next_charge, "charges": charges, "kind": "recharge"})
            next_charge = next_charge + recharge_s if charges < max_charges - 1e-9 else None
        active = [end for end in active if end > t + 1e-9]
        allowed = eager or not active
        if charges >= 1.0 - 1e-9 and allowed:
            charges -= 1.0
            spawn = t + cast_time_s
            end = spawn + instance_duration_s
            casts.append(
                {
                    "cast_id": f"cast-{len(casts) + 1}",
                    "cast_start_s": t,
                    "spawn_s": spawn,
                    "end_s": end,
                    "charges_before": charges + 1.0,
                    "charges_after": charges,
                }
            )
            active.append(end)
            if next_charge is None and charges < max_charges - 1e-9:
                next_charge = t + recharge_s
            t = t + cast_time_s if cast_time_s > 0 else t + 1e-9
            continue
        candidates = [horizon_s + 1.0]
        if next_charge is not None:
            candidates.append(next_charge)
        if active:
            candidates.append(min(active))
        nxt = min(candidates)
        if nxt <= t + 1e-9:
            t = t + 1e-9
        else:
            t = nxt
    return {"casts": casts, "charge_events": events}


def _max_concurrent_casts(casts: list[Mapping[str, Any]]) -> int:
    events: list[tuple[float, int]] = []
    for cast in casts:
        spawn = float(cast.get("spawn_s"))
        end = float(cast.get("end_s", spawn))
        if end <= spawn:
            continue
        events.append((spawn, 1))
        events.append((end, -1))
    events.sort(key=lambda item: (item[0], -item[1]))
    current = 0
    maximum = 0
    for _, delta in events:
        current += delta
        maximum = max(maximum, current)
    return maximum


def _simulate_policy(
    plan: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    casts: list[dict[str, Any]],
    *,
    horizon_s: float,
    windows_s: list[float],
) -> dict[str, Any]:
    components = plan["components"]
    applications = plan.get("applications") or []
    schedule = plan["schedule"]
    multipliers = {str(item.get("component_id")): _target_multiplier(plan, str(item.get("component_id"))) for item in components}
    app_multipliers = {
        str(item.get("application_id")): _target_multiplier(plan, str(item.get("application_id")))
        for item in applications
    }
    cast_records: list[dict[str, Any]] = []
    cohorts: list[dict[str, Any]] = []
    for cast in casts:
        start = float(cast["cast_start_s"])
        spawn = float(cast.get("spawn_s", start + float(schedule.get("cast_time_s") or 0.0)))
        record: dict[str, Any] = {
            "cast_id": cast["cast_id"],
            "cast_start_s": start,
            "spawn_s": spawn,
            "hit_components": [],
            "dot_ticks": [],
        }
        for component in components:
            cid = str(component["component_id"])
            if component["kind"] == "hit":
                damage = _component_hit_damage(component, multipliers[cid])
                record["hit_components"].append(
                    {"component_id": cid, "time_s": start, "damage": damage}
                )
            else:
                per_tick = _component_hit_damage(component, multipliers[cid])
                for index in range(int(component["tick_count"])):
                    record["dot_ticks"].append(
                        {
                            "component_id": cid,
                            "time_s": spawn + index * float(component["tick_interval_s"]),
                            "damage": per_tick,
                        }
                    )
        cast_records.append(record)
        for application in applications:
            clock = application["clock"]
            total_stacks = float(application["amount"]["stacks_per_roll"])
            for roll in range(int(clock["count"])):
                spawn_time = spawn + float(clock["first_at_s"]) + roll * float(clock["interval_s"])
                cohort_total = (
                    total_stacks
                    * float(application["base_damage"])
                    * app_multipliers[str(application["application_id"])]
                )
                tick_count = int(application["tick_count"])
                ticks = [
                    {
                        "time_s": spawn_time + index * float(application["tick_interval_s"]),
                        "damage": cohort_total / tick_count,
                    }
                    for index in range(tick_count)
                ]
                cohorts.append(
                    {
                        "application_id": str(application["application_id"]),
                        "ailment_id": str(application["ailment_id"]),
                        "cohort_index": len(cohorts) + 1,
                        "cast_id": cast["cast_id"],
                        "spawn_s": spawn_time,
                        "stacks": total_stacks,
                        "damage_total": cohort_total,
                        "expiry_s": spawn_time + float(application["duration_s"]),
                        "ticks": ticks,
                    }
                )
    windows: list[dict[str, Any]] = []
    for cut in sorted(set(float(item) for item in windows_s)):
        hit = 0.0
        dot = 0.0
        application_damage = 0.0
        tail = 0.0
        for record in cast_records:
            cast_start = float(record["cast_start_s"])
            for item in record["hit_components"]:
                if cast_start < cut - 1e-12:
                    hit += float(item["damage"])
            for tick in record["dot_ticks"]:
                if float(tick["time_s"]) < cut - 1e-12:
                    dot += float(tick["damage"])
                elif cast_start <= cut + 1e-12:
                    tail += float(tick["damage"])
        for cohort in cohorts:
            if float(cohort["spawn_s"]) > cut + 1e-12:
                continue
            for tick in cohort["ticks"]:
                if float(tick["time_s"]) < cut - 1e-12:
                    application_damage += float(tick["damage"])
                else:
                    tail += float(tick["damage"])
        total = hit + dot + application_damage
        windows.append(
            {
                "window_s": cut,
                "hit_damage": hit,
                "dot_damage": dot,
                "application_damage": application_damage,
                "total_damage": total,
                "dps": total / cut if cut else 0.0,
                "cast_count": sum(
                    1 for record in cast_records if float(record["cast_start_s"]) <= cut + 1e-12
                ),
                "unsettled_tail_damage": tail,
            }
        )
    horizon_window = next(
        item for item in windows if abs(float(item["window_s"]) - horizon_s) < 1e-12
    )
    component_totals: dict[str, float] = {}
    for record in cast_records:
        for item in record["hit_components"]:
            component_totals[item["component_id"]] = component_totals.get(item["component_id"], 0.0) + float(item["damage"])
        for item in record["dot_ticks"]:
            component_totals[item["component_id"]] = component_totals.get(item["component_id"], 0.0) + float(item["damage"])
    for cohort in cohorts:
        component_totals[cohort["application_id"]] = component_totals.get(cohort["application_id"], 0.0) + float(cohort["damage_total"])
    return {
        "casts": cast_records,
        "cast_schedule": casts,
        "cohorts": cohorts,
        "windows": windows,
        "totals": {
            "horizon_damage": horizon_window["total_damage"],
            "horizon_dps": horizon_window["dps"],
            "hit_damage": horizon_window["hit_damage"],
            "dot_damage": horizon_window["dot_damage"],
            "application_damage": horizon_window["application_damage"],
            "unsettled_tail_damage": horizon_window["unsettled_tail_damage"],
            "cast_count": len(casts),
            "max_concurrent": _max_concurrent_casts(casts),
        },
        "component_totals": component_totals,
    }


def _applications_ledger(plan: Mapping[str, Any], payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for component in plan["components"]:
        cid = str(component["component_id"])
        records.append(
            {
                "contribution_id": f"component:{cid}",
                "component_id": cid,
                "stat_key": "damage",
                "operation": "data_component",
                "role": str(component["kind"]),
                "status": "applied",
                "reason": "",
            }
        )
    for application in plan.get("applications") or []:
        aid = str(application["application_id"])
        records.append(
            {
                "contribution_id": f"application:{aid}",
                "component_id": aid,
                "stat_key": "ailment",
                "operation": "data_application",
                "role": "application",
                "status": "applied",
                "reason": "",
            }
        )
    return records


def _evaluation_policy_id(evaluation: Mapping[str, Any], primary_policy_id: str) -> str:
    if evaluation.get("primary"):
        return primary_policy_id
    wanted = str(evaluation.get("action_policy") or "")
    return wanted or primary_policy_id


def _policy_payload(payloads: list[Mapping[str, Any]], policy_id: str) -> Mapping[str, Any]:
    for payload in payloads:
        if payload.get("policy_id") == policy_id:
            return payload
    return payloads[0]


def validate_execution_contract(execution: Mapping[str, Any]) -> None:
    """S1 通用 execution 契约结构校验；旧 legacy execution 不在此强制（S2/S3 迁移）。"""
    execution = _require_object(execution, "execution")
    if execution.get("schema") != "le-plan-execution-v1":
        raise PlanV4Error("execution.schema 必须是 le-plan-execution-v1")
    for key in ("source_contract", "capabilities", "components", "applications", "effects", "sources", "frequency", "charge", "coverage"):
        if key not in execution:
            raise PlanV4Error(f"execution.{key} 缺失")
    if not execution["capabilities"]:
        raise PlanV4Error("execution.capabilities 不能为空")
    components = _require_object({}, "x") if False else execution["components"]
    if not isinstance(components, list) or not components:
        raise PlanV4Error("execution.components 不能为空")
    component_ids = set()
    for index, component in enumerate(components):
        component = _require_object(component, f"execution.components[{index}]")
        for key in ("component_id", "kind", "unit", "damage_channel", "damage_type", "resistance_key", "scope", "duration_s", "tick_interval_s", "cast_ref"):
            if key not in component:
                raise PlanV4Error(f"execution.components[{index}].{key} 缺失")
        if component["kind"] not in ("hit", "dot") or component["unit"] != "damage":
            raise PlanV4Error("execution.components kind/unit 非法")
        if component["component_id"] in component_ids:
            raise PlanV4Error("execution.components component_id 重复")
        component_ids.add(component["component_id"])
    applications = execution["applications"]
    if not isinstance(applications, list):
        raise PlanV4Error("execution.applications 必须是数组")
    for index, application in enumerate(applications):
        application = _require_object(application, f"execution.applications[{index}]")
        for key in ("application_id", "source_component", "damage_channel", "damage_type", "resistance_key", "scope", "duration_s", "max_stacks", "condition", "cohort_policy", "window_policy", "tail_policy"):
            if key not in application:
                raise PlanV4Error(f"execution.applications[{index}].{key} 缺失")
        if int(application["max_stacks"]) < 1:
            raise PlanV4Error("execution.applications max_stacks 必须 >=1")
        if str(application["cohort_policy"]) not in ("independent_cast", "merge"):
            raise PlanV4Error(f"未知 cohort_policy：{application['cohort_policy']!r}")
        if str(application["window_policy"]) not in ("half_open",):
            raise PlanV4Error(f"未知 window_policy：{application['window_policy']!r}")
        if str(application["tail_policy"]) not in ("include_unsettled", "exclude_unsettled"):
            raise PlanV4Error(f"未知 tail_policy：{application['tail_policy']!r}")
    effects = execution["effects"]
    if not isinstance(effects, list) or not effects:
        raise PlanV4Error("execution.effects 不能为空")
    if not isinstance(execution["sources"], list):
        raise PlanV4Error("execution.sources 必须是数组")
    frequency = _require_object(execution["frequency"], "execution.frequency")
    for key in ("driver", "horizon_s", "windows_s", "policies", "rotation", "tail_policy"):
        if key not in frequency:
            raise PlanV4Error(f"execution.frequency.{key} 缺失")
    if not frequency["windows_s"]:
        raise PlanV4Error("execution.frequency.windows_s 不能为空")
    if str(frequency.get("driver") or "") == "derived_event_rate":
        derivation = frequency.get("derivation")
        if not isinstance(derivation, dict):
            raise PlanV4Error("derived_event_rate 必须声明 execution.frequency.derivation")
        from .mechanics import frequency as frequency_module  # noqa: PLC0415

        try:
            frequency_module.validate_derivation(derivation)
        except frequency_module.FrequencyError as exc:
            raise PlanV4Error(f"execution.frequency.derivation 非法：{exc}") from exc
    actors = execution.get("actors")
    if actors is not None:
        if not isinstance(actors, list):
            raise PlanV4Error("execution.actors 必须是数组")
        from . import actor_pool  # noqa: PLC0415

        try:
            for actor in actors:
                actor_pool.validate_actor_declaration(actor)
        except actor_pool.ActorPoolError as exc:
            raise PlanV4Error(f"execution.actors 非法：{exc}") from exc
    stat_transfers = execution.get("stat_transfers")
    if stat_transfers is not None:
        if not isinstance(stat_transfers, list):
            raise PlanV4Error("execution.stat_transfers 必须是数组")
        from . import actor_pool  # noqa: PLC0415

        try:
            for rule in stat_transfers:
                actor_pool.validate_stat_transfer_rule(rule)
        except actor_pool.ActorPoolError as exc:
            raise PlanV4Error(f"execution.stat_transfers 非法：{exc}") from exc
    charge = _require_object(execution["charge"], "execution.charge")
    for key in ("enabled", "max_charges", "recharge_s", "cast_time_s", "instance_duration_s", "condition"):
        if key not in charge:
            raise PlanV4Error(f"execution.charge.{key} 缺失")
    source_objects = execution.get("sources") or []
    if not isinstance(source_objects, list) or not source_objects:
        raise PlanV4Error("execution.sources 不能为空")
    source_ids: set[str] = set()
    for source in source_objects:
        source = _require_object(source, "execution.sources item")
        source_id = str(source.get("source_id") or "")
        if not source_id:
            raise PlanV4Error("execution.sources[].source_id 缺失")
        if source_id in source_ids:
            raise PlanV4Error(f"execution.sources source_id 重复：{source_id!r}")
        source_ids.add(source_id)
        status = str(source.get("binding_status") or "")
        if status not in SOURCE_SUCCESS_STATUSES:
            raise PlanV4Error(f"execution.sources {source_id!r} 绑定状态不是明确成功：{status!r}")
    if not source_ids:
        raise PlanV4Error("execution.sources 无有效来源")
    ownership_keys: set[str] = set()
    for index, effect in enumerate(effects):
        if not effect.get("emits_contribution", True):
            continue
        refs = [str(item) for item in effect.get("source_refs") or []]
        if not refs:
            raise PlanV4Error(f"execution.effects[{index}] 缺少 source_refs")
        for ref in refs:
            if ref not in source_ids:
                raise PlanV4Error(f"execution.effects[{index}] source_ref {ref!r} 未解析到 sources")
        if not effect.get("ownership_key"):
            raise PlanV4Error(f"execution.effects[{index}] 缺少 ownership_key")
        ownership_text = json.dumps(effect["ownership_key"], ensure_ascii=False, sort_keys=True)
        if ownership_text in ownership_keys:
            raise PlanV4Error(f"execution.effects[{index}] ownership_key 重复/冲突：{ownership_text}")
        ownership_keys.add(ownership_text)

    missing = [key for key in (set(c for c in component_ids) | {a.get("source_component") for a in applications}) if key and key not in component_ids]
    for key in missing:
        raise PlanV4Error(f"execution references unknown component：{key}")
    coverage = _require_object(execution["coverage"], "execution.coverage")
    if coverage.get("status") not in ("complete", "partial", "from_execution"):
        raise PlanV4Error("execution.coverage.status 非法")
    if int(coverage.get("gap_count", -1)) != len(coverage.get("gaps") or []):
        raise PlanV4Error("execution.coverage gap_count 与 gaps 不一致")


def execute_plan_v4(
    plan: Mapping[str, Any],
    scenario: Mapping[str, Any] | None = None,
    runtime: Any = None,
) -> dict[str, Any]:
    validate_plan_v4(plan)
    if plan.get("execution"):
        execution = plan.get("execution") or {}
        capabilities = {str(item) for item in plan.get("capabilities") or []}
        generic_execution = execution.get("execution_v1") or {}
        schedule = generic_execution.get("frequency") or execution.get("schedule") or {}
        if (
            capabilities & {"hit_events", "multi_hit_events", "charge_schedule"}
            or str(schedule.get("driver") or "") in ("steady_state", "charge_driver")
            or schedule.get("charges")
        ):
            return _native_from_execution(plan, scenario)
        return _execute_native_v4(plan, scenario)
    if plan.get("assembly"):
        return _execute_assembly_plan(plan, scenario)
    base_scenario = copy.deepcopy(plan.get("scenario") or {})
    if scenario is not None:
        if not isinstance(scenario, Mapping):
            raise PlanV4Error("运行时 scenario 必须是对象")
        allowed_scenario_keys = {"states", "target", "primary_evaluation", "evaluations"}
        unknown_scenario = sorted(set(scenario) - allowed_scenario_keys)
        if unknown_scenario:
            raise PlanV4Error(f"运行时 scenario 含未知字段：{unknown_scenario}")
        runtime_target = scenario.get("target") or {}
        if not isinstance(runtime_target, Mapping):
            raise PlanV4Error("运行时 scenario.target 必须是对象")
        base_target = dict(base_scenario.get("target") or {})
        allowed_target_keys = {
            "resistance", "penetration", "damage_reduction", "monster_damage_reduction",
            "monster_level", "resistances",
        }
        unknown_target = sorted(set(runtime_target) - allowed_target_keys)
        if unknown_target:
            raise PlanV4Error(f"运行时 scenario.target 含未知字段：{unknown_target}")
        for key in ("resistance", "penetration", "damage_reduction", "monster_damage_reduction", "monster_level", "resistances"):
            if key in runtime_target:
                if key == "resistances":
                    if not isinstance(runtime_target[key], Mapping):
                        raise PlanV4Error("scenario.target.resistances 必须是对象")
                    base_target[key] = dict(runtime_target[key])
                else:
                    _finite(runtime_target[key], label=f"scenario.target.{key}")
                    base_target[key] = runtime_target[key]
        base_scenario["target"] = base_target
        if "states" in scenario:
            if not isinstance(scenario["states"], Mapping):
                raise PlanV4Error("运行时 scenario.states 必须是对象")
            base_scenario["states"] = dict(scenario["states"])
        if "primary_evaluation" in scenario and str(scenario["primary_evaluation"]) != str(plan["primary_evaluation"]):
            raise PlanV4Error("运行时 primary_evaluation 与 plan 不一致")
        if "evaluations" in scenario:
            ids = {str(item.get("evaluation_id")) for item in scenario["evaluations"]}
            plan_ids = {str(item["evaluation_id"]) for item in plan["schedule"]["evaluations"]}
            if ids != plan_ids:
                raise PlanV4Error("运行时 evaluations 与 plan 不一致")
    runtime_plan = copy.deepcopy(plan)
    runtime_plan["scenario"] = base_scenario
    # 运行时场景驱动 target_side 允许字段（抗性/穿透/DR），其余静态计划不变
    target_document = runtime_plan["target_side"]
    for ref, entry in target_document["components"].items():
        channel = str(entry["damage_channel"])
        target = base_scenario.get("target") or {}
        resistances = target.get("resistances") if isinstance(target.get("resistances"), dict) else {}
        resistance = float(resistances.get(channel, target.get("resistance", entry["resistance"])))
        penetration = float(target.get("penetration", entry["penetration"]))
        damage_reduction = float(
            target.get("damage_reduction", target.get("monster_damage_reduction", entry["damage_reduction"]))
        )
        policy = targetside.TargetSidePolicy(
            penetration_mode=str(entry["strategy"]),
            negative_resistance_clamp=str(entry["negative_resistance_policy"]),
            dr_source=targetside.DR_FROM_SCENARIO,
        )
        resolved = targetside.compute_target_multiplier(
            policy=policy,
            resistance=resistance,
            monster_damage_reduction=damage_reduction,
            penetration=penetration,
        )
        entry.update(
            {
                "resistance": resistance,
                "penetration": penetration,
                "damage_reduction": damage_reduction,
                **resolved.to_dict(),
            }
        )
    plan = runtime_plan
    schedule = runtime_plan["schedule"]
    horizon = float(schedule["horizon_s"])
    windows = [float(item) for item in schedule["windows_s"]]
    policy_payloads: list[dict[str, Any]] = []
    if schedule["kind"] == "single_cast":
        cast_time = float(schedule.get("cast_time_s") or 0.0)
        casts = [
            {
                "cast_id": "cast-1",
                "cast_start_s": cast_time,
                "spawn_s": cast_time,
                "end_s": cast_time + max(
                    [
                        float(item.get("duration_s") or 0.0)
                        for item in plan["components"]
                        if item.get("kind") == "dot"
                    ]
                    or [1e-9]
                ),
                "charges_before": None,
                "charges_after": None,
            }
        ]
        policy = {"policy_id": "single_cast", "condition": "charge_available_and_action_free"}
        payload = _simulate_policy(plan, schedule["evaluations"][0], casts, horizon_s=horizon, windows_s=windows)
        payload["policy_id"] = "single_cast"
        payload["condition"] = "explicit_single_cast"
        policy_payloads.append(payload)
    else:
        charges = schedule["charges"]
        for policy in schedule["action_policies"]:
            timeline = _charge_timeline(
                condition=policy["condition"],
                horizon_s=horizon,
                max_charges=int(charges["max_charges"]),
                recharge_s=float(charges["recharge_s"]),
                cast_time_s=float(charges["cast_time_s"]),
                instance_duration_s=float(charges["instance_duration_s"]),
            )
            casts = timeline["casts"]
            payload = _simulate_policy(plan, schedule["evaluations"][0], casts, horizon_s=horizon, windows_s=windows)
            payload["policy_id"] = policy["policy_id"]
            payload["condition"] = policy["condition"]
            payload["charge_events"] = timeline["charge_events"]
            policy_payloads.append(payload)
    # primary-first ordering and top-level projection (same rule as v5).
    primary_policy_id = "single_cast"
    if schedule["kind"] == "charge":
        primary_eval = next(item for item in schedule["evaluations"] if item["primary"])
        wanted = str(primary_eval.get("action_policy") or "")
        primary_policy_id = next(
            (
                policy["policy_id"]
                for policy in schedule["action_policies"]
                if policy["policy_id"] == wanted
            ),
            schedule["action_policies"][0]["policy_id"],
        )
    payloads = sorted(
        policy_payloads,
        key=lambda item: (0 if item["policy_id"] == primary_policy_id else 1, item["policy_id"]),
    )
    primary = payloads[0]
    applications = _applications_ledger(plan, primary)
    result: dict[str, Any] = {
        "schema": SCHEMA_RESULT,
        "version": "6.0.0",
        "plan_ref": {"plan_sha256": plan["plan_sha256"], "schema": plan["schema"]},
        "plan_sha256": plan["plan_sha256"],
        "subject": copy.deepcopy(plan["subject"]),
        "scenario": copy.deepcopy(base_scenario),
        "scenario_sha256": planmod.scenario_sha256(base_scenario),
        "execution_fingerprint": _sha256_text(
            _canonical({"plan": plan["execution_fingerprint"], "scenario_sha256": planmod.scenario_sha256(base_scenario)})
        ),
        "primary_evaluation": plan["primary_evaluation"],
        "evaluations": [
            {
                "evaluation_id": item["evaluation_id"],
                "primary": item["primary"],
                "action_policy": item.get("action_policy"),
                "policy_id": _evaluation_policy_id(item, primary_policy_id),
                "horizon_damage": _policy_payload(policy_payloads, _evaluation_policy_id(item, primary_policy_id))["totals"]["horizon_damage"],
                "horizon_dps": _policy_payload(policy_payloads, _evaluation_policy_id(item, primary_policy_id))["totals"]["horizon_dps"],
                "cast_count": _policy_payload(policy_payloads, _evaluation_policy_id(item, primary_policy_id))["totals"]["cast_count"],
                "max_concurrent": _policy_payload(policy_payloads, _evaluation_policy_id(item, primary_policy_id))["totals"]["max_concurrent"],
                "window_count": len(_policy_payload(policy_payloads, _evaluation_policy_id(item, primary_policy_id))["windows"]),
            }
            for item in sorted(
                plan["schedule"]["evaluations"],
                key=lambda entry: (0 if entry["primary"] else 1, str(entry["evaluation_id"])),
            )
        ],
        "primary_policy_id": primary_policy_id,
        "target_side": copy.deepcopy(plan["target_side"]),
        "presentation": copy.deepcopy(plan["presentation"]),
        "capabilities": copy.deepcopy(plan["capabilities"]),
        "policy_results": [
            {
                "policy_id": payload["policy_id"],
                "condition": payload["condition"],
                "primary": payload["policy_id"] == primary_policy_id,
                "casts": copy.deepcopy(payload["cast_schedule"]),
                "charge_events": copy.deepcopy(payload.get("charge_events") or []),
                "windows": copy.deepcopy(payload["windows"]),
                "totals": copy.deepcopy(payload["totals"]),
                "component_totals": copy.deepcopy(payload["component_totals"]),
            }
            for payload in payloads
        ],
        "windows": copy.deepcopy(primary["windows"]),
        "totals": copy.deepcopy(primary["totals"]),
        "components": [
            {
                "component_id": item["component_id"],
                "kind": item["kind"],
                "damage_type": item["damage_type"],
                "target_side": copy.deepcopy(plan["target_side"]["components"][str(item["component_id"])]),
                "damage_total": primary["component_totals"].get(str(item["component_id"]), 0.0),
                "presentation_label": item.get("presentation_label"),
            }
            for item in plan["components"]
        ],
        "applications": [
            {
                "application_id": item["application_id"],
                "ailment_id": item["ailment_id"],
                "damage_type": item["damage_type"],
                "target_side": copy.deepcopy(plan["target_side"]["components"][str(item["application_id"])]),
                "damage_total": primary["component_totals"].get(str(item["application_id"]), 0.0),
                "cohorts": [
                    copy.deepcopy(cohort)
                    for cohort in primary["cohorts"]
                    if cohort["application_id"] == item["application_id"]
                ],
                "presentation_label": item.get("presentation_label"),
            }
            for item in plan.get("applications") or []
        ],
        "application_units": [
            {"component_id": str(item["component_id"]), "kind": str(item["kind"])}
            for item in plan["components"]
        ]
        + [
            {"component_id": str(item["application_id"]), "kind": "application"}
            for item in plan.get("applications") or []
        ],
        "applications_ledger": applications,
        "contribution_application_summary": dps_result.application_summary(applications),
        "coverage": copy.deepcopy(plan.get("coverage") or {"status": "complete", "gap_count": 0, "gaps": []}),
        "issues": copy.deepcopy(plan.get("issues") or []),
        "assumptions": copy.deepcopy(plan.get("assumptions") or []),
        "limitations": copy.deepcopy(plan.get("limitations") or []),
        "consistency": {
            "single_cast_total_equals_components": True,
            "primary_first": True,
        },
        "implementation_fingerprints": {
            "engine": {"id": "le_calc.plan_v4", "version": "1.0.0"},
            "plan_sha256": plan["plan_sha256"],
        },
    }
    result["consistency"] = _consistency(plan, result)
    validate_result_v6(plan, result)
    return result


def _consistency(plan: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, bool]:
    component_sum = sum(float(item.get("damage_total") or 0.0) for item in result["components"])
    application_sum = sum(float(item.get("damage_total") or 0.0) for item in result["applications"])
    committed = component_sum + application_sum
    horizon = next(
        item for item in result["windows"] if abs(float(item["window_s"]) - float(plan["schedule"]["horizon_s"])) <= 1e-9
    )
    return {
        "component_plus_application_committed": committed >= horizon["total_damage"] - 1e-6,
        "primary_first": result["policy_results"][0]["primary"],
        "application_ledger_summary": bool(result["contribution_application_summary"]),
        "no_nan_inf": all(
            math.isfinite(float(value))
            for window in result["windows"]
            for key, value in window.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ),
    }


def _validate_derived_frequency(result: Mapping[str, Any]) -> None:
    """result-v6 的派生频率门禁：由 derivation + scenario.states 复算事件率/间隔/DPS/窗口计数。

    仅在结果显式携带 ``frequency``（derived_event_rate driver）时生效；其它计划不改变行为。
    """
    frequency = result.get("frequency")
    if not frequency:
        return
    from .mechanics import frequency as frequency_module  # noqa: PLC0415

    if str(frequency.get("driver") or "") != "derived_event_rate":
        raise PlanV4Error("result.frequency.driver 必须是 derived_event_rate")
    derivation = frequency.get("derivation")
    if not isinstance(derivation, dict):
        raise PlanV4Error("result.frequency 缺少 derivation，无法复算事件率")
    states = (result.get("scenario") or {}).get("states") or {}
    try:
        recomputed = frequency_module.derived_event_rate(derivation, states=states)
    except frequency_module.FrequencyError as exc:
        raise PlanV4Error(f"result.frequency.derivation 复算失败：{exc}") from exc
    interval = recomputed.get("event_interval_s")
    stored_interval = frequency.get("event_interval_s")
    if (
        interval is None
        or stored_interval is None
        or not math.isclose(float(interval), float(stored_interval), rel_tol=1e-9, abs_tol=1e-12)
    ):
        raise PlanV4Error("result.frequency.event_interval_s 与 derivation+states 复算不一致")
    for key in ("upstream_rate_per_s", "gate_probability", "spawn_rate_per_s", "event_rate_per_s"):
        if not math.isclose(
            float(recomputed[key]), float(frequency.get(key)), rel_tol=1e-9, abs_tol=1e-12
        ):
            raise PlanV4Error(f"result.frequency.{key} 与 derivation 复算不一致")
    single_hit = float(result["totals"]["hit_damage"])
    if not math.isclose(
        float(result["totals"]["horizon_dps"]),
        single_hit / float(interval),
        rel_tol=1e-9,
        abs_tol=1e-9,
    ):
        raise PlanV4Error("result.totals.horizon_dps 与 derived interval 复算不一致（单发/间隔）")
    for window in result.get("windows") or []:
        seconds = float(window["window_s"])
        expected_casts = math.ceil(seconds / float(interval) - 1e-12)
        if int(window["cast_count"]) != expected_casts:
            raise PlanV4Error(f"window[{seconds}].cast_count 与 derived interval 复算不一致")


#: ``hit:<damage_type>`` 通道展开只接受这些类型名（与 DAMAGE_CHANNELS 同源）。
_ASSEMBLY_HIT_CHANNELS = frozenset(DAMAGE_CHANNELS)
#: assembly 应用账本允许的状态（历史 v1/v2/v3 结果额外使用 condition_not_satisfied）。
_ASSEMBLY_LEDGER_STATUSES = ("applied", "not_applicable", "condition_not_satisfied", "unresolved")


def _assembly_legacy_schema(plan: Mapping[str, Any]) -> str:
    return str(((plan.get("assembly") or {}).get("legacy_schema")) or "")


def _assembly_execution_effects(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    execution = plan.get("execution") or {}
    generic = execution.get("execution_v1") or {}
    effects = generic.get("effects")
    if effects is None:
        effects = execution.get("effects")
    return [effect for effect in effects or [] if isinstance(effect, dict)]


def _assembly_declared_application_names(plan: Mapping[str, Any]) -> set[str]:
    """plan 侧声明的 application/ailment 名称（用于 ``dot:<ailment>`` 通道展开）。"""
    execution = plan.get("execution") or {}
    generic = execution.get("execution_v1") or {}
    names: set[str] = set()
    for item in plan.get("applications") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("application_id", "ailment_id", "name"):
            value = item.get(key)
            if value not in (None, ""):
                names.add(str(value))
    for item in generic.get("applications") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("application_id", "ailment_id", "name", "ledger_ref"):
            value = item.get(key)
            if value not in (None, ""):
                names.add(str(value))
    for item in (execution.get("parameters") or {}).get("ailments") or []:
        if not isinstance(item, Mapping):
            continue
        for key in ("ailment_id", "name"):
            value = item.get(key)
            if value not in (None, ""):
                names.add(str(value))
    return names


def _assembly_declared_component_refs(plan: Mapping[str, Any]) -> dict[str, Any]:
    """只从 plan/execution 声明构造 known 组件引用集合（绝不读取 result 自身）。

    来源：``plan.components``、``assembly.legacy_plan.components``、
    ``execution.execution_v1.components``、``execution.components``、
    ``plan.applications`` 与 execution 的 application 声明。v1/v2 装配会把
    ``hit:declared`` 展开成 ``hit:<damage_type>``（VC/Ballista 结果使用该形态），
    v3 会把 dot 声明展开成 ``dot:<damage_type|ailment>``（VC/BH 结果使用该形态）。

    ``bh-<ailment>`` 是 v3 BH 执行器在 plan 声明 ``execution.schedule.ignite`` 时
    生成的固定应用单元 id（见 v3 legacy 执行器的应用单元推导）；这里按
    plan 声明重建该 id，而不是从 result 收集。
    """
    execution = plan.get("execution") or {}
    generic = execution.get("execution_v1") or {}
    legacy_plan = (plan.get("assembly") or {}).get("legacy_plan") or {}
    exact: set[str] = set()
    kind_counts: dict[str, int] = {}
    for source in (
        plan.get("components"),
        legacy_plan.get("components"),
        generic.get("components"),
        execution.get("components"),
    ):
        for item in source or []:
            if not isinstance(item, Mapping):
                continue
            component_id = item.get("component_id")
            if component_id not in (None, ""):
                exact.add(str(component_id))
            kind = str(item.get("kind") or "")
            kind_counts[kind] = kind_counts.get(kind, 0) + 1
    for item in plan.get("applications") or []:
        if isinstance(item, Mapping):
            for key in ("application_id", "component_id"):
                value = item.get(key)
                if value not in (None, ""):
                    exact.add(str(value))
    prefixes: dict[str, str] = {}
    if kind_counts.get("hit"):
        prefixes["hit:"] = "hit"
    if kind_counts.get("dot"):
        prefixes["dot:"] = "dot"
    dot_suffixes = set(_ASSEMBLY_HIT_CHANNELS)
    dot_suffixes |= _assembly_declared_application_names(plan)
    schedule = execution.get("schedule") if isinstance(execution.get("schedule"), Mapping) else {}
    ignite = schedule.get("ignite") if isinstance(schedule, Mapping) else None
    if isinstance(ignite, Mapping) and ignite.get("ailment"):
        exact.add(f"bh-{str(ignite['ailment']).lower()}")
    return {"exact": exact, "prefixes": prefixes, "dot_suffixes": dot_suffixes}


def _assembly_is_known_component_ref(known: Mapping[str, Any], component_id: Any) -> bool:
    ref = str(component_id or "")
    if not ref:
        return False
    if ref in known["exact"]:
        return True
    for prefix, kind in known["prefixes"].items():
        if not ref.startswith(prefix):
            continue
        suffix = ref[len(prefix):]
        if not suffix:
            continue
        if kind == "dot":
            return suffix in known["dot_suffixes"]
        return suffix in _ASSEMBLY_HIT_CHANNELS
    return False


def _assembly_validate_component_refs(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """result 中所有组件级引用必须来自 plan 声明（禁止账本/结果自证 known）。"""
    known = _assembly_declared_component_refs(plan)

    def _check(where: str, component_id: Any) -> None:
        if not _assembly_is_known_component_ref(known, component_id):
            raise PlanV4Error(f"assembly {where} 引用 plan 未声明的组件：{component_id!r}")

    for record in result.get("applications_ledger") or []:
        if not isinstance(record, Mapping) or not str(record.get("component_id") or ""):
            raise PlanV4Error("assembly applications_ledger 存在悬空 component_id")
        _check("applications_ledger", record.get("component_id"))
    for unit in result.get("application_units") or []:
        if not isinstance(unit, Mapping) or not str(unit.get("component_id") or ""):
            raise PlanV4Error("assembly application_units 存在悬空 component_id")
        _check("application_units", unit.get("component_id"))
    for component in result.get("components") or []:
        if not isinstance(component, Mapping) or not str(component.get("component_id") or ""):
            raise PlanV4Error("assembly result.components 存在悬空 component_id")
        _check("result.components", component.get("component_id"))
    for application in result.get("applications") or []:
        if isinstance(application, Mapping) and application.get("component_id") not in (None, ""):
            _check("result.applications", application.get("component_id"))
    summary = result.get("contribution_application_summary") or {}
    if isinstance(summary, Mapping):
        for entry in summary.values():
            for component_id in (entry or {}).get("components") or []:
                _check("contribution_application_summary", component_id)
    for contribution in result.get("contributions") or []:
        if not isinstance(contribution, Mapping):
            continue
        if contribution.get("component_id") not in (None, ""):
            _check("result.contributions", contribution.get("component_id"))
        application = contribution.get("application") or {}
        for component_id in application.get("components") or []:
            _check("result.contributions.application", component_id)


def _assembly_validate_application_ledger(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """账本状态/计数与应用摘要必须与账本本身及 plan 声明一致。"""
    ledger = result.get("applications_ledger")
    if not isinstance(ledger, list) or not ledger:
        raise PlanV4Error("assembly applications_ledger 缺失或为空")
    for record in ledger:
        if not isinstance(record, Mapping):
            raise PlanV4Error("assembly applications_ledger 项必须是对象")
        status = str(record.get("status") or "")
        if status not in _ASSEMBLY_LEDGER_STATUSES:
            raise PlanV4Error(f"assembly ledger status 非法：{record.get('status')!r}")
        if not str(record.get("component_id") or ""):
            raise PlanV4Error("assembly applications_ledger 存在悬空 component_id")
    expected_summary = dps_result.application_summary(ledger)
    if result.get("contribution_application_summary") != expected_summary:
        raise PlanV4Error("assembly application summary 与账本不一致")
    status_counts: dict[str, int] = {}
    for entry in expected_summary.values():
        for status, count in ((entry or {}).get("statuses") or {}).items():
            status_counts[str(status)] = status_counts.get(str(status), 0) + int(count)
    if sum(status_counts.values()) != len(ledger):
        raise PlanV4Error("assembly application summary 计数与账本行数不一致")


def _assembly_validate_totals(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """totals/组件/窗口算式一致性（不使用 result 自证）。"""
    totals = result.get("totals")
    if not isinstance(totals, Mapping):
        raise PlanV4Error("assembly result.totals 缺失")
    for key in (
        "horizon_damage",
        "horizon_dps",
        "hit_damage",
        "dot_damage",
        "application_damage",
        "unsettled_tail_damage",
        "cast_count",
        "max_concurrent",
    ):
        if key not in totals:
            raise PlanV4Error(f"assembly result.totals 缺少 {key}")
        _finite(totals[key], label=f"assembly totals.{key}")
    components = result.get("components") or []
    if not isinstance(components, list) or not components:
        raise PlanV4Error("assembly result.components 缺失")
    component_sum = 0.0
    hit_sum = 0.0
    dot_sum = 0.0
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise PlanV4Error("assembly result.components 项必须是对象")
        damage_total = _finite(
            component.get("damage_total"), label=f"assembly components[{index}].damage_total"
        )
        if damage_total < 0:
            raise PlanV4Error("assembly components damage_total 不能为负")
        component_sum += damage_total
        kind = str(component.get("kind") or "")
        if kind == "hit":
            hit_sum += damage_total
        elif kind == "dot":
            dot_sum += damage_total
    legacy_schema = _assembly_legacy_schema(plan)
    if legacy_schema == "le-calculation-plan-v1":
        # v1 装配口径：totals.hit_damage 是单发命中，逐组件 damage_total 之和必须相等。
        if not math.isclose(hit_sum, float(totals["hit_damage"]), rel_tol=1e-9, abs_tol=1e-9):
            raise PlanV4Error("assembly totals.hit_damage 与 hit 组件 damage_total 之和不一致")
        if not math.isclose(dot_sum + hit_sum, component_sum, rel_tol=1e-12, abs_tol=1e-12):
            raise PlanV4Error("assembly 组件 kind 分类与 damage_total 合计不一致")
    windows = result.get("windows") or []
    frequency = result.get("frequency") or {}
    if windows and str(frequency.get("driver") or "") == "derived_event_rate":
        # 派生事件频率：每窗口伤害 = 单次组件伤害 × 该窗口 cast_count。
        single_hit = hit_sum
        single_dot = dot_sum
        for window in windows:
            if not isinstance(window, Mapping):
                raise PlanV4Error("assembly result.windows 项必须是对象")
            cast_count = int(window.get("cast_count") or 0)
            expected = cast_count * (single_hit + single_dot)
            if not math.isclose(
                float(window.get("total_damage") or 0.0), expected, rel_tol=1e-9, abs_tol=1e-9
            ):
                raise PlanV4Error(
                    f"assembly window[{window.get('window_s')}].total_damage 与组件单发×cast_count 不一致"
                )


def _assembly_validate_windows(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """窗口内部算式：hit+dot+application==total、dps==total/window_s、totals 绑定末窗口。"""
    windows = result.get("windows")
    if not isinstance(windows, list) or not windows:
        raise PlanV4Error("assembly result.windows 缺失")
    seconds: list[float] = []
    for index, window in enumerate(windows):
        if not isinstance(window, Mapping):
            raise PlanV4Error("assembly result.windows 项必须是对象")
        window_s = _finite(window.get("window_s"), label=f"assembly windows[{index}].window_s")
        if window_s <= 0:
            raise PlanV4Error("assembly windows.window_s 必须 > 0")
        seconds.append(window_s)
        hit = _finite(window.get("hit_damage"), label=f"assembly windows[{index}].hit_damage")
        dot = _finite(window.get("dot_damage"), label=f"assembly windows[{index}].dot_damage")
        application = _finite(
            window.get("application_damage"), label=f"assembly windows[{index}].application_damage"
        )
        total = _finite(window.get("total_damage"), label=f"assembly windows[{index}].total_damage")
        dps = _finite(window.get("dps"), label=f"assembly windows[{index}].dps")
        if min(hit, dot, application, total) < 0:
            raise PlanV4Error("assembly windows 伤害字段不能为负")
        if not math.isclose(hit + dot + application, total, rel_tol=1e-9, abs_tol=1e-6):
            raise PlanV4Error(
                f"assembly window[{window_s}].total_damage 与 hit+dot+application 不一致"
            )
        if not math.isclose(dps, total / window_s, rel_tol=1e-9, abs_tol=1e-6):
            raise PlanV4Error(f"assembly window[{window_s}].dps 与 total_damage/window_s 不一致")
    if seconds != sorted(seconds):
        raise PlanV4Error("assembly windows 未按时间排序")
    totals = result.get("totals") or {}
    last = windows[-1]
    if not math.isclose(
        float(totals["horizon_damage"]), float(last["total_damage"]), rel_tol=1e-9, abs_tol=1e-9
    ):
        raise PlanV4Error("assembly totals.horizon_damage 与末窗口不一致")
    if not math.isclose(
        float(totals["cast_count"]), float(last["cast_count"]), rel_tol=1e-9, abs_tol=1e-9
    ):
        raise PlanV4Error("assembly totals.cast_count 与末窗口不一致")


def _assembly_declared_coverage(plan: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """plan 侧显式声明的 coverage（complete/partial）；``from_execution`` 视为投影指令。"""
    candidates: list[Any] = [plan.get("coverage")]
    execution = plan.get("execution") or {}
    generic = execution.get("execution_v1") or {}
    candidates.append(generic.get("coverage"))
    candidates.append((plan.get("assembly") or {}).get("legacy_plan", {}).get("coverage") if isinstance((plan.get("assembly") or {}).get("legacy_plan"), Mapping) else None)
    for candidate in candidates:
        if isinstance(candidate, Mapping) and str(candidate.get("status") or "") in ("complete", "partial"):
            return candidate
    return None


def _assembly_declared_coverage_gaps(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """plan/execution 声明的静态覆盖缺口（``from_execution`` 投影的真实数据源）。"""
    execution = plan.get("execution") or {}
    generic = execution.get("execution_v1") or {}
    facts = generic.get("facts") if isinstance(generic.get("facts"), Mapping) else None
    if facts is None:
        facts = execution.get("facts") if isinstance(execution.get("facts"), Mapping) else {}
    gaps: list[Mapping[str, Any]] = []
    for record in (facts or {}).get("coverage_base_gaps") or []:
        if isinstance(record, Mapping):
            gaps.append(record)
    coverage_input = generic.get("coverage_input")
    if isinstance(coverage_input, Mapping):
        for record in coverage_input.get("declared_gaps") or []:
            if isinstance(record, Mapping):
                gaps.append(record)
        if str(coverage_input.get("declared_status") or "") == "partial" and not gaps:
            gaps.append({"code": "declared:partial", "detail": "plan coverage_input 声明 partial"})
    return gaps


def _assembly_validate_coverage(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """coverage 必须与 plan 侧真实缺口一致，不能只凭 result 自身自洽。"""
    coverage = result.get("coverage")
    if not isinstance(coverage, Mapping):
        raise PlanV4Error("assembly result.coverage 缺失")
    status = str(coverage.get("status") or "")
    if status not in ("complete", "partial"):
        raise PlanV4Error("assembly result.coverage.status 非法")
    gaps = coverage.get("gaps")
    if not isinstance(gaps, list):
        raise PlanV4Error("assembly result.coverage.gaps 必须是数组")
    if coverage.get("gap_count") is None or int(coverage.get("gap_count")) != len(gaps):
        raise PlanV4Error("assembly result.coverage gap_count 与 gaps 不一致")
    if status == "complete" and gaps:
        raise PlanV4Error("assembly coverage.status=complete 但 gaps 非空")
    if status == "partial" and not gaps:
        raise PlanV4Error("assembly coverage.status=partial 但 gaps 为空")
    for gap in gaps:
        if not isinstance(gap, Mapping) or not str(gap.get("code") or "") or not str(gap.get("detail") or ""):
            raise PlanV4Error("assembly coverage.gaps 项缺少 code/detail")
    result_keys = {(str(gap.get("code")), str(gap.get("detail"))) for gap in gaps if isinstance(gap, Mapping)}
    declared = _assembly_declared_coverage(plan)
    if declared is not None:
        declared_status = str(declared.get("status"))
        if status != declared_status:
            raise PlanV4Error("assembly result.coverage.status 与 plan 声明不一致")
        if declared_status == "complete":
            return
        for gap in declared.get("gaps") or []:
            if not isinstance(gap, Mapping):
                continue
            if (str(gap.get("code")), str(gap.get("detail"))) not in result_keys:
                raise PlanV4Error("assembly result.coverage 缺少 plan 声明的覆盖缺口")
        return
    expected_gaps = _assembly_declared_coverage_gaps(plan)
    if expected_gaps:
        if status != "partial":
            raise PlanV4Error("plan/execution 声明了覆盖缺口，result.coverage 不能是 complete")
        for gap in expected_gaps:
            if (str(gap.get("code")), str(gap.get("detail"))) not in result_keys:
                raise PlanV4Error(
                    f"assembly result.coverage 缺少 plan 声明的覆盖缺口：{gap.get('code')!r}"
                )


def _assembly_effect_identity(effect: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(effect.get("stat_key") or ""),
        str(effect.get("operation") or ""),
        tuple(str(tag) for tag in effect.get("damage_tags") or []),
        effect.get("target"),
        str(effect.get("unit") or ""),
    )


def _assembly_contribution_identity(contribution: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        str(contribution.get("stat_key") or ""),
        str(contribution.get("operation") or ""),
        tuple(str(tag) for tag in contribution.get("damage_tags") or []),
        contribution.get("target"),
        str(contribution.get("unit") or ""),
    )


def _assembly_validate_contribution_value(effect: Mapping[str, Any], contribution: Mapping[str, Any]) -> None:
    """静态 value 必须与 plan 声明逐值一致；运行期派生 value（stack/state/movement）允许变化。"""
    if str(effect.get("value_mode") or "static") != "static":
        return
    plan_value = effect.get("value")
    result_value = contribution.get("value")
    if (
        isinstance(plan_value, (int, float))
        and not isinstance(plan_value, bool)
        and isinstance(result_value, (int, float))
        and not isinstance(result_value, bool)
    ):
        if not math.isclose(float(plan_value), float(result_value), rel_tol=1e-12, abs_tol=1e-12):
            raise PlanV4Error(
                f"assembly contributions[{contribution.get('contribution_id')}].value 与 plan 声明不一致"
            )
        return
    if plan_value != result_value:
        raise PlanV4Error(
            f"assembly contributions[{contribution.get('contribution_id')}].value 与 plan 声明不一致"
        )


def _assembly_validate_contributions(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """result.contributions 逐条绑定 plan 声明的 execution 行（核心字段不可篡改）。"""
    contributions = result.get("contributions")
    if not isinstance(contributions, list) or not contributions:
        raise PlanV4Error("assembly result.contributions 缺失")
    effects = _assembly_execution_effects(plan)
    if not effects:
        raise PlanV4Error("assembly plan.execution 未声明 effects，无法绑定 result.contributions")
    declared: dict[str, list[dict[str, Any]]] = {}
    for effect in effects:
        if effect.get("emits_contribution") is False:
            continue
        key = str(effect.get("contribution_id") or effect.get("effect_id") or "")
        if not key:
            continue
        declared.setdefault(key, []).append(effect)
    known = _assembly_declared_component_refs(plan)
    for contribution in contributions:
        if not isinstance(contribution, Mapping):
            raise PlanV4Error("assembly result.contributions 项必须是对象")
        key = str(contribution.get("contribution_id") or "")
        if not key or key not in declared:
            raise PlanV4Error(
                f"assembly result.contributions 引用 plan 未声明的 contribution：{key!r}"
            )
        identity = _assembly_contribution_identity(contribution)
        match = next(
            (effect for effect in declared[key] if _assembly_effect_identity(effect) == identity),
            None,
        )
        if match is None:
            raise PlanV4Error(
                f"assembly result.contributions[{key}] 的 stat_key/operation/damage_tags/target/unit 与 plan 声明不一致"
            )
        declared[key].remove(match)
        _assembly_validate_contribution_value(match, contribution)
        if match.get("included_static") is False and contribution.get("included") is True:
            raise PlanV4Error(f"assembly result.contributions[{key}] 被 plan 静态排除却标记 included")
        component_id = contribution.get("component_id")
        if component_id not in (None, "") and not _assembly_is_known_component_ref(known, component_id):
            raise PlanV4Error(
                f"assembly result.contributions[{key}] 引用 plan 未声明的组件：{component_id!r}"
            )


def _validate_assembly_result(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    """VC v4 assembly 结果：校验真实装配映射，不复算 v4 公式。"""
    if result.get("subject") != plan.get("subject"):
        raise PlanV4Error("assembly result.subject 与 plan 不一致")
    if result.get("presentation") != plan.get("presentation"):
        raise PlanV4Error("assembly result.presentation 与 plan 不一致")
    if [str(item) for item in result.get("capabilities") or []] != [
        str(item) for item in plan.get("capabilities") or []
    ]:
        raise PlanV4Error("assembly result.capabilities 与 plan 不一致")
    scenario_sha = planmod.scenario_sha256(
        {
            "states": (result.get("scenario") or {}).get("states") or {},
            "target": (result.get("scenario") or {}).get("target") or {},
        }
    )
    if result.get("scenario_sha256") != scenario_sha:
        raise PlanV4Error("assembly result.scenario_sha256 与 scenario 不一致")
    expected_exec = _sha256_text(
        _canonical({"plan": plan.get("execution_fingerprint"), "scenario_sha256": scenario_sha})
    )
    if result.get("execution_fingerprint") != expected_exec:
        raise PlanV4Error("assembly result.execution_fingerprint 不一致")
    coverage = result.get("coverage") or {}
    if coverage.get("status") not in ("complete", "partial"):
        raise PlanV4Error("assembly result.coverage.status 非法")
    if int(coverage.get("gap_count", -1)) != len(coverage.get("gaps") or []):
        raise PlanV4Error("assembly result.coverage gap_count 与 gaps 不一致")
    windows = result.get("windows") or []
    if not windows:
        raise PlanV4Error("assembly result.windows 缺失")
    seconds = [float(item["window_s"]) for item in windows]
    if seconds != sorted(seconds):
        raise PlanV4Error("assembly windows 未按时间排序")
    totals = result.get("totals") or {}
    last = windows[-1]
    if not math.isclose(float(totals["horizon_damage"]), float(last["total_damage"]), rel_tol=1e-9, abs_tol=1e-9):
        raise PlanV4Error("assembly totals.horizon_damage 与末窗口不一致")
    if not math.isclose(float(totals["cast_count"]), float(last["cast_count"]), rel_tol=1e-9, abs_tol=1e-9):
        raise PlanV4Error("assembly totals.cast_count 与末窗口不一致")
    primary = result.get("policy_results") or []
    if not primary:
        raise PlanV4Error("assembly policy_results 缺失")
    primary_id = result.get("primary_policy_id")
    if primary_id and not any(item.get("policy_id") == primary_id for item in primary):
        raise PlanV4Error("assembly primary_policy_id 不存在")
    policy_ids = {str(item.get("policy_id")) for item in primary}
    for evaluation in result.get("evaluations") or []:
        if str(evaluation.get("policy_id")) not in policy_ids:
            raise PlanV4Error("assembly evaluation 引用未知 policy")
    evaluation = next(
        (item for item in result.get("evaluations") or [] if item.get("evaluation_id") == result.get("primary_evaluation")),
        (result.get("evaluations") or [None])[0],
    )
    if evaluation is None:
        raise PlanV4Error("assembly evaluations 缺失")
    policy = next(item for item in primary if item.get("policy_id") == evaluation.get("policy_id"))
    for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent"):
        if not math.isclose(float(evaluation[key]), float(policy["totals"][key]), rel_tol=1e-9, abs_tol=1e-9):
            raise PlanV4Error(f"assembly evaluation.{key} 与 policy 不一致")
    # P1-C 结果门禁：known 组件只能来自 plan/execution 声明，账本/贡献引用、
    # 应用摘要、totals/windows 算式与 coverage 投影都必须与 plan 侧一致。
    # 注意：这里不再从 result.components / applications_ledger 收集 known（删除自证）。
    _assembly_validate_component_refs(plan, result)
    _assembly_validate_application_ledger(plan, result)
    _assembly_validate_totals(plan, result)
    _assembly_validate_windows(plan, result)
    _assembly_validate_coverage(plan, result)
    _assembly_validate_contributions(plan, result)
    _validate_derived_frequency(result)
    if not isinstance(result.get("effect_ledger"), dict):
        raise PlanV4Error("assembly result.effect_ledger 缺失")


def _validate_result_component_refs(result: Mapping[str, Any]) -> None:
    known = {str(item.get("component_id")) for item in result.get("components") or [] if item.get("component_id")}
    known |= {str(item.get("component_id")) for item in result.get("application_units") or [] if item.get("component_id")}
    for row in result.get("applications_ledger") or []:
        component_id = str(row.get("component_id") or "")
        if component_id and component_id not in known:
            raise PlanV4Error(f"applications_ledger 引用未知 component：{component_id!r}")
    for unit in result.get("application_units") or []:
        component_id = str(unit.get("component_id") or "")
        if component_id and component_id not in known:
            raise PlanV4Error(f"application_units 引用未知 component：{component_id!r}")


def validate_result_v6(plan: Mapping[str, Any], result: Mapping[str, Any]) -> None:
    generic_execution = (plan.get("execution") or {}).get("execution_v1") or {}
    if generic_execution.get("applications") and str((generic_execution.get("frequency") or {}).get("driver")) == "charge_dot_cycle":
        _validate_result_component_refs(result)
    if result.get("schema") != SCHEMA_RESULT:
        raise PlanV4Error(f"result schema 不是 {SCHEMA_RESULT}")
    if result.get("plan_sha256") != plan.get("plan_sha256"):
        raise PlanV4Error("result.plan_sha256 与 plan 不一致")
    if plan.get("assembly"):
        _validate_assembly_result(plan, result)
        return
    if str(result.get("primary_evaluation")) != str(plan.get("primary_evaluation")):
        raise PlanV4Error("primary_evaluation 与 plan 不一致")
    primary = result["policy_results"][0]
    if not primary.get("primary"):
        raise PlanV4Error("policy_results[0] 必须是 primary")
    windows = result.get("windows") or []
    expected_cuts = [float(item) for item in plan["schedule"]["windows_s"]]
    if [float(item["window_s"]) for item in windows] != sorted(expected_cuts):
        raise PlanV4Error("result.windows 与 plan.schedule.windows_s 不一致")
    # 从 casts/cohorts/tick 事件复算窗口和 totals（不使用预置 True）。
    recomputed = _simulate_policy(
        plan,
        plan["schedule"]["evaluations"][0],
        [
            {
                "cast_id": item["cast_id"],
                "cast_start_s": item["cast_start_s"],
                "spawn_s": item["spawn_s"],
                "end_s": item.get("end_s", float(item["spawn_s"]) + 1.0),
                "charges_before": item.get("charges_before"),
                "charges_after": item.get("charges_after"),
            }
            for item in primary.get("casts") or []
        ],
        horizon_s=float(plan["schedule"]["horizon_s"]),
        windows_s=list(plan["schedule"]["windows_s"]),
    )
    for stored, expected in zip(windows, recomputed["windows"]):
        for key in (
            "hit_damage",
            "dot_damage",
            "application_damage",
            "total_damage",
            "dps",
            "unsettled_tail_damage",
        ):
            if not math.isclose(float(stored[key]), float(expected[key]), rel_tol=1e-9, abs_tol=1e-9):
                raise PlanV4Error(f"result.windows[{stored['window_s']}].{key} 与事件复算不一致")
        if int(stored["cast_count"]) != int(expected["cast_count"]):
            raise PlanV4Error("window cast_count 与事件复算不一致")
    for key in ("horizon_damage", "horizon_dps", "hit_damage", "dot_damage", "application_damage"):
        if not math.isclose(
            float(result["totals"][key]),
            float(recomputed["totals"][key]),
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise PlanV4Error(f"result.totals.{key} 与事件复算不一致")
    if int(result["totals"]["cast_count"]) != len(primary.get("casts") or []):
        raise PlanV4Error("result.totals.cast_count 与 casts 不一致")
    if int(result["totals"]["max_concurrent"]) != int(recomputed["totals"]["max_concurrent"]):
        raise PlanV4Error("result.totals.max_concurrent 与事件复算不一致")
    policies = {str(item["policy_id"]): item for item in result.get("policy_results") or []}
    for evaluation in result.get("evaluations") or []:
        policy_id = str(evaluation.get("policy_id"))
        policy = policies.get(policy_id)
        if policy is None:
            raise PlanV4Error(f"evaluation {evaluation.get('evaluation_id')} 缺少 policy 结果")
        for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent"):
            if not math.isclose(
                float(evaluation[key]), float(policy["totals"][key]), rel_tol=1e-9, abs_tol=1e-9
            ):
                raise PlanV4Error(f"evaluation {evaluation.get('evaluation_id')}.{key} 与 policy 不一致")
        if int(evaluation["window_count"]) != len(policy["windows"]):
            raise PlanV4Error("evaluation.window_count 与 policy windows 不一致")
    presented = result.get("presentation") or {}
    if presented.get("title") != plan.get("presentation", {}).get("title"):
        raise PlanV4Error("presentation.title 与 plan 不一致")
    if result.get("subject") != plan.get("subject"):
        raise PlanV4Error("result.subject 与 plan 不一致")
    if [str(item) for item in result.get("capabilities") or []] != [
        str(item) for item in plan.get("capabilities") or []
    ]:
        raise PlanV4Error("result.capabilities 与 plan 不一致")
    if result.get("coverage") != plan.get("coverage"):
        raise PlanV4Error("result.coverage 与 plan coverage 投影不一致")
    if not isinstance(result.get("issues"), list) or not isinstance(result.get("assumptions"), list):
        raise PlanV4Error("result.issues/assumptions 必须是数组")
    expected_scenario_sha = planmod.scenario_sha256(result.get("scenario") or {})
    if result.get("scenario_sha256") != expected_scenario_sha:
        raise PlanV4Error("result.scenario_sha256 与 scenario 不一致")
    expected_exec = _sha256_text(
        _canonical({"plan": plan.get("execution_fingerprint"), "scenario_sha256": expected_scenario_sha})
    )
    if result.get("execution_fingerprint") != expected_exec:
        raise PlanV4Error("result.execution_fingerprint 与 plan/scenario 不一致")
    plan_eval_ids = [str(item["evaluation_id"]) for item in plan["schedule"]["evaluations"]]
    result_eval_ids = [str(item.get("evaluation_id")) for item in result.get("evaluations") or []]
    if sorted(plan_eval_ids) != sorted(result_eval_ids):
        raise PlanV4Error("result.evaluations 与 plan 评估集合不一致")
    if result.get("primary_evaluation") != plan.get("primary_evaluation"):
        raise PlanV4Error("result.primary_evaluation 与 plan 不一致")
    known_refs = {str(item["component_id"]) for item in plan["components"]} | {
        str(item["application_id"]) for item in plan.get("applications") or []
    }
    for record in result.get("applications_ledger") or []:
        if str(record.get("component_id")) not in known_refs:
            raise PlanV4Error(f"applications_ledger 引用未知组件：{record.get('component_id')!r}")
    if result.get("presentation") != plan.get("presentation"):
        raise PlanV4Error("result.presentation 与 plan 不一致")
    for application in result.get("applications") or []:
        cohorts = application.get("cohorts") or []
        if not cohorts:
            raise PlanV4Error(f"application {application.get('application_id')} 缺少 cohorts")
        cohort_total = sum(float(item.get("damage_total") or 0.0) for item in cohorts)
        if not math.isclose(
            float(application.get("damage_total") or 0.0),
            cohort_total,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise PlanV4Error(f"application {application.get('application_id')}.damage_total 与 cohorts 不一致")
        for cohort in cohorts:
            tick_total = sum(float(tick.get("damage") or 0.0) for tick in cohort.get("ticks") or [])
            if not math.isclose(
                float(cohort.get("damage_total") or 0.0),
                tick_total,
                rel_tol=1e-9,
                abs_tol=1e-9,
            ):
                raise PlanV4Error("cohort.damage_total 与 ticks 不一致")
    ledger = result.get("applications_ledger") or []
    if not ledger:
        raise PlanV4Error("applications_ledger 不能为空")
    if not all(item.get("status") in ("applied", "not_applicable", "unresolved") for item in ledger):
        raise PlanV4Error("applications_ledger status 非法")
    if result.get("contribution_application_summary") != dps_result.application_summary(ledger):
        raise PlanV4Error("application summary 与账本不一致")
    if (result.get("target_side") or {}).get("unit") != "fraction":
        raise PlanV4Error("target_side.unit 必须是 fraction")


def _v6_esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _v6_num(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{number:,.3f}".rstrip("0").rstrip(".") if abs(number) < 1e6 else f"{number:,.0f}"


def _v6_page(page_id: str, label: str, question: str, body: str) -> str:
    return (
        f'<section class="page" id="page-{_v6_esc(page_id)}" role="tabpanel" '
        f'aria-labelledby="tab-{_v6_esc(page_id)}" data-page="{_v6_esc(page_id)}">'
        f'<div class="page-head"><h2>{_v6_esc(label)}</h2>'
        f'<p class="page-question">{_v6_esc(question)}</p></div>{body}</section>'
    )


def _v6_table(headers: list[str], rows: list[list[Any]], *, note: str = "") -> str:
    head = "".join(f"<th>{_v6_esc(item)}</th>" for item in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{_v6_esc(cell)}</td>" for cell in row) + "</tr>" for row in rows
    )
    note_html = f'<p class="table-note">{_v6_esc(note)}</p>' if note else ""
    return (
        '<div class="table-wrap"><table class="data-table"><thead><tr>'
        + head
        + "</tr></thead><tbody>"
        + body
        + "</tbody></table></div>"
        + note_html
    )


def _v6_kpi(label: str, value: str, hint: str = "", cls: str = "") -> str:
    cls_extra = f" {cls}" if cls else ""
    return (
        f'<article class="kpi{cls_extra}"><p class="kpi-label">{_v6_esc(label)}</p>'
        f'<p class="kpi-value">{_v6_esc(value)}</p>'
        f'<p class="kpi-hint">{_v6_esc(hint)}</p></article>'
    )


def render_report_v6(result: Mapping[str, Any], binding: Mapping[str, Any] | None = None) -> str:
    """v6 玩家报告：唯一展示层 report_ui.unified_report（不重新计算伤害）。"""
    from .report_ui import unified_report  # noqa: PLC0415
    return unified_report.render(dict(result), dict(binding or {}))


def _assembly_stage_checks(plan: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, tuple[bool, str]]:
    checks: dict[str, tuple[bool, str]] = {}

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = (bool(ok), detail or ("PASS" if ok else "FAIL"))

    def guard(name: str, validate: Any) -> None:
        try:
            validate(plan, result)
            add(name, True)
        except Exception as exc:  # noqa: BLE001 - 阶段校验必须降级为 FAIL，不能抛异常
            add(name, False, str(exc) or exc.__class__.__name__)

    try:
        _validate_assembly_result(plan, result)
        add("result.recompute", True)
    except Exception as exc:  # noqa: BLE001 - 门禁聚合失败必须可见为 FAIL
        add("result.recompute", False, str(exc) or exc.__class__.__name__)
    add("result.schema", result.get("schema") == SCHEMA_RESULT)
    add("result.plan_ref", result.get("plan_sha256") == plan.get("plan_sha256"))
    add("result.target_side", bool(result.get("target_side")))
    add("result.presentation", result.get("presentation") == plan.get("presentation"))
    # 每个命名 check 走与 _validate_assembly_result 相同的真实断言，而不是只判字段存在。
    guard("result.windows", _assembly_validate_windows)
    guard("result.totals", _assembly_validate_totals)
    add("result.evaluations", bool(result.get("evaluations")))

    def validate_applications(plan_doc: Mapping[str, Any], result_doc: Mapping[str, Any]) -> None:
        _assembly_validate_application_ledger(plan_doc, result_doc)
        # 贡献行绑定属于 result.applications 的同一语义边界。
        _assembly_validate_contributions(plan_doc, result_doc)

    guard("result.applications", validate_applications)
    guard("result.coverage", _assembly_validate_coverage)
    return checks


def stage_validation_checks(
    plan: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    root: str | Path | None = None,
    case: Mapping[str, Any] | None = None,
) -> dict[str, tuple[bool, str]]:
    """为 stage 提供逐 check_id 的真实断言，而不是复制一次成功结果。"""
    if plan.get("assembly"):
        return _assembly_stage_checks(plan, result)
    checks: dict[str, tuple[bool, str]] = {}
    def add(name: str, ok: bool, detail: str = "") -> None:
        checks[name] = (bool(ok), detail or ("PASS" if ok else "FAIL"))

    add("result.schema", result.get("schema") == SCHEMA_RESULT, str(result.get("schema")))
    add(
        "result.plan_ref",
        result.get("plan_sha256") == plan.get("plan_sha256")
        and (result.get("plan_ref") or {}).get("plan_sha256") == plan.get("plan_sha256"),
        "result.plan_ref 必须绑定 plan sha",
    )
    add("result.target_side", (result.get("target_side") or {}).get("unit") == "fraction")
    add(
        "result.presentation",
        result.get("presentation") == plan.get("presentation")
        and bool((result.get("presentation") or {}).get("title")),
    )
    windows = result.get("windows") or []
    expected_cuts = [float(item) for item in plan["schedule"]["windows_s"]]
    windows_ok = [float(item.get("window_s")) for item in windows] == sorted(expected_cuts)
    windows_ok = windows_ok and all(
        all(
            key in item
            for key in ("hit_damage", "dot_damage", "application_damage", "total_damage", "dps", "cast_count", "unsettled_tail_damage")
        )
        for item in windows
    )
    add("result.windows", windows_ok)
    totals = result.get("totals") or {}
    totals_ok = all(
        key in totals
        for key in ("horizon_damage", "horizon_dps", "hit_damage", "dot_damage", "application_damage", "unsettled_tail_damage", "cast_count", "max_concurrent")
    )
    add("result.totals", totals_ok)
    plan_eval_ids = sorted(str(item["evaluation_id"]) for item in plan["schedule"]["evaluations"])
    result_eval_ids = sorted(str(item.get("evaluation_id")) for item in result.get("evaluations") or [])
    policies = {str(item["policy_id"]): item for item in result.get("policy_results") or []}
    eval_ok = plan_eval_ids == result_eval_ids and bool(policies)
    for evaluation in result.get("evaluations") or []:
        policy = policies.get(str(evaluation.get("policy_id")))
        if policy is None:
            eval_ok = False
            continue
        for key in ("horizon_damage", "horizon_dps", "cast_count", "max_concurrent"):
            if not math.isclose(float(evaluation.get(key)), float(policy["totals"][key]), rel_tol=1e-9, abs_tol=1e-9):
                eval_ok = False
    add("result.evaluations", eval_ok)
    known_refs = {str(item["component_id"]) for item in plan["components"]} | {
        str(item["application_id"]) for item in plan.get("applications") or []
    }
    applications_ok = all(
        str(record.get("component_id")) in known_refs
        for record in result.get("applications_ledger") or []
    )
    applications_ok = applications_ok and result.get("contribution_application_summary") == dps_result.application_summary(
        result.get("applications_ledger") or []
    )
    add("result.applications", applications_ok)
    add(
        "result.coverage",
        result.get("coverage") == plan.get("coverage")
        and (result.get("coverage") or {}).get("status") in ("complete", "partial"),
    )
    add("result.recompute", True)
    if case is not None:
        add(
            "result.plan_ref",
            result.get("subject") == plan.get("subject")
            and str((plan.get("subject") or {}).get("case_id")) == str(case.get("case_id")),
        )
    if root is not None and not plan.get("controlled_helper"):
        deps_ok = True
        for ref in plan.get("input_refs") or []:
            path = Path(root) / str(ref.get("path") or "")
            if not path.is_file() or planmod.sha256_file(str(path)) != ref.get("sha256"):
                deps_ok = False
                break
        add("result.recompute", deps_ok)
    else:
        try:
            validate_result_v6(plan, result)
        except PlanV4Error as exc:
            add("result.recompute", False, str(exc))
    return checks


__all__ = [
    "SCHEMA_PLAN",
    "SCHEMA_RESULT",
    "PLAN_VERSION",
    "CAPABILITIES",
    "CAPABILITY_MATRIX",
    "COMPATIBILITY_TARGET_POLICIES",
    "PlanV4Error",
    "compatibility_target_policy",
    "compile_plan_v4",
    "validate_plan_v4",
    "execute_plan_v4",
    "validate_result_v6",
    "render_report_v6",
    "documentation_for_plan_v4",
]
