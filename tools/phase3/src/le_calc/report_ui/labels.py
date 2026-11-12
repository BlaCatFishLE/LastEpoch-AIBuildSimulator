"""Chinese label tables shared by every report page and the source catalogue."""

from __future__ import annotations

__all__ = [
    "STATUS_LABELS",
    "STATUS_TONES",
    "RULE_STATUS_LABELS",
    "RULE_STATUS_TONES",
    "CONDITION_LABELS",
    "SEVERITY_LABELS",
    "SEVERITY_TONES",
    "SEVERITY_RANK",
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "SLOT_LABELS",
    "CHANNEL_LABELS",
    "EVIDENCE_LABELS",
    "AFFECT_LABELS",
    "CONSISTENCY_LABELS",
    "MODE_LABELS",
    "affect_label",
    "PLAYER_STATUS_LABELS",
    "PLAYER_STATUS_TONES",
    "PLAYER_EFFECT_QUALIFIERS",
    "PLAYER_ATTRIBUTE_ALIASES",
    "PLAYER_ATTRIBUTE_SKIP_MARKERS",
    "PLAYER_REFERENCE_MARKERS",
    "PLAYER_ALWAYS_CONDITIONS",
    "PLAYER_SOURCE_CATEGORY_LABELS",
]

STATUS_LABELS = {
    "complete": "已完成",
    "assumption": "假设结果",
    "incomplete": "未完成",
    "unknown": "状态未知",
}

STATUS_TONES = {
    "complete": "ok",
    "assumption": "warn",
    "incomplete": "bad",
    "unknown": "muted",
}

RULE_STATUS_LABELS = {
    "confirmed": "已确认",
    "derived": "推导",
    "assumed": "假设",
    "unresolved": "未解决",
}

RULE_STATUS_TONES = {
    "confirmed": "ok",
    "derived": "info",
    "assumed": "warn",
    "unresolved": "bad",
}

CONDITION_LABELS = {
    "always": "恒定生效",
    "satisfied": "条件已满足",
    "not_satisfied": "条件未满足",
    "unknown": "条件待确认",
    "unsupported": "未建模",
}

SEVERITY_LABELS = {
    "critical": "严重",
    "error": "错误",
    "high": "高",
    "warning": "警告",
    "medium": "中",
    "info": "提示",
    "low": "低",
    "unknown": "未知",
}

SEVERITY_TONES = {
    "critical": "bad",
    "error": "bad",
    "high": "bad",
    "warning": "warn",
    "medium": "warn",
    "info": "info",
    "low": "muted",
    "unknown": "muted",
}

SEVERITY_RANK = {
    "critical": 0,
    "error": 1,
    "high": 2,
    "warning": 3,
    "medium": 4,
    "info": 5,
    "low": 6,
    "unknown": 7,
}

CATEGORY_LABELS = {
    "equipment": "装备",
    "idol": "神像",
    "blessing": "祝福",
    "passive_node": "职业被动",
    "skill_node": "技能节点",
    "idol_altar": "神像祭坛",
    "base_character": "角色基础",
    "derived": "派生属性",
    "quest": "任务奖励",
    "unknown": "其他来源",
}

CATEGORY_ORDER = [
    "equipment",
    "idol",
    "blessing",
    "idol_altar",
    "passive_node",
    "skill_node",
    "base_character",
    "derived",
    "quest",
    "unknown",
]

SLOT_LABELS = {
    "weapon1": "武器",
    "weapon2": "副手",
    "amulet": "项链",
    "chest": "胸甲",
    "feet": "鞋子",
    "hands": "手套",
    "head": "头盔",
    "relic": "遗物",
    "ring1": "戒指 1",
    "ring2": "戒指 2",
    "waist": "腰带",
    "idol_altar": "神像祭坛",
}

CHANNEL_LABELS = {
    "implicit": "基底词缀",
    "normal": "普通词缀",
    "sealed": "封印词缀",
    "corrupted": "腐化词缀",
    "unique": "独特效果",
    "unique_description": "独特说明",
    "exalted": "崇高词缀",
    "affix": "词缀",
}

EVIDENCE_LABELS = {
    "game_data": "游戏数据",
    "user_rule": "用户规则",
    "catalog": "数据目录",
    "third_party_reference": "第三方参考",
    "public_doc": "官方文档",
    "derivation": "推导计算",
    "reverse_engineering": "逆向工程",
    "user_in_game_measurement": "游戏内实测",
    "user_message": "用户消息",
}

