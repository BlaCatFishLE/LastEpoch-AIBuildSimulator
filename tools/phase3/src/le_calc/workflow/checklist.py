"""Fixed DPS inspection checklist (``le-workflow-v1`` / P2).

The implementation plan (§6.1) fixes **nine** inspection items for every damage
component.  They are data, not branches: :func:`resolve_build` (in
:mod:`le_calc.workflow.domain`) and the analysis-plan writer consume this table and
report per-item reuse / agent-gap / status.  Keep the ids stable — analysis plans
and the query index reference them.

Each item declares:

* ``id`` — stable snake_case id;
* ``title_zh`` / ``title_en`` — display titles (Chinese docs are primary);
* ``automatic_reuse`` — what the existing engine/knowledge catalog already provides;
* ``agent_gap`` — what an agent must still decide or research (``无`` when fully covered);
* ``components`` — mechanism ``applicability.components`` values that cover this item;
* ``calculation_blocks`` — skill-template ``calculation_blocks[].block`` ids used;
* ``notes`` — free-form boundaries (why an item may be not-applicable for a model).
"""

from __future__ import annotations

from typing import Any

SCHEMA_VERSION = "le-workflow-checklist-v1"

CHECKLIST: tuple[dict[str, Any], ...] = (
    {
        "id": "damage_components",
        "title_zh": "伤害组件与标签",
        "title_en": "Damage components and tags",
        "automatic_reuse": (
            "技能模板 components + 机制登记的 applicability.components/tags；"
            "标签匹配算子 common-tag-match 负责 Elemental/DoT/Ailment 等展开"
        ),
        "agent_gap": "新组件、额外伤害来源、技能形态变化（如新增 hit 分量）",
        "components": ["hit", "dot", "tick", "ignite", "ailment", "body"],
        "calculation_blocks": ["tag_match"],
        "notes": "组件缺失时该技能的 damage 条目不可信；只能列缺口，不得假定为零。",
    },
    {
        "id": "base_and_added_damage",
        "title_zh": "基础伤害与点伤",
        "title_en": "Base damage and added damage",
        "automatic_reuse": (
            "技能模板组件的基础值读取路径 + added 缩放算子（base + added×scaling）；"
            "added 来源来自静态快照 contributions"
        ),
        "agent_gap": "双持/非本类型点伤/特殊属性继承/effectiveness 变化",
        "components": ["body", "hit", "dot", "tick"],
        "calculation_blocks": ["added_scaling", "tag_match"],
        "notes": "added 行必须按标签与来源实例逐条计入账本，不能只取合计。",
    },
    {
        "id": "increased_reduced",
        "title_zh": "Increased / Reduced",
        "title_en": "Increased / Reduced",
        "automatic_reuse": "按标签收集、统一单位、加算算子 common-increased-aggregate（含属性缩放行）",
        "agent_gap": "新词条语义、作用对象、未知转换顺序",
        "components": ["body", "hit", "dot", "ignite", "tick"],
        "calculation_blocks": ["increased_aggregate", "tag_match"],
        "notes": "同一贡献命中多个组件时各自计入一次；不得跨组件去重丢失。",
    },
    {
        "id": "more_less",
        "title_zh": "More / Less",
        "title_en": "More / Less",
        "automatic_reuse": "已知乘区分组 + 组内相加/组间相乘算子 common-more-groups",
        "agent_gap": "新独立乘区、特殊叠层、与其它来源的关系",
        "components": ["body", "hit", "dot", "ignite", "tick"],
        "calculation_blocks": ["more_groups"],
        "notes": "more_group 必须显式来自机制定义；禁止仅凭词条文本的 more 一词推定。",
    },
    {
        "id": "critical_strike",
        "title_zh": "暴击",
        "title_en": "Critical strike",
        "automatic_reuse": "已支持 Hit 的暴击率/暴伤与期望倍率（crit 计算块）",
        "agent_gap": "特殊暴击转化、上限、继承、非 Hit 组件的暴击口径",
        "components": ["crit"],
        "calculation_blocks": ["crit"],
        "notes": "模型没有 crit 计算块时该项为 not_applicable（如 BH 本体 hit=false），不是缺口。",
    },
    {
        "id": "target_side",
        "title_zh": "目标侧结算",
        "title_en": "Target-side settlement",
        "automatic_reuse": (
            "抗性/穿透/目标减伤算子 common-target-resistance-penetration 与 "
            "common-monster-dr；场景目标由 case/scenario 显式给出"
        ),
        "agent_gap": "新防御机制、特殊目标条件、结算顺序、负抗性/DR 钳制",
        "components": ["target_side", "body", "hit", "dot", "ignite"],
        "calculation_blocks": ["target_side"],
        "notes": "body 与 ailment 的穿透适用性可能不同（如 Fire Penetration with Ignite）。",
    },
    {
        "id": "rate_and_frequency",
        "title_zh": "次数和频率",
        "title_en": "Count and frequency",
        "automatic_reuse": (
            "攻/施速、冷却、充能、多段与持续时间算子 common-frequency-cooldown；"
            "技能模板 schedule 声明 driver/windows/hit_weights"
        ),
        "agent_gap": "充能触发、特殊调度、多段权重、持续时间与冷却的交互",
        "components": ["frequency", "cooldown", "cast", "loop"],
        "calculation_blocks": ["frequency"],
        "notes": "频率必须与单次伤害分开；没有 schedule 不得用『满层伤害 × 攻速』冒充循环。",
    },
    {
        "id": "conditions_and_stacks",
        "title_zh": "条件和叠层",
        "title_en": "Conditions and stacks",
        "automatic_reuse": (
            "固定条件估算 + 条件效果通道（发现→场景→求值→重算）；"
            "算子 common-conditional-state-eval；层数/状态显式传入"
        ),
        "agent_gap": "消耗顺序、覆盖率、时间依赖与交互、获取/维持成本",
        "components": ["conditional", "condition_state", "duration"],
        "calculation_blocks": [],
        "notes": "固定满层状态不等于真实维持率；条件假设必须在结果里保留 assumed 标注。",
    },
    {
        "id": "aggregation_and_coverage",
        "title_zh": "汇总和覆盖",
        "title_en": "Aggregation and coverage",
        "automatic_reuse": "单次/窗口/尾部与来源账、规则账、一致性检查（common-ledger）",
        "agent_gap": "尚未纳入的伤害来源、模型范围声明、双基线口径",
        "components": ["ledger"],
        "calculation_blocks": ["ledger"],
        "notes": "legacy/build_base 等基线分开核对，不相减当作增益；局部伤害不得称作完整 BD DPS。",
    },
)

EXPECTED_COUNT = 9


def checklist() -> list[dict[str, Any]]:
    """Return a deep-enough copy of the fixed checklist (callers may annotate)."""

    return [dict(item, components=list(item["components"]),
                 calculation_blocks=list(item["calculation_blocks"]))
            for item in CHECKLIST]


def item_by_id(item_id: str) -> dict[str, Any]:
    for item in CHECKLIST:
        if item["id"] == item_id:
            return dict(item, components=list(item["components"]),
                        calculation_blocks=list(item["calculation_blocks"]))
    raise KeyError(item_id)


def component_index() -> dict[str, list[str]]:
    """Map mechanism component value -> checklist item ids (for plan annotation)."""

    index: dict[str, list[str]] = {}
    for item in CHECKLIST:
        for component in item["components"]:
            index.setdefault(component, []).append(item["id"])
    return index


__all__ = ["CHECKLIST", "EXPECTED_COUNT", "SCHEMA_VERSION", "checklist",
           "component_index", "item_by_id"]
