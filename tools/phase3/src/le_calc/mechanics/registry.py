"""共享计算块适配器注册表（P2）。

本注册表是 ``le_calc.mechanics`` 的唯一说明入口：每个适配器在这里登记
**职责、入口符号、参数化差异、是否已接入生产、差异类**。它是代码内的声明
（不是新增知识源）；规则/证据仍以 ``tools/phase3/rules/`` 与 ``docs/evidence/``
为准。

P2 的接入边界（与实施方案 §6.2 的“先适配器、后合并”一致）：

* ``target_side``：**已接入生产**——``damage._target_multiplier``、
  ``hit_damage.resolve_target``、``hit_damage.compute_single_hit`` 的组件目标
  乘区都调共享算子；两案例 ``reproduce`` 保持逐位不变。
* 其它适配器：**仅适配器 + 等价性测试**，生产调用点暂不改；P2b/P3 再按
  回归一致前提逐个切换。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import (
    crit_expected,
    frequency,
    increased_aggregate,
    ledger,
    more_groups,
    tag_match,
    target_side,
)

SCHEMA_VERSION = "le-mechanics-registry-v1"


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    module: str
    purpose: str
    entry_points: tuple[str, ...]
    policy_parameters: tuple[str, ...]
    integration_status: str  # "integrated" | "adapter_only"
    integration_targets: tuple[str, ...]
    difference_classes: tuple[str, ...]
    next_step: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "module": self.module,
            "purpose": self.purpose,
            "entry_points": list(self.entry_points),
            "policy_parameters": list(self.policy_parameters),
            "integration_status": self.integration_status,
            "integration_targets": list(self.integration_targets),
            "difference_classes": list(self.difference_classes),
            "next_step": self.next_step,
        }


ADAPTER_SPECS: dict[str, AdapterSpec] = {
    "target_side": AdapterSpec(
        name="target_side",
        module="le_calc.mechanics.target_side",
        purpose=(
            "目标侧结算：actual = model × (1 - eff_res) × (1 - monster_dr)；"
            "穿透模式/负抗性策略/DR 来源显式参数化"
        ),
        entry_points=(
            "compute_target_multiplier",
            "resolve_monster_dr",
            "bh_target_side",
            "resolve_hit_target",
            "hit_component_target",
            "TargetSidePolicy",
            "TargetSideResult",
            "MonsterDR",
        ),
        policy_parameters=(
            "penetration_mode ∈ {fold_into_resistance, separate_multiplier}",
            "negative_resistance_clamp ∈ {allow, clamp_at_zero}（仅 fold 模式定义）",
            "dr_source ∈ {scenario_override, table_by_level}",
            "resistance_source ∈ {scenario_target, ruleset_base}（body 用 scenario_target，BH Ignite 用 ruleset_base）",
            "penetration_applied（组件级 body/ignite 分流由调用方传入）",
        ),
        integration_status="integrated",
        integration_targets=(
            "damage._target_multiplier（BH body）",
            "hit_damage.resolve_target（FR target 解析/DR 查表）",
            "hit_damage.compute_single_hit（FR 组件目标乘区）",
            "ignite._ignite_target（BH Ignite，resistance_source=ruleset_base）",
        ),
        difference_classes=(
            "res=0 或 pen=0 时 fold 与 separate 严格相等（浮点逐位）",
            "res≠0 且 pen>0 时差交叉项 res·pen·(1-dr)（预期差异，不强行统一）",
            "negative_resistance_clamp 只在有效抗性为负时生效",
            "scenario_override 忽略等级表；table_by_level 忽略 scenario 显式 DR",
            "resistance_source：body 读 scenario.target.resistances，BH Ignite 读 ruleset base（P3 前是隐式差异）",
        ),
        next_step="P3 已全部接入；新模型按 ruleset 配置实例化策略，不再复制公式",
    ),
    "tag_match": AdapterSpec(
        name="tag_match",
        module="le_calc.mechanics.tag_match",
        purpose="标签适用性统一签名；未知/忽略标签策略参数化，不推定文本语义",
        entry_points=(
            "tags_match",
            "TagPolicy",
            "bh_tag_policy",
            "hit_tag_policy",
            "bh_component_tags",
            "classify_difference",
            "classify_tag_set_differences",
        ),
        policy_parameters=(
            "ignored_tags（BH metadata 标签集 / FR 的 Elemental 跳过）",
            "known_tags（BH 类型+类别词表 / FR 空集）",
            "unknown_tag_policy ∈ {ignore, restrict}",
        ),
        integration_status="integrated",
        integration_targets=(
            "damage.tags_match（BH 适用性）",
            "hit_damage.tags_apply（FR 适用性）",
        ),
        difference_classes=(
            "bh_metadata_ignored：LowLife/HighLife/FullLife/Curse/Transform/Potion/"
            "PetResisted/去职标签，BH 忽略而 FR 要求闭包含有",
            "bh_unknown_tag_ignored：规则外标签 BH 忽略、FR 限制（真实数据未出现）",
            "Elemental：BH 忽略 / FR 展开，结果等价（无差异类）",
        ),
        next_step="P3 已切换；后续新标签语义只需新增 TagPolicy，不改调用点",
    ),
    "increased_aggregate": AdapterSpec(
        name="increased_aggregate",
        module="le_calc.mechanics.increased_aggregate",
        purpose="加算池 total=Σvalues（+属性加数），factor=1+total",
        entry_points=("aggregate", "aggregate_rows", "IncreasedAggregate"),
        policy_parameters=(
            "attribute_increase（BH 属性缩放；FR 为 0）",
            "求和顺序 = 调用方传入的行顺序（不重排）",
        ),
        integration_status="integrated",
        integration_targets=(
            "damage._collect_increased（BH 本体 inc 池）",
            "hit_damage.compute_single_hit（FR inc 池）",
            "ignite._ignite_scaling（BH Ignite inc 池）",
        ),
        difference_classes=("两模型数学相同；差异只在行来源（ruleset/snapshot）",),
        next_step="P3 已切换；新增池只需传入行值数组",
    ),
    "more_groups": AdapterSpec(
        name="more_groups",
        module="le_calc.mechanics.more_groups",
        purpose="more 分组乘积；group 必须显式来自机制/规则字段，不按文本推定",
        entry_points=(
            "aggregate",
            "product",
            "MoreRow",
            "MoreGroup",
            "MoreAggregate",
            "rows_from_modifier_rows",
            "rows_from_hit_rows",
        ),
        policy_parameters=(
            "组键 = row.group or row.contribution_id（显式数据字段）",
            "组内加算、组间乘算；组按名称排序后乘（与既有实现一致）",
            "row_indices 保留每组原始行下标，调用方可重建自己的行投影",
        ),
        integration_status="integrated",
        integration_targets=(
            "damage.group_more_rows（BH 供 compute_body/tree_more_groups 使用）",
            "hit_damage._more_groups（FR 伤害 more 池）",
            "hit_simulate._attack_speed_pools（FR 攻速 more 池）",
        ),
        difference_classes=(
            "两模型分组数学相同；BH 的组来自 ruleset node/player row，FR 来自 snapshot/effect row",
            "FR crit 的 chance/multiplier 内部 more 组乘法保持原插入顺序（不改浮点乘法顺序，未切换）",
        ),
        next_step="P3 已切换（伤害/攻速 more 池）；crit 内部 more 组因乘法顺序保持独立",
    ),
    "crit_expected": AdapterSpec(
        name="crit_expected",
        module="le_calc.mechanics.crit_expected",
        purpose="avg crit = 1-c+c·m；带模型 capability 开关（BH 无暴击）",
        entry_points=(
            "expected_crit",
            "avg_crit",
            "crit_pools",
            "CritCapability",
            "CritPools",
            "CritResult",
            "BH_CRIT_CAPABILITY",
            "FR_HIT_CRIT_CAPABILITY",
        ),
        policy_parameters=(
            "crit_enabled capability（BH=False / FR=True）",
            "chance_cap / base chance / base multiplier 显式传入",
        ),
        integration_status="integrated",
        integration_targets=("hit_damage._crit_section（FR 命中暴击；BH 不适用）",),
        difference_classes=(
            "BH 伤害链无 crit 因子；共享算子关闭时 avg_crit=1.0（不得默认开启）",
            "FR crit 池的行筛选与 more 组乘法顺序保留在 _crit_section（不改浮点顺序）",
        ),
        next_step="P3 已切换 FR；BH 保持 capability=false 且不进入伤害链",
    ),
    "frequency": AdapterSpec(
        name="frequency",
        module="le_calc.mechanics.frequency",
        purpose="冷却/攻速→施放速率与窗口骨架；BH charge 事件流语义暂不合并",
        entry_points=(
            "effective_cooldown",
            "cast_time",
            "final_attack_rate",
            "attack_interval",
            "cast_policy",
            "damage_weights_factor",
            "window_skeleton",
        ),
        policy_parameters=(
            "cdr_applied_separately（BH v2 的 CDR/ICR 分流）",
            "binding policy：min(1/attack_interval, 1/effective_cooldown)",
            "damage_weights / hits_per_cast / rotation_more_factor 显式传入",
            "cooldown_limited_rate 保留 FR 以 cooldown<=0 为守卫的独立口径",
        ),
        integration_status="integrated",
        integration_targets=(
            "cooldown.compute_cooldown / compute_cast_time（BH）",
            "hit_simulate 的 final_aps/attack_interval/effective_cooldown（FR）",
            "hit_simulate._policy_block 的 cast policy + window skeleton（FR）",
        ),
        difference_classes=(
            "BH 窗口由逐事件伤害切片聚合（simulate.window_metrics），事件语义与 dps 除法均不合并",
            "BH charge 事件流/重放保持 simulate 实现，不合并",
            "FR 冷却守卫是 cooldown<=0，BH 是 ICR 分母<=0：cooldown_limited_rate 单独保留 FR 语义",
            "FR 窗口命中权重与 Rhythm 平均显式保留为输入",
        ),
        next_step="P3 已切换数学等价部分；事件流/charge 重放/窗口事件聚合仍各自实现",
    ),
    "ledger": AdapterSpec(
        name="ledger",
        module="le_calc.mechanics.ledger",
        purpose="contribution→行投影薄封装；不改变行内容/顺序（报告层合并留 P3）",
        entry_points=(
            "project_source_ledger",
            "project_contribution_row",
            "project_hit_source_ledger",
            "ledger_summary",
            "field_sets",
        ),
        policy_parameters=("投影实现由模型决定：BH ledger / FR hit_report（不统一字段）",),
        integration_status="adapter_only",
        integration_targets=(),
        difference_classes=(
            "BH 行含 player_label/raw_value/normalized_value；FR 行含 stat_key 显示投影",
            "字段合并属于报告层（P3），本层只固定投影入口",
        ),
        next_step="P3：报告层统一行 schema 时以本模块为入口，不改数值",
    ),
}


@dataclass(frozen=True)
class ModelCapabilities:
    model_id: str
    crit_enabled: bool
    target_side: target_side.TargetSidePolicy | None
    target_side_dr_source: str
    frequency_semantics: str
    ledger_projection: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "crit_enabled": self.crit_enabled,
            "target_side": self.target_side.to_dict() if self.target_side else None,
            "target_side_dr_source": self.target_side_dr_source,
            "frequency_semantics": self.frequency_semantics,
            "ledger_projection": self.ledger_projection,
        }


#: 两个当前案例模型的共享块能力声明（BH 的负抗性策略由 ruleset 决定，不在此硬编码）
MODEL_CAPABILITIES: dict[str, ModelCapabilities] = {
    "black-hole": ModelCapabilities(
        model_id="black-hole",
        crit_enabled=False,
        target_side=target_side.BH_TARGET_SIDE_POLICY,
        target_side_dr_source=target_side.DR_FROM_SCENARIO,
        frequency_semantics="charge_based_cooldown+cast_time+event_window_metrics",
        ledger_projection="bh_ledger",
    ),
    "flame-reave-hit": ModelCapabilities(
        model_id="flame-reave-hit",
        crit_enabled=True,
        target_side=target_side.FR_HIT_TARGET_SIDE_POLICY,
        target_side_dr_source=target_side.DR_FROM_TABLE,
        frequency_semantics="cooldown_limited|attack_speed_limited+window_skeleton",
        ledger_projection="hit_report",
    ),
}


def get(name: str) -> AdapterSpec:
    """按名字取适配器声明；未知名字明确失败（不静默返回 None）。"""
    try:
        return ADAPTER_SPECS[name]
    except KeyError as exc:  # pragma: no cover - defensive
        raise KeyError(
            f"未登记的共享计算块适配器 {name!r}；已登记：{', '.join(sorted(ADAPTER_SPECS))}"
        ) from exc


def describe() -> list[dict[str, Any]]:
    """全部适配器声明（JSON 可序列化，按名字排序）。"""
    return [ADAPTER_SPECS[name].to_dict() for name in sorted(ADAPTER_SPECS)]


def integration_report() -> dict[str, Any]:
    """接入状态汇总：已接入 / 未接入清单（P2b/P3 输入）。"""
    integrated = sorted(name for name, spec in ADAPTER_SPECS.items() if spec.integration_status == "integrated")
    adapter_only = sorted(name for name, spec in ADAPTER_SPECS.items() if spec.integration_status == "adapter_only")
    return {
        "schema": SCHEMA_VERSION,
        "integrated": integrated,
        "adapter_only": adapter_only,
        "model_capabilities": {
            key: value.to_dict() for key, value in sorted(MODEL_CAPABILITIES.items())
        },
    }


def modules() -> dict[str, Any]:
    """适配器模块对象（供需要直接调用/自省的调用方）。"""
    return {
        "target_side": target_side,
        "tag_match": tag_match,
        "increased_aggregate": increased_aggregate,
        "more_groups": more_groups,
        "crit_expected": crit_expected,
        "frequency": frequency,
        "ledger": ledger,
    }


__all__ = [
    "ADAPTER_SPECS",
    "AdapterSpec",
    "MODEL_CAPABILITIES",
    "ModelCapabilities",
    "SCHEMA_VERSION",
    "describe",
    "get",
    "integration_report",
    "modules",
]