AFFECT_LABELS = {
    "single_cast": "单次释放",
    "single_cast.body": "本体",
    "single_cast.body.components": "本体分量",
    "single_cast.body.event_trace": "本体事件",
    "single_cast.ignite": "点燃",
    "single_cast.ignite.application": "点燃施加",
    "single_cast.ignite.exclusions": "点燃排除项",
    "single_cast.event_trace": "事件时间轴",
    "single_cast.time_series": "伤害曲线",
    "single_cast.cast": "施法",
    "single_cast": "单次释放",
    "loops": "CD 循环",
    "loops.windows": "循环窗口",
    "scenario.target": "目标场景",
    "target": "目标侧",
    "cooldown": "冷却与充能",
    "cast": "施法时间",
    "duration": "持续时间",
    "body": "本体",
    "ignite": "点燃",
    "inputs": "输入派生",
    "resistances": "抗性",
    "mana": "法力",
    "attributes": "属性",
    "conditions": "条件",
    "contributions": "来源账",
    "passive_tree": "被动树",
    "skill_tree": "技能树",
    "excluded_sources": "排除来源",
    "input": "输入",
}

CONSISTENCY_LABELS = {
    "all_pass": "全部一致性检查通过",
    "body_ticks_sum_equals_component_total": "本体逐跳合计 = 分量总伤害",
    "ignite_cohorts_sum_equals_total": "点燃逐批合计 = 点燃总伤害",
    "loop_concurrency_expected": "循环并发黑洞数符合预期",
    "loop_policies_ordered": "循环策略顺序正确",
    "loop_windows_monotone": "循环窗口伤害单调递增",
    "single_total_equals_body_plus_ignite": "单次总伤害 = 本体 + 点燃",
    "window_plus_tail_equals_ignite_total": "黑洞结束前点燃 + 尾部 = 点燃总伤害",
}

MODE_LABELS = {
    "single": "单次释放",
    "loop": "CD 循环",
}


# --------------------------------------------------------------------------- #
# player-facing vocabulary (增伤来源 / Buff 设置 pages)
# --------------------------------------------------------------------------- #

#: one primary status per source card; engineering states stay in the data layer
PLAYER_STATUS_LABELS = {
    "applied": "已生效",
    "single_cast": "仅本次施法",
    "condition_unmet": "条件不足",
    "unavailable": "暂不可用",
    "not_enabled": "未启用",
    "unknown": "待确认",
}

PLAYER_STATUS_TONES = {
    "applied": "ok",
    "single_cast": "info",
    "condition_unmet": "muted",
    "unavailable": "muted",
    "not_enabled": "muted",
    "unknown": "muted",
}

#: parenthetical qualifiers in conditional-effect names -> player qualifier.
#: Only the passive/active split becomes an attribute qualifier; other
#: parentheses become a condition line instead (see player_effect_condition).
PLAYER_EFFECT_QUALIFIERS = {
    "被动": "常驻",
    "激活": "激活额外",
}

#: conditional-effect value-line labels -> readable attribute name
PLAYER_ATTRIBUTE_ALIASES = {
    "被动点燃持续": "点燃持续时间",
    "激活增量（相对被动）": "点燃持续时间",
    "激活期间该节点总计": "",
    "Fire Resistance Shred 每层": "火焰抗性削减（每层）",
    "Fire Resistance Shred 持续": "火焰抗性削减持续",
    "Fire Resistance Shred 层数上限": "火焰抗性削减层数上限",
    "Fire Resistance Shred Boss 效果系数": "火焰抗性削减（对 Boss）",
    "Fire Resistance Shred 非 Boss 最大减抗": "火焰抗性削减上限（非 Boss）",
    "转换比例": "异常几率转换比例",
    "每层 more 火焰伤害": "每层火焰伤害（独立）",
    "每层 more 冰霜伤害": "每层冰霜伤害（独立）",
    "每层 more 护甲（非伤害）": "每层护甲（独立，非伤害）",
    "每第三次施放 more 伤害": "每第三次施放伤害（独立）",
    "每第三次施放 increased 范围": "每第三次施放范围（加算）",
    "下一次元素法术 increased damage": "下一次元素法术伤害增伤",
    "increased 冷却恢复速度": "冷却恢复速度（加算）",
    "increased 火焰伤害": "火焰伤害增伤",
    "被消费点燃的 more 伤害（4 点实际值）": "被消耗点燃的伤害（独立，4 点实际值）",
    "立即结算点燃伤害（动作）": "立即结算点燃伤害",
    "被消费的点燃范围": "被消耗的点燃范围",
    "对冰缓目标 more 伤害": "对冰缓目标伤害增伤（独立）",
}

#: value-line labels treated as reference/duplicate summaries and not rendered
PLAYER_REFERENCE_MARKERS = ("参考", "期间总计", "总计（")

#: labels ignored entirely because they repeat the effect name/status
PLAYER_ATTRIBUTE_SKIP_MARKERS = ()

#: conditions that say "no condition" and should not be rendered as a condition
PLAYER_ALWAYS_CONDITIONS = (
    "常驻被动，无需额外条件",
    "无需额外条件",
    "无可变条件",
)

#: source-browser category headings for cross-skill conditional sources
PLAYER_SOURCE_CATEGORY_LABELS = {
    "other_skill": "其他技能",
    "action": "动作",
    "main_skill": "主技能",
}


def affect_label(value: str) -> str:
    return AFFECT_LABELS.get(value, value)
