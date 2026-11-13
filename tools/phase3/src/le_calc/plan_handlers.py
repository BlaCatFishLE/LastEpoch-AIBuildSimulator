"""计算计划 handler 静态注册表（编译器/执行器共用）。

* **编译期 handler**：把输入/绑定解释成计划 effects 的分类标签，仅作来源记录，
  执行器不会重新执行它们。
* **执行期 handler**：执行器唯一允许调用的数值/条件实现。执行器不得动态 import
  计划里给出的模块路径，只能通过本表按 ``handler_id`` 取实现。

handler 版本/实现指纹参与 plan.compiler 与 execution_fingerprint：改动执行语义
的实现文件会让旧计划不再匹配新执行器。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

VERSION = "1.0.0"

#: 编译期 handler（effect.handler_id 的合法取值）
COMPILE_HANDLERS: dict[str, str] = {
    "base_contribution-v1": "contributions/base attribute rows",
    "generic_mod_mapping-v1": "calculation-input modifier normalization",
    "passive_mapping-v1": "planner passive normalization",
    "node_binding-v1": "skill-node tree/node/points binding",
    "profile_effect-v1": "profile-v2 declared effect binding",
    "state_scaling-v1": "per-active-state rule",
    "movement_conversion-v1": "movement speed to damage conversion",
    "attribute_scaling-v1": "attribute ratio to increased damage",
    "attribute_conversion-v1": "strength to brutality conversion",
    "cooldown_reduction-v1": "cooldown node reduction",
    "ailment_chance-v1": "ailment application chance source",
    "template_effect-v1": "native skill-template effect declaration",
    "hit_events-v1": "plan-v2 base/return hit count binding",
    "rotation-v1": "plan-v2 rotation average factor",
    "item_effectiveness-v1": "plan-v2 item stat effectiveness rule",
    "added_damage_conversion-v1": "plan-v2 added damage pool conversion",
    "dot_components-v3": "plan-v3 pure-DoT body components and tick model",
    "ignite_model-v3": "plan-v3 independent ailment application clock (Heat Wave expected stacks)",
    "charge_schedule_bind-v3": "plan-v3 charge-based eager/patient cast scheduling",
    "dot_target-v3": "plan-v3 target resistance/DR resolution for DoT",
    "row_reclassifications-v3": "plan-v3 explicit row reclassification/exclusion",
    "overcapped_scaling-v3": "plan-v3 overcapped-resistance threshold scaling",
    # 通用 actor/派生频率扩展：数据声明式，不按技能名/ability_id 分派。
    "derived_events-v1": "derived event-rate declaration binding (scenario control + node gate + lifecycle)",
    "stat_transfer-v1": "explicit owner→actor stat transfer rule binding",
    "actor_base-v1": "actor base stat rows from dataset minion/ability declaration",
}

#: 执行期 handler（executor_requirements.handlers 的合法取值）
EXECUTOR_HANDLERS: dict[str, str] = {
    "condition_eval-v1": "restricted condition program / condition code evaluation",
    "hit_component_assembly-v1": "hit component pools, crit, target side",
    "dot_component_assembly-v1": "ailment/dot component pools and application",
    "frequency_fixed_repeat-v1": "fixed_repeat interval and window aggregation",
    "target_side-v1": "target resistance/DR resolution",
    "increased_aggregate-v1": "increased additive pool",
    "more_groups-v1": "more multiplicative groups",
    "crit_expected-v1": "crit expected value",
    "tag_match-v1": "damage tag applicability",
    "attack_speed_pool-v1": "attack speed additive pool projection",
    "cooldown_recovery_pool-v1": "cooldown recovery additive pool projection",
    "ledger_projection-v1": "component application ledger projection",
    "coverage_projection-v1": "coverage gap projection",
    "hit_multi_component_assembly-v2": "plan-v2 multi-hit components/per-cast weights",
    "frequency_steady_state-v2": "plan-v2 named policies and expected-cast windows",
    "dot_component_assembly-v3": "plan-v3 dot tick loop, target settlement and windows",
    "ignite_cohort-v3": "plan-v3 independent Ignite roll/expiry cohorts",
    "charge_schedule-v3": "plan-v3 charge ledger with eager/patient policies",
    "frequency_derived_events-v1": "derived event rate → interval from declarative lifecycle derivation",
}

ALL_HANDLERS: dict[str, dict[str, str]] = {
    **{handler_id: {"phase": "compile", "description": description} for handler_id, description in COMPILE_HANDLERS.items()},
    **{handler_id: {"phase": "execute", "description": description} for handler_id, description in EXECUTOR_HANDLERS.items()},
}

#: 固定执行阶段（方案 §5.5 的 8 个阶段 + 投影）；只支持这一种顺序。
STAGE_ORDER = (
    "baseline",
    "pre_attribute",
    "pre_attribute_effects",
    "attribute_conversion",
    "attribute_scaling",
    "derived_pools",
    "damage_components",
    "frequency",
    "projection",
)

#: 计划 effect category → 所属固定阶段；编译器和校验器共用，禁止任意依赖声明。
CATEGORY_STAGE = {
    "base": "baseline",
    "mod": "baseline",
    "passive": "baseline",
    "node_binding": "pre_attribute_effects",
    "attribute_conversion": "attribute_conversion",
    "attribute_scaling": "attribute_scaling",
    "effect": "derived_pools",
    "row_reclassification": "baseline",
    "state_scaling": "derived_pools",
    "movement_conversion": "derived_pools",
    "cooldown_reduction": "frequency",
}

#: 支持固定阶段之间的依赖边（只允许这些边，且必须完整出现）。
STAGE_DEPENDENCIES = tuple(
    {"from": source, "to": target}
    for source, target in zip(STAGE_ORDER, STAGE_ORDER[1:])
)


def canonical_dependencies() -> list[dict[str, str]]:
    return [dict(edge) for edge in STAGE_DEPENDENCIES]


#: 来源绑定失败状态：失败来源不得支撑“已生效/已计入”的效果。
SOURCE_FAILURE_STATUSES = frozenset(
    {"source_absent", "identity_mismatch", "semantics_mismatch", "unresolved", "unverified"}
)

#: 计划来源 kind 白名单（新增 kind 必须升级契约，不能自由字符串）。
SOURCE_KINDS = frozenset(
    {
        "item_mod",
        "skill_node",
        "passive_node",
        "base_character",
        "effect",
        "rule_source",
        "state_source",
        "template_constant",
    }
)

COMPILER_FILES = (
    "tools/phase3/src/le_calc/conditional_node_buffs.py",
    "tools/phase3/src/le_calc/conditional_item_buffs.py",
    "tools/phase3/src/le_calc/plan_compiler.py",
    "tools/phase3/src/le_calc/plan_compiler_v3.py",
    "tools/phase3/src/le_calc/contribution_mapping.py",
    "tools/phase3/src/le_calc/effect_binding.py",
    "tools/phase3/src/le_calc/condition_codes.py",
    "tools/phase3/src/le_calc/actor_pool.py",
)
EXECUTOR_FILES = (
    "tools/phase3/src/le_calc/plan_executor.py",
    "tools/phase3/src/le_calc/plan_executor_v2.py",
    "tools/phase3/src/le_calc/plan_executor_v3.py",
    "tools/phase3/src/le_calc/dps_components.py",
    "tools/phase3/src/le_calc/dps_result.py",
    "tools/phase3/src/le_calc/condition_codes.py",
    "tools/phase3/src/le_calc/calculation_plan.py",
    "tools/phase3/src/le_calc/mechanics/crit_expected.py",
    "tools/phase3/src/le_calc/mechanics/increased_aggregate.py",
    "tools/phase3/src/le_calc/mechanics/more_groups.py",
    "tools/phase3/src/le_calc/mechanics/tag_match.py",
    "tools/phase3/src/le_calc/mechanics/target_side.py",
    "tools/phase3/src/le_calc/mechanics/attribute_conversion.py",
    "tools/phase3/src/le_calc/mechanics/dot.py",
    "tools/phase3/src/le_calc/mechanics/applications.py",
    "tools/phase3/src/le_calc/mechanics/charge.py",
    "tools/phase3/src/le_calc/mechanics/frequency.py",
    "tools/phase3/src/le_calc/mechanics/execution.py",
)
SHARED_FILES = ("tools/phase3/src/le_calc/plan_handlers.py", "tools/phase3/src/le_calc/model.py")


def repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _file_sha(relative_path: str) -> str:
    path = repo_root() / relative_path
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_fingerprint() -> dict[str, Any]:
    """编译器/执行器实现指纹（不依赖 git HEAD，覆盖未提交代码）。"""
    compiler_files = {path: _file_sha(path) for path in COMPILER_FILES + SHARED_FILES}
    executor_files = {path: _file_sha(path) for path in EXECUTOR_FILES + SHARED_FILES}
    compiler_digest = hashlib.sha256(
        json.dumps({"version": VERSION, "files": compiler_files}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    executor_digest = hashlib.sha256(
        json.dumps({"version": VERSION, "files": executor_files}, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "version": VERSION,
        "compiler_fingerprint": compiler_digest,
        "executor_fingerprint": executor_digest,
        "compiler_files": compiler_files,
        "executor_files": executor_files,
    }


def handler_versions(phase: str = "execute") -> list[dict[str, str]]:
    table = EXECUTOR_HANDLERS if phase == "execute" else COMPILE_HANDLERS
    return [{"handler_id": handler_id, "version": VERSION} for handler_id in sorted(table)]


# ---------------------------------------------------------------------------
# 执行期实现（延迟导入避免模块环）
# ---------------------------------------------------------------------------


def _condition_eval(code: str, states: dict[str, Any]) -> Any:
    from .condition_codes import evaluate_condition

    return evaluate_condition(code, states)


def _hit_component_assembly(**kwargs: Any) -> Any:
    from .dps_components import build_hit_components

    return build_hit_components(**kwargs)


def _dot_component_assembly(**kwargs: Any) -> Any:
    from .dps_components import build_dot_components

    return build_dot_components(**kwargs)


def _frequency_fixed_repeat(**kwargs: Any) -> Any:
    from . import dps_components

    action = kwargs.pop("action")
    if action == "cast_interval":
        return dps_components.cast_interval(**kwargs)
    if action == "hit_windows":
        return dps_components.hit_windows(**kwargs)
    raise ValueError(f"frequency_fixed_repeat 不支持的 action：{action!r}")


def _hit_multi_component_assembly_v2(**kwargs: Any) -> Any:
    from . import plan_executor_v2

    return plan_executor_v2.build_hit_components_v2(**kwargs)


def _frequency_steady_state_v2(**kwargs: Any) -> Any:
    from . import plan_executor_v2

    action = kwargs.pop("action", "steady_state")
    if action == "steady_state":
        return plan_executor_v2.steady_state_frequency(**kwargs)
    if action == "rotation_factor":
        return plan_executor_v2.rotation_factor(**kwargs)
    raise ValueError(f"frequency_steady_state-v2 不支持的 action：{action!r}")


def _target_side(**kwargs: Any) -> Any:
    from .mechanics import target_side

    action = kwargs.pop("action")
    if action == "resolve_hit_target":
        return target_side.resolve_hit_target(**kwargs)
    if action == "hit_component_target":
        return target_side.hit_component_target(**kwargs)
    raise ValueError(f"target_side 不支持的 action：{action!r}")


def _increased_aggregate(values: Any, **kwargs: Any) -> Any:
    from .mechanics.increased_aggregate import aggregate

    return aggregate(values, **kwargs)


def _more_groups(rows: Any) -> Any:
    from .dps_components import more_product

    return more_product(rows)


def _crit_expected(**kwargs: Any) -> Any:
    from .mechanics.crit_expected import expected_crit

    return expected_crit(**kwargs)


def _tag_match(damage_tags: Any, component_tags: Any, policy: Any = None) -> Any:
    from .dps_components import DPS_TAG_POLICY
    from .mechanics.tag_match import tags_match

    return tags_match(damage_tags, component_tags, policy or DPS_TAG_POLICY)


def _attack_speed_pool(**kwargs: Any) -> Any:
    from .dps_components import attack_speed_increased

    return attack_speed_increased(**kwargs)


def _cooldown_recovery_pool(**kwargs: Any) -> Any:
    from .dps_components import cooldown_recovery_increased

    return cooldown_recovery_increased(**kwargs)


def _dot_component_assembly_v3(**kwargs: Any) -> Any:
    from .mechanics.applications import single_cast
    from .mechanics.dot import dot_component_result

    action = kwargs.pop("action", "component")
    if action == "component":
        return dot_component_result(**kwargs)
    if action == "single_cast":
        return single_cast(**kwargs)
    raise ValueError(f"dot_component_assembly-v3 不支持的 action：{action!r}")


def _ignite_cohort_v3(**kwargs: Any) -> Any:
    from .mechanics.applications import application_cohort, application_core

    action = kwargs.pop("action", "core")
    if action == "core":
        return application_core(**kwargs)
    if action == "application":
        return application_cohort(**kwargs)
    raise ValueError(f"ignite_cohort-v3 不支持的 action：{action!r}")


def _frequency_derived_events(**kwargs: Any) -> Any:
    from .mechanics.frequency import derived_event_rate

    derivation = kwargs.pop("derivation")
    states = kwargs.pop("states", {})
    return derived_event_rate(derivation, states=states)


def _charge_schedule_v3(**kwargs: Any) -> Any:
    from .mechanics.charge import charge_loop, charge_parameters

    action = kwargs.pop("action", "loop")
    if action == "loop":
        return charge_loop(**kwargs)
    if action == "parameters":
        return charge_parameters(**kwargs)
    raise ValueError(f"charge_schedule-v3 不支持的 action：{action!r}")


def _ledger_projection(**kwargs: Any) -> Any:
    from . import plan_executor

    return plan_executor.project_ledger(**kwargs)


def _coverage_projection(**kwargs: Any) -> Any:
    from . import plan_executor

    return plan_executor.project_coverage(**kwargs)


EXECUTOR_IMPLEMENTATIONS: dict[str, Callable[..., Any]] = {
    "condition_eval-v1": _condition_eval,
    "hit_component_assembly-v1": _hit_component_assembly,
    "dot_component_assembly-v1": _dot_component_assembly,
    "frequency_fixed_repeat-v1": _frequency_fixed_repeat,
    "target_side-v1": _target_side,
    "increased_aggregate-v1": _increased_aggregate,
    "more_groups-v1": _more_groups,
    "crit_expected-v1": _crit_expected,
    "tag_match-v1": _tag_match,
    "attack_speed_pool-v1": _attack_speed_pool,
    "cooldown_recovery_pool-v1": _cooldown_recovery_pool,
    "ledger_projection-v1": _ledger_projection,
    "coverage_projection-v1": _coverage_projection,
    "hit_multi_component_assembly-v2": _hit_multi_component_assembly_v2,
    "frequency_steady_state-v2": _frequency_steady_state_v2,
    "dot_component_assembly-v3": _dot_component_assembly_v3,
    "ignite_cohort-v3": _ignite_cohort_v3,
    "charge_schedule-v3": _charge_schedule_v3,
    "frequency_derived_events-v1": _frequency_derived_events,
}


class PlanRuntime:
    """执行期白名单 runtime：只暴露注册表内实现。"""

    def __init__(self, implementations: dict[str, Callable[..., Any]] | None = None) -> None:
        self._implementations = dict(implementations if implementations is not None else EXECUTOR_IMPLEMENTATIONS)
        self.executor_fingerprint = implementation_fingerprint()["executor_fingerprint"]

    def handler(self, handler_id: str) -> Callable[..., Any]:
        if handler_id not in self._implementations:
            raise KeyError(f"未注册的执行期 handler：{handler_id}")
        return self._implementations[handler_id]

    def has(self, handler_id: str) -> bool:
        return handler_id in self._implementations


__all__ = [
    "ALL_HANDLERS",
    "COMPILE_HANDLERS",
    "COMPILER_FILES",
    "EXECUTOR_FILES",
    "EXECUTOR_HANDLERS",
    "EXECUTOR_IMPLEMENTATIONS",
    "CATEGORY_STAGE",
    "PlanRuntime",
    "SHARED_FILES",
    "SOURCE_FAILURE_STATUSES",
    "SOURCE_KINDS",
    "STAGE_DEPENDENCIES",
    "STAGE_ORDER",
    "canonical_dependencies",
    "VERSION",
    "handler_versions",
    "implementation_fingerprint",
    "repo_root",
]

