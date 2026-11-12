"""plan-v3 编译器：BH 纯 DoT / 独立施加时钟 / 充能调度的共享定义装配。

该模块只消费 FrozenContext + ResolvedModel（共享定义 + 当前实例绑定 + 具名场景），
不 import 旧 BH 计算器；数值语义在 plan_executor_v3 中执行，编译期只做绑定与结构固化。
"""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping, Optional

from . import calculation_plan as planmod
from . import contribution_mapping as mapping
from . import effect_binding as binding
from . import model
from . import plan_handlers
from . import plan_compiler as core
from .plan_compiler import CompileResult, PlanCompileError
from .plan_executor_v3 import _resistance_totals

COMPILER_VERSION_V3 = "3.0.0"


def _fake_values(profile: Mapping[str, Any]) -> dict[str, Any]:
    states, target = core._fake_scenario(dict(profile))
    return {**states, **target}, states, target


def _row_meta(row: model.Contribution, *, category: str, handler_id: str, condition_ref: Optional[str] = None, condition_code_static: bool = False) -> dict[str, Any]:
    return {
        "category": category,
        "stage": plan_handlers.CATEGORY_STAGE[category],
        "handler_id": handler_id,
        "included_static": bool(row.included),
        "condition_status_static": row.condition_status,
        "condition_code": row.condition_code,
        "condition_code_static": condition_code_static,
        "condition_ref": condition_ref,
        "value_mode": "static",
        "coverage_class": (row.raw or {}).get("coverage_class"),
        "coverage_status": "mapped",
        "rule_id": None,
        "required": True,
    }


def _raw_tags(row: model.Contribution) -> set[str]:
    return set(mapping.decode_tag_names((row.raw or {}).get("tags")))


def _row_matches(row: model.Contribution, match: Mapping[str, Any]) -> bool:
    raw = row.raw or {}
    if "contribution_id" in match and str(row.contribution_id) != str(match["contribution_id"]):
        return False
    if "source_kind" in match and str(row.source_kind) != str(match["source_kind"]):
        return False
    if "raw_property" in match and raw.get("property") != match["raw_property"]:
        return False
    if "raw_special_tag" in match and raw.get("special_tag") != match["raw_special_tag"]:
        return False
    stat_payload = raw.get("stat") or {}
    if "raw_stat_property" in match and stat_payload.get("property") != match["raw_stat_property"]:
        return False
    if "raw_stat_special_tag" in match and stat_payload.get("specialTag") != match["raw_stat_special_tag"]:
        return False
    if "raw_tags_contains" in match:
        wanted = {str(item) for item in match["raw_tags_contains"]}
        if not wanted <= _raw_tags(row):
            return False
    if "stat_name_contains" in match and str(match["stat_name_contains"]) not in str(row.stat_name or ""):
        return False
    if "readable_text_contains" in match and str(match["readable_text_contains"]) not in str(raw.get("readable_text") or ""):
        return False
    if "item_slot" in match:
        instance_id = str(raw.get("item_instance_id") or "")
        if instance_id.split(".")[-1] != str(match["item_slot"]):
            return False
    if "stat_key" in match and str(row.stat_key) != str(match["stat_key"]):
        return False
    if "operation" in match and str(row.operation) != str(match["operation"]):
        return False
    if "target" in match and str(row.target or "") != str(match["target"]):
        return False
    return True


