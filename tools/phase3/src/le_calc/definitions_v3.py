"""definitions_v3：共享定义 + 当前实例选择器 + 场景（VC 首个切片）。"""
from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping, Optional

from . import calculation_plan as planmod
from . import effect_binding as binding
from . import plan_compiler
from .plan_model import ResolvedModel

SCHEMA = 'le-skill-template-v3'
SCENARIO_SCHEMA = 'le-skill-scenario-v3'
BINDING_VERSION = 'definitions_v3.selector-v1'

_TOP_ALLOWED = {'schema', 'template_id', 'ability_id', 'binding_version', 'title', 'definition', 'scenario_schema', 'migration', 'capabilities', 'applicability', 'preferred'}
_DEFINITION_ALLOWED = {
    'plan_version', 'skill', 'controls', 'state_sources', 'effects', 'node_bindings', 'rules',
    'attribute_conversion', 'ailments', 'coverage', 'limitations', 'evidence', 'evaluations',
    'assembly_plan_version', 'conditional_buff_library',
    # 通用 actor / 派生频率声明（数据声明式；未声明即行为不变）
    'actor', 'frequency',
}
_PLAN_VERSIONS = ('le-calculation-plan-v1', 'le-calculation-plan-v2', 'le-calculation-plan-v3', 'le-calculation-plan-v4')
_EVALUATION_ALLOWED = {'evaluation_id', 'label', 'metrics', 'primary', 'action_policy', 'evidence', 'notes'}
_SELECTOR_ALLOWED = {
    'kind', 'item_definition_entity_id', 'item_base_entity_id', 'expected_source',
    'expected_property', 'expected_special_tag', 'expected_tags', 'expected_unique_mod_index',
    'expected_item_mod_index', 'expected_description_index', 'selection', 'expected_definition_entity_id',
}

#: 模板可声明/路由可检查的稳定能力 ID（不是游戏机制确认状态）。
KNOWN_CAPABILITIES = frozenset({
    'hit_components', 'hit_events', 'multi_hit_events', 'item_effectiveness',
    'added_damage_conversion', 'dot_components', 'ignite_clock', 'charge_schedule',
    'attribute_scaling', 'target_side_explicit', 'presentation',
    # 通用 actor/派生频率能力（v4 起可声明；白名单不等于机制已确认）
    'minion_actor', 'explicit_stat_transfer', 'derived_event_rate',
})
#: 当前生产引擎按 plan 版本实际执行的能力白名单。
SUPPORTED_CAPABILITIES = {
    'le-calculation-plan-v1': frozenset({'hit_components', 'attribute_scaling'}),
    'le-calculation-plan-v2': frozenset({
        'hit_components', 'hit_events', 'multi_hit_events', 'item_effectiveness',
        'added_damage_conversion', 'attribute_scaling',
    }),
    'le-calculation-plan-v3': frozenset({
        'hit_components', 'dot_components', 'ignite_clock', 'charge_schedule',
        'attribute_scaling',
    }),
    'le-calculation-plan-v4': frozenset({
        'hit_components', 'dot_components', 'applications', 'charge_schedule',
        'presentation', 'attribute_scaling', 'hit_events', 'multi_hit_events',
        'item_effectiveness', 'added_damage_conversion', 'ignite_clock',
        'minion_actor', 'explicit_stat_transfer', 'derived_event_rate',
    }),
}
#: 适用性判定只读取这些稳定 FrozenContext 事实；缺事实返回 undetermined，不猜。
APPLICABILITY_FACTS = (
    'main_skill.ability_id', 'main_skill.class', 'main_skill.mastery', 'main_skill.damage_tags',
    'main_skill.component_damage_types', 'main_skill.conversion_premises',
    'build.revision_id', 'dataset.dataset_id',
)
_APPLICABILITY_ALLOWED = {
    'ability_id', 'required_class', 'required_skill_tags', 'forbidden_skill_tags',
    'required_conversion_premises',
    'required_facts', 'notes', 'evidence',
}


class DefinitionBindingError(ValueError):
    pass