def _apply_reclassifications(rows: list[model.Contribution], rules: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    applied: list[dict[str, Any]] = []
    for index, rule in enumerate(rules):
        rule_id = str(rule.get("id") or f"reclassification:{index}")
        matched: list[str] = []
        for row in rows:
            if not _row_matches(row, rule.get("match") or {}):
                continue
            matched.append(row.contribution_id)
            if rule.get("exclude"):
                row.included = False
                row.exclusion_reason = str(rule.get("reason"))
                row.status = model.STATUS_DERIVED
                row.notes = (row.notes or "") + f" [plan-v3 排除: {rule.get('label') or rule_id}]"
                continue
            settings = rule.get("set") or {}
            if "stat_key" in settings:
                row.stat_key = str(settings["stat_key"])
            if "operation" in settings:
                row.operation = str(settings["operation"])
            if "target" in settings:
                row.target = settings["target"]
            if "damage_tags" in settings:
                row.damage_tags = [str(item) for item in settings["damage_tags"]]
            if "included" in settings:
                row.included = bool(settings["included"])
                if row.included:
                    row.exclusion_reason = None
            if "exclusion_reason" in settings:
                row.exclusion_reason = settings["exclusion_reason"]
            if "status" in settings:
                row.status = str(settings["status"])
            if "notes" in settings:
                row.notes = str(settings["notes"])
            row.notes = (row.notes or "") + f" [plan-v3 重分类: {rule.get('label') or rule_id}]"
        applied.append(
            {
                "rule_id": rule_id,
                "label": rule.get("label"),
                "matched": matched,
                "exclude": bool(rule.get("exclude")),
                "status": "applied" if matched else "unmatched",
            }
        )
    return applied


def _apply_overcapped_scaling(rows: list[model.Contribution], rules: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not rules:
        return []
    totals = _resistance_totals(rows)
    applied: list[dict[str, Any]] = []
    for index, rule in enumerate(rules):
        rule_id = str(rule.get("id") or f"overcapped:{index}")
        resistance_key = str(rule.get("source_resistance") or "").lower()
        threshold = float(rule.get("threshold_percent") or 0.0)
        cap = float(rule.get("cap_percent") or 0.0)
        overcap = float(totals.get(resistance_key, {}).get("overcapped_percent") or 0.0)
        steps = min(cap, overcap) / threshold if threshold > 0 else 0.0
        matched: list[str] = []
        for row in rows:
            if not _row_matches(row, rule.get("match") or {}):
                continue
            row.value = float(row.value) * steps
            row.status = model.STATUS_DERIVED
            raw = dict(row.raw or {})
            raw["overcapped_scaling"] = {
                "rule_id": rule_id,
                "source_resistance": resistance_key,
                "overcapped_percent": overcap,
                "threshold_percent": threshold,
                "cap_percent": cap,
                "steps": steps,
            }
            row.raw = raw
            row.notes = (
                (row.notes or "")
                + f" [plan-v3 阈值缩放: {overcap:g}% overcapped / {threshold:g}% step = ×{steps:g}]"
            ).strip()
            matched.append(row.contribution_id)
        applied.append(
            {
                "rule_id": rule_id,
                "label": rule.get("label"),
                "matched": matched,
                "overcapped_percent": overcap,
                "steps": steps,
                "status": "applied" if matched else "unmatched",
            }
        )
    return applied


def _skill_value(rows: list[model.Contribution], stat_key: str) -> float:
    return float(
        sum(
            float(row.value)
            for row in rows
            if row.active and row.stat_key == stat_key and row.operation == "increased"
        )
    )


def _node_points(calc: Mapping[str, Any], tree_id: str, node_id: str) -> int:
    for spec in (calc.get("skills") or {}).get("specializations") or []:
        tree = str((spec.get("tree") or {}).get("entity_id") or "")
        if tree.rsplit(":", 1)[-1] != tree_id:
            continue
        for node in spec.get("nodes") or []:
            if str(node.get("node_id")) == str(node_id):
                return int(node.get("points") or 0)
    return 0


def _catalog_skill_name(catalog: Any, ability_id: str) -> str:
    for skill in getattr(catalog, "_skills", []) or []:  # pragma: no cover - 目录实现细节兜底
        if str(skill.get("entity_id") or "").endswith(":" + ability_id):
            return str(skill.get("name") or "")
    return ability_id


def _verify_bh_skill_constants(profile: Mapping[str, Any], catalog: Any, *, ability_id: str) -> dict[str, Any]:
    """BH 纯 DoT 技能常量核对：基础伤害/类型/added scaling/持续时间/施放速度。"""
    declared_skill = profile.get("skill") or {}
    skill = catalog.skill_by_name(str(declared_skill.get("skill_name") or "Black Hole"))
    if not skill:
        return {"status": binding.BINDING_UNRESOLVED, "reason": "目录缺少技能 " + ability_id}
    skill_entity_id = str(skill.get("entity_id") or "")
    components = catalog.skill_components(skill_entity_id)
    damage_component = next(
        (item for item in components if item.get("component_type") == "RepeatedlyDamageEnemiesWithinRadius"),
        None,
    )
    duration_component = next(
        (item for item in components if item.get("component_type") == "DestroyAfterDuration"),
        None,
    )
    if damage_component is None or duration_component is None:
        return {"status": binding.BINDING_UNRESOLVED, "reason": "目录缺少 BH 伤害/持续时间组件"}
    config = (damage_component.get("config") or {}).get("baseDamageStats") or {}
    observed = {
        "base_damage": float(damage_component.get("base_damage") or 0.0),
        "damage_type": str(damage_component.get("damage_type") or ""),
        "added_damage_scaling": float(config.get("addedDamageScaling") or 0.0),
        "base_duration_s": float((duration_component.get("config") or {}).get("duration") or 0.0),
        "cast_time_ms": skill.get("cast_time_ms"),
        "extensions": skill.get("extensions") or {},
    }
    declared = profile.get("skill") or {}
    declared_base = {str(k): float(v) for k, v in (declared.get("base_damage") or {}).items()}
    differences: list[str] = []
    if abs(float(declared_base.get(observed["damage_type"], -1.0)) - observed["base_damage"]) > 1e-9:
        differences.append("base_damage")
    declared_dot = (profile.get("rules") or {}).get("dot_components") or {}
    if abs(float(declared_dot.get("added_damage_scaling") or 0.0) - observed["added_damage_scaling"]) > 1e-9:
        differences.append("added_damage_scaling")
    if abs(float(declared_dot.get("base_duration_s") or 0.0) - observed["base_duration_s"]) > 1e-9:
        differences.append("base_duration_s")
    extensions = observed["extensions"]
    animation_speed = float(declared.get("animation_speed") or 0.0)
    speed_multiplier = float(declared.get("speed_multiplier") or 0.0)
    base_rate = float(declared.get("base_cast_rate_per_s") or 0.0)
    if abs(animation_speed - float(extensions.get("as") or 0.0)) > 1e-9:
        differences.append("animation_speed")
    if abs(speed_multiplier - float(extensions.get("speedMultiplier") or 0.0)) > 1e-9:
        differences.append("speed_multiplier")
    if abs(base_rate - animation_speed * speed_multiplier) > 1e-9:
        differences.append("base_cast_rate_per_s")
    return {
        "status": binding.BINDING_VERIFIED if not differences else binding.BINDING_SEMANTICS_MISMATCH,
        "reason": "" if not differences else f"BH 技能常量与目录不一致：{differences}",
        "observed": observed,
        "differences": differences,
        "skill_name": _catalog_skill_name(catalog, ability_id) or "Black Hole",
    }


def compile_v3(context: core.FrozenContext, resolved_model: Any) -> CompileResult:
    resolved_model.validate()
    profile = resolved_model.assembler_view()
    calc = context.calc
    catalog = context.catalog
    case = context.case
    rules = profile.get("rules") or {}
    try:
        binding.validate_plan_v3_rules(rules)
    except binding.BindingError as exc:
        raise PlanCompileError(str(exc)) from exc
    ability = str(case.get("main_skill", {}).get("ability_id") or profile.get("ability_id") or "")
    if ability != profile.get("ability_id"):
        raise PlanCompileError("技能绑定不匹配")
    spec = next(
        (
            row
            for row in (calc.get("skills") or {}).get("specializations") or []
            if str((row.get("tree") or {}).get("entity_id", "")).endswith(":" + ability)
        ),
        None,
    )
    if spec is None:
        raise PlanCompileError(f"计算输入没有主技能专精：{ability}")
    skill_name = str((spec.get("owner_skill") or {}).get("name") or "")
    skill_constants = _verify_bh_skill_constants(profile, catalog, ability_id=ability)
    if skill_constants.get("status") != binding.BINDING_VERIFIED:
        raise PlanCompileError(str(skill_constants.get("reason") or "技能常量与目录不一致"))
    skill_name = str(skill_constants.get("skill_name") or skill_name or "Black Hole")
    fake_values, fake_states, fake_target = _fake_values(profile)
    facts = binding.equipment_facts(calc, catalog)
    class_entity_id = ((calc.get("character") or {}).get("class") or {}).get("entity_id") or ""
    class_base = mapping.class_base(catalog, class_entity_id)

    rows: list[model.Contribution] = []
    row_meta: dict[int, dict[str, Any]] = {}
    conditions: list[dict[str, Any]] = []
    condition_index: dict[str, str] = {}

    def register_condition(program: dict[str, Any]) -> str:
        digest = planmod.sha256_text(planmod.canonical_json(program))[:16]
        condition_id = f"cond:{digest}"
        if condition_id not in condition_index:
            conditions.append({"condition_id": condition_id, **program})
            condition_index[condition_id] = condition_id
        return condition_id

    def append(row: model.Contribution, meta: dict[str, Any]) -> None:
        existing = next((item for item in rows if item.contribution_id == row.contribution_id), None)
        if existing is not None:
            if abs(float(existing.value) - float(row.value)) > 1e-12:
                raise PlanCompileError(
                    f"同一 contribution_id 出现不同数值：{row.contribution_id}"
                    f"（{existing.value} vs {row.value}）"
                )
            return
        rows.append(row)
        row_meta[id(row)] = meta

    for row in mapping.base_contributions(catalog, calc, calc["dataset_id"]):
        append(row, _row_meta(row, category="base", handler_id="base_contribution-v1"))
    mods, _descriptions = mapping.mod_contributions(
        calc,
        dataset_id=calc["dataset_id"],
        states=fake_values,
        calc_input_label=case["inputs"]["calculation_input"],
        map_mod_fn=mapping.map_mod,
    )
    for row in mods:
        append(
            row,
            _row_meta(
                row,
                category="mod",
                handler_id="generic_mod_mapping-v1",
                condition_code_static=False,
            ),
        )
    passive_rows, deferred = mapping.passive_contributions(
        catalog,
        calc,
        dataset_id=calc["dataset_id"],
        condition_evaluator=binding.passive_condition_evaluator(facts),
    )
    for row in passive_rows:
        append(
            row,
            _row_meta(
                row,
                category="passive",
                handler_id="passive_mapping-v1",
                condition_code_static=bool(row.condition_code),
            ),
        )
    attribute_info = mapping.attribute_detail(rows, class_base)
    attributes = attribute_info["attributes"]
    per_attribute_gaps: list[dict[str, Any]] = []
    for row, stat in deferred:
        _row, problem = binding.resolve_per_attribute(row, stat, attributes)
        if problem:
            row.included = False
            row.exclusion_reason = f"per 缩放属性未声明：{problem}"
            row.status = model.STATUS_UNRESOLVED
            per_attribute_gaps.append(
                {
                    "code": "per_attribute_unresolved",
                    "detail": f"{row.contribution_id}：{problem}",
                    "count": 1,
                    "contribution_ids": [row.contribution_id],
                }
            )
        append(row, _row_meta(row, category="passive", handler_id="passive_mapping-v1"))

    # 技能节点绑定：条件转成 plan condition program，执行期按场景重新求值。
    node_results = binding.bind_node_bindings(calc, catalog, dict(profile), states=fake_values)
    for result in node_results:
        if result.contribution is None:
            continue
        rule = next(
            (item for item in profile.get("node_bindings") or [] if str(item.get("id")) == str(result.effect_id)),
            {},
        )
        # 同一节点多个 stat 绑定会有相同默认 contribution_id：用 stat/stat_index 消歧。
        raw_row = dict(result.contribution.raw or {})
        result.contribution.contribution_id = (
            f"skill:{raw_row.get('tree')}:{raw_row.get('node')}:"
            f"{result.contribution.stat_key}:{raw_row.get('stat_index')}"
        )
        terms: list[dict[str, Any]] = []
        for key in ([rule.get("condition")] if rule.get("condition") else []) + list(rule.get("requires_states") or []):
            terms.append({"kind": "control_true", "key": str(key)})
        condition_ref = register_condition({"op": "all", "terms": terms}) if terms else None
        scope = rule.get("scope")
        if scope:
            raw = dict(result.contribution.raw or {})
            raw["scope"] = str(scope)
            result.contribution.raw = raw
        append(
            result.contribution,
            _row_meta(
                result.contribution,
                category="node_binding",
                handler_id="node_binding-v1",
                condition_ref=condition_ref,
                condition_code_static=bool(result.contribution.condition_code),
            ),
        )

    # 已绑定的跨技能/被动 Buff：来源绑定与条件程序复用统一贡献链。
    from .conditional_node_buffs import contributions as buff_contributions
    for row, program in buff_contributions(profile):
        append(row, _row_meta(row, category="effect", handler_id="generic_mod_mapping-v1",
                             condition_ref=register_condition(program)))

    reclass_applied = _apply_reclassifications(rows, rules.get("row_reclassifications") or [])
    overcap_applied = _apply_overcapped_scaling(rows, rules.get("overcapped_scaling") or [])
    attributes = mapping.attribute_detail(rows, class_base)["attributes"]

    # --- dot 组件装配 ------------------------------------------------------
    dot_rule = rules["dot_components"]
    skill_profile = profile.get("skill") or {}
    skill_base_damage = {
        str(key): float(value) for key, value in (skill_profile.get("base_damage") or {}).items()
    }
    if not skill_base_damage:
        raise PlanCompileError("plan-v3 定义缺少 skill.base_damage")
    duration_stat_key = str(dot_rule.get("duration_stat_key") or "skill_duration")
    duration_increased = _skill_value(rows, duration_stat_key)
    duration_s = float(dot_rule["base_duration_s"]) * (1.0 + duration_increased)
    components: list[dict[str, Any]] = []
    for index, component in enumerate(dot_rule["components"]):
        for requirement in component.get("require_nodes") or []:
            tree_id = str(requirement.get("tree") or ability)
            node_id = str(requirement.get("node") or "")
            if _node_points(calc, tree_id, node_id) <= 0:
                raise PlanCompileError(
                    f"plan-v3 组件 {component.get('component_id')} 需要节点 {tree_id}:{node_id} 已投入点数"
                )
        base_source = component.get("base_source") or {}
        source_type = str(base_source.get("damage_type") or "Cold")
        if source_type not in skill_base_damage:
            raise PlanCompileError(f"skill.base_damage 缺少 {source_type} 基础伤害")
        damage_type = str(component.get("damage_type") or source_type)
        tags = [str(tag) for tag in component.get("tags") or []]
        if str(component.get("component_id")) in {item["component_id"] for item in components}:
            raise PlanCompileError(f"重复组件 ID：{component.get('component_id')}")
        components.append(
            {
                "component_id": str(component["component_id"]),
                "kind": "dot",
                "damage_type": damage_type,
                "tags": tags,
                "scope": "body",
                "crit_capable": False,
                "base_damage": float(skill_base_damage[source_type]),
                "added_damage_scaling": float(dot_rule["added_damage_scaling"]),
                "duration_s": duration_s,
                "tick_interval_s": float(dot_rule["tick_interval_s"]),
                "damage_at_start": bool(dot_rule["damage_at_start"]),
                "remainder_policy": str(dot_rule["remainder_policy"]),
            }
        )

    # --- Ignite 施加模型 ---------------------------------------------------
    ignite_rule = rules["ignite_model"]
    chance_source = ignite_rule["chance_source"]
    chance_value, chance_points, chance_check = binding.bind_node_value(
        calc,
        catalog,
        tree_id=str(chance_source.get("tree") or ability),
        node_id=str(chance_source.get("node") or ""),
        stat_index=int(chance_source.get("stat_index") or 0),
    )
    if not chance_check.ok or chance_value is None or chance_points <= 0:
        raise PlanCompileError(
            f"heat wave 机会来源绑定失败：{chance_check.status} {chance_check.detail or ''}"
        )
    schedule_rule = rules["charge_schedule"]
    charges_extra = int(round(_skill_sum_operation(rows, "charges", "added")))
    flat_total = _skill_sum_operation(rows, "cooldown_flat", "added")
    cdr_total = _skill_sum_operation(rows, "cooldown_reduction", "increased")
    schedule_payload = {
        "schema": "le-schedule-v3",
        "driver": "charge_dot_cycle",
        "horizon_s": float(schedule_rule["horizon_s"]),
        "windows_s": [float(item) for item in schedule_rule["windows_s"]],
        "cast": {
            "base_rate_per_s": float(skill_profile.get("base_cast_rate_per_s") or 0.0),
            "animation_speed": float(skill_profile.get("animation_speed") or 0.0),
            "speed_multiplier": float(skill_profile.get("speed_multiplier") or 1.0),
        },
        "charges": {
            "base_max_charges": int(schedule_rule["base_max_charges"]),
            "extra_max_charges": charges_extra,
            "base_recharge_s": float(schedule_rule["base_recharge_s"]),
            "flat_adjustment_s": flat_total,
            "cooldown_reduction": cdr_total,
        },
        "action_policies": copy.deepcopy(schedule_rule["action_policies"]),
        "primary_policy": str(schedule_rule["primary_policy"]),
        "ignite": {
            "ailment": str(ignite_rule.get("ailment") or "Ignite"),
            "damage_type": str(ignite_rule.get("damage_type") or "Fire"),
            "base_damage": float(ignite_rule["base_damage"]),
            "base_duration_s": float(ignite_rule["base_duration_s"]),
            "scaling_tags": [str(item) for item in ignite_rule["scaling_tags"]],
            "duration_stat_key": str(ignite_rule.get("duration_stat_key") or "ailment_duration"),
            "rolls_per_second": float(ignite_rule["rolls_per_second"]),
            "first_roll_at": str(ignite_rule["first_roll_at"]),
            "chance_total": float(chance_value),
            "chance_points": int(chance_points),
            "chance_per_point": float(chance_value) / float(chance_points),
        },
        "target": {
            "monster_damage_reduction_control": str(
                rules["dot_target"]["monster_damage_reduction_control"]
            ),
            "monster_damage_reduction_default": float(
                rules["dot_target"].get("monster_damage_reduction_default", 0.0)
            ),
        },
    }
    if not schedule_payload["cast"]["base_rate_per_s"] > 0:
        raise PlanCompileError("skill.base_cast_rate_per_s 必须 > 0")
    if not 0.0 <= cdr_total < 1.0:
        raise PlanCompileError(f"CDR 合计必须在 [0,1)：{cdr_total}")

    # --- effects / sources -------------------------------------------------
    effects: list[dict[str, Any]] = []
    used_effect_ids: dict[str, int] = {}
    for index, row in enumerate(rows):
        meta = row_meta[id(row)]
        serialized = core._serialize_effect(index, row, meta)
        serialized["stage"] = plan_handlers.CATEGORY_STAGE.get(str(meta.get("category") or "effect"), "damage_components")
        serialized["source_refs"] = [core._effect_source_id(row, meta)]
        serialized["ownership_key"] = core._ownership_payload(row)
        serialized["required"] = True
        serialized["support_status"] = str(meta.get("coverage_status") or "mapped")
        serialized["contribution_id"] = row.contribution_id
        base_id = serialized["effect_id"]
        if base_id in used_effect_ids:
            used_effect_ids[base_id] += 1
            serialized["effect_id"] = f"{base_id}#{used_effect_ids[base_id]}"
        else:
            used_effect_ids[base_id] = 0
        effects.append(serialized)
    sources_by_id: dict[str, dict[str, Any]] = {}
    for effect in effects:
        if not effect.get("emits_contribution"):
            continue
        for source_id in effect.get("source_refs") or []:
            owner = effect.get("owner") or {}
            source_facts = effect.get("source_facts") or {}
            source_kind = str(effect.get("source_kind") or "derived")
            identity: dict[str, Any] = {}
            if source_id.startswith("item_mod:"):
                source_kind = "item_mod"
                identity = {"mod_id": owner.get("mod_id") or source_id.split(":", 1)[1]}
            elif source_id.startswith("skill_node:"):
                source_kind = "skill_node"
                identity = dict(owner)
            elif source_id.startswith("passive_node:"):
                source_kind = "passive_node"
                identity = dict(owner)
            elif source_id.startswith("effect:"):
                source_kind = str(source_facts.get("kind") or "rule_source")
                identity = dict(source_facts) or {"effect_id": source_id.split(":", 1)[1]}
            elif source_id.startswith("base_character:"):
                source_kind = "base_character"
                identity = {"source_path": effect.get("source_path")}
            else:
                source_kind = "rule_source"
                identity = {"source_path": effect.get("source_path")}
            if source_kind not in plan_handlers.SOURCE_KINDS:
                source_kind = "rule_source"
            record = sources_by_id.get(source_id)
            if record is None:
                record = {
                    "source_id": source_id,
                    "kind": source_kind,
                    "identity": identity,
                    "semantics": {
                        "stat_keys": [],
                        "binding_status": effect.get("coverage_status") or "mapped",
                    },
                    "binding_status": effect.get("coverage_status") or "mapped",
                    "evidence": [],
                }
                sources_by_id[source_id] = record
            if effect.get("stat_key") not in record["semantics"]["stat_keys"]:
                record["semantics"]["stat_keys"].append(effect.get("stat_key"))
            for item in effect.get("evidence") or []:
                if item not in record["evidence"]:
                    record["evidence"].append(item)
    sources = list(sources_by_id.values())

    diagnostics: list[dict[str, Any]] = [
        {
            "code": gap["code"],
            "message": gap["detail"],
            "severity": "warning",
            "related_ids": list(gap["contribution_ids"]),
            "affects_coverage": True,
        }
        for gap in per_attribute_gaps
    ]
    for item in reclass_applied:
        if item["status"] != "applied":
            diagnostics.append(
                {
                    "code": "row_reclassification_unmatched",
                    "message": f"规则 {item['rule_id']} 未命中任何当前行",
                    "severity": "warning",
                    "related_ids": [],
                    "affects_coverage": False,
                }
            )
    for item in overcap_applied:
        if item["status"] != "applied":
            diagnostics.append(
                {
                    "code": "overcapped_rule_unmatched",
                    "message": f"规则 {item['rule_id']} 未命中当前行",
                    "severity": "warning",
                    "related_ids": [],
                    "affects_coverage": False,
                }
            )

    controls = core._control_entries(dict(profile))
    target_control = str(rules["dot_target"]["monster_damage_reduction_control"])
    if target_control not in {control["key"] for control in controls}:
        controls.append(
            {
                "control_id": f"target.{target_control}",
                "namespace": "target",
                "key": target_control,
                "label": "Monster Damage Reduction",
                "type": "number",
                "default": float(rules["dot_target"].get("monster_damage_reduction_default", 0.0)),
                "min": 0.0,
                "max": 1.0,
                "readonly": False,
            }
        )

    evaluations = [
        {
            "evaluation_id": str(item["evaluation_id"]),
            "label": item.get("label") or item["evaluation_id"],
            "metrics": list(item.get("metrics") or []),
            "primary": bool(item.get("primary")),
            "action_policy": str(item.get("action_policy")),
            "scenario_path": item.get("scenario_path"),
            "scenario_sha256": item.get("scenario_sha256"),
        }
        for item in resolved_model.evaluations
    ]
    if not evaluations or sorted(item["action_policy"] for item in evaluations) != ["eager", "patient"]:
        raise PlanCompileError("plan-v3 需要 eager/patient 两个具名评估")
    fingerprint = plan_handlers.implementation_fingerprint()
    parameters = {
        "skill": {
            "base_damage": skill_base_damage,
            "added_damage_scaling": float(dot_rule["added_damage_scaling"]),
            "crit_chance": 0.0,
            "crit_multiplier": 0.0,
            "cooldown_s": float(schedule_rule["base_recharge_s"]),
            "action_s": 1.0 / float(schedule_payload["cast"]["base_rate_per_s"]),
            "skill_name": skill_name,
            "base_duration_s": duration_s,
        },
        "crit": {},
        "ailments": [],
        "additional_chance": {},
        "damage_to_penetration": None,
        "cooldown_reduction": cdr_total,
        "attribute_scaling": {
            "attribute": str((rules.get("attribute_scaling") or {}).get("attribute") or "intelligence"),
            "increased_per_point": float(
                (rules.get("attribute_scaling") or {}).get("increased_per_point") or 0.0
            ),
        },
    }
    plan_payload: dict[str, Any] = {
        "schema": planmod.SCHEMA_V3,
        "subject": {
            "case_id": str(case.get("case_id") or ""),
            "build_id": str(
                (context.build_identity or {}).get("build_id")
                or (case.get("build") or {}).get("build_id")
                or f"build:{case.get('case_id') or 'synthetic'}"
            ),
            "revision_id": str(
                (context.build_identity or {}).get("revision_id")
                or (case.get("build") or {}).get("revision_id")
                or "synthetic-rev-1"
            ),
            "main_ability_id": ability,
        },
        "compiler": {
            "version": COMPILER_VERSION_V3,
            "origin": "definitions_v3",
            "implementation_fingerprint": fingerprint["compiler_files"],
            "fingerprint": fingerprint["compiler_fingerprint"],
        },
        "executor_requirements": {
            "handlers": plan_handlers.handler_versions("execute"),
            "executor_fingerprint": fingerprint["executor_fingerprint"],
        },
        "input_refs": copy.deepcopy(context.input_refs),
        "sources": sources,
        "facts": {
            "character": copy.deepcopy(calc.get("character")),
            "skill_name": skill_name,
            "attributes": copy.deepcopy(attributes),
            "equipment_facts": facts.to_dict(),
            "skill_constants": copy.deepcopy(skill_constants),
            "ledger_static": {
                "node_bindings": [result.to_dict() for result in node_results],
                "effects": [],
                "state_scalings": [],
                "movement_conversion": {},
                "attribute_scaling": {},
                "attribute_conversion": {},
                "cooldown_reduction": {},
                "ailment_chance_sources": [],
            },
            "reclassifications": reclass_applied,
            "overcapped_scaling": overcap_applied,
            "duration_increased": duration_increased,
            "coverage_base_gaps": per_attribute_gaps,
        },
        "parameters": parameters,
        "controls": controls,
        "conditions": conditions,
        "components": components,
        "effects": effects,
        "evaluation": {
            "stage_order": list(plan_handlers.STAGE_ORDER),
            "effect_stage": {effect["effect_id"]: effect["stage"] for effect in effects},
            "dependencies": plan_handlers.canonical_dependencies(),
            "ownership": "unique_by_ownership_key",
        },
        "schedule": schedule_payload,
        "diagnostics": diagnostics,
        "assumptions": [
            {
                "assumption_id": "rule:dot_tick_model",
                "status": "confirmed",
                "kind": "dot_tick",
                "scope": "damage",
                "note": (
                    f"{len(components)} 个纯 DoT 组件共享 {duration_s:g}s 持续时间；"
                    f"tick 间隔 {float(dot_rule['tick_interval_s']):g}s，"
                    "按 ceil_full_ticks（末跳为满伤害，不合并余量）计。"
                ),
            },
            {
                "assumption_id": "rule:ignite_application",
                "status": "confirmed",
                "kind": "ignite_clock",
                "scope": "damage",
                "note": (
                    f"Heat Wave 期望层数模型：chance_total={float(chance_value):g}"
                    f"（{chance_points} 点）、每秒 {float(ignite_rule['rolls_per_second']):g} 次判定、"
                    "首判在 spawn；每个判定批次独立计时并各自到期（不合并、不延长既有层数）。"
                ),
            },
            {
                "assumption_id": "rule:charge_schedule",
                "status": "confirmed",
                "kind": "charge",
                "scope": "frequency",
                "note": (
                    f"充能调度：max_charges={int(schedule_rule['base_max_charges']) + charges_extra}、"
                    f"recharge=({float(schedule_rule['base_recharge_s']):g}+{flat_total:g})"
                    f"×(1-{cdr_total:g})/(1+ICR)；eager/patient 两种 action policy 均输出。"
                ),
            },
        ] + [{"assumption_id": "buff:"+e["id"], "status": e["status"], "kind": "conditional_buff", "scope": "single_cast" if e.get("single_cast_only") else "fixed_conditions", "note": e["note"], "evidence": e["evidence"]} for e in profile.get("_bound_conditional_buffs") or []],
        "scope": {
            "skill": skill_name,
            "ailment_labels": ["Ignite"],
            "limitations": [str(item) for item in profile.get("limitations") or []],
        },
        "presentation": {
            "skill_name": skill_name,
            "skill_names": {
                row["tree"]["entity_id"].rsplit(":", 1)[-1]: row["tree"]["name"]
                for row in (calc.get("skills") or {}).get("specializations") or []
            },
            "character": copy.deepcopy(calc.get("character")),
            "equipment": [
                {
                    **copy.deepcopy(item),
                    "mods": copy.deepcopy(
                        [
                            mod
                            for mod in calc.get("mods") or []
                            if mod.get("item_instance_id") == item["instance_id"]
                        ]
                    ),
                }
                for item in calc.get("items") or []
            ],
            "controls": copy.deepcopy(profile.get("controls") or []),
        },
        "evaluations": evaluations,
    }
    plan = planmod.finalize_plan(plan_payload, schema=planmod.SCHEMA_V3)
    issues = planmod.validate_plan_structure(plan)
    if issues:
        raise PlanCompileError(
            "plan-v3 结构校验失败：" + "; ".join(f"{item['code']}:{item['message']}" for item in issues[:4])
        )
    return CompileResult("completed", "partial", plan, diagnostics)


def _skill_sum_operation(rows: list[model.Contribution], stat_key: str, operation: str) -> float:
    return float(
        sum(
            float(row.value)
            for row in rows
            if row.active and row.stat_key == stat_key and row.operation == operation
        )
    )


__all__ = ["COMPILER_VERSION_V3", "compile_v3"]