def validate_template(template: Mapping[str, Any]) -> None:
    if not isinstance(template, Mapping):
        raise DefinitionBindingError("template 必须是对象")
    unknown = set(template) - _TOP_ALLOWED
    if unknown:
        raise DefinitionBindingError(f"template 顶层未知字段：{sorted(unknown)}")
    if template.get('schema') != SCHEMA:
        raise DefinitionBindingError(f"template schema 不是 {SCHEMA}：{template.get('schema')!r}")
    if template.get('binding_version') != BINDING_VERSION:
        raise DefinitionBindingError(f"binding_version 不支持：{template.get('binding_version')!r}")
    if not isinstance(template.get('ability_id'), str) or not template['ability_id']:
        raise DefinitionBindingError("template.ability_id 缺失")
    definition = template.get('definition')
    if not isinstance(definition, dict) or not definition:
        raise DefinitionBindingError("template.definition 必须是非空对象")
    unknown = set(definition) - _DEFINITION_ALLOWED
    if unknown:
        raise DefinitionBindingError(f"definition 未知字段：{sorted(unknown)}")
    missing = {'skill', 'controls', 'state_sources', 'effects', 'node_bindings', 'rules', 'attribute_conversion', 'ailments', 'coverage', 'limitations', 'evidence'} - set(definition)
    if missing:
        raise DefinitionBindingError(f"definition 缺少字段：{sorted(missing)}")
    for key, types in (
        ('skill', dict), ('controls', list), ('state_sources', dict), ('effects', list),
        ('node_bindings', list), ('rules', dict), ('ailments', list), ('coverage', dict),
        ('limitations', list), ('evidence', list),
    ):
        if not isinstance(definition[key], types):
            raise DefinitionBindingError(f"definition.{key} 类型必须是 {types.__name__}")
    plan_version = str(definition.get('plan_version') or 'le-calculation-plan-v1')
    if plan_version not in _PLAN_VERSIONS:
        raise DefinitionBindingError(f"definition.plan_version 不受支持：{plan_version!r}")
    if plan_version == 'le-calculation-plan-v4':
        assembly_version = str(definition.get('assembly_plan_version') or '')
        if assembly_version not in (
            'le-calculation-plan-v1', 'le-calculation-plan-v2', 'le-calculation-plan-v3', 'v4-actor'
        ):
            raise DefinitionBindingError(
                'plan-v4 定义必须声明 assembly_plan_version（v1/v2/v3 或 v4-actor）'
            )
        plan_version = assembly_version
    evaluations = definition.get('evaluations')
    if plan_version == 'le-calculation-plan-v3':
        if not isinstance(definition.get('rules'), dict) or not definition['rules'].get('dot_components'):
            raise DefinitionBindingError("plan-v3 定义必须声明 rules.dot_components（纯 DoT 组件来源）")
        if not definition['rules'].get('ignite_model'):
            raise DefinitionBindingError("plan-v3 定义必须声明 rules.ignite_model（独立施加时钟）")
        if not definition['rules'].get('charge_schedule'):
            raise DefinitionBindingError("plan-v3 定义必须声明 rules.charge_schedule（充能调度）")
        if not isinstance(evaluations, list) or not evaluations:
            raise DefinitionBindingError("plan-v3 定义必须声明 evaluations（eager/patient 两个具名评估）")
        seen_ids = set()
        policies = []
        primaries = 0
        for index, evaluation in enumerate(evaluations):
            if not isinstance(evaluation, dict):
                raise DefinitionBindingError(f"definition.evaluations[{index}] 必须是对象")
            unknown = set(evaluation) - _EVALUATION_ALLOWED
            if unknown:
                raise DefinitionBindingError(f"definition.evaluations[{index}] 含未支持字段：{sorted(unknown)}")
            evaluation_id = evaluation.get('evaluation_id')
            if not isinstance(evaluation_id, str) or not evaluation_id:
                raise DefinitionBindingError(f"definition.evaluations[{index}] 缺少 evaluation_id")
            if evaluation_id in seen_ids:
                raise DefinitionBindingError(f"evaluation_id 重复：{evaluation_id}")
            seen_ids.add(evaluation_id)
            policy = evaluation.get('action_policy')
            if policy not in ('eager', 'patient'):
                raise DefinitionBindingError(f"definition.evaluations[{index}].action_policy 必须是 eager/patient")
            policies.append(policy)
            if bool(evaluation.get('primary')):
                primaries += 1
        if primaries != 1:
            raise DefinitionBindingError("definition.evaluations 必须恰好一个 primary=true")
        if sorted(policies) != ['eager', 'patient']:
            raise DefinitionBindingError("plan-v3 evaluations 必须是 eager/patient 各一个")
    elif plan_version == 'le-calculation-plan-v2':
        components = (definition.get('skill') or {}).get('components') or {}
        if not isinstance(components, dict) or not components:
            raise DefinitionBindingError("plan-v2 定义必须声明 skill.components")
        if not any(kind in components for kind in ('hit', 'dot')):
            raise DefinitionBindingError("plan-v2 定义 skill.components 必须包含 hit 或 dot")
        if not isinstance(evaluations, list) or not evaluations:
            raise DefinitionBindingError("plan-v2 定义必须声明 evaluations（具名评估）")
        seen_ids: set[str] = set()
        primaries = 0
        for index, evaluation in enumerate(evaluations):
            if not isinstance(evaluation, dict):
                raise DefinitionBindingError(f"definition.evaluations[{index}] 必须是对象")
            unknown = set(evaluation) - _EVALUATION_ALLOWED
            if unknown:
                raise DefinitionBindingError(
                    f"definition.evaluations[{index}] 含未支持字段：{sorted(unknown)}"
                )
            evaluation_id = evaluation.get('evaluation_id')
            if not isinstance(evaluation_id, str) or not evaluation_id:
                raise DefinitionBindingError(f"definition.evaluations[{index}] 缺少 evaluation_id")
            if evaluation_id in seen_ids:
                raise DefinitionBindingError(f"evaluation_id 重复：{evaluation_id}")
            seen_ids.add(evaluation_id)
            metrics = evaluation.get('metrics')
            if not isinstance(metrics, list) or not metrics or any(
                not isinstance(item, str) or not item for item in metrics
            ):
                raise DefinitionBindingError(
                    f"definition.evaluations[{index}].metrics 必须是非空字符串数组"
                )
            if bool(evaluation.get('primary')):
                primaries += 1
        if primaries != 1:
            raise DefinitionBindingError("definition.evaluations 必须恰好一个 primary=true")
    elif evaluations is not None:
        raise DefinitionBindingError("plan-v1 定义不得声明 evaluations（需显式升级 plan_version）")
    capabilities = template.get('capabilities')
    if capabilities is not None:
        if not isinstance(capabilities, list) or any(
            not isinstance(item, str) or not item for item in capabilities
        ):
            raise DefinitionBindingError("template.capabilities 必须是字符串数组")
        if len(set(capabilities)) != len(capabilities):
            raise DefinitionBindingError("template.capabilities 不得重复")
        unknown = sorted(set(capabilities) - KNOWN_CAPABILITIES)
        if unknown:
            raise DefinitionBindingError(
                f"template.capabilities 含未知能力：{unknown}（允许 {sorted(KNOWN_CAPABILITIES)}）"
            )
    if capabilities:
        declared_capabilities = set(capabilities)
        rules = definition.get("rules") or {}
        if "minion_actor" in declared_capabilities and not definition.get("actor"):
            raise DefinitionBindingError("声明 minion_actor 能力必须提供 definition.actor")
        if "explicit_stat_transfer" in declared_capabilities:
            if not definition.get("actor"):
                raise DefinitionBindingError("声明 explicit_stat_transfer 能力必须提供 definition.actor")
            has_transfer = isinstance(rules, dict) and bool(
                rules.get("stat_transfer") or rules.get("attribute_scaling") or rules.get("node_attribute_scaled")
            )
            if not has_transfer:
                raise DefinitionBindingError(
                    "声明 explicit_stat_transfer 能力必须提供 rules.stat_transfer / "
                    "rules.attribute_scaling / rules.node_attribute_scaled 之一"
                )
        if "derived_event_rate" in declared_capabilities and not definition.get("frequency"):
            raise DefinitionBindingError(
                "声明 derived_event_rate 能力必须提供 definition.frequency"
            )
    applicability = template.get('applicability')
    if applicability is not None:
        if not isinstance(applicability, dict):
            raise DefinitionBindingError("template.applicability 必须是对象")
        unknown = sorted(set(applicability) - _APPLICABILITY_ALLOWED)
        if unknown:
            raise DefinitionBindingError(f"template.applicability 含未支持字段：{unknown}")
        declared_ability = applicability.get('ability_id')
        if declared_ability is not None and str(declared_ability) != str(template.get('ability_id')):
            raise DefinitionBindingError("template.applicability.ability_id 必须与 template.ability_id 一致")
        for key in ('required_skill_tags', 'forbidden_skill_tags'):
            value = applicability.get(key)
            if value is not None and (
                not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value)
            ):
                raise DefinitionBindingError(f"template.applicability.{key} 必须是字符串数组")
        required_facts = applicability.get('required_facts')
        if required_facts is not None:
            if not isinstance(required_facts, list) or any(
                not isinstance(item, str) or not item for item in required_facts
            ):
                raise DefinitionBindingError("template.applicability.required_facts 必须是字符串数组")
            unknown_facts = sorted(set(required_facts) - set(APPLICABILITY_FACTS))
            if unknown_facts:
                raise DefinitionBindingError(
                    f"template.applicability.required_facts 含未知事实：{unknown_facts}"
                )
        notes = applicability.get('notes')
        if notes is not None and (
            not isinstance(notes, list) or any(not isinstance(item, str) for item in notes)
        ):
            raise DefinitionBindingError("template.applicability.notes 必须是字符串数组")
    if not definition['controls']:
        raise DefinitionBindingError("definition.controls 不能为空")
    scenario_schema = template.get('scenario_schema')
    if not isinstance(scenario_schema, dict) or scenario_schema.get('schema') != SCENARIO_SCHEMA:
        raise DefinitionBindingError("scenario_schema 缺失或 schema 不正确")
    # 关键 selector 结构校验 + 禁止当前实例字段混入定义
    def check_source(source: Any, path: str) -> None:
        if not isinstance(source, dict):
            return
        selector = source.get('binding_selector')
        if selector is None:
            return
        if not isinstance(selector, dict):
            raise DefinitionBindingError(f"{path}.binding_selector 必须是对象")
        unknown = set(selector) - _SELECTOR_ALLOWED
        if unknown:
            raise DefinitionBindingError(f"{path}.binding_selector 未知字段：{sorted(unknown)}")
        if selector.get('kind') not in ('equipment_mod', 'unique_description'):
            raise DefinitionBindingError(f"{path}.binding_selector.kind 不支持")
        if not selector.get('item_definition_entity_id') and not selector.get('item_base_entity_id'):
            raise DefinitionBindingError(f"{path}.binding_selector 缺少 item_definition_entity_id/item_base_entity_id")
        if 'item_instance_id' in selector or 'mod_id' in selector:
            raise DefinitionBindingError(f"{path}.binding_selector 不允许当前实例字段")
        selection = selector.get('selection', 'one')
        if selection not in ('one', 'many'):
            raise DefinitionBindingError(f"{path}.binding_selector.selection 只能是 one/many")

    for control, source in (definition['state_sources'] or {}).items():
        check_source(source, f"state_sources.{control}")
    for effect in definition['effects']:
        check_source(effect.get('source'), f"effects.{effect.get('id')}.source")
    movement = ((definition['rules'] or {}).get('movement_conversion') or {}).get('source')
    check_source(movement, 'rules.movement_conversion.source')
    item_effectiveness = (definition['rules'] or {}).get('item_effectiveness')
    if isinstance(item_effectiveness, dict):
        check_source(item_effectiveness.get('source'), 'rules.item_effectiveness.source')

    def forbid_instance_fields(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ('item_instance_id', 'mod_id') and not path.endswith('binding_selector'):
                    raise DefinitionBindingError(f"{path}.{key} 是当前实例字段，不得进入共享定义")
                forbid_instance_fields(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                forbid_instance_fields(child, f"{path}.{index}")

    forbid_instance_fields(definition, 'definition')
    rules = definition.get('rules')
    if not isinstance(rules, dict):
        raise DefinitionBindingError("definition.rules 必须是对象")
    unknown_rules = set(rules) - binding.KNOWN_RULE_HANDLERS
    if unknown_rules:
        raise DefinitionBindingError(f"未知规则处理器：{sorted(unknown_rules)}")
    conversion = definition.get('attribute_conversion')
    if conversion is not None and (not isinstance(conversion, dict) or not conversion):
        raise DefinitionBindingError("definition.attribute_conversion 必须是非空对象或 null")
    if isinstance(conversion, dict) and not isinstance(conversion.get('rules'), str):
        raise DefinitionBindingError("definition.attribute_conversion.rules 必须是路径字符串")
    if isinstance(conversion, dict):
        expected = conversion.get('expected')
        if not isinstance(expected, dict) or not expected:
            raise DefinitionBindingError(
                "definition.attribute_conversion.expected 必须是非空稳定身份锚点"
            )
        if not (
            expected.get('item_definition_entity_id')
            or expected.get('definition_entity_id')
            or expected.get('base_entity_id')
        ):
            raise DefinitionBindingError(
                "definition.attribute_conversion.expected 缺少稳定定义身份（item_definition_entity_id/definition_entity_id）"
            )
    for effect in definition.get('effects') or []:
        source = effect.get('source') if isinstance(effect, dict) else None
        if not isinstance(source, dict) or source.get('kind') not in binding.KNOWN_EFFECT_SOURCE_KINDS:
            raise DefinitionBindingError(f"effect {effect.get('id')!r}.source 类型不支持")
        if effect.get('operation') not in ('added', 'increased', 'more', 'base', 'flag'):
            raise DefinitionBindingError(f"effect {effect.get('id')!r}.operation 不支持")
    for control, source in (definition.get('state_sources') or {}).items():
        if not isinstance(source, dict) or source.get('kind') not in (binding.EQUIPMENT_MOD, binding.UNIQUE_DESCRIPTION):
            raise DefinitionBindingError(f"state_sources.{control}.kind 不支持")


def _items(calc: Mapping[str, Any], selector: Mapping[str, Any]) -> list[dict[str, Any]]:
    definition = str(selector.get('item_definition_entity_id') or '')
    base = str(selector.get('item_base_entity_id') or '')
    result = []
    for item in calc.get('items') or []:
        if definition:
            if str(item.get('definition_entity_id') or '') == definition:
                result.append(item)
        elif base:
            if str(item.get('base_entity_id') or '') == base:
                result.append(item)
    return result


def _match_mods(calc: Mapping[str, Any], selector: Mapping[str, Any]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    kind = str(selector.get('kind') or '')
    for item in _items(calc, selector):
        mods = [m for m in item.get('mods') or [] if isinstance(m, dict)]
        if kind == 'unique_description':
            rows = [m for m in mods if '#unique_description#' in str(m.get('mod_id') or '')]
            index = selector.get('expected_description_index')
            if index is not None:
                for position, mod in enumerate(rows):
                    if position == int(index):
                        matches.append({'item': item, 'mod': mod, 'description_index': position})
                        break
                continue
            contains = str(selector.get('expected_text_contains') or '')
            for position, mod in enumerate(rows):
                if contains and contains not in str(mod.get('readable_text') or ''):
                    continue
                matches.append({'item': item, 'mod': mod, 'description_index': position})
            continue
        for position, mod in enumerate(mods):
            if selector.get('expected_source') is not None and mod.get('source') != selector.get('expected_source'):
                continue
            if selector.get('expected_property') is not None and mod.get('property') != selector.get('expected_property'):
                continue
            if selector.get('expected_special_tag') is not None and mod.get('special_tag') != selector.get('expected_special_tag'):
                continue
            if selector.get('expected_tags') is not None and mod.get('tags') != selector.get('expected_tags'):
                continue
            unique = mod.get('unique') or {}
            if selector.get('expected_unique_mod_index') is not None and unique.get('mod_index') != selector.get('expected_unique_mod_index'):
                continue
            if selector.get('expected_item_mod_index') is not None and position != selector.get('expected_item_mod_index'):
                continue
            matches.append({'item': item, 'mod': mod, 'position': position})
    return matches


def _source_with_match(source: dict[str, Any], match: Mapping[str, Any], kind: str) -> dict[str, Any]:
    resolved = copy.deepcopy(source)
    resolved.pop('binding_selector', None)
    resolved['item_instance_id'] = match['item'].get('instance_id')
    if kind == 'unique_description':
        resolved['description_index'] = int(match.get('description_index') or 0)
    else:
        resolved['mod_id'] = (match.get('mod') or {}).get('mod_id')
    return resolved


def _resolve_effect(source: dict[str, Any], calc: Mapping[str, Any], *, required: bool = True) -> list[dict[str, Any]]:
    selector = source.get('binding_selector')
    if not isinstance(selector, dict):
        return [copy.deepcopy(source)]
    matches = _match_mods(calc, selector)
    selection = str(selector.get('selection') or 'one')
    if not matches:
        if required:
            raise DefinitionBindingError("必需 effect 来源在当前实例中不存在")
        # 交给共享装配器的可选来源语义：不伪造实例，形成 not_applicable。
        resolved = copy.deepcopy(source)
        resolved.pop('binding_selector', None)
        resolved['_binding_source_absent'] = True
        return [resolved]
    if selection == 'one' and len(matches) > 1:
        raise DefinitionBindingError(f"selector 单实例选择歧义：命中 {len(matches)} 个实例")
    return [_source_with_match(source, match, str(selector.get('kind'))) for match in matches]


def _resolve_single(source: dict[str, Any], calc: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    selector = source.get('binding_selector')
    if not isinstance(selector, dict):
        return copy.deepcopy(source)
    matches = _match_mods(calc, selector)
    if len(matches) != 1:
        raise DefinitionBindingError(f"{label} selector 必须唯一命中，实际 {len(matches)} 个")
    return _source_with_match(source, matches[0], str(selector.get('kind')))


def _resolve_optional_single(
    source: dict[str, Any],
    calc: Mapping[str, Any],
    *,
    label: str,
    required: bool = True,
) -> dict[str, Any]:
    selector = source.get('binding_selector')
    if not isinstance(selector, dict):
        return copy.deepcopy(source)
    matches = _match_mods(calc, selector)
    if len(matches) == 0:
        if required:
            raise DefinitionBindingError(f"{label} 必需来源缺失")
        resolved = copy.deepcopy(source)
        resolved.pop('binding_selector', None)
        resolved['_binding_source_absent'] = True
        return resolved
    if len(matches) > 1:
        raise DefinitionBindingError(f"{label} selector 命中 {len(matches)} 个实例；单实例歧义")
    return _source_with_match(source, matches[0], str(selector.get('kind')))


def _fact(document: Mapping[str, Any], dotted: str) -> Any:
    node: Any = document
    for part in dotted.split('.'):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def skill_facts(case: Mapping[str, Any]) -> dict[str, Any]:
    """只收集稳定定义/当前 FrozenContext 事实；不读取 roll/实例 ID。"""
    facts: dict[str, Any] = {}
    main_skill = case.get('main_skill') if isinstance(case.get('main_skill'), dict) else {}
    tags = main_skill.get('damage_tags') or main_skill.get('tags')
    if isinstance(tags, list) and tags:
        facts['main_skill.damage_tags'] = [str(item) for item in tags if str(item)]
    for key in ('component_damage_types', 'conversion_premises'):
        value = (case.get('main_skill') or {}).get(key)
        if isinstance(value, list):
            facts[f'main_skill.{key}'] = [str(item) for item in value if str(item)]
    for key, value in (
        ('main_skill.ability_id', main_skill.get('ability_id')),
        ('main_skill.class', main_skill.get('class')),
        ('main_skill.mastery', main_skill.get('mastery')),
        ('build.revision_id', (case.get('build') or {}).get('revision_id')),
        ('dataset.dataset_id', case.get('dataset_expected', {}).get('dataset_id') if isinstance(case.get('dataset_expected'), dict) else None),
    ):
        if value not in (None, ''):
            facts[key] = value
    return facts


def check_applicability(template: Mapping[str, Any], case: Mapping[str, Any]) -> dict[str, Any]:
    """模板适用性判定：applied / not_applicable / undetermined，并列出缺失事实。"""
    applicability = template.get('applicability') if isinstance(template.get('applicability'), dict) else {}
    facts = skill_facts(case)
    case_ability = str(facts.get('main_skill.ability_id') or '')
    template_ability = str(template.get('ability_id') or '')
    reasons: list[str] = []
    missing: list[str] = []
    if not case_ability:
        missing.append('main_skill.ability_id')
    elif case_ability != template_ability:
        reasons.append(f'ability_id 不匹配：case={case_ability!r} template={template_ability!r}')
    for key in applicability.get('required_facts') or []:
        if key in facts:
            continue
        missing.append(str(key))
    required_class = applicability.get('required_class')
    if required_class:
        actual_class = facts.get('main_skill.class')
        if not actual_class:
            missing.append('main_skill.class')
        elif str(actual_class) != str(required_class):
            reasons.append(f'职业不匹配：required={required_class!r} case={actual_class!r}')
    required_tags = [str(item) for item in (applicability.get('required_skill_tags') or [])]
    forbidden_tags = [str(item) for item in (applicability.get('forbidden_skill_tags') or [])]
    if required_tags or forbidden_tags:
        tags = facts.get('main_skill.damage_tags')
        if not isinstance(tags, list):
            missing.append('main_skill.damage_tags')
        else:
            missing_tags = sorted(set(required_tags) - set(tags))
            if missing_tags:
                reasons.append(f'技能标签不满足：缺少 {missing_tags}（case={tags}）')
            blocked_tags = sorted(set(forbidden_tags) & set(tags))
            if blocked_tags:
                reasons.append(f'技能标签被禁止：{blocked_tags}')
    required_premises = [str(item) for item in (applicability.get('required_conversion_premises') or [])]
    if required_premises:
        premises = facts.get('main_skill.conversion_premises')
        if not isinstance(premises, list):
            missing.append('main_skill.conversion_premises')
        else:
            missing_premises = sorted(set(required_premises) - set(premises))
            if missing_premises:
                reasons.append(f'转换前提不满足：缺少 {missing_premises}（case={premises}）')
    if reasons:
        status = 'not_applicable'
    elif missing:
        status = 'undetermined'
    else:
        status = 'applicable'
    return {
        'status': status,
        'reasons': reasons,
        'missing_facts': sorted(set(missing)),
        'facts': facts,
        'applicability': {
            key: applicability.get(key)
            for key in ('ability_id', 'required_class', 'required_skill_tags', 'forbidden_skill_tags',
                        'required_conversion_premises', 'required_facts')
            if applicability.get(key) is not None
        },
    }


def _validate_control_value(control: Mapping[str, Any], value: Any, *, label: str) -> None:
    """按模板 control 的 type/min/max 校验场景数值；越界在绑定期明确拒绝。

    P2：不能把越界值留到执行期（例如 trap_detonation_rate_per_s=0 在旧模板会
    直到 ``FrequencyError`` 才失败）；错误信息必须包含 control 名与合法范围。
    """
    key = str(control.get('key') or '')
    control_type = str(control.get('type') or '')
    minimum = control.get('min')
    maximum = control.get('max')
    range_text = f"[{minimum!r}, {maximum!r}]" if minimum is not None or maximum is not None else "未声明"
    if control_type == 'boolean':
        if type(value) is not bool:
            raise DefinitionBindingError(
                f"{label} 控制项 {key!r} 类型必须是 boolean（模板 control 定义），"
                f"实际 {type(value).__name__}={value!r}"
            )
        return
    if control_type in ('integer', 'number', 'float'):
        if control_type == 'integer':
            if isinstance(value, bool) or not isinstance(value, int):
                raise DefinitionBindingError(
                    f"{label} 控制项 {key!r} 类型必须是 integer（模板 control 定义），"
                    f"实际 {type(value).__name__}={value!r}"
                )
        elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise DefinitionBindingError(
                f"{label} 控制项 {key!r} 类型必须是 number（模板 control 定义），"
                f"实际 {type(value).__name__}={value!r}"
            )
        if minimum is not None and value < minimum:
            raise DefinitionBindingError(
                f"{label} 控制项 {key!r} 值 {value!r} 越界：合法范围 {range_text}"
                "（模板 control min/max 定义）"
            )
        if maximum is not None and value > maximum:
            raise DefinitionBindingError(
                f"{label} 控制项 {key!r} 值 {value!r} 越界：合法范围 {range_text}"
                "（模板 control min/max 定义）"
            )
        return
    # 未知 control 类型：只在模板声明了数值范围时按数值校验，不扩张类型语义。
    if minimum is None and maximum is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise DefinitionBindingError(
            f"{label} 控制项 {key!r} 必须是数值（模板 control 声明了 min/max），"
            f"实际 {type(value).__name__}={value!r}"
        )
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise DefinitionBindingError(
            f"{label} 控制项 {key!r} 值 {value!r} 越界：合法范围 {range_text}"
            "（模板 control min/max 定义）"
        )


def bind_template(
    template: Mapping[str, Any],
    *,
    context: plan_compiler.FrozenContext,
    scenario: Mapping[str, Any],
    scenarios: Optional[list[Mapping[str, Any]]] = None,
) -> ResolvedModel:
    """把共享定义 + 当前 FrozenContext + 场景绑定为 ResolvedModel。

    ``scenarios`` 只用于 plan-v2 具名评估：每项必须含 ``evaluation_id`` 与
    ``scenario``（已加载的场景文档）以及可选的 ``scenario_path`` /
    ``scenario_sha256``。plan-v1 保持单场景语义。
    """
    validate_template(template)
    plan_version = str((template.get('definition') or {}).get('plan_version') or 'le-calculation-plan-v1')
    declared_capabilities = set(template.get('capabilities') or [])
    unsupported = sorted(declared_capabilities - SUPPORTED_CAPABILITIES.get(plan_version, frozenset()))
    if unsupported:
        raise DefinitionBindingError(
            f"capability_gap: 模板能力 {unsupported} 不受 {plan_version} 支持"
        )
    applicability = check_applicability(template, getattr(context, 'case', {}) or {})
    if applicability['status'] == 'not_applicable':
        raise DefinitionBindingError(
            'not_applicable: ' + '；'.join(applicability['reasons'])
        )
    if scenario.get('schema') != SCENARIO_SCHEMA:
        raise DefinitionBindingError(f"scenario schema 不是 {SCENARIO_SCHEMA}")
    if str(scenario.get('template_id') or '') != str(template.get('template_id') or ''):
        raise DefinitionBindingError("scenario.template_id 与模板不一致")
    ability = str(template.get('ability_id') or '')
    case_ability = str((context.case.get('main_skill') or {}).get('ability_id') or '')
    if ability != case_ability:
        raise DefinitionBindingError(f"template ability_id={ability!r} 与 case.main_skill={case_ability!r} 不一致")
    definition = copy.deepcopy(template['definition'])
    calc = context.calc
    from .conditional_node_buffs import bind as bind_conditional_buffs
    dynamic_defaults = bind_conditional_buffs(definition, context)
    for control, source in (definition.get('state_sources') or {}).items():
        definition['state_sources'][control] = _resolve_single(
            source, calc, label=f"state_sources.{control}"
        )
    effects: list[dict[str, Any]] = []
    for effect in definition.get('effects') or []:
        source = effect.get('source') or {}
        for resolved_source in _resolve_effect(
            source, calc, required=bool(effect.get('required', True))
        ):
            item = copy.deepcopy(effect)
            item['source'] = resolved_source
            effects.append(item)
    definition['effects'] = effects
    movement_rule = definition.get('rules', {}).get('movement_conversion') or {}
    movement = movement_rule.get('source')
    if isinstance(movement, dict):
        definition['rules']['movement_conversion']['source'] = _resolve_optional_single(
            movement,
            calc,
            label='rules.movement_conversion.source',
            required=bool(movement_rule.get('required', True)),
        )
    item_effectiveness = (definition.get('rules') or {}).get('item_effectiveness')
    if isinstance(item_effectiveness, dict) and isinstance(item_effectiveness.get('source'), dict):
        source = item_effectiveness['source']
        if isinstance(source.get('binding_selector'), dict):
            definition['rules']['item_effectiveness']['source'] = _resolve_single(
                source, calc, label='rules.item_effectiveness.source'
            )
    conversion = definition.get('attribute_conversion') or {}
    expected = conversion.get('expected') if isinstance(conversion, dict) else None
    if isinstance(expected, dict):
        selector = expected.get('binding_selector')
        if isinstance(selector, dict):
            matches = _match_mods(calc, selector)
            if len(matches) != 1:
                raise DefinitionBindingError(f"attribute_conversion selector 必须唯一命中，实际 {len(matches)} 个")
            expected['item_instance_id'] = matches[0]['item'].get('instance_id')
            expected.pop('binding_selector', None)
            expected.pop('item_definition_entity_id', None)
        elif expected.get('item_definition_entity_id'):
            matching = _items(calc, {'item_definition_entity_id': expected['item_definition_entity_id']})
            if len(matching) != 1:
                raise DefinitionBindingError(
                    f"attribute_conversion 定义命中 {len(matching)} 个当前实例；必须唯一"
                )
            expected['item_instance_id'] = matching[0].get('instance_id')
            expected.pop('item_definition_entity_id', None)
    coverage = definition.get('coverage') or {}
    for key in ('confirmed_inactive', 'not_applicable_mods', 'assumed_active_mods'):
        for row in coverage.get(key) or []:
            selector = row.get('binding_selector')
            if isinstance(selector, dict):
                matches = _match_mods(calc, selector)
                if len(matches) != 1:
                    raise DefinitionBindingError(f"coverage.{key} selector 必须唯一命中，实际 {len(matches)} 个")
                row['mod_id'] = matches[0]['mod'].get('mod_id')
                row.pop('binding_selector', None)
    # 派生事件率的 gate 在模板里可能只声明 skill_node 来源（points 来自当前输入专精）；
    # 契约校验要求 probability 已解析，因此在这里用同一 FrozenContext 做一次绑定期解析。
    # 该解析与编译期 resolve_derived_frequency 使用同一硬校验（点数只来自当前输入）。
    if isinstance(definition.get('frequency'), dict):
        from . import plan_compiler_actor  # noqa: PLC0415

        try:
            definition['frequency'] = plan_compiler_actor.resolve_derived_frequency(
                definition['frequency'],
                catalog=context.catalog,
                calc=calc,
            )
        except plan_compiler_actor.ActorCompileError as exc:
            raise DefinitionBindingError(f"definition.frequency 绑定失败：{exc}") from exc
    binding.validate_definition_contract(
        definition, allow_instance_fields=True, allow_unresolved_sources=True
    )
    plan_version = str(definition.get('plan_version') or 'le-calculation-plan-v1')

    def _checked_defaults(doc: Mapping[str, Any], *, label: str) -> dict[str, Any]:
        if not isinstance(doc, Mapping) or doc.get('schema') != SCENARIO_SCHEMA:
            raise DefinitionBindingError(f"{label} 不是 {SCENARIO_SCHEMA} 场景")
        if str(doc.get('template_id') or '') != str(template.get('template_id') or ''):
            raise DefinitionBindingError(f"{label}.template_id 与模板不一致")
        raw = doc.get('controls')
        if not isinstance(raw, dict):
            raise DefinitionBindingError(f"{label}.controls 必须是对象")
        return {**dynamic_defaults, **dict(raw)}

    controls = copy.deepcopy(definition.pop('controls'))
    keys = {str(control.get('key')) for control in controls}

    def _apply_defaults(defaults: Mapping[str, Any], *, label: str) -> None:
        if set(defaults) != keys:
            raise DefinitionBindingError(
                f"{label} 与模板控制项不一致：missing={sorted(keys - set(defaults))}, "
                f"extra={sorted(set(defaults) - keys)}"
            )
        for control in controls:
            key = str(control.get('key'))
            value = defaults[key]
            # P2：场景控件数值必须按模板 control min/max 在绑定期校验（越界立即拒绝）。
            _validate_control_value(control, value, label=label)
            control['default'] = value

    evaluations: list[dict[str, Any]] = []
    if plan_version in ('le-calculation-plan-v2', 'le-calculation-plan-v3') or (plan_version == 'le-calculation-plan-v4' and definition.get('evaluations')):
        declared = {
            str(item.get('evaluation_id')): item
            for item in definition.get('evaluations') or []
        }
        if not isinstance(scenarios, list) or not scenarios:
            raise DefinitionBindingError("plan-v2 绑定需要 case 提供 scenarios（具名评估场景）")
        supplied: dict[str, dict[str, Any]] = {}
        for item in scenarios:
            if not isinstance(item, Mapping):
                raise DefinitionBindingError("scenarios 条目必须是对象")
            evaluation_id = str(item.get('evaluation_id') or '')
            if not evaluation_id:
                raise DefinitionBindingError("scenarios 条目缺少 evaluation_id")
            if evaluation_id in supplied:
                raise DefinitionBindingError(f"scenario evaluation_id 重复：{evaluation_id}")
            supplied[evaluation_id] = dict(item)
        if set(supplied) != set(declared):
            raise DefinitionBindingError(
                f"scenarios 与模板 evaluations 不一致：missing={sorted(set(declared)-set(supplied))}, "
                f"extra={sorted(set(supplied)-set(declared))}"
            )
        for evaluation_id, meta in declared.items():
            entry = supplied[evaluation_id]
            doc = entry.get('scenario')
            label = f"scenarios[{evaluation_id}]"
            defaults = _checked_defaults(doc, label=label)
            _apply_defaults(defaults, label=label)
            target = dict(plan_compiler.DEFAULT_TARGET)
            target['monster_level'] = doc.get('monster_level')
            target['resistance'] = doc.get('resistance')
            if plan_version == 'le-calculation-plan-v3':
                target_rule = (definition.get('rules') or {}).get('dot_target') or {}
                control = str(target_rule.get('monster_damage_reduction_control') or 'monster_damage_reduction')
                extra_target = doc.get('target') or {}
                target[control] = extra_target.get(
                    control,
                    doc.get(control, target_rule.get('monster_damage_reduction_default', 0.0)),
                )
            scenario_payload = {
                'scenario_id': doc.get('scenario_id'),
                'monster_level': doc.get('monster_level'),
                'resistance': doc.get('resistance'),
                'controls': defaults,
            }
            evaluations.append(
                {
                    'evaluation_id': evaluation_id,
                    'label': meta.get('label') or evaluation_id,
                    'metrics': list(meta.get('metrics') or []),
                    'primary': bool(meta.get('primary')),
                    'action_policy': meta.get('action_policy'),
                    'scenario': scenario_payload,
                    'scenario_path': entry.get('scenario_path'),
                    'scenario_sha256': planmod.scenario_sha256(
                        {'states': defaults, 'target': target}
                    ),
                }
            )
        primary = next(item for item in evaluations if item['primary'])
        defaults = dict(primary['scenario']['controls'])
        scenario_payload = dict(primary['scenario'])
    else:
        if scenarios not in (None, []):
            raise DefinitionBindingError("plan-v1 定义不接受多场景 evaluations")
        defaults = _checked_defaults(scenario, label='scenario')
        _apply_defaults(defaults, label='scenario')
        scenario_payload = {
            'scenario_id': scenario.get('scenario_id'),
            'monster_level': scenario.get('monster_level'),
            'resistance': scenario.get('resistance'),
            'controls': dict(defaults),
        }
    return ResolvedModel(
        origin='definitions_v3',
        ability_id=ability,
        definition=definition,
        controls=controls,
        scenario=scenario_payload,
        instance={
            'case_id': context.case.get('case_id'),
            'build_id': (context.case.get('build') or {}).get('build_id'),
            'revision_id': (context.case.get('build') or {}).get('revision_id'),
            'calculation_input_sha256': next(
                (str(ref.get('sha256')) for ref in context.input_refs if ref.get('role') == 'calculation_input'),
                None,
            ),
        },
        evidence=list(template.get('migration') and [{'kind': 'migration', 'detail': str(template.get('migration'))}] or []),
        evaluations=evaluations,
    )

